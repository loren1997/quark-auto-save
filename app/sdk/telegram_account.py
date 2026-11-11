"""Optional, bounded Telegram user-session reads; credentials never enter app config."""

import asyncio
from contextlib import contextmanager
import json
import logging
import math
import os
from pathlib import Path
import re
import ssl as ssl_module
import tempfile
import time
from urllib.parse import unquote, urlsplit

from .common import extract_share_urls

try:
    from telethon import TelegramClient, functions, types
    from telethon.network.connection import ConnectionTcpFull
    from telethon.sessions import StringSession
    from python_socks import ProxyType
    from python_socks.async_.asyncio.v2 import Proxy
except ImportError:
    TelegramClient = functions = types = StringSession = Proxy = None
    ConnectionTcpFull = object


REASONS = {
    "DISABLED": "未设置 Telegram 私有会话存储", "SDK_MISSING": "Telegram 依赖未安装，账号渠道已禁用",
    "AUTH_REQUIRED": "Telegram 尚未登录", "INVALID_INPUT": "Telegram 参数格式不正确",
    "API_CONFIG_INVALID": "Telegram API ID 或 API Hash 不正确", "CODE_INVALID": "Telegram 验证码不正确",
    "CODE_EXPIRED": "Telegram 登录验证码已过期，请重新获取", "PASSWORD_INVALID": "Telegram 两步验证密码不正确",
    "ACCOUNT_REVOKED": "Telegram 会话已失效，请重新登录", "ACCOUNT_BANNED": "Telegram 账号已停用或受限",
    "CHANNEL_UNAVAILABLE": "Telegram 频道不存在、私有或无权访问", "CHANNEL_RESTRICTED": "Telegram 频道受限，已跳过",
    "FOLDER_INVALID": "Telegram 文件夹链接无效或已过期", "FLOOD_WAIT": "Telegram 请求频率受限，等待后自动重试",
    "TIMEOUT": "Telegram 请求超时", "NETWORK_ERROR": "Telegram 网络连接失败",
    "BUSY": "Telegram 账号正在使用，稍后自动重试", "STORAGE_ERROR": "Telegram 私有会话存储不可用",
    "RPC_ERROR": "Telegram 请求失败", "LOGOUT_UNCONFIRMED": "已清除本地会话；远端会话撤销未确认，可在 Telegram 的设备设置中终止此会话",
}
PENDING_TTL = 600
USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")


class _AccountError(Exception):
    def __init__(self, code):
        self.code = code


def _failure(error):
    name = type(error).__name__
    if isinstance(error, _AccountError):
        code = error.code
    elif name in {"FloodWaitError", "FloodPremiumWaitError"}:
        code = "FLOOD_WAIT"
    elif name in {"PhoneCodeInvalidError", "PhoneCodeEmptyError"}:
        code = "CODE_INVALID"
    elif name in {"PhoneCodeExpiredError", "PhoneCodeHashEmptyError"}:
        code = "CODE_EXPIRED"
    elif name == "PasswordHashInvalidError":
        code = "PASSWORD_INVALID"
    elif name in {"ApiIdInvalidError", "ApiIdPublishedFloodError"}:
        code = "API_CONFIG_INVALID"
    elif name in {"AuthKeyUnregisteredError", "AuthKeyDuplicatedError", "SessionRevokedError", "SessionExpiredError", "UnauthorizedError"}:
        code = "ACCOUNT_REVOKED"
    elif name in {"UserDeactivatedBanError", "UserDeactivatedError", "PhoneNumberBannedError", "FrozenMethodInvalidError"}:
        code = "ACCOUNT_BANNED"
    elif name in {"ChannelPrivateError", "ChannelInvalidError", "UsernameInvalidError", "UsernameNotOccupiedError", "ChatAdminRequiredError", "ForbiddenError", "ChannelPublicGroupNaError", "UserBannedInChannelError"}:
        code = "CHANNEL_UNAVAILABLE"
    elif name in {"InviteSlugExpiredError", "InviteSlugEmptyError"}:
        code = "FOLDER_INVALID"
    elif isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        code = "TIMEOUT"
    elif isinstance(error, (OSError, ConnectionError)) or name.startswith("Proxy"):
        code = "NETWORK_ERROR"
    else:
        code = "RPC_ERROR"
    seconds = max(1, int(getattr(error, "seconds", 60))) if code == "FLOOD_WAIT" else 0
    return {"ok": False, "code": code, "error": REASONS[code], "reason": REASONS[code], "retry_after": seconds}


