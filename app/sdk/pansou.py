import re

import requests

from .common import SearchError, extract_share_urls, iso_to_cst, normalize_share_url


class PanSou:
    """
    PanSou 类，用于获取云盘资源
    """

    def __init__(self, server, token=""):
        self.server = server
        self.session = requests.Session()
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def search(self, keyword: str, refresh: bool = False, channels=None, plugins=None) -> list:
        """搜索资源

        Args:
            keyword (str): 搜索关键字

        Returns:
            list: 资源列表
        """
        try:
            server = self.server.rstrip("/")
            url = server if server.endswith("/api/search") else f"{server}/api/search"
            params = {"kw": keyword, "cloud_types": ["quark"], "res": "all", "src": "all", "refresh": refresh}
            if channels:
                params["channels"] = channels
            if plugins:
                params["plugins"] = plugins
            response = self.session.post(url, json=params, timeout=(5, 25))
            response.raise_for_status()
            result = response.json()
        except requests.HTTPError as error:
            raise SearchError(f"HTTP {error.response.status_code}") from error
        except requests.RequestException as error:
            raise SearchError(f"网络请求失败 ({type(error).__name__})") from error
        except ValueError as error:
            raise SearchError("搜索源返回的不是 JSON") from error
        if not isinstance(result, dict):
            raise SearchError("搜索源响应格式错误")
        if result.get("error") or result.get("code", 0) not in (0, 200, "0", "200") or result.get("success") is False:
            raise SearchError(str(result.get("message") or result.get("error") or "搜索源拒绝请求")[:200])
        # 公共代理包装 code/data，原生 PanSou 直接返回 merged_by_type。
        data = result.get("data", result)
        if not isinstance(data, dict):
            raise SearchError("搜索源响应格式错误")
        # 原生 Go 服务在零结果时省略空数组和空字典。
        if data.get("total") == 0 and not data.get("results") and not data.get("merged_by_type"):
            return []
        merged = data.get("merged_by_type", {})
        raw = data.get("results", [])
        if not isinstance(merged, dict) or not isinstance(raw, list):
            raise SearchError("搜索源响应格式错误")
        if "merged_by_type" not in data and "results" not in data:
            raise SearchError("搜索源响应缺少 merged_by_type")
        entries = merged.get("quark", [])
        if not isinstance(entries, list):
            raise SearchError("夸克搜索结果格式错误")
        # 原始 TG 结果可能只命中一个字，正文嗅探前先过滤明显无关的消息。
        terms = [part.casefold() for part in re.split(r"[\W_]+", keyword) if part]
        matching = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            links = item.get("links") or []
            titles = [str(link.get("work_title") or "") for link in links if isinstance(link, dict)] if isinstance(links, list) else []
            text = re.sub(r"[\W_]+", "", str(item.get("title") or "") + str(item.get("content") or "") + "".join(titles)).casefold()
            if all(term in text for term in terms):
                matching.append(item)
        return self.format_search_results(entries + matching)

    def format_search_results(self, search_results: list) -> list:
        """格式化搜索结果

        Args:
            search_results (list): 搜索结果列表

        Returns:
            list: 夸克网盘资源列表
        """
        pattern = (
            r'^(.*?)'
            r'(?:'
            r'[【\[]?'
            r'(?:简介|介绍|描述)'
            r'[】\]]?'
            r'[:：]?'
            r')'
            r'(.*)$'
        )
        format_results = []
        link_array = set()
        for item in search_results:
            if not isinstance(item, dict):
                continue
            note = str(item.get("note") or item.get("title") or "")
            body = str(item.get("content") or "")
            text_urls = extract_share_urls(note + "\n" + body)
            links = []
            structured = item.get("links") or []
            structured = structured if isinstance(structured, list) else []
            structured_count = bool(item.get("url")) + sum(isinstance(link, dict) and link.get("type") in (None, "quark") for link in structured)
            context = note + "\n" + body if not text_urls and structured_count == 1 else ""
            url = normalize_share_url(item.get("url", ""), item.get("password", ""), context)
            if url:
                links.append((url, ""))
            for link in structured:
                if not isinstance(link, dict) or link.get("type") not in (None, "quark"):
                    continue
                url = normalize_share_url(link.get("url"), link.get("password"), context)
                if url:
                    links.append((url, str(link.get("work_title") or "")))
            links.extend((url, "") for url in text_urls)
            tm = item.get("datetime", "")
            if tm:
                tm = iso_to_cst(tm)

            match = re.search(pattern, note, re.DOTALL)
            if match:
                title = match.group(1)
                content = match.group(2)
            else:
                title = note
                content = body

            for url, work_title in links:
                if url in link_array:
                    continue
                link_array.add(url)
                format_results.append({
                    "shareurl": url,
                    "taskname": (work_title or title).strip(),
                    "content": content.strip(),
                    "datetime": tm,
                    "channel": item.get("source") or item.get("channel") or "",
                    "source": "PanSou"
                })

        return format_results


if __name__ == "__main__":
    server: str = "https://so.252035.xyz"
    pansou = PanSou(server)
    results = pansou.search("哪吒")
    for item in results:
        print(f"标题: {item['taskname']}")
        print(f"描述: {item['content']}")
        print(f"链接: {item['shareurl']}")
        print(f"时间: {item['datetime']}")
        print("-" * 50)
