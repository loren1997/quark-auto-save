"""Offline container check: start Chromium, execute JavaScript, persist a profile."""
import tempfile
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright
from telegram_web.server import make_handler
from telegram_web.worker import TelegramWebWorker


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