def _proxy(value):
    if not value:
        return None
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\x00-\x20\x7f]", value):
        raise _AccountError("INVALID_INPUT")
    try:
        p = urlsplit(value)
        if (p.scheme not in {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}
                or not p.hostname or p.path not in ("", "/") or p.query or p.fragment):
            raise ValueError()
        port = p.port if p.port is not None else (443 if p.scheme == "https" else 80 if p.scheme == "http" else 1080)
        if not 1 <= port <= 65535:
            raise ValueError()
        return {"proxy_type": p.scheme.rstrip("ah"), "addr": p.hostname, "port": port,
                "rdns": True, "username": unquote(p.username) if p.username else None,
                "password": unquote(p.password) if p.password else None}
    except ValueError:
        raise _AccountError("INVALID_INPUT") from None


class _TLSProxyConnection(ConnectionTcpFull):
    async def _connect(self, timeout=None, ssl=None):
        # Telethon's normal proxy path supports plain HTTP CONNECT; preserve TLS for https:// proxies.
        proxy = Proxy(ProxyType.HTTP, self._proxy["addr"], self._proxy["port"],
                      username=self._proxy.get("username"), password=self._proxy.get("password"),
                      rdns=True, proxy_ssl=ssl_module.create_default_context())
        stream = await proxy.connect(self._ip, self._port, timeout=timeout)
        self._reader, self._writer = stream.reader, stream.writer
        self._codec = self.packet_codec(self)
        self._init_conn()
        await self._writer.drain()


