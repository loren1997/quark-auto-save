from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from telegram_web.server import make_handler


class TelegramWebServerBoundaryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        token_file = Path(folder.name) / "token"
        self.token = "a" * 64
        token_file.write_text(self.token, encoding="ascii")
        self.worker = Mock()
        self.worker.submit.return_value = {"success": True, "data": {
            "available": True, "authorized": False, "state": "disconnected", "scope": "archived"}}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.worker, token_file))
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, *, authorization=None, body=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        self.addCleanup(connection.close)
        headers = {"Content-Type": "application/json"}
        if authorization is not None:
            headers["Authorization"] = authorization
        connection.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.headers), json.loads(response.read())

    def test_missing_wrong_or_non_ascii_auth_never_reaches_worker(self):
        for authorization in (None, "Bearer " + "b" * 64, "Bearer caf\xe9"):
            with self.subTest(authorization=authorization):
                self.assertEqual(self.request("GET", "/status", authorization=authorization)[0], 401)
        self.worker.submit.assert_not_called()

    def test_fixed_route_contract_does_not_expose_arbitrary_browser_or_join_actions(self):
        authorization = "Bearer " + self.token
        for path in ("/join", "/evaluate", "/navigate", "/status?url=https://example.org"):
            self.assertEqual(self.request("POST", path, authorization=authorization, body={})[0], 404)
        self.worker.submit.assert_not_called()
        status, headers, data = self.request("POST", "/search", authorization=authorization,
                                             body={"query": "作品", "limit": 20, "timeout": 25})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.worker.submit.assert_called_once_with("search", {"query": "作品", "limit": 20, "timeout": 25})
        self.assertEqual(data["data"]["scope"], "archived")

    def test_invalid_body_is_rejected_before_worker(self):
        status, _, _ = self.request("POST", "/password", authorization="Bearer " + self.token, body=[])
        self.assertEqual(status, 400)
        self.worker.submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
