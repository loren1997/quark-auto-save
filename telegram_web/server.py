"""Internal, authenticated bridge. The browser itself has no exposed control port."""
import hmac
import json
import os
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from telegram_web.worker import TelegramWebWorker


def make_handler(worker, token_path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Request paths and login payloads must never reach access logs.

        def reply(self, status, result):
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def dispatch(self):
            self.connection.settimeout(10)
            if self.command == "GET" and self.path == "/health":
                return self.reply(200, {"ok": True})
            try:
                token = Path(token_path).read_text(encoding="ascii").strip()
            except (OSError, UnicodeError):
                token = ""
            if len(token) != 64 or not hmac.compare_digest(
                    self.headers.get("Authorization", "").encode("utf-8"), ("Bearer " + token).encode("ascii")):
                return self.reply(401, {"success": False, "message": "Unauthorized"})
            action = {("GET", "/status"): "status", ("POST", "/start"): "start",
                      ("POST", "/poll"): "poll", ("POST", "/password"): "password",
                      ("DELETE", "/session"): "logout", ("POST", "/search"): "search"}.get(
                          (self.command, self.path))
            if action is None:
                return self.reply(404, {"success": False, "message": "Not found"})
            payload = {}
            if self.command != "GET":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 8192 or self.headers.get_content_type() != "application/json":
                        raise ValueError()
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError()
                except (ValueError, UnicodeError, OSError):
                    return self.reply(400, {"success": False, "message": "Invalid request"})
            try:
                result = worker.submit(action, payload)
            except Exception:
                print("Telegram Web worker request failed", flush=True)
                result = {"success": False, "data": {"available": False, "authorized": False,
                          "state": "error", "scope": "archived", "reason": "浏览器连接暂不可用"}}
            self.reply(200, result)

        do_GET = do_POST = do_DELETE = dispatch
    return Handler


def main():
    def stop(*_):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, stop)
    worker = TelegramWebWorker(os.environ.get("TELEGRAM_WEB_PROFILE", "/data/profile"))
    server = ThreadingHTTPServer(("0.0.0.0", 8765), make_handler(
        worker, os.environ.get("TELEGRAM_WEB_TOKEN_FILE", "/app/config/.telegram-web-token")))
    print("Telegram Web archive service ready", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        worker.close()


if __name__ == "__main__":
    main()
