"""Optional headless Web-sidecar UI test; no real Telegram account or browser profile."""
import base64
import copy
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit
import zlib

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))
import run as web


def png(color):
    def chunk(name, data):
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0" + bytes(color))) + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(data).decode()


def main():
    output = ROOT / "test-results"
    output.mkdir(exist_ok=True)
    errors, console, calls, writes = [], [], [], []
    attempt = "a" * 32
    pictures = [png((0, 0, 0)), png((10, 20, 30))]
    secret = "fake-web-two-factor-password"
    state = {"available": True, "authorized": False, "state": "disconnected", "scope": "archived",
             "reason": "", "retry_after": 0, "archived_count": 7}
    control = {"phase": "qr", "polls": 0}

    def response(success=True, **extra):
        return {"success": success, "data": dict(state, profile="private-profile", cookie="private-cookie"), **extra}

    class FakeWeb:
        def __init__(self, health_path):
            pass

        def status(self):
            calls.append(("status", {}))
            return response()

        def start(self):
            calls.append(("start", {}))
            state.update(state="starting", authorized=False, reason="")
            return response(attempt_id=attempt)

        def poll(self, id):
            calls.append(("poll", {"id": id}))
            assert id == attempt
            if control["phase"] == "authorized":
                state.update(state="authorized", authorized=True, reason="")
                return response()
            if control["phase"] == "password":
                state.update(state="waiting_password", reason="请输入两步验证密码（模拟）")
                return response(attempt_id=attempt)
            state.update(state="waiting_qr")
            control["polls"] += 1
            return response(attempt_id=attempt, qr={"id": attempt, "image": pictures[min(control["polls"] - 1, 1)]})

        def password(self, id, password):
            calls.append(("password", {"id": id}))
            assert id == attempt
            if password != secret:
                state.update(reason="两步验证密码错误（模拟）")
                return response(success=False, attempt_id=attempt)
            state.update(reason="Telegram 正在验证两步验证密码，请稍候")
            return response(code="PASSWORD_PENDING", attempt_id=attempt)

        def logout(self):
            calls.append(("logout", {}))
            state.update(state="disconnected", authorized=False, reason="")
            return response()

    with tempfile.TemporaryDirectory() as directory, patch.object(web, "TelegramWeb", FakeWeb), \
            patch.object(web, "reload_tasks", return_value=True), \
            patch.object(web, "get_run_manager", return_value=SimpleNamespace(list=lambda: [])), \
            patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected backend network")):
        web.CONFIG_PATH = str(Path(directory) / "config.json")
        config = json.loads((ROOT / "quark_config.json").read_text(encoding="utf-8"))
        config.update(webui={"username": "test", "password": "test-password"}, cookie=["TEST_COOKIE"],
                      crontab="0 8 * * *", tasklist=[], discovery={"savepath": "/cartoon", "auto_download": False})
        config.setdefault("source", {}).update(telegram_web={"enable": False}, telegram={"enable": True, "auto_join": False})
        web.Config.write_json(web.CONFIG_PATH, config)
        web.config_data = copy.deepcopy(config)
        web.task_plugins_config_default = {}
        health = Path(web.Config.search_health_path(web.CONFIG_PATH))
        legacy = health.with_name("." + health.name + ".telegram.json")
        legacy.write_text("private-legacy-session", encoding="utf-8")
        server = make_server("127.0.0.1", 0, web.app, threaded=True)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("console", lambda message: console.append(message.text))

                def route_request(route):
                    target = urlsplit(route.request.url)
                    if target.hostname == "127.0.0.1" and target.port == server.server_port:
                        if route.request.method in ("POST", "DELETE") and target.path != "/login":
                            writes.append((target.path, route.request.method, route.request.post_data_json))
                        route.continue_()
                    elif target.hostname == "api.github.com":
                        route.fulfill(json=[{"name": "dev"}])
                    else:
                        route.abort()

                page.route("**/*", route_request)
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                page.locator('input[name="username"]').fill("test")
                page.locator('input[name="password"]').fill("test-password")
                page.locator('button[type="submit"]').click()
                page.wait_for_function("typeof app !== 'undefined' && app.formData.config_revision && app.telegramStatus && !app.telegramBusy")
                page.locator("a.nav-link").filter(has_text="系统配置").click()
                assert calls == [("status", {})], "mount must not start the NAS browser"
                for old in ("telegram-api-id", "telegram-api-hash", "telegram-phone", "telegram-mode-code", "telegram-auto-join"):
                    assert page.locator("#" + old).count() == 0

                def action(selector, endpoint):
                    with page.expect_response(lambda reply: urlsplit(reply.url).path == endpoint) as returned:
                        page.locator(selector).click()
                    page.wait_for_function("!app.telegramBusy")
                    return returned.value.json()

                page.locator("#discovery-savepath").fill("/unsaved-web-path")
                started = action("#telegram-start-qr", "/api/telegram/web/start")
                assert started["attempt_id"] == attempt
                assert started["data"]["scope"] == "archived"
                assert not page.locator("#telegram-account-enabled").is_checked()
                page.wait_for_function("document.querySelector('#telegram-qr-image')?.naturalWidth > 0")
                assert page.locator("#telegram-qr-image").get_attribute("src") == pictures[0]
                page.wait_for_function("image => app.telegramQr.image === image", arg=pictures[1])
                page.wait_for_function("!app.telegramBusy")
                page.locator("a.nav-link").filter(has_text="任务列表").click()
                before = len([kind for kind, _ in calls if kind == "poll"])
                page.wait_for_timeout(2300)
                assert len([kind for kind, _ in calls if kind == "poll"]) == before
                control["phase"] = "password"
                page.locator("a.nav-link").filter(has_text="系统配置").click()
                page.locator("#telegram-password").wait_for(timeout=7000)
                assert page.evaluate("app.telegramAttemptId") == attempt
                assert page.locator("#telegram-qr-image").count() == 0
                page.locator("#telegram-password").fill("wrong-password")
                failed = action("#telegram-submit-password", "/api/telegram/web/password")
                assert not failed["success"]
                assert page.locator("#telegram-password").input_value() == ""
                assert page.get_by_role("alert").filter(has_text="两步验证密码错误").is_visible()
                assert page.evaluate("app.telegramAttemptId") == attempt
                with page.expect_response(lambda reply: urlsplit(reply.url).path == "/api/telegram/web/poll"):
                    page.wait_for_timeout(2300)
                page.wait_for_function("!app.telegramBusy")
                assert page.get_by_role("alert").filter(has_text="两步验证密码错误").is_visible()
                page.locator("#telegram-password").fill(secret)
                pending = action("#telegram-submit-password", "/api/telegram/web/password")
                assert pending["success"] and pending["data"]["state"] == "waiting_password"
                assert page.locator("#telegram-password").input_value() == ""
                with page.expect_response(lambda reply: urlsplit(reply.url).path == "/api/telegram/web/poll") as verified:
                    control["phase"] = "authorized"
                authorized = verified.value.json()
                page.wait_for_function("app.telegramStatus.authorized && !app.telegramBusy")
                assert authorized["data"]["authorized"]
                assert page.locator("#telegram-account-enabled").is_checked()
                assert page.evaluate("app.telegramAttemptId") is None
                assert page.evaluate("app.telegramLogin.password") == ""
                assert page.evaluate("app.formData.config_revision") == authorized["config_revision"]
                assert page.locator("#discovery-savepath").input_value() == "/unsaved-web-path"
                assert web.Config.read_json(web.CONFIG_PATH)["discovery"]["savepath"] == "/cartoon"
                page.locator("#telegram-status").scroll_into_view_if_needed()
                page.screenshot(path=str(output / "telegram-web-authorized.png"))

                with page.expect_response(lambda reply: urlsplit(reply.url).path == "/update") as saved:
                    page.locator('button[data-original-title="保存 CTRL+S"], button[title="保存 CTRL+S"]').click()
                assert saved.value.json()["success"]
                page.wait_for_function("!app.configModified")
                assert web.Config.read_json(web.CONFIG_PATH)["source"]["telegram_web"]["enable"]

                # A later worker write must keep the editor stale when logout returns a newer revision.
                page.locator("#discovery-savepath").fill("/unsaved-after-worker-change")
                stale_revision = page.evaluate("app.formData.config_revision")
                current = web.Config.read_json(web.CONFIG_PATH)
                current["tasklist"].append({"taskname": "后台新增任务", "shareurl": "", "savepath": "/cartoon"})
                web.Config.write_json(web.CONFIG_PATH, current)
                disconnected = action("#telegram-disconnect", "/api/telegram/web/session")
                assert not disconnected["data"]["enabled"]
                assert page.evaluate("app.formData.config_revision") == stale_revision
                assert page.get_by_role("alert").filter(has_text="后台配置也有变更").is_visible()
                assert page.locator("#discovery-savepath").input_value() == "/unsaved-after-worker-change"
                assert ("/api/telegram/web/session", "DELETE", {}) in writes
                with page.expect_response(lambda reply: urlsplit(reply.url).path == "/update") as rejected:
                    page.locator('button[data-original-title="保存 CTRL+S"], button[title="保存 CTRL+S"]').click()
                assert rejected.value.status == 409
                assert web.Config.read_json(web.CONFIG_PATH)["tasklist"] == current["tasklist"]
                assert legacy.read_text(encoding="utf-8") == "private-legacy-session"
                assert web.Config.read_json(web.CONFIG_PATH)["source"]["telegram"]["enable"]

                # Read-only status cannot reset an unsaved switch.
                page.locator("#telegram-account-enabled").check()
                action("#telegram-status", "/api/telegram/web/status")
                assert page.locator("#telegram-account-enabled").is_checked()
                page.set_viewport_size({"width": 390, "height": 844})
                page.locator("#telegram-status").scroll_into_view_if_needed()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                page.screenshot(path=str(output / "telegram-web-mobile.png"), full_page=True)
                config_posts = [body for path, _, body in writes if path == "/update"]
                output_text = json.dumps(config_posts, ensure_ascii=False) + "\n".join(console)
                for private in (secret, "wrong-password", "private-profile", "private-cookie", "private-legacy-session", *pictures):
                    assert private not in output_text, "private account material escaped a dedicated route"
                assert not errors, errors
                browser.close()
            print("Telegram Web UI smoke passed: status never starts browser; QR refresh; no polling away; "
                  "2FA error survives polling, delayed success and cleared password; archive-only toggle; logout; revision conflict "
                  "preserves worker task and edits; legacy session untouched; no secrets in config/console; mobile; no JS errors")
        finally:
            server.shutdown()
            worker.join(timeout=5)


if __name__ == "__main__":
    main()
