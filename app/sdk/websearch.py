"""有界的公开网页搜索；只读取页面，不执行脚本或提交表单。"""

from concurrent.futures import ThreadPoolExecutor
from html import unescape
from html.parser import HTMLParser
import ipaddress
from itertools import zip_longest
import os
import re
import socket
import threading
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from xml.etree import ElementTree

import requests
from requests.adapters import HTTPAdapter
from urllib3 import PoolManager
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import HTTPError as TransportError, NewConnectionError, ProtocolError, ReadTimeoutError
from urllib3.util.connection import create_connection

from .common import SearchError, extract_share_urls, normalize_share_url
from .channel_search import channel_queries, channel_results
from .source_health import SourceHealth
from run_progress import report_progress


MAX_BYTES = 1024 * 1024
_cache = {}
_cache_lock = threading.Lock()


def _public_addresses(host, port):
    try:
        # DNS 系统调用使用操作系统的超时，应用预算在它返回后检查。
        addresses = list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
        checked = [ipaddress.ip_address(address) for address in addresses]
        for ip in list(checked):
            if isinstance(ip, ipaddress.IPv6Address):
                checked.extend(address for address in (ip.ipv4_mapped, ip.sixtofour, *(ip.teredo or ())) if address)
                if ip in ipaddress.ip_network("64:ff9b::/96"):
                    checked.append(ipaddress.IPv4Address(int(ip) & 0xffffffff))
        if not addresses or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in checked):
            raise SearchError("网页地址解析到了非公网 IP，已拒绝访问")
        return addresses
    except (OSError, ValueError) as error:
        raise SearchError("网页域名解析失败") from error


