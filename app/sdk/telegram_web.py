"""Authenticated client for the private NAS Telegram Web sidecar."""

import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time
from urllib.parse import urlsplit

import requests
from .common import normalize_share_url


STATES = {"disconnected", "starting", "waiting_qr", "waiting_password", "authorized", "error", "unavailable"}
METHODS = {"status": ("GET", "/status"), "start": ("POST", "/start"), "poll": ("POST", "/poll"),
           "password": ("POST", "/password"), "logout": ("DELETE", "/session"), "search": ("POST", "/search")}
MAX_RESPONSE = 2 * 1024 * 1024


def shared_token_path(health_path=None):
    path = Path(health_path or os.environ.get("CONFIG_PATH", "./config/quark_config.json"))
    return path.parent / ".telegram-web-token"


def shared_token(path):
    """Publish a complete 0600 token without replacing a concurrent creator's token."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        descriptor, temporary = tempfile.mkstemp(prefix=".telegram-web-token.", dir=path.parent)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(secrets.token_hex(32))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
    if path.is_symlink():
        raise ValueError("Invalid token file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size != 64:
            raise ValueError("Invalid token file")
        if os.name != "nt" and info.st_mode & 0o077:
            os.fchmod(descriptor, 0o600)
        token = os.read(descriptor, 65).decode("ascii")
    finally:
        os.close(descriptor)
    if re.fullmatch(r"[a-f0-9]{64}", token) is None:
        raise ValueError("Invalid token file")
    return token


def _failure(reason, code="WEB_UNAVAILABLE"):
    return {"success": False, "code": code, "data": {
        "available": False, "authorized": False, "state": "unavailable", "scope": "archived",
        "reason": reason, "retry_after": 0, "archived_count": 0}}


def _number(value, default=0):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else default


def _safe_result(result, action):
    if not isinstance(result, dict) or type(result.get("success")) is not bool:
        raise ValueError("Invalid sidecar response")
    data = result.get("data")
    if not isinstance(data, dict) or data.get("scope") != "archived" or data.get("state") not in STATES:
        raise ValueError("Invalid sidecar scope or state")
    clean = {"success": result["success"], "data": {
        "available": data.get("available") is True, "authorized": data.get("authorized") is True,
        "state": data["state"], "scope": "archived", "reason": str(data.get("reason") or "")[:500],
        "retry_after": _number(data.get("retry_after")), "archived_count": _number(data.get("archived_count")),
    }}
    code = result.get("code")
    if isinstance(code, str) and re.fullmatch(r"[A-Z_]{1,64}", code):
        clean["code"] = code
    attempt = result.get("attempt_id")
    if action in ("start", "poll", "password") and isinstance(attempt, str) and re.fullmatch(r"[a-f0-9]{32}", attempt):
        clean["attempt_id"] = attempt
    qr = result.get("qr")
    if action in ("start", "poll") and isinstance(qr, dict):
        identifier, image = qr.get("id"), qr.get("image")
        if (isinstance(identifier, str) and re.fullmatch(r"[a-f0-9]{32}", identifier)
                and isinstance(image, str) and len(image) <= MAX_RESPONSE
                and (image == "" or re.fullmatch(r"data:image/png;base64,[A-Za-z0-9+/]+={0,2}", image))):
            clean["qr"] = {"id": identifier, "image": image}
    if action == "search":
        clean["results"] = []
        rows = result.get("results")
        for row in rows[:40] if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("source_type") != "telegram_web":
                continue
            if not isinstance(row.get("title"), str) or not isinstance(row.get("url"), str):
                continue
            shareurl = normalize_share_url(row["url"])
            if not shareurl:
                continue
            clean["results"].append({
                "taskname": row["title"][:200], "shareurl": shareurl, "source": "Telegram Web 归档",
                "content": row.get("content", "")[:8000] if isinstance(row.get("content", ""), str) else "",
                "source_type": "telegram_web",
                "datetime": row.get("datetime") if type(row.get("datetime")) is int and 0 < row["datetime"] < 253402300800 else 0,
                "source_url": row.get("source_url", "")[:2048] if isinstance(row.get("source_url", ""), str) else "",
            })
        clean["partial"] = result.get("partial") is True
        bot = result.get("bot")
        if isinstance(bot, dict):
            clean["bot"] = {key: _number(bot.get(key)) for key in ("requests", "links", "pending", "failed", "channel_posts", "buttons", "channel_archived", "details_read", "details_failed", "button_count", "reply_messages", "reply_rich", "reply_links")}
            rejected = bot.get("button_rejections")
            if isinstance(rejected, dict):
                clean["bot"]["button_rejections"] = {key: _number(rejected.get(key)) for key in
                    ("not_url", "invalid_url", "other_target", "other_action", "invalid_payload") if _number(rejected.get(key))}
        progress = result.get("progress")
        if isinstance(progress, dict) and progress.get("phase") in ("archives", "search", "complete"):
            clean["progress"] = {"phase": progress["phase"], "current": _number(progress.get("current")),
                                 "total": _number(progress.get("total"))}
            clean["progress"].update({key: _number(progress.get(key)) for key in
                                     ("global_pages", "global_dialogs", "direct_links") if key in progress})
    return clean


class TelegramWeb:
    def __init__(self, health_path=None):
        self.token_path = shared_token_path(health_path)
        self.url = os.environ.get("TELEGRAM_WEB_URL", "http://telegram-web:8765").rstrip("/")

    def _request(self, action, payload=None, timeout=15):
        try:
            parsed = urlsplit(self.url)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
                    or parsed.path or parsed.query or parsed.fragment):
                return _failure("Telegram Web 服务地址配置不正确", "WEB_CONFIG_INVALID")
            token = shared_token(self.token_path)
        except (OSError, ValueError, UnicodeError):
            return _failure("Telegram Web 共享认证文件不可用，请检查配置目录权限", "WEB_TOKEN_ERROR")
        method, path = METHODS[action]
        try:
            with requests.Session() as session:
                # Internal service credentials must never pass through an ambient proxy or redirect.
                session.trust_env = False
                with session.request(method, self.url + path, json=payload if method != "GET" else None,
                                     headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
                                     timeout=(min(2, timeout), timeout), allow_redirects=False, stream=True) as response:
                    if response.status_code == 401:
                        return _failure("Telegram Web 服务认证失败，请检查共享配置目录", "WEB_AUTH_FAILED")
                    if response.status_code != 200:
                        return _failure("Telegram Web 服务暂不可用，请检查 telegram-web 容器", "WEB_HTTP_ERROR")
                    content = bytearray()
                    for chunk in response.iter_content(8192):
                        content.extend(chunk)
                        if len(content) > MAX_RESPONSE:
                            raise ValueError("Response too large")
                    return _safe_result(json.loads(content.decode("utf-8")), action)
        except requests.Timeout:
            return _failure("Telegram Web 请求超时，请稍后重试", "WEB_TIMEOUT")
        except requests.RequestException:
            return _failure("无法连接 Telegram Web 服务，请检查 telegram-web 容器", "WEB_UNAVAILABLE")
        except (ValueError, UnicodeError, TypeError):
            return _failure("Telegram Web 服务响应格式不正确", "WEB_INVALID_RESPONSE")

    def status(self):
        return self._request("status", timeout=5)

    def start(self):
        return self._request("start", {}, timeout=30)

    def poll(self, id):
        return self._request("poll", {"id": id}, timeout=8)

    def password(self, id, password):
        return self._request("password", {"id": id, "password": password})

    def logout(self):
        return self._request("logout", {})

    def search(self, query, limit=20, deadline=None, refresh=False):
        if (not isinstance(query, str) or not query.strip() or len(query) > 512
                or type(limit) is not int or not 1 <= limit <= 40):
            return {**_failure("搜索参数不正确", "WEB_INVALID_INPUT"), "results": []}
        budget = min(25, deadline - time.monotonic()) if deadline is not None else 25
        if budget <= 0:
            return {**_failure("本轮搜索时间已用完", "WEB_TIMEOUT"), "results": [], "partial": True}
        return self._request("search", {"query": query.strip(), "limit": limit, "timeout": budget,
                                      **({"refresh": True} if refresh is True else {})}, timeout=budget + 2)
