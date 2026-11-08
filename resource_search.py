"""Web 搜索和失效订阅修复共用的资源搜索入口。"""

from concurrent.futures import ThreadPoolExecutor
import os
import re
from urllib.parse import urlsplit

from app.sdk.cloudsaver import CloudSaver
from app.sdk.common import SearchError
from app.sdk.pansou import PanSou


DEFAULT_PANSOU_SERVER = "https://so.252035.xyz"
MAX_PANSOU_SERVERS = 3


def _enabled(source):
    return str(source.get("enable", True)).lower() == "true"


def _server_url(value):
    if not isinstance(value, str):
        raise SearchError("搜索服务器地址必须是 HTTP(S) URL")
    value = value.strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise SearchError("搜索服务器地址必须是无账号、参数和片段的 HTTP(S) URL")
    return value


def _selection(value, label):
    if not value:
        return []
    if isinstance(value, str):
        value = [item for item in re.split(r"[,，\s]+", value.strip()) if item]
    if not isinstance(value, list) or len(value) > 20 or any(
        not isinstance(item, str) or not re.fullmatch(r"@?[A-Za-z0-9_-]{1,100}", item) for item in value
    ):
        raise SearchError(f"{label}最多 20 项，只能包含字母、数字、下划线和连字符")
    return list(dict.fromkeys(item.lstrip("@") for item in value))


def search_resources(query, config, refresh=False):
    """最多聚合三个 PanSou 与一个 CloudSaver，并发为 2，保留部分源故障。

    PanSou fallback_servers 与 PANSOU_FALLBACK_SERVERS 环境变量为补充源，
    不转发主源 token；channels/plugins 可为数组或逗号、空白分隔名称。
    返回 {success, data, errors, message}，所有源故障时 success=False。
    """
    if not isinstance(query, str) or len(query) > 200:
        return {"success": False, "data": [], "errors": [], "message": "搜索词必须是 200 字以内的文本"}
    query = query.strip()
    if not query:
        return {"success": True, "data": [], "errors": [], "message": ""}
    sources = config.get("source") or {}
    results, errors, jobs = [], [], []
    successful = 0
    ps = sources.get("pansou") or {}
    if _enabled(ps):
        fallback = ps.get("fallback_servers") or []
        if isinstance(fallback, str):
            fallback = fallback.splitlines()
        if not isinstance(fallback, list):
            errors.append({"source": "PanSou", "message": "聚合服务器必须是地址列表或换行分隔地址"})
            fallback = []
        servers = [ps.get("server") or DEFAULT_PANSOU_SERVER] + fallback + os.getenv("PANSOU_FALLBACK_SERVERS", "").splitlines()
        seen = set()
        try:
            selection = {key: _selection(ps.get(key), label) for key, label in (("channels", "频道"), ("plugins", "插件"))}
        except SearchError as error:
            errors.append({"source": "PanSou", "message": str(error)})
            servers = []
        for index, server in enumerate(servers):
            if not server:
                continue
            label = "PanSou" if index == 0 else f"PanSou 聚合 {len(seen)}"
            try:
                server = _server_url(server)
                if server in seen:
                    continue
                if len(seen) == MAX_PANSOU_SERVERS:
                    errors.append({"source": "PanSou", "message": "本次只查询前三个不同服务器，已忽略多余地址"})
                    break
                seen.add(server)
                jobs.append((label, server, ps.get("token", "") if index == 0 else ""))
            except (SearchError, ValueError) as error:
                errors.append({"source": label, "message": str(error)})
    cs = sources.get("cloudsaver") or {}
    if _enabled(cs) and cs.get("server"):
        try:
            jobs.append(("CloudSaver", _server_url(cs["server"]), ""))
        except (SearchError, ValueError) as error:
            errors.append({"source": "CloudSaver", "message": str(error)})

    def search_source(job):
        label, server, token = job
        client = CloudSaver(server) if label == "CloudSaver" else PanSou(server, token=token)
        try:
            if label == "CloudSaver":
                client.set_auth(cs.get("username", ""), cs.get("password", ""), cs.get("token", ""))
                response = client.auto_login_search(query)
                if not response.get("success"):
                    raise SearchError(response.get("message", "搜索失败"))
                return client.clean_search_results(response.get("data", []))
            return client.search(query, refresh=refresh, **selection)
        finally:
            client.session.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [(job[0], executor.submit(search_source, job)) for job in jobs]
        for label, future in futures:
            try:
                entries = future.result()
                successful += 1
                results.extend({**item, "source": label} for item in entries)
            except (SearchError, ValueError) as error:
                errors.append({"source": label, "message": str(error)})
    if not successful and not errors:
        errors.append({"source": "搜索配置", "message": "没有启用的搜索源，请启用 PanSou 或配置 CloudSaver"})
    results.sort(key=lambda item: item.get("datetime", ""), reverse=True)
    unique = {}
    for item in results:
        # 同一分享的多个频道可能只有一个提供提取码，优先保留带码链接。
        parts = urlsplit(item["shareurl"])
        key = (parts.path.rstrip("/"), parts.fragment)
        previous = unique.get(key)
        if previous is None:
            unique[key] = item
            continue
        winner = item if not urlsplit(previous["shareurl"]).query and parts.query else previous
        for field in ("source", "channel"):
            labels = (str(value).split(" / ") for value in (previous.get(field), item.get(field)) if value)
            winner[field] = " / ".join(dict.fromkeys(label for group in labels for label in group))
        unique[key] = winner
    return {
        "success": successful > 0,
        "data": list(unique.values()),
        "errors": errors,
        "message": "；".join(f"{error['source']}: {error['message']}" for error in errors),
    }
