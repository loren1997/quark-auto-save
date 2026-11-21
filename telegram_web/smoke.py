"""Offline container check: start Chromium, execute JavaScript, persist a profile."""
import tempfile
import json
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright
from telegram_web.server import make_handler
from telegram_web.worker import TelegramWebWorker, WEB_URL


def check_password_login(page, folder):
    # Mirrors WebK PasswordInputHelpers' two opacity-zero autofill decoys.
    fixture = """<!doctype html><style>
      .stealthy{opacity:0;pointer-events:none;z-index:-1;outline:none;max-width:1px;
      max-height:1px;margin:0;position:absolute;top:0;left:0;overflow:hidden}
      </style><div class="_pagePassword_fixture_1"><div class="input-field">
      <input type="password" class="stealthy" tabindex="-1" aria-hidden="true">
      <input id="real-password" type="password" name="notsearch_password" class="input-field-input">
      <input type="password" class="stealthy" tabindex="-1" aria-hidden="true">
      </div></div><textarea id="chat-composer"></textarea><script>
      window.rootScope = {managers:{apiManager:{invokeApi(){throw Error('No network in fixture');}}}};
      window.submitted = 0;
      document.querySelector('#real-password').addEventListener('keypress', event => {
        if(event.key !== 'Enter') return;
        window.submitted++;
        const input = event.target;
        input.disabled = true;
        setTimeout(() => {
          if(window.outcome === 'error') {
            input.disabled = false; input.classList.add('error');
          } else {
            window.rootScope.myId = 123;
            document.querySelector('._pagePassword_fixture_1').style.display = 'none';
          }
        }, window.delay || 0);
      });</script>"""
    page.route("**/*", lambda route: route.fulfill(content_type="text/html", body=fixture)
               if route.request.url == WEB_URL else route.abort())
    base = Path(folder) / "login-fixture"
    base.mkdir()
    worker = TelegramWebWorker(base)
    worker._page = page

    def reset(outcome="success", delay=0):
        page.goto(WEB_URL)
        page.evaluate("([outcome,delay]) => {window.outcome=outcome;window.delay=delay;}", [outcome, delay])
        worker._attempt = "a" * 32
        worker._expires = time.monotonic() + 60

    def submit():
        return worker._password({"id": "a" * 32, "password": "test-only-2fa"})

    reset()
    legacy = page.locator('[class*="pagePassword"] input[type="password"]').filter(visible=True)
    assert legacy.count() == 3, "Fixture must reproduce the three-input bug"
    result = submit()
    assert result["success"] and result["data"]["authorized"], result
    assert page.locator("#real-password").input_value() == ""
    assert page.locator("#chat-composer").input_value() == ""
    assert page.locator(".stealthy").evaluate_all("items => items.every(input => input.value === '')")

    reset("error")
    result = submit()
    assert result.get("code") == "PASSWORD_FAILED", result
    assert page.locator("#real-password").input_value() == ""

    reset()
    page.locator("#real-password").evaluate("input => input.type = 'text'")
    assert submit()["data"]["authorized"]

    reset()
    page.locator("#real-password").evaluate("input => input.after(input.cloneNode())")
    assert not submit()["success"]
    assert page.evaluate("window.submitted") == 0
    assert page.locator("input").evaluate_all("items => items.every(input => input.value === '')")

    reset(delay=8000)
    pending = submit()
    assert pending.get("code") == "PASSWORD_PENDING", pending
    assert page.locator("#real-password").input_value() == ""
    assert submit().get("code") == "PASSWORD_PENDING"
    assert page.evaluate("window.submitted") == 1
    page.wait_for_function("() => !!window.rootScope.myId", timeout=5000)
    assert worker._poll({"id": "a" * 32})["data"]["authorized"]
    page.unroute("**/*")
    page.goto("about:blank")
    print("Official-style 2FA DOM: decoys, wrong password, success, revealed field, ambiguous input and delayed verification passed", flush=True)


def main():
    with tempfile.TemporaryDirectory() as folder, sync_playwright() as driver:
        profile = Path(folder) / "profile"
        for index in range(2):
            context = driver.chromium.launch_persistent_context(str(profile), headless=True)
            page = context.pages[0]
            page.goto("about:blank")
            assert page.evaluate("1 + 1") == 2
            if index == 0:
                context.add_cookies([{"name": "profile-check", "value": "ok",
                                     "domain": "example.invalid", "path": "/", "expires": 2147483647}])
                check_password_login(page, folder)
            else:
                assert any(item["name"] == "profile-check" for item in context.cookies())
            context.close()
        token = Path(folder) / "token"
        token.write_text("a" * 64)
        worker = TelegramWebWorker(Path(folder) / "worker-profile")
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(worker, token))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            request = Request(f"http://127.0.0.1:{server.server_port}/status",
                              headers={"Authorization": "Bearer " + "a" * 64})
            with urlopen(request, timeout=5) as response:
                result = json.load(response)
            assert result["data"]["scope"] == "archived"
            assert result["data"]["available"] is True
            assert result["data"]["authorized"] is False
        finally:
            server.shutdown()
            server.server_close()
            worker.close()
    print("Headless Chromium, persistent profile and authenticated service: passed", flush=True)


if __name__ == "__main__":
    main()
