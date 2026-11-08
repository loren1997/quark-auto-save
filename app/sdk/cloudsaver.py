import re
import requests
from .common import extract_share_urls, iso_to_cst, normalize_share_url


class CloudSaver:
    """
    CloudSaver 类，用于获取云盘资源
    """

    def __init__(self, server):
        self.server = server.rstrip("/")
        self.username = None
        self.password = None
        self.token = None
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def set_auth(self, username, password, token=""):
        self.username = username
        self.password = password
        self.token = token
        self.session.headers.update({"Authorization": f"Bearer {self.token}"})

    def login(self):
        if not self.username or not self.password:
            return {"success": False, "message": "CloudSaver未设置用户名或密码"}
        try:
            url = f"{self.server}/api/user/login"
            data = {"username": self.username, "password": self.password}
            response = self.session.post(url, json=data, timeout=(5, 25))
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                return {"success": False, "message": "CloudSaver登录响应格式错误"}
            if result.get("success") and isinstance(result.get("data"), dict) and result["data"].get("token"):
                self.token = result.get("data", {}).get("token")
                self.session.headers.update({"Authorization": f"Bearer {self.token}"})
                return {"success": True, "token": self.token}
            else:
                return {
                    "success": False,
                    "message": f"CloudSaver登录{result.get('message', '未知错误')}",
                }
        except requests.HTTPError as e:
            return {"success": False, "message": f"CloudSaver登录 HTTP {e.response.status_code}"}
        except (requests.RequestException, ValueError) as e:
            return {"success": False, "message": f"CloudSaver登录失败 ({type(e).__name__})"}

    def search(self, keyword, last_message_id=""):
        """
        搜索资源

        Args:
            keyword (str): 搜索关键词
            last_message_id (str): 上一条消息ID，用于分页

        Returns:
            list: 搜索结果列表
        """
        try:
            url = f"{self.server}/api/search"
            params = {"keyword": keyword, "lastMessageId": last_message_id}
            response = self.session.get(url, params=params, timeout=(5, 25))
            if response.status_code in (401, 403):
                return {"success": False, "auth_error": True, "message": "CloudSaver登录已失效"}
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                return {"success": False, "message": "CloudSaver响应格式错误"}
            if result.get("success"):
                data = result.get("data", [])
                if not isinstance(data, list):
                    return {"success": False, "message": "CloudSaver搜索结果格式错误"}
                return {"success": True, "data": data}
            else:
                return {"success": False, "message": result.get("message", "未知错误")}
        except requests.HTTPError as e:
            return {"success": False, "message": f"CloudSaver HTTP {e.response.status_code}"}
        except (requests.RequestException, ValueError) as e:
            return {"success": False, "message": f"CloudSaver搜索失败 ({type(e).__name__})"}

    def auto_login_search(self, keyword, last_message_id=""):
        """
        自动登录并搜索资源

        Args:
            keyword (str): 搜索关键词
            last_message_id (str): 上一条消息ID，用于分页
        """
        result = self.search(keyword, last_message_id)
        if result.get("success"):
            return result
        else:
            if (
                result.get("auth_error")
                or
                result.get("message") == "无效的 token"
                or result.get("message") == "未提供 token"
            ):
                login_result = self.login()
                if login_result.get("success"):
                    result = self.search(keyword, last_message_id)
                    result["new_token"] = login_result.get("token")
                    return result
                else:
                    return {
                        "success": False,
                        "message": login_result.get("message", "未知错误"),
                    }
            return {"success": False, "message": result.get("message", "未知错误")}

    def clean_search_results(self, search_results):
        """
        清洗搜索结果

        Args:
            search_results (list): 搜索结果列表

        Returns:
            list: 夸克网盘链接列表
        """
        pattern_title = r"(名称|标题)[：:]?(.*)"
        pattern_content = r"(描述|简介)[：:]?(.*)(链接|标签)"
        clean_results = []
        link_array = set()
        for channel in search_results:
            if not isinstance(channel, dict):
                continue
            items = channel.get("list") or []
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "")
                if match := re.search(pattern_title, title, re.DOTALL):
                    title = match.group(2)
                title = title.replace("&amp;", "&").strip()
                content = str(item.get("content") or "")
                text_urls = extract_share_urls(title + "\n" + content)
                urls = []
                structured = item.get("cloudLinks") or []
                structured = structured if isinstance(structured, list) else []
                count = sum(isinstance(link, dict) and link.get("cloudType") == "quark" for link in structured)
                context = content if not text_urls and count == 1 else ""
                for link in structured:
                    if isinstance(link, dict) and link.get("cloudType") == "quark":
                        url = normalize_share_url(link.get("link"), link.get("password") or link.get("code"), context)
                        if url and url not in urls:
                            urls.append(url)
                urls.extend(url for url in text_urls if url not in urls)
                if match := re.search(pattern_content, content, re.DOTALL):
                    content = match.group(2)
                content = content.replace('<mark class="highlight">', "").replace("</mark>", "").strip()
                pubdate = iso_to_cst(item["pubDate"]) if item.get("pubDate") else ""
                for url in urls:
                    if url in link_array:
                        continue
                    link_array.add(url)
                    clean_results.append({
                        "shareurl": url,
                        "taskname": title,
                        "content": content,
                        "datetime": pubdate,
                        "tags": item.get("tags", []),
                        "channel": item.get("channelId") or channel.get("channelId") or "",
                        "source": "CloudSaver",
                    })
        return clean_results


# 测试示例
if __name__ == "__main__":
    # 创建CloudSaver实例
    server = ""
    username = ""
    password = ""
    token = ""
    cloud_saver = CloudSaver(server)
    cloud_saver.set_auth(username, password, token)
    # 搜索资源
    results = cloud_saver.auto_login_search("黑镜")
    # 提取夸克网盘链接
    clean_results = cloud_saver.clean_search_results(results.get("data", []))
    # 打印结果
    for item in clean_results:
        print(f"标题: {item['taskname']}")
        print(f"描述: {item['content']}")
        print(f"链接: {item['shareurl']}")
        print(f"标签: {' '.join(item['tags'])}")
        print("-" * 50)
