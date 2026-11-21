import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import requests
from app.sdk.telegram_web import TelegramWeb, shared_token

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
with contextlib.redirect_stdout(io.StringIO()):
    import run as web


ATTEMPT = "a" * 32
PNG = "data:image/png;base64,iVBORw0KGgo="


def result(state="disconnected", success=True, **extra):
    data = {"available": True, "authorized": state == "authorized", "state": state, "scope": "archived",
            "reason": "", "retry_after": 0, "archived_count": 5}
    return {"success": success, "data": data, **extra}


class TelegramWebClientTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.client = TelegramWeb(str(self.folder / "health.json"))
        self.client.url = "http://telegram-web:8765"
        patcher = patch("app.sdk.telegram_web.requests.Session")
        self.session_factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.http = self.session_factory.return_value.__enter__.return_value
        self.response = self.http.request.return_value.__enter__.return_value
        self.response.status_code = 200
        self.respond(result())

    def respond(self, body):
        self.response.iter_content.return_value = [json.dumps(body).encode()]

    def test_shared_token_is_complete_private_and_not_replaced_by_concurrent_creators(self):
        path = self.folder / ".telegram-web-token"
        with ThreadPoolExecutor(max_workers=2) as pool:
            tokens = list(pool.map(lambda _: shared_token(path), range(8)))
        self.assertEqual(len(set(tokens)), 1)
        self.assertRegex(tokens[0], r"^[a-f0-9]{64}$")
        self.assertEqual(path.read_text(), tokens[0])
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(file.name for file in self.folder.iterdir()), [".telegram-web-token"])

    def test_status_calls_only_readonly_sidecar_and_does_not_use_proxy_or_redirects(self):
        returned = self.client.status()
        self.assertTrue(returned["success"])
        call = self.http.request.call_args
        self.assertEqual(call.args, ("GET", "http://telegram-web:8765/status"))
        self.assertIsNone(call.kwargs["json"])
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertFalse(self.http.trust_env)
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer " + self.client.token_path.read_text())
        self.assertNotIn(self.client.token_path.read_text(), json.dumps(returned))

    def test_each_explicit_operation_uses_fixed_path_and_schema(self):
        for method, arguments, verb, path, payload in (
            ("start", (), "POST", "/start", {}), ("poll", (ATTEMPT,), "POST", "/poll", {"id": ATTEMPT}),
            ("password", (ATTEMPT, "fake-password"), "POST", "/password", {"id": ATTEMPT, "password": "fake-password"}),
            ("logout", (), "DELETE", "/session", {}),
        ):
            getattr(self.client, method)(*arguments)
            call = self.http.request.call_args
            self.assertEqual(call.args, (verb, "http://telegram-web:8765" + path))
            self.assertEqual(call.kwargs["json"], payload)

    def test_status_strips_private_fields_and_qr_and_requires_archive_scope(self):
        body = result(attempt_id=ATTEMPT, qr={"id": ATTEMPT, "image": PNG}, profile="secret-profile",
                      results=[{"content": "private-message"}])
        body["data"].update(cookie="private-cookie", session="private-session")
        self.respond(body)
        clean = self.client.status()
        self.assertEqual(set(clean), {"success", "data"})
        self.assertNotIn("private-", json.dumps(clean))
        body["data"]["scope"] = "all"
        self.respond(body)
        rejected = self.client.status()
        self.assertFalse(rejected["success"])
        self.assertEqual(rejected["code"], "WEB_INVALID_RESPONSE")

    def test_qr_is_png_only_and_only_returned_for_start_or_poll(self):
        self.respond(result("waiting_qr", attempt_id=ATTEMPT, qr={"id": ATTEMPT, "image": PNG, "token": "secret"}))
        self.assertEqual(self.client.start()["qr"], {"id": ATTEMPT, "image": PNG})
        self.assertEqual(self.client.poll(ATTEMPT)["attempt_id"], ATTEMPT)
        self.assertNotIn("qr", self.client.password(ATTEMPT, "private-password"))
        self.respond(result("waiting_qr", qr={"id": ATTEMPT, "image": "https://external.example/qr"}))
        self.assertNotIn("qr", self.client.start())

    def test_search_normalizes_only_archived_share_results_and_does_not_expose_cursor(self):
        self.respond(result("authorized", results=[
            {"title": "作品 第二集", "url": "https://pan.quark.cn/s/abcdef123456", "content": "已归档的消息",
             "source_type": "telegram_web", "peer_id": "private-id"},
            {"title": "无关链接", "url": "https://example.com", "source_type": "telegram_web"},
        ], partial=True, progress={"phase": "search", "current": 10, "total": 15, "cursor": "private-cursor"},
            bot={"requests": 1, "links": 1, "pending": 0, "failed": 0, "payload": "private-start"}))
        returned = self.client.search("作品", limit=20, deadline=time.monotonic() + 10)
        self.assertEqual(returned["results"][0]["taskname"], "作品 第二集")
        self.assertEqual(returned["results"][0]["shareurl"], "https://pan.quark.cn/s/abcdef123456")
        self.assertEqual(len(returned["results"]), 1)
        self.assertTrue(returned["partial"])
        self.assertEqual(returned["bot"], {"requests": 1, "links": 1, "pending": 0, "failed": 0})
        self.assertNotIn("private-", json.dumps(returned))
        call = self.http.request.call_args
        self.assertEqual(call.args[1], "http://telegram-web:8765/search")
        self.assertLessEqual(call.kwargs["json"]["timeout"], 10)
        self.assertEqual(call.kwargs["json"]["limit"], 20)
        self.http.request.reset_mock()
        self.assertFalse(self.client.search("作品", deadline=time.monotonic() - 1)["success"])
        self.http.request.assert_not_called()

    def test_transport_failures_never_echo_passwords_or_token(self):
        for error in (requests.Timeout("private-password"), requests.ConnectionError("private-token")):
            self.http.request.side_effect = error
            returned = self.client.password(ATTEMPT, "private-password")
            self.assertFalse(returned["success"])
            self.assertNotIn("private-", json.dumps(returned))
        self.http.request.side_effect = None
        for status in (401, 302, 500):
            self.response.status_code = status
            returned = self.client.status()
            self.assertFalse(returned["success"])
            self.assertNotIn(self.client.token_path.read_text(), json.dumps(returned))

    def test_bad_url_token_or_oversized_reply_fails_closed(self):
        for url in ("file:///config", "http://user:password@example.com", "http://telegram-web/path"):
            self.client.url = url
            self.assertFalse(self.client.status()["success"])
        self.http.request.assert_not_called()
        self.client.url = "http://telegram-web:8765"
        self.client.token_path.write_text("private-broken-token")
        self.assertEqual(self.client.status()["code"], "WEB_TOKEN_ERROR")
        self.http.request.assert_not_called()
        self.client.token_path.unlink()
        self.response.iter_content.return_value = [b"x" * (2 * 1024 * 1024 + 1)]
        self.assertEqual(self.client.status()["code"], "WEB_INVALID_RESPONSE")


class TelegramWebAppTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config_path = str(Path(directory.name) / "config.json")
        self.config = {"webui": {"username": "test", "password": "test-only"}, "tasklist": [],
                       "crontab": "0 8 * * *", "source": {"telegram_web": {"enable": False},
                                                       "telegram": {"enable": True, "auto_join": False}}}
        self.patch("CONFIG_PATH", self.config_path)
        self.patch("config_data", copy.deepcopy(self.config))
        self.sdk = self.patch("TelegramWeb").return_value
        self.sdk.status.return_value = result()
        self.sdk.start.return_value = result("waiting_qr", attempt_id=ATTEMPT, qr={"id": ATTEMPT, "image": PNG})
        self.sdk.poll.return_value = result("waiting_password", attempt_id=ATTEMPT)
        self.sdk.password.return_value = result("authorized")
        self.sdk.logout.return_value = result()
        self.manager = self.patch("get_run_manager")
        self.manager.return_value.list.return_value = []
        self.patch("reload_tasks", return_value=True)
        web.Config.write_json(self.config_path, self.config)
        self.client = web.app.test_client()
        with self.client.session_transaction() as session:
            session["token"] = web.get_login_token()

    def patch(self, name, *args, **kwargs):
        patcher = patch.object(web, name, *args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_all_routes_authenticate_and_never_cache(self):
        anonymous = web.app.test_client()
        for path, method in (("status", "get"), ("start", "post"), ("poll", "post"),
                             ("password", "post"), ("session", "delete")):
            response = getattr(anonymous, method)("/api/telegram/web/" + path, json={})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.sdk.status.assert_not_called()
        self.sdk.start.assert_not_called()

    def test_status_is_readonly_and_strips_attempt_and_secret_fields(self):
        body = result(qr={"id": ATTEMPT, "image": PNG}, attempt_id=ATTEMPT, token="private-token")
        body["data"].update(profile="private-profile", session="private-session")
        self.sdk.status.return_value = body
        response = self.client.get("/api/telegram/web/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["data"]["scope"], "archived")
        self.assertNotIn("attempt_id", response.json)
        self.assertNotIn("qr", response.json)
        self.assertNotIn("private-", response.get_data(as_text=True))
        self.sdk.status.assert_called_once_with()
        self.sdk.start.assert_not_called()
        self.manager.assert_not_called()
        self.assertEqual(web.Config.read_json(self.config_path), self.config)

    def test_mutations_require_same_origin_json_and_exact_bounded_parameters(self):
        for path, payload in (("start", {}), ("poll", {"id": ATTEMPT}),
                              ("password", {"id": ATTEMPT, "password": "fake-secret"})):
            target = "/api/telegram/web/" + path
            self.assertEqual(self.client.post(target, json=payload, headers={"Origin": "https://other.example"}).status_code, 403)
            self.assertEqual(self.client.post(target, data="{}").status_code, 400)
            self.assertEqual(self.client.post(target, data=" " * 8193, content_type="application/json").status_code, 413)
        for path, payload in (("start", {"url": "http://private-host"}), ("poll", {}),
                              ("poll", {"id": "short"}), ("password", {"id": ATTEMPT, "password": ""}),
                              ("password", {"id": ATTEMPT, "password": "x" * 1025}),
                              ("password", {"id": ATTEMPT, "password": "x", "phone": "private-phone"})):
            self.assertEqual(self.client.post("/api/telegram/web/" + path, json=payload).status_code, 400)
        self.assertEqual(self.client.delete("/api/telegram/web/session", json={"all": True}).status_code, 400)
        self.sdk.start.assert_not_called()
        self.sdk.password.assert_not_called()

    def test_web_config_accepts_enable_only_and_rejects_embedded_secrets_or_urls(self):
        for settings in ({"enable": "true"}, {"enable": True, "token": "private-token"},
                         {"enable": True, "password": "private-password"},
                         {"enable": True, "profile": "private-profile"},
                         {"enable": True, "server": "https://other.example"}):
            response = self.client.post("/update", json={"source": {"telegram_web": settings}})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(web.Config.read_json(self.config_path), self.config)
            self.assertNotIn("private-", response.get_data(as_text=True))
        response = self.client.post("/update", json={"source": {"telegram_web": {"enable": True}}})
        self.assertTrue(response.json["success"])
        self.assertEqual(web.Config.read_json(self.config_path)["source"]["telegram_web"], {"enable": True})

    def test_qr_and_password_stages_do_not_enable_until_authorized(self):
        started = self.client.post("/api/telegram/web/start", json={})
        self.assertEqual(started.json["qr"], {"id": ATTEMPT, "image": PNG})
        self.assertFalse(started.json["data"]["enabled"])
        waiting = self.client.post("/api/telegram/web/poll", json={"id": ATTEMPT})
        self.assertEqual(waiting.json["attempt_id"], ATTEMPT)
        self.assertEqual(waiting.json["data"]["state"], "waiting_password")
        self.assertNotIn("config_revision", waiting.json)
        self.sdk.poll.assert_called_once_with(id=ATTEMPT)
        failure = result("waiting_password", success=False, attempt_id=ATTEMPT)
        failure["data"]["reason"] = "两步验证密码错误"
        self.sdk.password.return_value = failure
        rejected = self.client.post("/api/telegram/web/password", json={"id": ATTEMPT, "password": "fake-secret"})
        self.assertFalse(rejected.json["success"])
        self.assertEqual(rejected.json["data"]["reason"], "两步验证密码错误")
        self.assertEqual(web.Config.read_json(self.config_path), self.config)

    def test_authorization_uses_latest_config_and_preserves_concurrent_edit_guard(self):
        old_form = self.client.get("/data").json["data"]
        latest = copy.deepcopy(self.config)
        latest["tasklist"] = [{"taskname": "后台新增"}]
        web.Config.write_json(self.config_path, latest)
        response = self.client.post("/api/telegram/web/password", json={"id": ATTEMPT, "password": "fake-secret"})
        self.assertEqual(response.json["previous_config_revision"], web.config_revision(latest))
        self.assertNotEqual(old_form["config_revision"], response.json["previous_config_revision"])
        latest["source"]["telegram_web"]["enable"] = True
        self.assertEqual(web.Config.read_json(self.config_path), latest)
        self.assertEqual(response.json["config_revision"], web.config_revision(latest))
        old_form["source"]["telegram_web"]["enable"] = True
        self.assertEqual(self.client.post("/update", json=old_form).status_code, 409)
        self.sdk.password.assert_called_once_with(id=ATTEMPT, password="fake-secret")
        self.manager.assert_not_called()

    def test_logout_disables_web_only_and_secrets_do_not_enter_config_or_logs(self):
        self.config["source"]["telegram_web"]["enable"] = True
        web.Config.write_json(self.config_path, self.config)
        response = self.client.delete("/api/telegram/web/session", json={})
        self.assertFalse(response.json["data"]["enabled"])
        self.assertTrue(web.Config.read_json(self.config_path)["source"]["telegram"]["enable"])
        self.assertIn("previous_config_revision", response.json)
        self.sdk.logout.assert_called_once_with()
        self.sdk.password.side_effect = RuntimeError("private-password private-profile private-token")
        with self.assertLogs(level="WARNING") as logs:
            failed = self.client.post("/api/telegram/web/password", json={"id": ATTEMPT, "password": "private-password"})
        output = failed.get_data(as_text=True) + "\n".join(logs.output) + self.client.get("/data").get_data(as_text=True)
        self.assertEqual(failed.status_code, 503)
        self.assertNotIn("private-", output)
        self.manager.assert_not_called()


if __name__ == "__main__":
    unittest.main()
