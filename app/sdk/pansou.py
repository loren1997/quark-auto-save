import re

import requests

from .common import SearchError, iso_to_cst, normalize_share_url


class PanSou:
    """
    PanSou 类，用于获取云盘资源
    """

    def __init__(self, server, token=""):
        self.server = server
        self.session = requests.Session()
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def search(self, keyword: str, refresh: bool = False) -> list:
        """搜索资源

        Args:
            keyword (str): 搜索关键字

        Returns:
            list: 资源列表
        """
        try:
            server = self.server.rstrip("/")
            url = server if server.endswith("/api/search") else f"{server}/api/search"
            params = {"kw": keyword, "cloud_types": ["quark"], "res": "merge", "refresh": refresh}
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
        if not isinstance(data, dict) or not isinstance(data.get("merged_by_type"), dict):
            raise SearchError("搜索源响应缺少 merged_by_type")
        entries = data["merged_by_type"].get("quark", [])
        if not isinstance(entries, list):
            raise SearchError("夸克搜索结果格式错误")
        return self.format_search_results(entries)

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
        link_array = []
        for item in search_results:
            if not isinstance(item, dict):
                continue
            note = str(item.get("note") or item.get("title") or "")
            url = normalize_share_url(item.get("url", ""), item.get("password", ""), note)
            tm = item.get("datetime", "")
            if tm:
                tm = iso_to_cst(tm)

            match = re.search(pattern, note, re.DOTALL)
            if match:
                title = match.group(1)
                content = match.group(2)
            else:
                title = note
                content = ""

            if url != "" and url not in link_array:
                link_array.append(url)
                format_results.append({
                    "shareurl": url,
                    "taskname": title.strip(),
                    "content": content.strip(),
                    "datetime": tm,
                    "channel": item.get("source", ""),
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
