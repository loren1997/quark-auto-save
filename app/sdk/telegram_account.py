"""Optional, bounded Telegram user-session reads; credentials never enter app config."""

import asyncio
import base64
from contextlib import contextmanager
import json
import logging
import math
import os
from pathlib import Path
import re
import ssl as ssl_module
import tempfile
import threading
import time
import uuid
from urllib.parse import unquote, urlsplit

from .common import extract_share_urls

try:
    from telethon import TelegramClient, events, functions, types
    from telethon.network.connection import ConnectionTcpFull
    from telethon.sessions import StringSession
    from python_socks import ProxyType
    from python_socks.async_.asyncio.v2 import Proxy
except ImportError:
    TelegramClient = events = functions = types = StringSession = Proxy = None
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
    "API_CREDENTIALS_REQUIRED": "请先填写自己的 Telegram API ID 和 API Hash",
    "QR_EXPIRED": "Telegram 扫码登录已过期，请重新生成二维码",
    "CHANNEL_APPROVAL_REQUIRED": "该频道需要审核，已跳过自动加入", "CHANNEL_PAID": "该频道需要付费，已跳过自动加入",
    "ACCOUNT_CHANGED": "Telegram 登录账号已更换，本轮加入已取消",
}
PENDING_TTL = 600
QR_TTL = 180
_QR_WORKERS = {}
_QR_LOCK = threading.Lock()
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
    elif name in {"AuthTokenExpiredError", "AuthTokenInvalidError", "AuthTokenInvalid1Error", "AuthTokenAlreadyAcceptedError", "AuthRestartError"}:
        code = "QR_EXPIRED"
    elif name == "InviteRequestSentError":
        code = "CHANNEL_APPROVAL_REQUIRED"
    elif name in {"StarsSubscriptionRequiredError", "StarsPaymentRequiredError"}:
        code = "CHANNEL_PAID"
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
        if state not in {"disconnected", "waiting_code", "waiting_qr", "waiting_password", "authorized", "reauthorize", "suspended"}:
            state = "disconnected"
        code = data.get("last_error", "")
        if state.startswith("waiting_") and float(data.get("expires", 0)) <= time.time():
            if state == "waiting_qr":
                code = "QR_EXPIRED"
            state = "disconnected"
        if state == "waiting_qr" and time.time() - float(data.get("qr_heartbeat", 0)) > 45:
            state, code = "disconnected", "QR_EXPIRED"
        return {"available": available, "authorized": available and state == "authorized" and bool(data.get("session")),
                "state": state if available else "unavailable", "reason": REASONS.get(code, "") if available else REASONS["SDK_MISSING"],
                "retry_after": max(0, math.ceil(float(data.get("retry_until", 0)) - time.time())),
                "credentials_saved": bool(data.get("api_id") and re.fullmatch(r"[a-fA-F0-9]{32}", str(data.get("api_hash", ""))))}

    def status(self):
        try:
            return self._status(self._load())
        except (OSError, ValueError, _AccountError):
            return {**self._status({}), "reason": REASONS["STORAGE_ERROR"]}

    def _make_client(self, data, receive_updates=False):
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
            flood_sleep_threshold=0, receive_updates=receive_updates, base_logger=logger)

    @staticmethod
    def _app_credentials(data):
        return {key: data[key] for key in ("api_id", "api_hash", "proxy") if key in data}

    def _stop_qr(self):
        with _QR_LOCK:
            worker = _QR_WORKERS.get(str(self.path))
            if worker:
                worker["stop"].set()

    def authorization_generation(self):
        """Opaque local namespace for account-owned bookkeeping; never part of public status."""
        try:
            with self._locked():
                data = self._load()
                if not self._status(data)["authorized"]:
                    return ""
                if not re.fullmatch(r"[a-f0-9]{32}", str(data.get("authorization_generation", ""))):
                    data["authorization_generation"] = uuid.uuid4().hex
                    self._save(data)
                return data["authorization_generation"]
        except (OSError, ValueError, _AccountError):
            return ""

    def _operate(self, action, *, prepared=None, require_auth=True, deadline=None, preflight=None):
        client, data = None, {}
        try:
            if TelegramClient is None:
                raise _AccountError("SDK_MISSING")
            with self._locked():
                old = self._load()
                if float(old.get("retry_until", 0)) > time.time():
                    return {**self._status(old), "ok": False, "code": "FLOOD_WAIT", "error": REASONS["FLOOD_WAIT"]}
                if prepared is not None and old.get("qr_id"):
                    self._stop_qr()
                    old = self._app_credentials(old)
                    self._save(old)
                data = prepared if prepared is not None else old
                if data.get("state", "").startswith("waiting_") and float(data.get("expires", 0)) <= time.time():
                    data.clear()
                    self._save(data)
                    raise _AccountError("CODE_EXPIRED")
                if require_auth and not self._status(data)["authorized"]:
                    raise _AccountError("AUTH_REQUIRED")
                if not data.get("api_id") or not data.get("api_hash"):
                    raise _AccountError("AUTH_REQUIRED")
                if preflight is not None:
                    preflight(data)
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
        def check_step(data):
            qr_password = data.get("login_method") == "qr" and data.get("state") == "waiting_password"
            if data.get("state") not in {"waiting_code", "waiting_password"} or (not data.get("phone_code_hash") and not qr_password):
                raise _AccountError("AUTH_REQUIRED")
            if (password and data["state"] != "waiting_password") or (code and data["state"] != "waiting_code"):
                raise _AccountError("INVALID_INPUT")
        async def sign(client, data):
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
            data["authorization_generation"] = uuid.uuid4().hex
            for field in ("phone", "phone_code_hash", "expires", "login_method", "qr_id", "qr_owner", "qr_heartbeat", "qr_url", "qr_expires"):
                data.pop(field, None)
            return {"ok": True, "code": "", "error": ""}
        return self._operate(sign, require_auth=False, preflight=check_step)

    def logout(self):
        self._stop_qr()
        try:
            with self._locked():
                try:
                    data = self._load()
                    qr_pending = bool(data.get("qr_id"))
                    confirmed = not data.get("session") and not qr_pending
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
                        confirmed = bool(asyncio.run(bounded())) and not qr_pending
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

    def request_qr(self, api_id=None, api_hash=None, proxy=None):
        """Start one bounded QR flow; no SMS, and no waiting for the user to scan."""
        try:
            if TelegramClient is None:
                raise _AccountError("SDK_MISSING")
            with self._locked():
                old = self._load()
                if self._status(old)["authorized"]:
                    return {**self._status(old), "ok": True, "code": "", "error": ""}
                if float(old.get("retry_until", 0)) > time.time():
                    return {**self._status(old), "ok": False, "code": "FLOOD_WAIT", "error": REASONS["FLOOD_WAIT"]}
                data = self._app_credentials(old)
                if api_id is not None or api_hash is not None:
                    if (isinstance(api_id, bool) or not str(api_id).isdigit() or not 0 < int(api_id) <= 2147483647
                            or not isinstance(api_hash, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash)):
                        raise _AccountError("INVALID_INPUT")
                    data.update(api_id=int(api_id), api_hash=api_hash)
                if not self._status(data)["credentials_saved"]:
                    raise _AccountError("API_CREDENTIALS_REQUIRED")
                if proxy is not None:
                    _proxy(proxy)
                    data["proxy"] = proxy
                self._stop_qr()
                now = time.time()
                qr_id = uuid.uuid4().hex
                data.update(state="waiting_qr", login_method="qr", qr_id=qr_id, qr_owner=os.getpid(),
                            qr_heartbeat=now, expires=now + QR_TTL)
                self._save(data)
                worker = {"id": qr_id, "stop": threading.Event()}
                thread = threading.Thread(target=self._qr_worker, args=(dict(data), worker), daemon=True,
                                          name="telegram-qr-login")
                worker["thread"] = thread
                with _QR_LOCK:
                    _QR_WORKERS[str(self.path)] = worker
            thread.start()
            return {**self._status(data), "ok": True, "code": "", "error": "", "qr_id": qr_id,
                    "qr_url": "", "expires_at": 0}
        except Exception as error:
            result = _failure(error if isinstance(error, _AccountError) else _AccountError("STORAGE_ERROR"))
            return {**self.status(), **result}

    def poll_qr(self, qr_id):
        try:
            if not isinstance(qr_id, str) or not re.fullmatch(r"[a-f0-9]{32}", qr_id):
                raise _AccountError("QR_EXPIRED")
            data = self._load()
            if qr_id not in {data.get("qr_id"), data.get("qr_last_id")}:
                raise _AccountError("QR_EXPIRED")
            status = self._status(data)
            if status["state"] in {"authorized", "waiting_password"}:
                return {**status, "ok": True, "code": "", "error": ""}
            if status["state"] != "waiting_qr":
                code = data.get("last_error") if data.get("last_error") in REASONS else "QR_EXPIRED"
                if data.get("qr_id"):
                    self._stop_qr()
                    self._qr_publish(data, {"state": "disconnected", "last_error": code}, final=True)
                failure = _failure(_AccountError(code))
                if code == "FLOOD_WAIT":
                    failure["retry_after"] = status["retry_after"]
                return {**status, **failure}
            url = data.get("qr_url", "") if float(data.get("qr_expires", 0)) > time.time() else ""
            return {**status, "ok": True, "code": "", "error": "", "qr_id": qr_id,
                    "qr_url": url, "expires_at": data.get("qr_expires", 0) if url else 0}
        except Exception as error:
            result = _failure(error if isinstance(error, _AccountError) else _AccountError("STORAGE_ERROR"))
            return {**self.status(), **result}

    def _qr_publish(self, owner, values, final=False):
        """A superseded/cancelled worker cannot resurrect its session."""
        end = time.monotonic() + 0.3
        while True:
            try:
                with self._locked():
                    current = self._load()
                    if current.get("qr_id") != owner["qr_id"] or current.get("qr_owner") != owner["qr_owner"]:
                        return False
                    if final:
                        current = self._app_credentials(current)
                        current["qr_last_id"] = owner["qr_id"]
                    current.update(values)
                    self._save(current)
                    return True
            except _AccountError as error:
                if error.code != "BUSY" or time.monotonic() >= end:
                    return False
                time.sleep(0.02)
            except OSError:
                return False

    def _qr_worker(self, data, worker):
        async def run():
            client, handler = None, None
            completed = False
            failure = _failure(_AccountError("QR_EXPIRED"))
            deadline = time.monotonic() + max(0, data["expires"] - time.time())
            changed = asyncio.Event()
            async def call(awaitable):
                result = await asyncio.wait_for(awaitable, timeout=max(0.001, min(8, deadline - time.monotonic())))
                if worker["stop"].is_set():
                    raise _AccountError("QR_EXPIRED")
                return result
            try:
                client = self._make_client(data, receive_updates=True)
                client.session.save_entities = False
                async def handler(_update):
                    changed.set()
                client.add_event_handler(handler, events.Raw(types.UpdateLoginToken))
                await call(client.connect())
                token_expires, heartbeat = 0, 0
                while not worker["stop"].is_set() and time.monotonic() < deadline:
                    current = self._load()
                    if current.get("qr_id") != data["qr_id"] or current.get("qr_owner") != data["qr_owner"]:
                        break
                    now = time.time()
                    if changed.is_set() or now >= token_expires:
                        changed.clear()
                        try:
                            response = await call(client(functions.auth.ExportLoginTokenRequest(data["api_id"], data["api_hash"], [])))
                            if isinstance(response, types.auth.LoginTokenMigrateTo):
                                await call(client._switch_dc(response.dc_id))
                                response = await call(client(functions.auth.ImportLoginTokenRequest(response.token)))
                            if isinstance(response, types.auth.LoginTokenSuccess):
                                await call(client._on_login(response.authorization.user))
                                completed = self._qr_publish(data, {"state": "authorized", "session": client.session.save(),
                                    "authorization_generation": uuid.uuid4().hex}, final=True)
                                return
                            if not isinstance(response, types.auth.LoginToken) or not 0 < len(response.token) <= 256:
                                raise _AccountError("RPC_ERROR")
                            token_expires = response.expires.timestamp() if hasattr(response.expires, "timestamp") else float(response.expires)
                            if not now < token_expires <= now + 600:
                                raise _AccountError("QR_EXPIRED")
                            token_expires = min(token_expires, data["expires"])
                            url = "tg://login?token=" + base64.urlsafe_b64encode(response.token).decode("ascii").rstrip("=")
                            if not self._qr_publish(data, {"qr_url": url, "qr_expires": token_expires,
                                    "session": client.session.save(), "qr_heartbeat": time.time()}):
                                break
                            heartbeat = time.time()
                        except Exception as error:
                            if type(error).__name__ != "SessionPasswordNeededError":
                                raise
                            completed = self._qr_publish(data, {"state": "waiting_password", "login_method": "qr",
                                "session": client.session.save(), "expires": time.time() + PENDING_TTL}, final=True)
                            return
                    if time.time() - heartbeat >= 5:
                        if not self._qr_publish(data, {"qr_heartbeat": time.time()}):
                            break
                        heartbeat = time.time()
                    try:
                        await asyncio.wait_for(changed.wait(), timeout=min(1, max(0.001, token_expires - time.time())))
                    except asyncio.TimeoutError:
                        pass
            except Exception as error:
                failure = _failure(error)
            finally:
                if client is not None:
                    if handler:
                        client.remove_event_handler(handler)
                    if not completed:
                        # Invalidate only this abandoned QR session, including a late scan after cancellation.
                        try:
                            await asyncio.wait_for(client.log_out(), timeout=2)
                        except Exception:
                            pass
                    try:
                        await asyncio.wait_for(client.disconnect(), timeout=1)
                    except Exception:
                        pass
                if not completed:
                    values = {"state": "disconnected", "last_error": failure["code"]}
                    if failure["retry_after"]:
                        values["retry_until"] = time.time() + failure["retry_after"]
                    self._qr_publish(data, values, final=True)
        try:
            asyncio.run(run())
        finally:
            with _QR_LOCK:
                if _QR_WORKERS.get(str(self.path)) is worker:
                    _QR_WORKERS.pop(str(self.path), None)

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

    def discover_channels(self, seeds=(), cursor=0, deadline=None):
        if (isinstance(cursor, bool) or not isinstance(cursor, int) or not 0 <= cursor <= 1000000000
                or not isinstance(seeds, (list, tuple)) or len(seeds) > 40
                or any(not isinstance(name, str) or not USERNAME.fullmatch(name) for name in seeds)):
            return {**_failure(_AccountError("INVALID_INPUT")), "channels": [], "errors": [], "next_cursor": cursor}
        output = {"channels": [], "errors": [], "next_cursor": (cursor + 1) % 1000000001}
        async def discover(client, data):
            if seeds and cursor % 6 == 5:
                entity = await client.get_entity(seeds[(cursor // 6) % len(seeds)])
                self._channel(entity)
                response = await client(functions.channels.GetChannelRecommendationsRequest(channel=entity))
            else:
                query = ("quark", "kuake", "夸克网盘", "影视资源", "动漫资源")[cursor % 6 % 5]
                response = await client(functions.contacts.SearchRequest(q=query, limit=30))
            seen = set()
            for entity in (getattr(response, "chats", []) or [])[:60]:
                try:
                    channel = self._channel(entity)
                    if channel["name"].casefold() not in seen:
                        output["channels"].append(channel)
                        seen.add(channel["name"].casefold())
                except _AccountError:
                    continue
            return {"ok": True, "code": "", "error": ""}
        end = min(deadline, time.monotonic() + 20) if deadline is not None else time.monotonic() + 20
        result = self._operate(discover, deadline=end)
        if not result.get("ok"):
            output["errors"].append({"source": "Telegram 频道发现", "code": result["code"],
                                    "error": result["error"], "retry_after": result["retry_after"]})
        return {**result, **output}

    def join_channel(self, name, deadline=None, expected_generation=None):
        if (not isinstance(name, str) or not USERNAME.fullmatch(name)
                or (expected_generation is not None and (not isinstance(expected_generation, str)
                    or not re.fullmatch(r"[a-f0-9]{32}", expected_generation)))):
            return {**_failure(_AccountError("INVALID_INPUT")), "joined": False, "already_member": False}
        async def join(client, data):
            if expected_generation is not None and data.get("authorization_generation") != expected_generation:
                raise _AccountError("ACCOUNT_CHANGED")
            entity = await client.get_entity(name)
            self._channel(entity)
            if not entity.left:
                return {"ok": True, "code": "", "error": "", "joined": False, "already_member": True}
            if entity.join_request:
                raise _AccountError("CHANNEL_APPROVAL_REQUIRED")
            if getattr(entity, "subscription_until_date", None) or getattr(entity, "subscription_pricing", None):
                raise _AccountError("CHANNEL_PAID")
            try:
                await client(functions.channels.JoinChannelRequest(channel=entity))
            except Exception as error:
                if type(error).__name__ != "UserAlreadyParticipantError":
                    raise
                return {"ok": True, "code": "", "error": "", "joined": False, "already_member": True}
            return {"ok": True, "code": "", "error": "", "joined": True, "already_member": False}
        end = min(deadline, time.monotonic() + 15) if deadline is not None else time.monotonic() + 15
        return {"joined": False, "already_member": False, **self._operate(join, deadline=end)}

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
