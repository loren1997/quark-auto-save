"""Web 搜索和失效订阅修复共用的资源搜索入口。"""

from urllib.parse import urlsplit

from app.sdk.cloudsaver import CloudSaver
from app.sdk.common import SearchError
from app.sdk.pansou import PanSou


DEFAULT_PANSOU_SERVER = "https://so.252035.xyz"


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


def search_resources(query, config, refresh=False):
    """返回 {success, data, errors, message}；所有源故障时 success=False。

    data 保持 UI 的 taskname/shareurl/content/datetime/channel/source 字段。
    config 为项目完整配置。PanSou 支持 server、token、fallback_servers
    (字符串数组或换行分隔地址)；备用源只在主源失败或零结果时尝试，不携带主源 token。
    """
    if not isinstance(query, str) or len(query) > 200:
        return {"success": False, "data": [], "errors": [], "message": "搜索词必须是 200 字以内的文本"}
    query = query.strip()
    if not query:
        return {"success": True, "data": [], "errors": [], "message": ""}
    sources = config.get("source", {})
    results, errors = [], []
    successful = 0
    ps = sources.get("pansou", {})
    if _enabled(ps):
        fallback = ps.get("fallback_servers") or []
        if isinstance(fallback, str):
            fallback = fallback.splitlines()
        if not isinstance(fallback, list):
            errors.append({"source": "PanSou", "message": "备用服务器必须是地址列表或换行分隔地址"})
            fallback = []
        servers = [ps.get("server") or DEFAULT_PANSOU_SERVER] + fallback
        seen = set()
        for index, server in enumerate(servers):
            try:
                server = _server_url(server)
                if server in seen:
                    continue
                seen.add(server)
                client = PanSou(server, token=ps.get("token", "") if index == 0 else "")
                try:
                    entries = client.search(query, refresh=refresh)
                finally:
                    client.session.close()
                successful += 1
                results.extend(entries)
                if entries:
                    break
            except (SearchError, ValueError) as error:
                errors.append({"source": "PanSou" if index == 0 else f"PanSou 备用 {index}", "message": str(error)})
    cs = sources.get("cloudsaver", {})
    if _enabled(cs) and cs.get("server"):
        try:
            client = CloudSaver(_server_url(cs["server"]))
            client.set_auth(cs.get("username", ""), cs.get("password", ""), cs.get("token", ""))
            try:
                response = client.auto_login_search(query)
            finally:
                client.session.close()
            if not response.get("success"):
                raise SearchError(response.get("message", "搜索失败"))
            results.extend(client.clean_search_results(response.get("data", [])))
            successful += 1
        except (SearchError, ValueError) as error:
            errors.append({"source": "CloudSaver", "message": str(error)})
    if not successful and not errors:
        errors.append({"source": "搜索配置", "message": "没有启用的搜索源，请启用 PanSou 或配置 CloudSaver"})
    results.sort(key=lambda item: item.get("datetime", ""), reverse=True)
    unique = {}
    for item in results:
        # 同一分享的多个频道可能只有一个提供提取码，优先保留带码链接。
        parts = urlsplit(item["shareurl"])
        key = (parts.path, parts.fragment)
        previous = unique.get(key)
        if previous is None or (not urlsplit(previous["shareurl"]).query and parts.query):
            unique[key] = item
    return {
        "success": successful > 0,
        "data": list(unique.values()),
        "errors": errors,
        "message": "；".join(f"{error['source']}: {error['message']}" for error in errors),
    }
