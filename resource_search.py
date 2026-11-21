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
    telegram_wait = re.fullmatch(r"Telegram 查询限流，剩余 (\d{1,8}) 秒后自动重试", message)
    if telegram_wait:
        return telegram_wait.group(0)
    status = re.search(r"HTTP\s*(\d{3})", message, re.I)
    if status:
        return f"HTTP {status.group(1)}"
    for pattern, reason in (
        (r"Telegram 暂时限制查询|Telegram 查询限流|FLOOD(?:_PREMIUM)?_WAIT", "Telegram 查询限流，等待冷却后自动重试"),
        (r"归档消息详情读取失败", "归档消息详情读取失败，稍后自动重试"),
        (r"没有启用的搜索源|未启用其他索引来源", "未启用其他索引来源"),
        (r"本次查找已跳过此前失败的来源", "本次查找已跳过此前失败的来源"),
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


def search_telegram_resources(query, config, health_path=None, limit=20, deadline=None):
    from app.sdk.telegram_web import TelegramWeb

    if str(((config.get("source") or {}).get("telegram_web") or {}).get("enable", False)).lower() != "true":
        return {"success": True, "data": [], "errors": [], "partial": False, "message": ""}
    report_progress("search", source="Telegram归档", query=query)
    print(f"Telegram归档：正在搜索 {query}", flush=True)
    try:
        result = TelegramWeb(health_path).search(query, limit=limit, deadline=deadline)
    except Exception:
        result = {"success": False, "data": {"reason": "Telegram 归档查询暂不可用"}}
    candidates = result.get("results") or []
    data, progress = result.get("data") or {}, result.get("progress") or {}
    reading_archives = progress.get("phase") == "archives"
    if reading_archives:
        print(f"Telegram归档：归档列表尚未读完，本次已读 {progress.get('current', 0)} 个对话；暂不判断频道是否缺失", flush=True)
    else:
        print(f"Telegram归档：{data.get('archived_count', 0)} 个归档对话，"
              f"已检查 {progress.get('current', 0)} 条消息，返回 {len(candidates)} 条夸克候选"
              + ("；还有结果待继续查询" if result.get("partial") else ""), flush=True)
    errors = []
    bot = result.get("bot") or {}
    if bot:
        channel_state = "归档列表未读完，奥斯卡频道状态待确认" if reading_archives else ("奥斯卡资源频道在归档中" if bot.get("channel_archived") else "未检测到已归档的奥斯卡资源频道")
        print(f"Telegram归档资源按钮：{channel_state}；本批定向查询 {bot.get('channel_posts', 0)} 条帖子，补读 {bot.get('details_read', 0)} 条详情，读取 {bot.get('button_count', 0)} 个按钮，识别 {bot.get('buttons', 0)} 个资源按钮；Oscar4KBot 请求 {bot.get('requests', 0)} 次，"
              f"新增 {bot.get('links', 0)} 条夸克候选，待获取 {bot.get('pending', 0)} 项，"
              f"未取得可关联资源回复 {bot.get('failed', 0)} 项", flush=True)
        rejected = bot.get("button_rejections") or {}
        reasons = [("not_url", "非链接按钮"), ("invalid_url", "链接格式不支持"), ("other_target", "其他目标"),
                   ("other_action", "非资源获取操作"), ("invalid_payload", "资源编号不支持")]
        summary = "，".join(f"{label} {rejected[key]}" for key, label in reasons if rejected.get(key))
        if summary:
            print(f"Telegram归档按钮筛选：{summary}", flush=True)
        if bot.get("failed"):
            errors.append({"source": "Telegram归档资源按钮", "message": "Oscar4KBot 暂不可用或未返回可关联的夸克资源，稍后自动重试"})
        if bot.get("details_failed"):
            print(f"Telegram归档资源按钮：{bot['details_failed']} 条消息详情未读完整，不能据此判断帖子没有资源按钮", flush=True)
            errors.append({"source": "Telegram归档消息详情", "message": "归档消息详情读取失败，稍后自动重试"})
    # A full candidate batch is pagination, not a failed source.
    if not result.get("success") or result.get("code"):
        reason = data.get("reason") or "Telegram 归档查询未完成"
        if result.get("code") == "FLOOD_WAIT":
            reason = f"Telegram 查询限流，剩余 {int(data.get('retry_after') or 0)} 秒后自动重试"
        errors.append({"source": "Telegram归档", "message": reason})
        print(f"Telegram归档：{reason}", flush=True)
    return {"success": bool(result.get("success") or candidates), "data": candidates, "errors": errors,
            "partial": bool(result.get("partial")), "code": result.get("code"),
            "message": "；".join(error["message"] for error in errors)}


def search_web_resources(query, config, refresh=False, failed_engines=None, health_path=None, include_archive=True):
    """查询公开网页与用户明确启用的 Telegram 归档对话。"""
    from app.sdk.channel_search import channel_queries
    from app.sdk.source_monitor import record_web_failures

    sources = config.get("source") or {}
    source = sources.get("websearch") or {}
    channels = sources.get("channelsearch") or {}
    archive_enabled = include_archive and str((sources.get("telegram_web") or {}).get("enable", False)).lower() == "true"
    if not _enabled(source) and not _enabled(channels) and not archive_enabled:
        return {"success": True, "data": [], "errors": [], "message": ""}
    if not isinstance(query, str) or not query.strip() or len(query) > 200:
        return {"success": False, "data": [], "errors": [], "message": "搜索词必须是 200 字以内的文本"}
    client = WebSearch(health_path=health_path)
    entries, errors, successful = [], [], False
    deadline = time.monotonic() + 32
    try:
        channel_queries(channels, query)
        options = {"failed_engines": failed_engines} if failed_engines is not None else {}
        with ThreadPoolExecutor(max_workers=2) as executor:
            web = None
            if _enabled(source) or _enabled(channels):
                web = executor.submit(client.search, query.strip(), refresh=refresh, channel_config=channels,
                                      include_engines=_enabled(source), **options)
            archive = None
            if archive_enabled and (failed_engines is None or "Telegram归档" not in failed_engines):
                archive = executor.submit(search_telegram_resources, query.strip(), config,
                                          health_path=health_path, limit=20, deadline=deadline)
            if web is not None:
                try:
                    entries.extend(web.result())
                    successful = True
                    errors.extend(_web_errors(client.errors))
                except (SearchError, ValueError) as error:
                    messages = client.errors if isinstance(client.errors, list) and client.errors else [error]
                    errors.extend(_web_errors(messages))
                record_web_failures(errors, health_path)
            if archive is not None:
                try:
                    result = archive.result()
                except Exception:
                    result = {"success": False, "data": [], "errors": [
                        {"source": "Telegram归档", "message": "Telegram 归档查询暂不可用"}]}
                candidates = result.get("data") or []
                # Manual search also keeps archive hits ahead of anonymous web results.
                entries[0:0] = candidates
                successful = successful or bool(result.get("success")) or bool(candidates)
                if result.get("errors"):
                    errors.extend(result["errors"])
                    if failed_engines is not None:
                        failed_engines.add("Telegram归档")
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
    if include_web and (_enabled(sources.get("websearch") or {}) or _enabled(sources.get("channelsearch") or {})
                        or str((sources.get("telegram_web") or {}).get("enable", False)).lower() == "true"):
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
