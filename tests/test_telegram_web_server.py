import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

from telegram_web.server import make_handler


class InternalBrowserServerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        token = Path(folder.name) / "token"
        token.write_text("a" * 64)
        self.worker = Mock()
        self.worker.submit.return_value = {"success": True, "data": {"scope": "archived"}}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.worker, token))
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def request(self, path, payload=None, token=True, method=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + "a" * 64
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response), response.headers
        except HTTPError as error:
            return error.code, json.load(error), error.headers

    def test_only_health_is_public_and_does_not_read_account(self):
        self.assertEqual(self.request("/health", token=False)[0], 200)
        for path in ("/status", "/profile", "/devtools"):
            self.assertEqual(self.request(path, token=False)[0], 401)
        self.worker.submit.assert_not_called()

    def test_fixed_routes_and_body_limits_reject_browser_control(self):
        self.assertEqual(self.request("/evaluate", {"script": "arbitrary"})[0], 404)
        self.assertEqual(self.request("/start", ["invalid"])[0], 400)
        self.assertEqual(self.request("/password", {"password": "x" * 9000})[0], 400)
        self.worker.submit.assert_not_called()

    def test_authenticated_requests_are_not_cacheable(self):
        status, body, headers = self.request("/search", {"query": "动画", "limit": 20})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["scope"], "archived")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.worker.submit.assert_called_once_with("search", {"query": "动画", "limit": 20})

    def test_transport_errors_never_echo_password_or_profile(self):
        self.worker.submit.side_effect = RuntimeError("secret-password /private/profile")
        status, body, _ = self.request("/password", {"password": "secret-password"})
        self.assertEqual(status, 200)
        self.assertFalse(body["success"])
        self.assertNotIn("secret-password", json.dumps(body))
        self.assertNotIn("/private/profile", json.dumps(body))
