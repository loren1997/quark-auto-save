"""Recheck expired failures with real queries; never install remote adapters."""

import logging
import os
import re
import time
from urllib.parse import urlencode, urlsplit

from .common import SearchError
from .source_health import SourceHealth


WEB_SOURCES = {
    "Meijusou": "https://meijusou.com/search?" + urlencode({"q": "电视剧"}),
    "PansoVIP": "https://www.panso.vip/search?" + urlencode({"q": "电视剧"}),
    "Google": "https://www.google.com/search?" + urlencode({"q": "电视剧 夸克", "gbv": 1, "num": 10}),
    "Bing": "https://cn.bing.com/search?" + urlencode({"q": "电视剧 夸克", "format": "rss", "mkt": "zh-CN"}),
    "DuckDuckGo": "https://html.duckduckgo.com/html/?" + urlencode({"q": "电视剧 夸克"}),
    "Sogou": "https://www.sogou.com/web?" + urlencode({"query": "电视剧 夸克"}),
}


def _enabled(value):
    return str(value).lower() == "true"


def record_web_failures(errors, health_path):
    from .channel_registry import ChannelRegistry

    health = SourceHealth(health_path)
    registry = ChannelRegistry(health_path)
    for error in errors:
        label, reason = error["source"], error["message"]
        if label.startswith("频道索引 Telegram @"):
            registry.record_failure(label.rsplit("@", 1)[1], reason)
            url = "https://t.me/" if reason in ("请求超时", "网络连接失败") else None
        else:
            engine = label.removeprefix("网页搜索 ")
            url = WEB_SOURCES.get(engine)
        if re.fullmatch(r"HTTP 5\d{2}", reason):
            reason = "网络连接失败"
        if not url or reason not in health.DURATIONS:
            continue
        try:
            health.check(url)
        except SearchError:
            continue  # 保留 HTTP 层已记录的 Retry-After。
        health.record(url, reason)


def maintain_sources(config, health_path):
    from resource_search import DEFAULT_PANSOU_SERVER, MAX_PANSOU_SERVERS, _server_url, search_error_reason
    from .channel_registry import ChannelRegistry
    from .channel_search import channel_queries, channel_results
    from .pansou import PanSou
    from .websearch import WebSearch, _search_results

    sources = config.get("source") or {}
    if not health_path or not _enabled((sources.get("monitor") or {}).get("enable", True)):
        return {"checked": 0, "recovered": []}
    ChannelRegistry(health_path).maintain(config)
    health = SourceHealth(health_path)
    probes = {}
    if _enabled((sources.get("websearch") or {}).get("enable", True)):
        probes.update({urlsplit(url).hostname: ("web", name, url, "") for name, url in WEB_SOURCES.items()})
    channels = sources.get("channelsearch") or {}
    try:
        for name, url in channel_queries(channels, "电视剧"):
            probes.setdefault(urlsplit(url).hostname, ("channel", name, url, ""))
    except SearchError:
        logging.warning("频道巡检配置无效，保留配置并跳过")
    pansou = sources.get("pansou") or {}
    if _enabled(pansou.get("enable", True)):
        extra = pansou.get("fallback_servers") or []
        if isinstance(extra, str):
            extra = extra.splitlines()
        if not isinstance(extra, list):
            logging.warning("PanSou 巡检聚合地址列表无效，保留配置并跳过")
            extra = []
        servers = [pansou.get("server") or DEFAULT_PANSOU_SERVER] + extra + os.getenv("PANSOU_FALLBACK_SERVERS", "").splitlines()
        seen = set()
        for index, value in enumerate(servers):
            if not value:
                continue
            try:
                url = _server_url(value)
                if url in seen:
                    continue
                if len(seen) == MAX_PANSOU_SERVERS:
                    break
                seen.add(url)
                token = pansou.get("token", "") if index == 0 else ""
                probes.setdefault(urlsplit(url).hostname, ("pansou", "PanSou", url, token))
            except (SearchError, ValueError):
                logging.warning("PanSou 巡检地址无效，保留配置并跳过")
    result = {"checked": 0, "recovered": []}
    due = [row for row in health.status() if row["status"] == "awaiting_probe" and row["host"] in probes][:2]
    for row in due:
        kind, name, url, token = probes[row["host"]]
        started = time.time()
        client = PanSou(url, token=token) if kind == "pansou" else WebSearch(health_path=health_path)
        try:
            health.check(url)
            if kind == "pansou":
                client.search("电视剧", refresh=True)
            else:
                body, final_url = client._fetch(url, time.monotonic() + 10)
                if kind == "channel":
                    channel_results(name, body, final_url, "电视剧")
                else:
                    _search_results(name, body, final_url)
            if health.clear(url, checked_after=started):
                result["recovered"].append(row["host"])
                logging.info("搜索来源查询恢复：%s", row["host"])
        except (SearchError, ValueError, TypeError) as error:
            reason = search_error_reason(error)
            if not reason.startswith("来源冷却中"):
                reason = reason if reason in health.DURATIONS else "响应格式异常"
                try:
                    health.check(url)
                except SearchError:
                    pass
                else:
                    response = getattr(error.__cause__, "response", None)
                    retry_after = response.headers.get("Retry-After") if response is not None else None
                    health.record(url, reason, retry_after)
        finally:
            client.session.close()
            result["checked"] += 1
    return result