def _validate_url(url):
    try:
        parts = urlsplit(url)
        if (len(url) > 4096 or parts.scheme not in ("http", "https") or not parts.hostname
                or parts.username or parts.password or "%" in parts.hostname
                or parts.port not in (None, 80, 443)):
            raise ValueError()
        _public_addresses(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
        return parts.hostname
    except (TypeError, ValueError) as error:
        raise SearchError("仅允许无账号的公网 HTTP(S) 网页地址") from error


class _PublicConnection:
    def _new_conn(self):
        # 连接已验证的 IP，而不是在校验后再次交给 HTTP 客户端解析域名。
        deadline = time.monotonic() + self.timeout
        addresses = _public_addresses(self.host, self.port)[:2]
        for index, address in enumerate(addresses):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                return create_connection((address, self.port), remaining / (len(addresses) - index), self.source_address, self.socket_options)
            except OSError:
                continue
        raise NewConnectionError(self, "公网服务器连接失败")


class _HTTPConnection(_PublicConnection, HTTPConnection):
    pass


class _HTTPSConnection(_PublicConnection, HTTPSConnection):
    pass


class _HTTPPool(HTTPConnectionPool):
    ConnectionCls = _HTTPConnection


class _HTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _HTTPSConnection


class _PublicAdapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        self.poolmanager = PoolManager(num_pools=4, maxsize=2, block=True, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"http": _HTTPPool, "https": _HTTPSPool}


class _Page(HTMLParser):
    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.title, self.text, self.links = [], [], []
        self._title = False
        self._skip = 0
        self._anchor = None
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style"):
            self._skip += 1
        if self._skip:
            return
        if tag == "title":
            self._title = True
        if tag == "a":
            self._anchor = {"url": attrs.get("href") or "", "class": attrs.get("class") or "", "text": []}
            if "pan.quark.cn/s/" in self._anchor["url"]:
                self.text.append(" " + self._anchor["url"] + " ")
        if tag in ("p", "div", "li", "tr"):
            self.text.append("\x1e")
        elif tag == "br":
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        if tag == "title":
            self._title = False
        if tag == "a" and self._anchor is not None:
            self.links.append({**self._anchor, "text": "".join(self._anchor["text"]).strip()})
            self._anchor = None
        if tag in ("p", "div", "li", "tr"):
            self.text.append("\x1e")

    def handle_data(self, text):
        if self._skip:
            return
        if self._title:
            self.title.append(text)
        if self._anchor is not None:
            self._anchor["text"].append(text)
        self.text.append(text)


class _SearchPage(_Page):
    def __init__(self, body):
        self.results = []
        self._result = None
        self._depth = 0
        self._heading = False
        self._targets = set()
        super().__init__(body)

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if self._skip:
            return
        if tag == "div":
            self._depth += 1
            classes = dict(attrs).get("class", "").split()
            if self._result is None and {"MjjYud", "g", "Gx5Zad", "ezO2md", "vrwrap"}.intersection(classes):
                self._result = (self._depth, len(self.links), len(self.text))
                self._targets = set()
            if self._result and "ext_query" in classes and dict(attrs).get("data-url"):
                self._targets.add(dict(attrs)["data-url"])
        if tag == "h3":
            self._heading = True
        if self._heading and self._anchor is not None and tag in ("h3", "a"):
            self._anchor["heading"] = []

    def handle_data(self, text):
        super().handle_data(text)
        if not self._skip and self._heading and self._anchor is not None and "heading" in self._anchor:
            self._anchor["heading"].append(text)

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        if tag == "h3":
            self._heading = False
        if tag == "div" and not self._skip:
            if self._result and self._depth == self._result[0]:
                _, start_link, start_text = self._result
                headings = [link for link in self.links[start_link:] if link.get("heading")]
                # 多条结果共用的外层容器不能把其他作品的分享链接混进摘要。
                if len(headings) == 1:
                    self.results.append({**headings[0], "snippet": " ".join(self.text[start_text:]),
                                         "target_url": next(iter(self._targets)) if len(self._targets) == 1 else ""})
                self._result = None
            self._depth -= 1


def _relevant(query, text):
    query = re.sub(r"第[一二三四五六七八九十百\d]+季|\bS\d+\b|\b(?:19|20)\d{2}\b", " ", query, flags=re.I)
    terms = [part.casefold() for part in re.split(r"[\W_]+", query) if part]
    text = re.sub(r"[\W_]+", "", unescape(text)).casefold()
    return bool(terms) and all(term in text for term in terms)


def _page_links(text):
    urls = []
    for block in text.split("\x1e"):
        links = extract_share_urls(block)
        # 同一个段落只有一条夸克链接时，也支持放在链接前面的提取码。
        if len(links) == 1 and len(re.findall(r"https?://", block)) == 1:
            links = [normalize_share_url(links[0], context=block)]
        urls.extend(links)
    return list(dict.fromkeys(urls))


def _search_results(engine, body, url):
    if engine == "Meijusou":
        if re.search(r"cf-chl-|captcha|verify you are human|访问过于频繁", body, re.I):
            raise SearchError("美剧搜要求人工验证，请稍后重试")
        page = _Page(body)
        results, seen = [], set()
        for item in page.links:
            link = urljoin(url, item["url"])
            parts = urlsplit(link)
            # 搜索结果与侧栏热门作品分开，只读取公开帖子标题。
            if ("line-clamp-2" in item["class"].split() and parts.hostname == "meijusou.com"
                    and re.fullmatch(r"/post/[A-Za-z0-9_-]+", parts.path)
                    and not parts.fragment and item["text"] and link not in seen):
                seen.add(link)
                results.append({"title": item["text"], "url": link, "snippet": ""})
        if not results and not re.search(r"\b0\s*条结果|没有找到相关帖子", " ".join(page.text)):
            raise SearchError("美剧搜页面格式已改变或需要验证")
        return results[:10]
    if engine == "Bing":
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError as error:
            raise SearchError("Bing 未返回 RSS，可能需要验证或服务不可用") from error
        if root.tag != "rss" or root.find("channel") is None:
            raise SearchError("Bing RSS 格式已改变")
        return [{"title": item.findtext("title", ""), "url": item.findtext("link", ""),
                 "snippet": item.findtext("description", "")} for item in root.findall("./channel/item")[:10]]
    if engine == "Google":
        if (urlsplit(url).hostname == "consent.google.com"
                or re.search(r"captcha-form|g-recaptcha|unusual traffic|verify you are human|/sorry/|Before you continue to Google", body, re.I)):
            raise SearchError("Google 要求人工验证或同意页面，请稍后重试")
        page = _SearchPage(body)
        results, seen = [], set()
        for item in page.results + [link for link in page.links if link.get("heading")]:
            link = urljoin(url, item["url"])
            parts = urlsplit(link)
            if parts.hostname == "google.com" or (parts.hostname or "").endswith(".google.com"):
                link = ((parse_qs(parts.query).get("q") or parse_qs(parts.query).get("url") or [""])[0]
                        if parts.path == "/url" else "")
            if link and link not in seen:
                seen.add(link)
                results.append({"title": "".join(item["heading"]).strip(), "url": link,
                                "snippet": item.get("snippet", "")})
        if not results and not re.search(r"did not match any documents|No results found|找不到和|没有找到", " ".join(page.text), re.I):
            raise SearchError("Google 页面格式已改变或要求启用 JavaScript/人工验证")
        return results[:10]
    if engine == "Sogou":
        if re.search(r"antispider|captcha|搜狗搜索访问异常|访问过于频繁|请输入验证码", body, re.I):
            raise SearchError("搜狗要求人工验证，请稍后重试")
        page = _SearchPage(body)
        results = [{"title": "".join(item["heading"]).strip(),
                    "url": urljoin(url, item["target_url"] or item["url"]), "snippet": item["snippet"]}
                   for item in page.results]
        if not results and not re.search(r"未找到与|没有找到|没有查询到", " ".join(page.text)):
            raise SearchError("搜狗页面格式已改变或需要验证")
        return results[:10]
    if re.search(r"anomaly\.js|challenge-form|anomaly-modal|unusual traffic|verify you are human", body, re.I):
        raise SearchError("DuckDuckGo 要求人工验证，请稍后重试")
    page = _Page(body)
    results = []
    for item in page.links:
        classes = item["class"].split()
        if not {"result__a", "result__snippet"}.intersection(classes):
            continue
        link = urljoin(url, item["url"])
        parts = urlsplit(link)
        if parts.hostname in ("duckduckgo.com", "html.duckduckgo.com"):
            link = (parse_qs(parts.query).get("uddg") or [""])[0]
        if link and "result__a" in classes:
            results.append({"title": item["text"], "url": link, "snippet": ""})
        elif link and results and link == results[-1]["url"]:
            # 摘要只归属紧邻、指向同一落地页的结果，避免串用另一条的分享和提取码。
            results[-1]["snippet"] = item["text"]
    if not results and not re.search(r"no-results|No results found|没有找到", body, re.I):
        raise SearchError("DuckDuckGo 页面格式已改变或需要验证")
    return results[:10]


class WebSearch:
    def __init__(self, health_path=None):
        self.health = SourceHealth(health_path)
        self.session = requests.Session()
        # 不读取 .netrc 或系统代理设置；显式环境代理由部署者信任并负责最终解析。
        self.session.trust_env = False
        self.session.proxies = {scheme: value for scheme in ("http", "https")
                                if (value := os.getenv(f"{scheme.upper()}_PROXY") or os.getenv(f"{scheme}_proxy"))}
        adapter = _PublicAdapter()
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers["User-Agent"] = "Mozilla/5.0 (compatible; QuarkAutoSave/3; public-page-reader)"
        self.errors = []

    def _fetch(self, url, deadline):
        for redirect in range(3):
            _validate_url(url)
            self.health.check(url)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SearchError("网页搜索已达到本次时间上限")
            try:
                with self.session.get(url, allow_redirects=False, stream=True,
                                      headers={"Cookie": "", "Authorization": None},
                                      timeout=(min(3, remaining), min(8, remaining))) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        if redirect == 2 or not response.headers.get("Location"):
                            raise SearchError("网页重定向次数超过上限")
                        url = urljoin(url, response.headers["Location"])
                        continue
                    if response.status_code in (403, 429):
                        self.health.record(url, f"HTTP {response.status_code}", response.headers.get("Retry-After"))
                    response.raise_for_status()
                    content_type = response.headers.get("Content-Type", "").lower()
                    if not any(kind in content_type for kind in ("text/", "application/xml", "application/rss+xml", "application/xhtml+xml")):
                        raise SearchError("搜索落地地址不是文本网页")
                    if int(response.headers.get("Content-Length", "0")) > MAX_BYTES:
                        raise SearchError("网页超过 1 MiB 读取上限")
                    chunks, size = [], 0
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise SearchError("网页搜索已达到本次时间预算")
                        connection = getattr(response.raw, "_connection", None)
                        sock = getattr(connection, "sock", None)
                        if sock is not None:
                            sock.settimeout(min(8, remaining))
                        # read1 不会为了凑满缓冲区而无限等待持续慢速发送的网页。
                        read1 = getattr(response.raw, "read1", None)
                        chunk = read1(8192, decode_content=True) if read1 else response.raw.read(1, decode_content=True)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_BYTES or time.monotonic() > deadline:
                            raise SearchError("网页超过大小或时间上限")
                        chunks.append(chunk)
                    encoding = response.encoding if "charset=" in content_type else "utf-8"
                    return b"".join(chunks).decode(encoding or "utf-8", errors="replace"), url
            except requests.HTTPError as error:
                raise SearchError(f"网页 HTTP {error.response.status_code}") from error
            except (requests.RequestException, TransportError, OSError, ValueError, LookupError) as error:
                raise SearchError(f"网页请求失败 ({type(error).__name__})") from error

    def _fetch_with_retry(self, url, deadline):
        for attempt in range(2):
            try:
                return self._fetch(url, deadline)
            except SearchError as error:
                cause = error.__cause__
                retryable = isinstance(cause, (requests.Timeout, requests.ConnectionError,
                                               NewConnectionError, ProtocolError, ReadTimeoutError))
                if (attempt or deadline - time.monotonic() < 5 or not retryable
                        or isinstance(cause, requests.exceptions.SSLError)):
                    raise

    def search(self, query, refresh=False, failed_engines=None, channel_config=None, include_engines=True):
        self.errors = []
        failed_engines = failed_engines if failed_engines is not None else set()
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise SearchError("网页搜索词必须是 1 至 200 字的文本")
        query = query.strip()
        channels = channel_queries(channel_config, query) if channel_config is not None else []
        cache_key = (query, tuple(name for name, _url in channels), include_engines) if channel_config is not None else query
        with _cache_lock:
            cached = _cache.get(cache_key)
            if not refresh and cached and time.monotonic() - cached[0] < 300:
                self.errors = list(cached[2])
                return [dict(item) for item in cached[1]]
        deadline = time.monotonic() + 32
        params = urlencode({"q": query + " 夸克"})
        engines = [("Meijusou", "https://meijusou.com/search?" + urlencode({"q": query})),
                   ("DuckDuckGo", "https://html.duckduckgo.com/html/?" + params),
                   ("Bing", "https://cn.bing.com/search?format=rss&mkt=zh-CN&" + params),
                   ("Google", "https://www.google.com/search?gbv=1&num=10&" + params),
                   ("Sogou", "https://www.sogou.com/web?" + urlencode({"query": query + " 夸克"}))]
        engines = [job for pair in zip_longest(engines if include_engines else [], channels) for job in pair if job]

        def search_engine(engine, url):
            report_progress("search", source=f"网页搜索 {engine}", query=query)
            fetch = self._fetch_with_retry if engine == "Meijusou" else self._fetch
            body, final_url = fetch(url, deadline)
            try:
                if engine.startswith(("Telegram @", "腾讯频道 ")):
                    return channel_results(engine, body, final_url, query)
                return _search_results(engine, body, final_url)
            except SearchError as error:
                if "人工验证" in str(error):
                    self.health.record(final_url, "需要人工验证")
                raise

        candidates, succeeded = [], 0
        with ThreadPoolExecutor(max_workers=2) as executor:
            jobs = [(engine, executor.submit(search_engine, engine, url)) for engine, url in engines
                    if engine not in failed_engines]
            for engine, future in jobs:
                try:
                    items = future.result()
                    succeeded += 1
                    candidates.extend({**item, "engine": engine} for item in items if _relevant(query, item["title"] + " " + item["snippet"]))
                except (SearchError, ValueError) as error:
                    failed_engines.add(engine)
                    self.errors.append(f"{engine}: {error}")
        if not succeeded:
            raise SearchError("；".join(self.errors) or "本次查找已跳过此前失败的网页搜索引擎")

        candidates.sort(key=lambda item: ("pan.quark.cn/s/" in item["snippet"], "网盘" in item["title"],
                                          "网盘" in item["snippet"], "夸克" in item["title"]), reverse=True)
        results, pages, seen = [], [], set()
        for item in candidates:
            if time.monotonic() >= deadline:
                self.errors.append("网页搜索已达到本次时间预算")
                break
            try:
                _validate_url(item["url"])
            except SearchError as error:
                self.errors.append(f"{item['engine']} 落地页: {error}")
                continue
            for url in extract_share_urls(item["url"] + " " + item["snippet"]):
                results.append(self._entry(item, url, item["title"], item["snippet"], item["url"]))
            if item["url"] not in seen and urlsplit(item["url"]).hostname != "pan.quark.cn":
                seen.add(item["url"])
                pages.append(item)
        first_pages = {}
        for item in pages:
            first_pages.setdefault(item["engine"], item)
        # 每个引擎先检查一个落地页，避免前一个引擎占满全部读取额度。
        selected_pages = list(first_pages.values())
        selected_pages.extend(item for item in pages if item not in selected_pages)

        def fetch_page(item):
            report_progress("search", source=f"网页搜索 {item['engine']} 落地页", query=query)
            body, final_url = self._fetch_with_retry(item["url"], deadline)
            if re.search(r"challenge-form|cf-chl-|verify you are human|EO_Bot_Ssid|__tst_status|访问过于频繁", body, re.I):
                self.health.record(final_url, "需要人工验证")
                raise SearchError("落地页要求人工验证")
            return body, final_url

        with ThreadPoolExecutor(max_workers=2) as executor:
            jobs = [(item, executor.submit(fetch_page, item)) for item in selected_pages[:max(4, len(first_pages))]]
            for item, future in jobs:
                try:
                    body, final_url = future.result()
                    page = _Page(body)
                    title = "".join(page.title).strip() or item["title"]
                    text = "".join(page.text)
                    for url in _page_links(text):
                        results.append(self._entry(item, url, title, text.replace("\x1e", "\n")[:1000], final_url))
                except (SearchError, ValueError) as error:
                    self.errors.append(f"{item['engine']} 落地页: {error}")
        unique = {}
        for item in results:
            parts = urlsplit(item["shareurl"])
            key = (parts.path, parts.fragment)
            old = unique.get(key)
            if old is None or (not urlsplit(old["shareurl"]).query and parts.query):
                unique[key] = item
        output = list(unique.values())
        # 跳过或失败的引擎不构成完整查询，也不能覆盖已有完整缓存。
        if not failed_engines:
            with _cache_lock:
                if output or not self.errors:
                    if len(_cache) >= 32:
                        _cache.pop(next(iter(_cache)))
                    _cache[cache_key] = (time.monotonic(), output, list(self.errors))
                else:
                    _cache.pop(cache_key, None)
        return [dict(item) for item in output]

    @staticmethod
    def _entry(item, url, title, content, page_url):
        page_url = item.get("page_url") or page_url
        return {"taskname": title, "shareurl": url, "content": content, "datetime": "",
                "source": "频道索引" if item["engine"].startswith(("Telegram @", "腾讯频道 ")) else "网页搜索",
                "channel": f"{item['engine']} · {urlsplit(page_url).hostname}",
                "page_url": page_url}