class TelegramAccount:
    def __init__(self, health_path=None, *, client_factory=None):
        self.path = Path(health_path).with_name("." + Path(health_path).name + ".telegram.json") if health_path else None
        self.client_factory = client_factory

    def _load(self):
        if not self.path or not self.path.exists():
            return {}
        if self.path.is_symlink() or self.path.stat().st_size > 65536:
            raise _AccountError("STORAGE_ERROR")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (ValueError, OSError):
            raise _AccountError("STORAGE_ERROR") from None

    def _save(self, data):
        fd, temporary = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @contextmanager
    def _locked(self):
        if not self.path:
            raise _AccountError("DISABLED")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(".lock")
        if lock_path.is_symlink():
            raise _AccountError("STORAGE_ERROR")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise _AccountError("BUSY") from None
            yield
        finally:
            os.close(fd)

    def _status(self, data):
        available = TelegramClient is not None
        state = data.get("state", "disconnected")
        if state not in {"disconnected", "waiting_code", "waiting_password", "authorized", "reauthorize", "suspended"}:
            state = "disconnected"
        if state.startswith("waiting_") and float(data.get("expires", 0)) <= time.time():
            state = "disconnected"
        code = data.get("last_error", "")
        return {"available": available, "authorized": available and state == "authorized" and bool(data.get("session")),
                "state": state if available else "unavailable", "reason": REASONS.get(code, "") if available else REASONS["SDK_MISSING"],
                "retry_after": max(0, math.ceil(float(data.get("retry_until", 0)) - time.time()))}

    def status(self):
        try:
            return self._status(self._load())
        except (OSError, ValueError, _AccountError):
            return {**self._status({}), "reason": REASONS["STORAGE_ERROR"]}

    def _make_client(self, data):
        proxy = _proxy(data.get("proxy"))
        # SDK logs may include proxy credentials. Surface fixed errors through operation results instead.
        logger = logging.getLogger("telegram_account.transport")
        logger.propagate = False
        if not any(isinstance(handler, logging.NullHandler) for handler in logger.handlers):
            logger.addHandler(logging.NullHandler())
        try:
            session = StringSession(data.get("session", ""))
        except (ValueError, TypeError):
            raise _AccountError("ACCOUNT_REVOKED") from None
        return (self.client_factory or TelegramClient)(session,
            data["api_id"], data["api_hash"], proxy=proxy,
            connection=_TLSProxyConnection if proxy and proxy["proxy_type"] == "https" else ConnectionTcpFull,
            timeout=8, connection_retries=0, request_retries=0, auto_reconnect=False,
            flood_sleep_threshold=0, receive_updates=False, base_logger=logger)

    def _operate(self, action, *, prepared=None, require_auth=True, deadline=None):
        client, data = None, {}
        try:
            if TelegramClient is None:
                raise _AccountError("SDK_MISSING")
            with self._locked():
                old = self._load()
                if float(old.get("retry_until", 0)) > time.time():
                    return {**self._status(old), "ok": False, "code": "FLOOD_WAIT", "error": REASONS["FLOOD_WAIT"]}
                data = prepared if prepared is not None else old
                if data.get("state", "").startswith("waiting_") and float(data.get("expires", 0)) <= time.time():
                    data.clear()
                    self._save(data)
                    raise _AccountError("CODE_EXPIRED")
                if require_auth and not self._status(data)["authorized"]:
                    raise _AccountError("AUTH_REQUIRED")
                if not data.get("api_id") or not data.get("api_hash"):
                    raise _AccountError("AUTH_REQUIRED")
                budget = min(30, deadline - time.monotonic()) if deadline is not None else 20
                if budget <= 0:
                    raise _AccountError("TIMEOUT")
                end = time.monotonic() + budget
                async def run():
                    nonlocal client
                    client = self._make_client(data)
                    client.session.save_entities = False
                    try:
                        await client.connect()
                        if require_auth and not await client.is_user_authorized():
                            raise _AccountError("ACCOUNT_REVOKED")
                        return await action(client, data)
                    finally:
                        data["session"] = client.session.save()
                        try:
                            await asyncio.wait_for(client.disconnect(), timeout=max(0.001, min(1, end - time.monotonic())))
                        except Exception:
                            pass  # Main operation still reports its sanitized result, and auto-reconnect is disabled.
                try:
                    async def bounded():
                        return await asyncio.wait_for(run(), timeout=budget)
                    result = asyncio.run(bounded())
                    data.pop("last_error", None)
                    data.pop("retry_until", None)
                except Exception as error:
                    result = _failure(error)
                    code = result["code"]
                    if code in {"ACCOUNT_REVOKED", "ACCOUNT_BANNED", "CODE_EXPIRED"}:
                        data.clear()
                        data["state"] = "suspended" if code == "ACCOUNT_BANNED" else "reauthorize"
                    if prepared is not None and not data.get("phone_code_hash") and code not in {"ACCOUNT_REVOKED", "ACCOUNT_BANNED", "CODE_EXPIRED"}:
                        data = old
                    data["last_error"] = code
                    if result["retry_after"]:
                        data["retry_until"] = time.time() + result["retry_after"]
                self._save(data)
                return {**self._status(data), **result}
        except Exception as error:
            result = _failure(error if isinstance(error, _AccountError) else _AccountError("STORAGE_ERROR"))
            return {**self.status(), **result}

    def request_code(self, api_id, api_hash, phone, proxy=None):
        try:
            if (isinstance(api_id, bool) or not str(api_id).isdigit() or not 0 < int(api_id) <= 2147483647
                    or not isinstance(api_hash, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash)
                    or not isinstance(phone, str) or not re.fullmatch(r"\+[0-9]{7,15}", phone)):
                raise _AccountError("INVALID_INPUT")
            _proxy(proxy)
        except _AccountError as error:
            return {**self.status(), **_failure(error)}
        data = {"api_id": int(api_id), "api_hash": api_hash, "phone": phone, "proxy": proxy or "",
                "state": "waiting_code", "expires": time.time() + PENDING_TTL}
        async def send(client, pending):
            sent = await client.send_code_request(phone)
            pending["phone_code_hash"] = sent.phone_code_hash
            return {"ok": True, "code": "", "error": ""}
        return self._operate(send, prepared=data, require_auth=False)

    def sign_in(self, code=None, password=None):
        if bool(code) == bool(password) or (code and (not isinstance(code, str) or not re.fullmatch(r"[0-9]{4,8}", code))) or (password and (not isinstance(password, str) or len(password) > 1024)):
            return {**self.status(), **_failure(_AccountError("INVALID_INPUT"))}
        async def sign(client, data):
            if data.get("state") not in {"waiting_code", "waiting_password"} or not data.get("phone_code_hash"):
                raise _AccountError("AUTH_REQUIRED")
            if password and data["state"] != "waiting_password":
                raise _AccountError("INVALID_INPUT")
            try:
                if password:
                    await client.sign_in(password=password)
                else:
                    await client.sign_in(phone=data["phone"], code=code, phone_code_hash=data["phone_code_hash"])
            except Exception as error:
                if type(error).__name__ != "SessionPasswordNeededError":
                    raise
                data["state"] = "waiting_password"
                return {"ok": True, "code": "", "error": ""}
            data["state"] = "authorized"
            for field in ("phone", "phone_code_hash", "expires"):
                data.pop(field, None)
            return {"ok": True, "code": "", "error": ""}
        return self._operate(sign, require_auth=False)

    def logout(self):
        try:
            with self._locked():
                try:
                    data = self._load()
                    confirmed = not data.get("session")
                except _AccountError:
                    data, confirmed = {}, False
                if data.get("session") and TelegramClient is not None:
                    end = time.monotonic() + 8
                    async def revoke():
                        client = self._make_client(data)
                        try:
                            await client.connect()
                            return await client.log_out()
                        finally:
                            await asyncio.wait_for(client.disconnect(), timeout=max(0.001, min(1, end - time.monotonic())))
                    try:
                        async def bounded():
                            return await asyncio.wait_for(revoke(), timeout=8)
                        confirmed = bool(asyncio.run(bounded()))
                    except Exception:
                        confirmed = False
                self._save({})
                result = {**self._status({}), "ok": True, "code": "", "error": ""}
                if not confirmed:
                    result.update(code="LOGOUT_UNCONFIRMED", reason=REASONS["LOGOUT_UNCONFIRMED"])
                return result
        except Exception as error:
            return {**self.status(), **_failure(error if isinstance(error, _AccountError) else _AccountError("STORAGE_ERROR"))}

    disconnect = logout

    @staticmethod
    def _channel(chat):
        if not isinstance(chat, types.Channel) or not chat.broadcast:
            raise _AccountError("CHANNEL_UNAVAILABLE")
        if chat.restricted or chat.scam or chat.fake:
            raise _AccountError("CHANNEL_RESTRICTED")
        name = chat.username or next((item.username for item in (chat.usernames or []) if item.active), "")
        if not USERNAME.fullmatch(name or "") or name.lower().endswith("bot"):
            raise _AccountError("CHANNEL_UNAVAILABLE")
        return {"name": name, "title": str(chat.title or "")[:200]}

    def read_folder(self, url, deadline=None):
        match = re.fullmatch(r"https://t\.me/addlist/([A-Za-z0-9_-]{8,128})/?", url or "") if isinstance(url, str) else None
        if not match:
            return {**_failure(_AccountError("FOLDER_INVALID")), "channels": [], "skipped": []}
        async def read(client, data):
            invite = await client(functions.chatlists.CheckChatlistInviteRequest(match.group(1)))
            peers = list(getattr(invite, "peers", []) or []) + list(getattr(invite, "already_peers", []) or []) + list(getattr(invite, "missing_peers", []) or [])
            wanted = {peer.channel_id for peer in peers if isinstance(peer, types.PeerChannel)}
            channels, skipped, seen = [], [], set()
            for chat in (getattr(invite, "chats", []) or [])[:200]:
                if getattr(chat, "id", None) not in wanted:
                    continue
                try:
                    channel = self._channel(chat)
                    if channel["name"].lower() not in seen:
                        seen.add(channel["name"].lower())
                        channels.append(channel)
                except _AccountError as error:
                    skipped.append({"code": error.code, "reason": REASONS[error.code]})
            return {"ok": True, "code": "", "error": "", "channels": channels, "skipped": skipped}
        return {"channels": [], "skipped": [], **self._operate(read, deadline=deadline)}

    def search(self, query, channels, limit=20, deadline=None):
        if (not isinstance(query, str) or not 0 < len(query.strip()) <= 512 or not isinstance(channels, (list, tuple))
                or len(channels) > 40 or any(not isinstance(name, str) or not USERNAME.fullmatch(name) for name in channels)
                or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 40):
            return {**_failure(_AccountError("INVALID_INPUT")), "candidates": [], "errors": [], "unavailable_channels": []}
        output = {"candidates": [], "errors": [], "unavailable_channels": []}
        async def search_channels(client, data):
            seen = set()
            for name in dict.fromkeys(channels):
                async def read():
                    entity = await client.get_entity(name)
                    channel = self._channel(entity)
                    async for message in client.iter_messages(entity, search=query.strip(), limit=min(40, limit * 2), wait_time=0):
                        text = str(getattr(message, "message", "") or "")[:8000]
                        title = next((line.strip() for line in text.splitlines() if line.strip()), "")[:200]
                        links = text + "\n" + "\n".join(str(getattr(item, "url", "") or "") for item in (getattr(message, "entities", []) or []))
                        if not title:
                            continue
                        for share in extract_share_urls(links):
                            if share in seen:
                                continue
                            seen.add(share)
                            page = f"https://t.me/{channel['name']}/{message.id}"
                            output["candidates"].append({"taskname": title, "shareurl": share, "content": text[:1000],
                                "source": "Telegram 账号", "channel": f"Telegram @{channel['name']}",
                                "source_type": "telegram", "source_url": page, "page_url": page, "datetime": ""})
                            if len(output["candidates"]) >= limit:
                                return
                try:
                    await asyncio.wait_for(read(), timeout=8)
                except Exception as error:
                    failure = _failure(error)
                    output["errors"].append({"source": f"Telegram @{name}", "channel": name, **failure})
                    if failure["code"] in {"CHANNEL_UNAVAILABLE", "CHANNEL_RESTRICTED"}:
                        output["unavailable_channels"].append({"name": name, "reason": failure["reason"],
                            "retry_after": 86400 if failure["code"] == "CHANNEL_RESTRICTED" else 3600})
                    if failure["code"] in {"FLOOD_WAIT", "ACCOUNT_REVOKED", "ACCOUNT_BANNED"}:
                        raise
                if len(output["candidates"]) >= limit:
                    break
            return {"ok": True, "code": "", "error": ""}
        result = self._operate(search_channels, deadline=deadline)
        return {**result, **output}
