"""One isolated, headless official Telegram Web session; no MTProto credentials.

All Playwright objects belong to the worker thread. The HTTP layer can invoke only
the six actions below, never arbitrary browser URLs, selectors or JavaScript.
"""

import base64
from concurrent.futures import Future, TimeoutError as FutureTimeout
import importlib.util
import json
import os
import queue
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit


WEB_URL = "https://web.telegram.org/k/"
LOGIN_SECONDS = 300
REASONS = {
    "DISCONNECTED": "Telegram Web 尚未连接",
    "SESSION_SAVED": "已有本机 Telegram Web 登录；开始连接或查询时恢复",
    "STARTING": "正在载入 Telegram 官方登录页面",
    "WAITING_QR": "请用 Telegram 手机客户端扫描二维码",
    "WAITING_PASSWORD": "请填写 Telegram 两步验证密码",
    "AUTHORIZED": "已连接；仅搜索已归档聊天",
    "UNAVAILABLE": "Telegram Web 浏览器组件不可用",
    "BUSY": "Telegram Web 正在处理其他请求，请稍后重试",
    "TIMEOUT": "Telegram Web 请求超时，请稍后重试",
    "LOGIN_EXPIRED": "本次登录已过期，请重新生成二维码",
    "STALE_ATTEMPT": "登录页面已更新，请使用当前二维码",
    "INVALID_INPUT": "请求参数不正确",
    "PASSWORD_FAILED": "两步验证未完成，请检查密码后重试",
    "PASSWORD_PENDING": "Telegram 正在验证两步验证密码，请稍候",
    "LOGIN_FORM_UNAVAILABLE": "Telegram 登录密码框暂不可用，请稍后重试",
    "AUTH_REQUIRED": "Telegram Web 登录已失效，请重新扫码",
    "BRIDGE_UNAVAILABLE": "Telegram 官方网页接口已变化，已停止读取",
    "PAGINATION_FAILED": "归档列表分页未完成，将保留进度继续查询",
    "SEARCH_FAILED": "Telegram 归档搜索暂时失败，请稍后重试",
    "BUDGET_EXHAUSTED": "本轮查询时间已到，将从保存的进度继续查询",
    "FLOOD_WAIT": "Telegram 暂时限制查询，请等待后自动重试",
    "LOGOUT_UNCONFIRMED": "本机连接已清除；未能确认远端会话撤销，可在 Telegram 设备列表中撤销",
    "ERROR": "Telegram Web 暂时不可用，请稍后重试",
}
QR_SELECTOR = '[class*="pageSignQR"] canvas, .page-signQR canvas, .page-signQR .qr-container canvas'
PASSWORD_SELECTOR = (':is([class*="pagePassword"], .page-password) '
                     'input:is([type="password"], [name="notsearch_password"])'
                     ':not(.stealthy):not([aria-hidden="true"])')
AUTH_STATE = """() => {
  if (location.origin !== 'https://web.telegram.org' || location.pathname !== '/k/') return 'unavailable';
  if (window.rootScope?.myId && window.rootScope?.managers?.apiManager?.invokeApi) return 'authorized';
  const visible = e => !!(e && e.getClientRects().length);
  if ([...document.querySelectorAll(__PASSWORD_SELECTOR__)].some(visible)) return 'waiting_password';
  if ([...document.querySelectorAll('[class*="pageSignQR"] canvas, .page-signQR canvas')].some(visible)) return 'waiting_qr';
  return 'starting';
}""".replace("__PASSWORD_SELECTOR__", json.dumps(PASSWORD_SELECTOR))


