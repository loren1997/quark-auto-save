"""Web 搜索和失效订阅修复共用的资源搜索入口。"""

from concurrent.futures import ThreadPoolExecutor
import os
import re
import time
from urllib.parse import urlsplit

from app.sdk.cloudsaver import CloudSaver
from app.sdk.common import SearchError
from app.sdk.pansou import PanSou
from app.sdk.source_health import SourceHealth
from app.sdk.websearch import WebSearch
from run_progress import report_progress


DEFAULT_PANSOU_SERVER = "https://so.252035.xyz"
MAX_PANSOU_SERVERS = 3


def search_error_reason(message):
    """Only fixed diagnostic categories may leave a transport exception."""
    message = str(message)
    cooldown = re.fullmatch(r"来源冷却中：(HTTP (?:403|404|410|429)|需要人工验证|请求超时|搜索源上游协议错误|网络连接失败|响应格式异常)（剩余 (\d{1,8}) 秒）", message)
    if cooldown:
        return cooldown.group(0)
    if message == "搜索源上游协议错误":
        return message
    status = re.search(r"HTTP\s*(\d{3})", message, re.I)
    if status:
        return f"HTTP {status.group(1)}"
    for pattern, reason in (
        (r"非公网", "地址解析为非公网 IP，已拒绝访问"),
        (r"域名解析|\bdns\b|NameResolution|gaierror", "DNS 域名解析失败"),
        (r"InvalidProxyURL|代理配置", "代理配置无效"),
        (r"proxy|代理", "代理连接失败"),
        (r"\bssl|\btls|certificate|证书", "TLS 或证书连接失败"),
        (r"验证码|人工验证|captcha|challenge", "需要人工验证"),
        (r"频道配置|频道列表", "频道配置无效"),
        (r"未返回公开消息|未返回公开帖子", "频道未返回可匿名读取的公开内容"),
        (r"timeout|超时", "请求超时"),
        (r"Network is unreachable|Network unreachable|Errno 101|网络不可达", "网络不可达，请检查容器出口与代理"),
        (r"MiB|大小|读取(?:上限|限制)", "网页超过读取限制"),
        (r"时间预算|时间上限", "达到本次查询时间预算"),
        (r"json|rss|格式|不是文本网页|decode", "响应格式异常"),
        (r"重定向", "网页重定向超过上限"),
        (r"仅允许无账号的公网|网页地址不符合访问规则", "网页地址不符合访问规则"),
        (r"connect|连接|网络", "网络连接失败"),
    ):
        if re.search(pattern, message, re.I):
            return reason
    return "查询失败"


def _web_errors(messages):
    errors = []
    for message in messages:
        match = re.match(r"^(Bing|DuckDuckGo|Google|Sogou|Meijusou|PansoVIP|Telegram @[A-Za-z][A-Za-z0-9_]{3,31}|腾讯频道 pd\d{5,20})(\s+落地页)?:\s*(.*)$", str(message), re.S)
        source = ("频道索引" if match and match[1].startswith(("Telegram @", "腾讯频道 ")) else "网页搜索") + (f" {match[1]}{match[2] or ''}" if match else "")
        error = {"source": source, "message": search_error_reason(match[3] if match else message)}
        if error not in errors:
            errors.append(error)
    return errors


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


