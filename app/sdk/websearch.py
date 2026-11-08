"""有界的公开网页搜索；只读取页面，不执行脚本或提交表单。"""

from concurrent.futures import ThreadPoolExecutor
from html import unescape
from html.parser import HTMLParser
import ipaddress
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
from urllib3.exceptions import HTTPError as TransportError, NewConnectionError
from urllib3.util.connection import create_connection

from .common import SearchError, extract_share_urls, normalize_share_url


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
    if engine == "Bing":
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError as error:
            raise SearchError("Bing 未返回 RSS，可能需要验证或服务不可用") from error
        if root.tag != "rss" or root.find("channel") is None:
            raise SearchError("Bing RSS 格式已改变")
        return [{"title": item.findtext("title", ""), "url": item.findtext("link", ""),
                 "snippet": item.findtext("description", "")} for item in root.findall("./channel/item")[:10]]
    if re.search(r"anomaly\.js|challenge-form|anomaly-modal|unusual traffic|verify you are human", body, re.I):
        raise SearchError("DuckDuckGo 要求人工验证，请稍后重试")
    page = _Page(body)
    results = []
    for item in page.links:
        if "result__a" not in item["class"].split():
            continue
        link = urljoin(url, item["url"])
        parts = urlsplit(link)
        if parts.hostname in ("duckduckgo.com", "html.duckduckgo.com"):
            link = (parse_qs(parts.query).get("uddg") or [""])[0]
        if link:
            results.append({"title": item["text"], "url": link, "snippet": ""})
    if not results and not re.search(r"no-results|No results found|没有找到", body, re.I):
        raise SearchError("DuckDuckGo 页面格式已改变或需要验证")
    return results[:10]


class WebSearch:
    def __init__(self):
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

    def search(self, query, refresh=False):
        self.errors = []
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise SearchError("网页搜索词必须是 1 至 200 字的文本")
        query = query.strip()
        with _cache_lock:
            cached = _cache.get(query)
            if not refresh and cached and time.monotonic() - cached[0] < 300:
                self.errors = list(cached[2])
                return [dict(item) for item in cached[1]]
        deadline = time.monotonic() + 32
        params = urlencode({"q": query + " 夸克"})
        engines = [("DuckDuckGo", "https://html.duckduckgo.com/html/?" + params),
                   ("Bing", "https://cn.bing.com/search?format=rss&mkt=zh-CN&" + params)]

        def search_engine(engine, url):
            body, final_url = self._fetch(url, deadline)
            return _search_results(engine, body, final_url)

        candidates, succeeded = [], 0
        with ThreadPoolExecutor(max_workers=2) as executor:
            jobs = [(engine, executor.submit(search_engine, engine, url)) for engine, url in engines]
            for engine, future in jobs:
                try:
                    items = future.result()
                    succeeded += 1
                    candidates.extend({**item, "engine": engine} for item in items if _relevant(query, item["title"] + " " + item["snippet"]))
                except (SearchError, ValueError) as error:
                    self.errors.append(f"{engine}: {error}")
        if not succeeded:
            raise SearchError("；".join(self.errors))

        candidates.sort(key=lambda item: ("夸克" in item["title"], "夸克" in item["snippet"]), reverse=True)
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
        with ThreadPoolExecutor(max_workers=2) as executor:
            jobs = [(item, executor.submit(self._fetch, item["url"], deadline)) for item in pages[:4]]
            for item, future in jobs:
                try:
                    body, final_url = future.result()
                    if re.search(r"challenge-form|cf-chl-|verify you are human|访问过于频繁", body, re.I):
                        raise SearchError("落地页要求人工验证")
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
        with _cache_lock:
            if len(_cache) >= 32:
                _cache.pop(next(iter(_cache)))
            _cache[query] = (time.monotonic(), output, list(self.errors))
        return [dict(item) for item in output]

    @staticmethod
    def _entry(item, url, title, content, page_url):
        return {"taskname": title, "shareurl": url, "content": content, "datetime": "",
                "source": "网页搜索", "channel": f"{item['engine']} · {urlsplit(page_url).hostname}",
                "page_url": page_url}