class TelegramWebWorker:
    ACTIONS = frozenset({"status", "start", "poll", "password", "logout", "search"})

    def __init__(self, profile_dir, *, clock=time.monotonic, browser_factory=None):
        self._base = Path(profile_dir).absolute()
        self._profile = self._base / "browser"
        self._authorized_marker = self._base / "authorized"
        self._clock = clock
        self._factory = browser_factory
        self._queue = queue.Queue(maxsize=8)
        self._thread = None
        self._thread_lock = threading.Lock()
        self._generation = 0
        self._context = self._page = self._playwright = None
        self._attempt = None
        self._expires = 0
        self._cooldown = 0
        self._state = "disconnected"
        self._reason = "DISCONNECTED"
        self._archived_count = 0
        self._available = browser_factory is not None or importlib.util.find_spec("playwright") is not None
        self._bridge = Path(__file__).with_name("archive_bridge.js").read_text(encoding="utf-8")

    def submit(self, action, payload=None):
        if action not in self.ACTIONS or not isinstance(payload or {}, dict):
            return self._result(False, "INVALID_INPUT")
        payload = dict(payload or {})
        try:
            budget = max(1.0, min(45.0, float(payload.get("timeout", 30))))
        except (ValueError, TypeError, OverflowError):
            return self._result(False, "INVALID_INPUT")
        if budget != budget:
            return self._result(False, "INVALID_INPUT")
        payload["timeout"] = budget
        future = Future()
        with self._thread_lock:
            if action == "logout":
                self._generation += 1
                self._attempt = None
                # Logout is a barrier: discard work already waiting behind an
                # active browser request, including passwords from old pages.
                while True:
                    try:
                        _, old_payload, old_future, _, _ = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    old_payload.clear()
                    if old_future.set_running_or_notify_cancel():
                        old_future.set_result(self._result(False, "STALE_ATTEMPT"))
                    self._queue.task_done()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="telegram-web", daemon=True)
                self._thread.start()
            try:
                self._queue.put_nowait((action, payload, future, self._clock() + budget + 5, self._generation))
            except queue.Full:
                return self._result(False, "BUSY")
        try:
            return future.result(timeout=budget + 6)
        except FutureTimeout:
            future.cancel()
            return self._result(False, "TIMEOUT")

    def _run(self):
        while True:
            try:
                action, payload, future, expires, generation = self._queue.get(timeout=1)
            except queue.Empty:
                if self._attempt and self._expires and self._clock() >= self._expires:
                    try:
                        self._refresh_state()
                    except Exception:
                        self._close_browser()
                        self._attempt = None
                        self._state, self._reason = "disconnected", "LOGIN_EXPIRED"
                continue
            try:
                if action is None:
                    self._close_browser()
                    if future.set_running_or_notify_cancel():
                        future.set_result(None)
                    return
                if future.cancelled() or self._clock() >= expires:
                    if future.set_running_or_notify_cancel():
                        future.set_result(self._result(False, "TIMEOUT"))
                    continue
                if not future.set_running_or_notify_cancel():
                    continue
                if generation != self._generation:
                    future.set_result(self._result(False, "STALE_ATTEMPT"))
                    continue
                result = getattr(self, "_" + action)(payload)
                if generation != self._generation:
                    self._attempt = None
                    result = self._result(False, "STALE_ATTEMPT")
                future.set_result(result)
            except Exception:
                # Browser exceptions can contain page content, profile paths or login data.
                if not future.done():
                    future.set_result(self._result(False, "ERROR"))
            finally:
                payload.clear()
                self._queue.task_done()

    def close(self):
        with self._thread_lock:
            if not self._thread or not self._thread.is_alive():
                return
            future = Future()
            try:
                self._queue.put((None, {}, future, 0, self._generation), timeout=2)
            except queue.Full:
                return
        try:
            future.result(timeout=50)
        except FutureTimeout:
            pass

    def _result(self, success=True, code=None, **extra):
        reason = code or self._reason
        data = {"available": self._available, "authorized": self._state == "authorized",
                "state": self._state, "reason": REASONS.get(reason, REASONS["ERROR"]),
                "retry_after": max(0, int(self._cooldown - self._clock())),
                "scope": "archived", "archived_count": self._archived_count}
        result = {"success": success, "data": data}
        if code:
            result["code"] = code
        result.update(extra)
        return result

    def _launch(self, navigate_timeout=18):
        if self._context:
            return
        if not self._available:
            self._state = "unavailable"
            self._reason = "UNAVAILABLE"
            return
        self._base.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._base.is_symlink() or self._profile.is_symlink():
            raise ValueError("unsafe profile")
        self._base.chmod(0o700)
        self._profile.mkdir(mode=0o700, exist_ok=True)
        self._profile.chmod(0o700)
        if self._factory:
            self._context = self._factory(self._profile)
        else:
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
            proxy = None
            proxy_url = os.environ.get("TELEGRAM_WEB_PROXY", "").strip()
            if proxy_url:
                parsed = urlsplit(proxy_url)
                if (parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname
                        or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                        or any(ord(char) < 32 for char in proxy_url)):
                    raise ValueError("invalid proxy")
                host = "[" + parsed.hostname + "]" if ":" in parsed.hostname else parsed.hostname
                proxy = {"server": f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port else "")}
                if parsed.username:
                    proxy["username"] = unquote(parsed.username)
                    proxy["password"] = unquote(parsed.password or "")
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self._profile), headless=True, locale="en-US", viewport={"width": 900, "height": 800},
                permissions=[], accept_downloads=False, proxy=proxy, timeout=10000,
                args=["--mute-audio", "--disable-dev-shm-usage", "--disable-notifications"])
        self._context.set_default_timeout(5000)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        try:
            self._page.goto(WEB_URL, wait_until="domcontentloaded", timeout=max(1000, int(navigate_timeout * 1000)))
        except Exception:
            self._close_browser()
            raise

    def _close_browser(self):
        try:
            if self._context:
                try:
                    self._context.close()
                except Exception:
                    pass
        finally:
            self._context = self._page = None
            if self._playwright:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None

    def _refresh_state(self):
        if not self._page:
            return
        state = self._page.evaluate(AUTH_STATE)
        if state not in {"authorized", "waiting_qr", "waiting_password", "starting"}:
            self._state, self._reason = "error", "BRIDGE_UNAVAILABLE"
            return
        self._state = state
        self._reason = {"authorized": "AUTHORIZED", "waiting_qr": "WAITING_QR",
                        "waiting_password": "WAITING_PASSWORD", "starting": "STARTING"}[state]
        if state == "authorized":
            self._expires = 0
            self._authorized_marker.touch(mode=0o600, exist_ok=True)
        elif self._attempt and self._clock() >= self._expires:
            self._close_browser()
            self._attempt = None
            self._state, self._reason = "disconnected", "LOGIN_EXPIRED"

    def _status(self, payload):
        # Ordinary status never starts a browser or initiates a login challenge.
        if self._page:
            self._refresh_state()
        elif self._authorized_marker.exists():
            self._state, self._reason = "disconnected", "SESSION_SAVED"
        return self._result()

    def _start(self, payload):
        self._launch()
        if not self._page:
            return self._result(False, "UNAVAILABLE")
        self._refresh_state()
        if self._state == "authorized":
            return self._result()
        if not self._page:
            self._launch()
        self._attempt = uuid.uuid4().hex
        self._expires = self._clock() + LOGIN_SECONDS
        # Default WebK starts with a QR card. Some versions start with the phone
        # card; only its explicit QR button is permitted here, never send-code.
        if self._state == "starting":
            button = self._page.get_by_role("button", name=re.compile(r"QR", re.I))
            if button.count() == 1 and button.is_visible():
                button.click(timeout=3000)
        return self._login_result()

    def _login_result(self):
        self._refresh_state()
        result = self._result(attempt_id=self._attempt)
        if self._attempt and self._state == "waiting_qr":
            canvas = self._page.locator(QR_SELECTOR).filter(visible=True)
            if canvas.count() == 1:
                raw = canvas.screenshot(type="png", timeout=4000)
                if len(raw) <= 1024 * 1024:
                    result["qr"] = {"id": self._attempt, "image": "data:image/png;base64," + base64.b64encode(raw).decode("ascii")}
        return result

    def _poll(self, payload):
        if not self._attempt or payload.get("id") != self._attempt:
            return self._result(False, "STALE_ATTEMPT")
        return self._login_result()

    def _password(self, payload):
        if not self._attempt or payload.get("id") != self._attempt:
            return self._result(False, "STALE_ATTEMPT")
        password = payload.pop("password", None)
        if not isinstance(password, str) or not 1 <= len(password) <= 1024:
            return self._result(False, "INVALID_INPUT")
        self._refresh_state()
        if self._state != "waiting_password":
            return self._result(False, "STALE_ATTEMPT")
        field = self._page.locator(PASSWORD_SELECTOR).filter(visible=True)
        if field.count() != 1:
            return self._result(False, "LOGIN_FORM_UNAVAILABLE")
        if not field.is_enabled():
            return self._result(True, "PASSWORD_PENDING", attempt_id=self._attempt)
        input_handle = field.element_handle(timeout=3000)
        try:
            field.fill(password, timeout=3000)
            # Only the official password input receives this keystroke, never the
            # desktop keyboard or a chat composer.
            field.press("Enter", timeout=3000)
            try:
                self._page.wait_for_function("""selector => !!window.rootScope?.myId ||
                  [...document.querySelectorAll(selector)].some(input => input.classList.contains('error'))""",
                                             arg=PASSWORD_SELECTOR, timeout=5000)
            except Exception:
                pass
            self._refresh_state()
            if self._state == "waiting_password" and "error" in (field.get_attribute("class") or "").split():
                return self._result(False, "PASSWORD_FAILED")
            if self._state == "waiting_password":
                return self._result(True, "PASSWORD_PENDING", attempt_id=self._attempt)
            return self._result(attempt_id=self._attempt)
        finally:
            password = None
            # WebK disables/hides the input during submission; clear that exact
            # element without another keystroke, even after its card is detached.
            if input_handle:
                try:
                    input_handle.evaluate("input => { input.value = ''; }")
                finally:
                    input_handle.dispose()

    def _logout(self, payload):
        self._attempt = None
        confirmed = not self._authorized_marker.exists() and self._context is None
        try:
            if self._page and self._state == "authorized":
                confirmed = bool(self._page.evaluate("""async () => {
                  if (location.origin !== 'https://web.telegram.org' || location.pathname !== '/k/') return false;
                  const api = window.rootScope?.managers?.apiManager;
                  if (!api?.invokeApi) return false;
                  try { await Promise.race([api.invokeApi('auth.logOut', {}, {noErrorBox:true, ignoreErrors:true, noRetry:true, rawError:true, floodMaxTimeout:0}),
                    new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), 3000))]); return true; } catch (_) { return false; }
                }"""))
        except Exception:
            confirmed = False
        finally:
            self._close_browser()
            self._authorized_marker.unlink(missing_ok=True)
            # Only our dedicated child profile is removed; never a supplied parent.
            if self._profile.exists():
                if self._profile.is_symlink() or self._profile.resolve().parent != self._base.resolve():
                    raise ValueError("unsafe profile")
                shutil.rmtree(self._profile)
            self._state = "disconnected"
            self._reason = "DISCONNECTED" if confirmed else "LOGOUT_UNCONFIRMED"
            self._expires = self._cooldown = 0
            self._archived_count = 0
        return self._result(code=None if confirmed else "LOGOUT_UNCONFIRMED")

    def _search(self, payload):
        began = self._clock()
        query = payload.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 300:
            return self._result(False, "INVALID_INPUT", results=[])
        try:
            limit = max(1, min(100, int(payload.get("limit", 30))))
        except (TypeError, ValueError, OverflowError):
            return self._result(False, "INVALID_INPUT", results=[])
        if self._clock() < self._cooldown:
            return self._result(False, "FLOOD_WAIT", results=[])
        if not self._page and not self._authorized_marker.exists():
            return self._result(False, "AUTH_REQUIRED", results=[])
        self._launch(navigate_timeout=min(18, payload["timeout"]))
        self._refresh_state()
        if self._state == "starting" and self._page:
            remaining = payload["timeout"] - (self._clock() - began)
            if remaining > 0:
                try:
                    self._page.wait_for_function("(" + AUTH_STATE + ")() !== 'starting'",
                                                 timeout=max(1, int(min(10, remaining) * 1000)))
                except Exception:
                    pass
                self._refresh_state()
            if self._state == "starting":
                return self._result(False, "TIMEOUT", results=[])
        if self._state != "authorized":
            if not self._attempt:
                self._close_browser()
                self._authorized_marker.unlink(missing_ok=True)
                self._state, self._reason = "disconnected", "AUTH_REQUIRED"
            return self._result(False, "AUTH_REQUIRED", results=[])
        remaining = payload["timeout"] - (self._clock() - began)
        if remaining <= 0:
            return self._result(False, "TIMEOUT", results=[])
        result = self._page.evaluate(self._bridge, {"query": query.strip(), "limit": limit, "timeout": remaining})
        if not isinstance(result, dict):
            return self._result(False, "BRIDGE_UNAVAILABLE", results=[])
        self._archived_count = max(0, int(result.get("archived_count", 0)))
        code = result.get("code")
        if code not in REASONS:
            code = "SEARCH_FAILED" if not result.get("success") else None
        if code == "FLOOD_WAIT":
            self._cooldown = self._clock() + max(1, min(86400, int(result.get("retry_after", 60))))
        if code == "AUTH_REQUIRED":
            self._state, self._reason = "disconnected", code
        return self._result(bool(result.get("success")), code, results=result.get("results", []),
                            partial=bool(result.get("partial")), progress=result.get("progress", {}),
                            bot=result.get("bot", {}))