def search_web_resources(query, config, refresh=False, failed_engines=None, health_path=None):
    """自动订阅在网盘索引未找到可验证资源时，单独查询公开网页。"""
    from app.sdk.channel_registry import ChannelRegistry
    from app.sdk.channel_search import DEFAULT_TELEGRAM_CHANNELS, channel_queries
    from app.sdk.source_monitor import record_telegram_failures, record_web_failures

    sources = config.get("source") or {}
    source = sources.get("websearch") or {}
    channels = sources.get("channelsearch") or {}
    account_enabled = (_enabled(channels)
                       and str((sources.get("telegram") or {}).get("enable", False)).lower() == "true")
    if not _enabled(source) and not _enabled(channels):
        return {"success": True, "data": [], "errors": [], "message": ""}
    if not isinstance(query, str) or not query.strip() or len(query) > 200:
        return {"success": False, "data": [], "errors": [], "message": "搜索词必须是 200 字以内的文本"}
    client = WebSearch(health_path=health_path)
    entries, errors, successful = [], [], False
    deadline = time.monotonic() + 32
    try:
        channel_queries(channels, query)  # 先校验用户名单，自动发现不能掩盖配置错误。
        configured = channels.get("telegram_channels", DEFAULT_TELEGRAM_CHANNELS) if _enabled(channels) else []
        if isinstance(configured, str):
            configured = [item.lstrip("@") for item in re.split(r"[,，\s]+", configured.strip()) if item]
        registry = ChannelRegistry(health_path)
        available = registry.available_channels(configured)
        if _enabled(channels) and available != configured:
            channels = {**channels, "telegram_channels": available}
        configured = available
        account_channels = list(configured)
        if _enabled(channels) and str(channels.get("auto_discover", True)).lower() == "true":
            extra = registry.active_channels(configured)
            if extra:
                channels = {**channels, "telegram_channels": list(dict.fromkeys(configured + extra))[:6]}
            if account_enabled:
                account_channels += registry.active_channels(configured, check_health=False)
        account_channels = list(dict.fromkeys(account_channels))
        if len(account_channels) > 6:
            offset = int(time.time() // 300) % len(account_channels)
            account_channels = (account_channels[offset:] + account_channels[:offset])[:6]
        options = {"failed_engines": failed_engines} if failed_engines is not None else {}
        with ThreadPoolExecutor(max_workers=2) as executor:
            web = executor.submit(client.search, query.strip(), refresh=refresh, channel_config=channels,
                                  include_engines=_enabled(source), **options)
            native = None
            native_started = time.time()
            if account_enabled and account_channels and (failed_engines is None or "Telegram账号" not in failed_engines):
                from app.sdk.telegram_account import TelegramAccount
                native = executor.submit(TelegramAccount(health_path).search, query.strip(), account_channels,
                                         limit=20, deadline=deadline)
            try:
                entries.extend(web.result())
                successful = True
                errors.extend(_web_errors(client.errors))
            except (SearchError, ValueError) as error:
                messages = client.errors if isinstance(client.errors, list) and client.errors else [error]
                errors.extend(_web_errors(messages))
            record_web_failures(errors, health_path)
            if native is not None:
                try:
                    result = native.result()
                except Exception:
                    result = {"ok": False, "code": "QUERY_FAILED"}
                native_errors = list(result.get("errors") or [])
                native_ok = bool(result.get("ok")) and (bool(result.get("candidates")) or not native_errors
                                                        or len(native_errors) < len(account_channels))
                successful = successful or native_ok or bool(result.get("candidates"))
                entries.extend(result.get("candidates") or [])
                if not result.get("ok") and not native_errors:
                    native_errors = [{"source": "Telegram账号", "code": result.get("code"),
                                      "retry_after": result.get("retry_after")}]
                record_telegram_failures(native_errors, health_path, account_channels,
                                         result.get("unavailable_channels") or [])
                registry.record_account_results(account_channels, result.get("candidates") or [], native_started)
                reasons = {"CHANNEL_UNAVAILABLE": "频道不可访问或已失效", "CHANNEL_RESTRICTED": "频道受限，已跳过",
                           "FLOOD_WAIT": "Telegram 请求过于频繁，稍后自动重试", "ACCOUNT_REVOKED": "Telegram 登录已失效",
                           "ACCOUNT_BANNED": "Telegram 账号受限", "AUTH_REQUIRED": "Telegram 账号未登录",
                           "SDK_MISSING": "Telegram 账号组件未安装", "TIMEOUT": "请求超时",
                           "NETWORK_ERROR": "网络连接失败", "BUSY": "Telegram 账号繁忙，稍后自动重试"}
                for error in native_errors:
                    label = str(error.get("source") or "")
                    label = "Telegram账号 · " + label if re.fullmatch(r"Telegram @[A-Za-z][A-Za-z0-9_]{3,31}", label) else "Telegram账号"
                    message = reasons.get(error.get("code"), "Telegram 账号查询失败")
                    errors.append({"source": label, "message": message})
                if not native_ok and failed_engines is not None:
                    failed_engines.add("Telegram账号")
    except (SearchError, ValueError) as error:
        errors.extend(_web_errors([error]))
    finally:
        client.session.close()
    return {"success": successful, "data": entries, "errors": errors,
            "message": "；".join(f"{error['source']}：{error['message']}" for error in errors)}


def search_resources(query, config, refresh=False, include_web=True, failed_sources=None, health_path=None):
    """聚合网盘索引与公开网页；索引源最多三个 PanSou 与一个 CloudSaver。

    PanSou fallback_servers 与 PANSOU_FALLBACK_SERVERS 环境变量为补充源，
    不转发主源 token；channels/plugins 可为数组或逗号、空白分隔名称。
    failed_sources 可由一次自动查找的多个关键词共用，避免重复等待故障源。
    返回 {success, data, errors, message}，所有源故障时 success=False。
    """
    if not isinstance(query, str) or len(query) > 200:
        return {"success": False, "data": [], "errors": [], "message": "搜索词必须是 200 字以内的文本"}
    query = query.strip()
    if not query:
        return {"success": True, "data": [], "errors": [], "message": ""}
    sources = config.get("source") or {}
    failed_sources = failed_sources if failed_sources is not None else set()
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
    if include_web and (_enabled(sources.get("websearch") or {}) or _enabled(sources.get("channelsearch") or {})):
        jobs.append(("网页搜索", "", ""))

    def search_source(job):
        label, server, token = job
        report_progress("search", source=label, query=query)
        if label == "网页搜索":
            response = search_web_resources(query, config, refresh=refresh, health_path=health_path)
            return response["data"], response["errors"], response["success"]
        client = CloudSaver(server) if label == "CloudSaver" else PanSou(server, token=token)
        health = SourceHealth(health_path)
        checked_after = time.time()
        try:
            health.check(server)
            if label == "CloudSaver":
                client.set_auth(cs.get("username", ""), cs.get("password", ""), cs.get("token", ""))
                response = client.auto_login_search(query)
                if not response.get("success"):
                    raise SearchError(response.get("message", "搜索失败"))
                entries = client.clean_search_results(response.get("data", []))
            else:
                entries = client.search(query, refresh=refresh, **selection)
            health.clear(server, checked_after=checked_after)
            return entries, [], True
        except SearchError as error:
            reason = search_error_reason(error)
            if reason in SourceHealth.DURATIONS:
                response = getattr(error.__cause__, "response", None)
                retry_after = response.headers.get("Retry-After") if response is not None else None
                health.record(server, reason, retry_after=retry_after)
            raise
        finally:
            client.session.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [(job, executor.submit(search_source, job)) for job in jobs
                   if (job[0], job[1]) not in failed_sources]
        for (label, server, _token), future in futures:
            try:
                entries, warnings, source_ok = future.result()
                successful += bool(source_ok)
                errors.extend(warnings)
                results.extend({**item, "source": item.get("source") or label} if label == "网页搜索"
                               else {**item, "source": label} for item in entries)
            except (SearchError, ValueError) as error:
                failed_sources.add((label, server))
                errors.append({"source": label, "message": str(error)})
    if not successful and not errors:
        errors.append({"source": "搜索源" if jobs else "搜索配置", "message":
                       "本次查找已跳过此前失败的来源" if jobs else
                       "没有启用的搜索源，请启用 PanSou、网页搜索或配置 CloudSaver"})
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
