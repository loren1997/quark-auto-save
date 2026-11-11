"""Optional, fully mocked Telegram UI smoke: python tests/ui_telegram_smoke.py."""
import copy
import json
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))
import run as web


def main():
    output = ROOT / "test-results"
    output.mkdir(exist_ok=True)
    errors, console, calls, writes = [], [], [], []
    state = {"available": True, "authorized": False, "state": "disconnected", "reason": "", "retry_after": 0}
    credentials = {"api_id": 456789, "api_hash": "abcdef0123456789abcdef0123456789",
                   "phone": "+15550001111", "proxy": "socks5://smoke-user:smoke-secret@127.0.0.1:1080"}
    password = "smoke-only-two-factor-password"
    folders = "https://t.me/addlist/SmokeFolderOne\nhttps://t.me/addlist/SmokeFolderTwo"

    class FakeAccount:
        def __init__(self, health_path):
            pass

        def status(self):
            calls.append(("status", {}))
            return dict(state)

        def request_code(self, **payload):
            calls.append(("code", payload))
            assert payload == credentials
            state.update(state="waiting_code", reason="", authorized=False)
            return dict(state, ok=True)

        def sign_in(self, **payload):
            calls.append(("signin", payload))
            if payload == {"code": "00000"}:
                state.update(reason="验证码错误（模拟）")
                return dict(state, ok=False)
            if payload == {"code": "12345"}:
                state.update(state="waiting_password", reason="请输入两步验证密码（模拟）")
            elif payload == {"password": password}:
                state.update(state="authorized", authorized=True, reason="")
            else:
                raise AssertionError("Unexpected mock login input")
            return dict(state, ok=True, session="must-not-leak-session")

        def logout(self):
            calls.append(("logout", {}))
            state.update(state="disconnected", authorized=False, reason="本地已注销；远端撤销未确认（模拟）")
            return dict(state, ok=False)

    def no_network(*args, **kwargs):
        raise AssertionError("Unexpected backend HTTP request")

    with tempfile.TemporaryDirectory() as folder, \
            patch.object(web, "TelegramAccount", FakeAccount), \
            patch.object(web, "reload_tasks", return_value=True), \
            patch.object(web, "get_run_manager", return_value=SimpleNamespace(list=lambda: [])), \
            patch("requests.sessions.Session.request", side_effect=no_network):
        web.CONFIG_PATH = str(Path(folder) / "config.json")
        config = json.loads((ROOT / "quark_config.json").read_text(encoding="utf-8"))
        config.update(webui={"username": "test", "password": "test-password"}, cookie=["TEST_COOKIE"],
                      crontab="0 8 * * *", tasklist=[], discovery={"savepath": "/cartoon", "auto_download": False})
        config.setdefault("source", {})["telegram"] = {"enable": False}
        config["source"].setdefault("channelsearch", {})["telegram_folders"] = []
        web.Config.write_json(web.CONFIG_PATH, config)
        web.config_data = copy.deepcopy(config)
        web.task_plugins_config_default = {}
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
                section = page.locator('[aria-label="Telegram 账号连接"]')
                page.locator("a.nav-link").filter(has_text="系统配置").click()
                assert calls == [("status", {})], "mount must only read local account state"
                assert section.get_by_text("未连接", exact=True).is_visible()

                def save():
                    with page.expect_response(lambda response: urlsplit(response.url).path == "/update") as result:
                        page.locator('button[data-original-title="保存 CTRL+S"], button[title="保存 CTRL+S"]').click()
                    assert result.value.json()["success"], "config save unexpectedly failed"
                    page.wait_for_function("!app.configModified")

                def action(selector, endpoint):
                    with page.expect_response(lambda response: urlsplit(response.url).path == endpoint) as result:
                        page.locator(selector).click()
                    page.wait_for_function("!app.telegramBusy")
                    return result.value.json()

                # Login input is independent from application config, even before submission.
                page.locator("#telegram-folders").fill(folders)
                for key, value in credentials.items():
                    page.locator("#telegram-" + key.replace("_", "-")).fill(str(value))
                save()
                assert web.Config.read_json(web.CONFIG_PATH)["source"]["channelsearch"]["telegram_folders"] == folders
                page.locator("#discovery-savepath").fill("/edited-before-login")
                page.locator("#telegram-folders").fill(folders + "\nhttps://t.me/addlist/UnsavedThird")
                old_revision = page.evaluate("app.formData.config_revision")

                result = action("#telegram-send-code", "/api/telegram/code")
                assert result["success"] and result["data"]["state"] == "waiting_code"
                assert page.evaluate("app.telegramLogin.api_hash === '' && app.telegramLogin.phone === '' && app.telegramLogin.proxy === ''")
                assert not page.locator("#telegram-account-enabled").is_checked()
                assert page.evaluate("app.formData.config_revision") == old_revision

                page.locator("#telegram-code").fill("00000")
                result = action("#telegram-submit-code", "/api/telegram/signin")
                assert not result["success"]
                assert section.get_by_role("alert").inner_text() == "验证码错误（模拟）"
                assert page.locator("#telegram-code").input_value() == ""
                page.locator("#telegram-code").fill("12345")
                result = action("#telegram-submit-code", "/api/telegram/signin")
                assert result["data"]["state"] == "waiting_password"
                page.locator("#telegram-password").wait_for()
                assert page.evaluate("app.telegramLogin.code") == ""
                assert section.get_by_role("alert").count() == 0

                page.locator("#telegram-password").fill(password)
                result = action("#telegram-submit-password", "/api/telegram/signin")
                assert result["success"] and result["data"]["authorized"]
                assert page.locator("#telegram-account-enabled").is_checked()
                assert page.evaluate("app.telegramLogin.code === '' && app.telegramLogin.password === ''")
                assert page.evaluate("app.formData.config_revision") == result["config_revision"] != old_revision
                assert page.locator("#discovery-savepath").input_value() == "/edited-before-login"
                assert page.locator("#telegram-folders").input_value().endswith("UnsavedThird")
                assert web.Config.read_json(web.CONFIG_PATH)["discovery"]["savepath"] == "/cartoon"
                section.get_by_text("已连接", exact=True).wait_for()
                section.screenshot(path=str(output / "telegram-ui-authorized.png"))
                save()
                saved = web.Config.read_json(web.CONFIG_PATH)
                assert saved["source"]["telegram"]["enable"]
                assert saved["discovery"]["savepath"] == "/edited-before-login"
                assert saved["source"]["channelsearch"]["telegram_folders"].endswith("UnsavedThird")

                # Business failure still applies the locally disconnected state/revision.
                page.locator("#discovery-savepath").fill("/edited-before-logout")
                result = action("#telegram-disconnect", "/api/telegram/session")
                assert not result["success"] and not result["data"]["enabled"]
                assert not page.locator("#telegram-account-enabled").is_checked()
                assert page.evaluate("app.formData.config_revision") == result["config_revision"]
                assert page.locator("#discovery-savepath").input_value() == "/edited-before-logout"
                assert page.evaluate("Object.values(app.telegramLogin).every(value => value === '')")
                assert "远端撤销未确认" in section.get_by_role("alert").inner_text()
                assert ("/api/telegram/session", "DELETE", {}) in writes
                save()
                assert web.Config.read_json(web.CONFIG_PATH)["discovery"]["savepath"] == "/edited-before-logout"
                assert not web.Config.read_json(web.CONFIG_PATH)["source"]["telegram"]["enable"]

                # A concurrent worker update cannot be hidden by adopting the login's
                # newer revision while retaining an older task list in the editor.
                page.locator("#discovery-savepath").fill("/unsaved-during-worker-update")
                stale_revision = page.evaluate("app.formData.config_revision")
                latest = web.Config.read_json(web.CONFIG_PATH)
                latest["tasklist"].append({"taskname": "后台新增任务", "shareurl": "", "savepath": "/cartoon"})
                web.Config.write_json(web.CONFIG_PATH, latest)
                for key, value in credentials.items():
                    page.locator("#telegram-" + key.replace("_", "-")).fill(str(value))
                action("#telegram-send-code", "/api/telegram/code")
                page.locator("#telegram-code").fill("12345")
                action("#telegram-submit-code", "/api/telegram/signin")
                page.locator("#telegram-password").fill(password)
                result = action("#telegram-submit-password", "/api/telegram/signin")
                assert result["previous_config_revision"] == web.config_revision(latest)
                assert result["previous_config_revision"] != stale_revision
                assert page.evaluate("app.formData.config_revision") == stale_revision
                assert "后台配置也有变更" in section.get_by_role("alert").inner_text()
                assert page.locator("#discovery-savepath").input_value() == "/unsaved-during-worker-update"
                with page.expect_response(lambda response: urlsplit(response.url).path == "/update") as rejected:
                    page.locator('button[data-original-title="保存 CTRL+S"], button[title="保存 CTRL+S"]').click()
                assert rejected.value.status == 409
                assert web.Config.read_json(web.CONFIG_PATH)["tasklist"] == latest["tasklist"]
                assert page.locator("#discovery-savepath").input_value() == "/unsaved-during-worker-update"
                action("#telegram-disconnect", "/api/telegram/session")

                # Reading status must not overwrite an unsaved enable switch. HTTP errors
                # with structured state still update the account panel without leaking inputs.
                page.locator("#telegram-account-enabled").check()
                action("#telegram-status", "/api/telegram/status")
                assert page.locator("#telegram-account-enabled").is_checked()
                page.route("**/api/telegram/status", lambda route: route.fulfill(
                    status=503, json={"success": False, "message": "连接组件未就绪（模拟）",
                                      "data": dict(state, available=False, state="unavailable", enabled=False)}))
                action("#telegram-status", "/api/telegram/status")
                section.get_by_text("连接组件不可用", exact=True).wait_for()
                assert section.get_by_role("alert").inner_text() == "连接组件未就绪（模拟）"
                assert page.locator("#telegram-account-enabled").is_checked()
                page.set_viewport_size({"width": 390, "height": 844})
                page.locator("#telegram-status").scroll_into_view_if_needed()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                page.screenshot(path=str(output / "telegram-ui-mobile.png"), full_page=True)

                config_posts = [body for path, method, body in writes if path == "/update"]
                assert len(config_posts) == 4
                non_account_payloads = json.dumps(config_posts, ensure_ascii=False) + "\n".join(console)
                for secret in (*credentials.values(), "00000", "12345", password, "must-not-leak-session"):
                    assert str(secret) not in non_account_payloads, "account credential escaped its dedicated request"
                assert not errors, errors
                browser.close()
            print("Telegram UI smoke passed: local status only; code/error/2FA/authorized flow; cleared credentials; "
                  "JSON logout and revision; unsaved edits preserved; opt-in toggle and folder save; "
                  "concurrent worker task protected by stale revision; no credentials in /update or console; "
                  "HTTP error state; mobile layout; no JS errors")
        finally:
            server.shutdown()
            worker.join(timeout=5)


if __name__ == "__main__":
    main()
