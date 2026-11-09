"""Read public channel previews without login or client-side scripts."""

import re
from urllib.parse import urlencode, urljoin, urlsplit

from .common import SearchError


DEFAULT_TELEGRAM_CHANNELS = ["guoman4K", "kuakedongman"]
DEFAULT_QQ_CHANNELS = ["pd26778899"]


def channel_queries(config, query):
    if not isinstance(config, dict):
        raise SearchError("频道配置必须是对象")
    if str(config.get("enable", True)).lower() != "true":
        return []
    jobs = []
    for key, defaults, pattern in (
        ("telegram_channels", DEFAULT_TELEGRAM_CHANNELS, r"[A-Za-z][A-Za-z0-9_]{3,31}"),
        ("qq_channels", DEFAULT_QQ_CHANNELS, r"pd\d{5,20}"),
    ):
        channels = config.get(key, defaults)
        if isinstance(channels, str):
            channels = [item.lstrip("@") for item in re.split(r"[,，\s]+", channels.strip()) if item]
        if (not isinstance(channels, list) or len(channels) > 6
                or any(not isinstance(item, str) or not re.fullmatch(pattern, item) for item in channels)):
            raise SearchError("频道列表每类最多 6 项，请填写有效的 Telegram 用户名或腾讯 pd 数字频道号")
        for channel in dict.fromkeys(channels):
            # shortcut: 每个频道只读一页；有实际漏搜证据时再加入有界翻页。
            if key == "telegram_channels":
                jobs.append((f"Telegram @{channel}", f"https://t.me/s/{channel}?" + urlencode({"q": query})))
            else:
                jobs.append((f"腾讯频道 {channel}", f"https://pd.qq.com/g/{channel}"))
    return jobs


def channel_results(engine, body, url, query):
    from .websearch import _Page, _page_links, _relevant

    expected_host = "t.me" if engine.startswith("Telegram @") else "pd.qq.com"
    if urlsplit(url).hostname != expected_host:
        raise SearchError("频道重定向到其他站点，已拒绝读取")
    if re.search(r"cf-chl-|captcha|verify you are human|EO_Bot_Ssid|__tst_status|访问过于频繁", body, re.I):
        raise SearchError("频道页面要求人工验证或浏览器验证")
    if engine.startswith("腾讯频道 "):
        channel = engine.split(" ", 1)[1]
        page = _Page(body)
        results, seen = [], set()
        for item in page.links:
            target = urljoin(url, item["url"])
            parts = urlsplit(target)
            if (parts.hostname == "pd.qq.com" and not parts.query and not parts.fragment
                    and re.fullmatch(r"/g/" + re.escape(channel) + r"/post/[A-Za-z0-9_-]+", parts.path)
                    and item["text"] and target not in seen):
                seen.add(target)
                if _relevant(query, item["text"]):
                    results.append({"title": item["text"], "url": target, "snippet": ""})
        if not seen and not re.search(r"暂无帖子|暂无内容|还没有帖子", " ".join(page.text)):
            raise SearchError("腾讯频道未返回公开帖子列表，可能需要浏览器或登录")
        return results[:10]

    class MessagePage(_Page):
        def __init__(self, html):
            self.messages, self.current = [], None
            self.depth = 0
            super().__init__(html)

        def handle_starttag(self, tag, attrs):
            super().handle_starttag(tag, attrs)
            if self._skip:
                return
            attrs = dict(attrs)
            if tag == "div":
                self.depth += 1
                classes = attrs.get("class", "").split()
                if "tgme_widget_message" in classes and attrs.get("data-post"):
                    self.current = {"depth": self.depth, "post": attrs["data-post"], "content": ""}
                if self.current and "tgme_widget_message_text" in classes:
                    self.current["text_depth"] = self.depth
                    self.current["text_start"] = len(self.text)

        def handle_endtag(self, tag):
            super().handle_endtag(tag)
            if tag == "div" and not self._skip:
                if self.current and self.depth == self.current.get("text_depth"):
                    self.current["content"] = "".join(self.text[self.current["text_start"]:])
                if self.current and self.depth == self.current["depth"]:
                    self.messages.append(self.current)
                    self.current = None
                self.depth -= 1

    page = MessagePage(body)
    channel = engine.split("@", 1)[1]
    results = []
    for message in page.messages[:40]:
        if not re.fullmatch(re.escape(channel) + r"/\d+", message["post"], re.I):
            continue
        content = message["content"]
        if not _relevant(query, content):
            continue
        title = next((line.strip() for line in content.replace("\x1e", "\n").splitlines() if line.strip()), "")
        for shareurl in _page_links(content):
            results.append({"title": title[:200], "url": shareurl, "snippet": content.replace("\x1e", "\n")[:1000],
                            "page_url": "https://t.me/" + message["post"]})
    if not page.messages and not re.search(r"tgme_channel_history.*(?:No posts found|No messages found|Nothing found)|tgme_widget_message_service.*No posts found", body, re.S | re.I):
        raise SearchError("Telegram 未返回公开消息列表，频道可能不公开或响应格式已改变")
    return results[:40]
