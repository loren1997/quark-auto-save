import contextlib
import copy
import base64
import io
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
with contextlib.redirect_stdout(io.StringIO()):
    import run as web


class TelegramAppTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config_path = str(Path(directory.name) / "config.json")
        self.config = {
            "webui": {"username": "test", "password": "web-only"},
            "cookie": ["quark-cookie"], "crontab": "0 8 * * *", "tasklist": [],
            "source": {
                "telegram": {"enable": False},
                "channelsearch": {"telegram_channels": ["custom_channel"], "telegram_folders": ["custom_folder"]},
            },
        }
        self.patch("CONFIG_PATH", self.config_path)
        self.patch("config_data", copy.deepcopy(self.config))
        self.account_class = self.patch("TelegramAccount")
        self.account = self.account_class.return_value
        self.account.status.return_value = self.state("disconnected")
        self.account.request_code.return_value = self.state("waiting_code")
        self.account.sign_in.return_value = self.state("authorized", authorized=True)
        self.account.logout.return_value = self.state("disconnected")
        self.qr_id = "0123456789abcdef0123456789abcdef"
        self.qr_url = "tg://login?token=MockLoginToken0123456789"
        self.account.request_qr.return_value = self.state(
            "waiting_qr", ok=True, credentials_saved=True, qr_id=self.qr_id,
            qr_url=self.qr_url, expires_at=1900000000)
        self.account.poll_qr.return_value = self.account.request_qr.return_value
        self.manager = self.patch("get_run_manager")
        self.manager.return_value.list.return_value = []
        self.reload = self.patch("reload_tasks")
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network request"))
        network.start()
        self.addCleanup(network.stop)
        web.Config.write_json(self.config_path, self.config)
        self.client = web.app.test_client()
        with self.client.session_transaction() as session:
            session["token"] = web.get_login_token()
        self.credentials = {"api_id": 12345, "api_hash": "abcdef0123456789abcdef0123456789", "phone": "+8613800138000"}

    def patch(self, name, *args, **kwargs):
        patcher = patch.object(web, name, *args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    @staticmethod
    def state(state, authorized=False, **extra):
        return {"available": True, "authorized": authorized, "state": state, "reason": "",
                "retry_after": 0, **extra}

    def saved(self):
        return web.Config.read_json(self.config_path)

    def test_all_routes_authenticate_before_parsing_or_calling_sdk(self):
        client = web.app.test_client()
        for path, method in (("status", "get"), ("code", "post"), ("signin", "post"), ("session", "delete"),
                             ("qr", "post"), ("qr/poll", "post")):
            with self.subTest(path=path):
                response = getattr(client, method)("/api/telegram/" + path, data="not-json")
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.account_class.assert_not_called()

    def test_status_is_read_only_and_returns_only_state_fields(self):
        self.config["source"]["telegram"]["enable"] = True
        web.Config.write_json(self.config_path, self.config)
        self.account.status.return_value = self.state("authorized", authorized=True, api_hash="private-hash",
                                                    phone="private-phone", session="private-session", code="private-code",
                                                    credentials_saved=True, qr_url=self.qr_url, qr_id=self.qr_id)
        response = self.client.get("/api/telegram/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertTrue(response.json["success"])
        self.assertEqual(set(response.json["data"]),
                         {"available", "authorized", "state", "reason", "retry_after", "credentials_saved", "enabled"})
        self.assertTrue(response.json["data"]["credentials_saved"])
        self.assertNotIn("qr", response.json)
        self.assertNotIn(self.qr_id, response.get_data(as_text=True))
        self.assertNotIn(self.qr_url, response.get_data(as_text=True))
        self.assertTrue(response.json["data"]["enabled"])
        self.assertEqual(self.saved(), self.config)
        self.account.request_code.assert_not_called()
        self.account.sign_in.assert_not_called()
        self.account_class.assert_called_once_with(web.Config.search_health_path(self.config_path))
        self.manager.assert_not_called()

    def test_code_parameters_are_validated_before_sdk(self):
        invalid = [[], None, {}, {**self.credentials, "session": "injected"},
                   {**self.credentials, "api_id": True}, {**self.credentials, "api_id": "12345"},
                   {**self.credentials, "api_id": 0}, {**self.credentials, "api_id": 2147483648},
                   {**self.credentials, "api_hash": "short"}, {**self.credentials, "phone": "13800138000"},
                   {**self.credentials, "phone": "+1"}, {**self.credentials, "phone": "+1234567890123456"},
                   *[{**self.credentials, "proxy": proxy} for proxy in
                     (True, {}, "file:///tmp/session", "http://host:99999", "http://host/path",
                      "http://host?secret=abc", "http://host#secret", "http://host\n", "http://" + "x" * 2048)]]
        for payload in invalid:
            with self.subTest(payload=payload):
                response = self.client.post("/api/telegram/code", json=payload)
                self.assertEqual(response.status_code, 400)
        self.account_class.assert_not_called()

    def test_signin_requires_exactly_one_bounded_code_or_password(self):
        for payload in ({}, {"code": 12345}, {"code": "12"}, {"code": "123456789"},
                        {"code": "12345", "password": "secret"}, {"password": ""},
                        {"password": "x" * 1025}, {"password": None}, {"code": "12345", "extra": True}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post("/api/telegram/signin", json=payload).status_code, 400)
        self.assertEqual(self.client.delete("/api/telegram/session", json={"phone": "private"}).status_code, 400)
        self.account_class.assert_not_called()

    def test_mutations_require_same_origin_json_and_limit_body(self):
        for path, method, payload in (("code", "post", self.credentials), ("signin", "post", {"code": "12345"}),
                                     ("session", "delete", {}), ("qr", "post", {}),
                                     ("qr/poll", "post", {"id": self.qr_id})):
            send = getattr(self.client, method)
            with self.subTest(path=path):
                self.assertEqual(send("/api/telegram/" + path, data="{}").status_code, 400)
                self.assertEqual(send("/api/telegram/" + path, json=payload,
                                      headers={"Origin": "https://other.example"}).status_code, 403)
                self.assertEqual(send("/api/telegram/" + path, json=payload,
                                      headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403)
                self.assertEqual(send("/api/telegram/" + path, data="{" + " " * 8192,
                                      content_type="application/json").status_code, 413)
                self.assertEqual(send("/api/telegram/" + path, data="{broken",
                                      content_type="application/json").status_code, 400)
        self.account_class.assert_not_called()

    def test_qr_requires_optional_paired_credentials_and_poll_requires_nonce(self):
        invalid = ({"api_id": 12345}, {"api_hash": "a" * 32}, {"api_id": True, "api_hash": "a" * 32},
                   {"phone": "+15550001111"}, {"qr_url": "https://other.example"}, {"proxy": "file:///tmp/token"},
                   {"api_id": 12345, "api_hash": "a" * 31}, {"id": self.qr_id})
        for payload in invalid:
            self.assertEqual(self.client.post("/api/telegram/qr", json=payload).status_code, 400)
        for payload in ({}, {"id": "short"}, {"id": 123}, {"id": "a" * 129}, {"id": "x/" + "a" * 32},
                        {"id": self.qr_id, "api_hash": "a" * 32}, {"qr_id": self.qr_id}):
            self.assertEqual(self.client.post("/api/telegram/qr/poll", json=payload).status_code, 400)
        self.account_class.assert_not_called()

    def test_qr_create_reuses_credentials_and_renders_only_local_svg(self):
        response = self.client.post("/api/telegram/qr", json={})
        self.assertEqual(response.status_code, 200)
        self.account.request_qr.assert_called_once_with()
        self.assertTrue(response.json["success"])
        self.assertTrue(response.json["data"]["credentials_saved"])
        qr = response.json["qr"]
        self.assertEqual(set(qr), {"id", "image", "expires_at"})
        self.assertEqual(qr["id"], self.qr_id)
        prefix, encoded = qr["image"].split(",", 1)
        self.assertEqual(prefix, "data:image/svg+xml;base64")
        svg = base64.b64decode(encoded)
        self.assertEqual(ET.fromstring(svg).tag, "{http://www.w3.org/2000/svg}svg")
        self.assertNotIn(self.qr_url.encode(), svg)
        self.assertNotIn(self.qr_url, response.get_data(as_text=True))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.saved(), self.config)
        self.account.request_code.assert_not_called()
        self.manager.assert_not_called()

    def test_qr_first_credentials_stay_out_of_config_and_poll_can_rotate_image(self):
        credentials = {key: self.credentials[key] for key in ("api_id", "api_hash")}
        initial = self.client.post("/api/telegram/qr", json=credentials).json
        self.account.request_qr.assert_called_once_with(**credentials)
        self.account.poll_qr.return_value = self.state(
            "waiting_qr", ok=True, credentials_saved=True, qr_id=self.qr_id,
            qr_url="tg://login?token=RotatedMockToken0123456789", expires_at=1900000030)
        polled = self.client.post("/api/telegram/qr/poll", json={"id": self.qr_id})
        self.account.poll_qr.assert_called_once_with(qr_id=self.qr_id)
        self.assertEqual(polled.json["qr"]["id"], initial["qr"]["id"])
        self.assertNotEqual(polled.json["qr"]["image"], initial["qr"]["image"])
        self.assertEqual(polled.json["qr"]["expires_at"], 1900000030)
        self.assertNotIn("config_revision", polled.json)
        self.assertEqual(self.saved(), self.config)

    def test_qr_background_start_returns_nonce_before_token_is_ready(self):
        self.account.request_qr.return_value = self.state(
            "waiting_qr", ok=True, credentials_saved=True, qr_id=self.qr_id, qr_url="", expires_at=0)
        started = self.client.post("/api/telegram/qr", json={})
        self.assertEqual(started.status_code, 200)
        self.assertEqual(started.json["qr"], {"id": self.qr_id, "image": "", "expires_at": 0})
        polled = self.client.post("/api/telegram/qr/poll", json={"id": started.json["qr"]["id"]})
        self.assertEqual(polled.status_code, 200)
        self.assertTrue(polled.json["qr"]["image"].startswith("data:image/svg+xml;base64,"))
        self.assertEqual(self.saved(), self.config)

    def test_qr_poll_authorization_and_already_authorized_create_use_revision_guard(self):
        for action, payload, operation in (("qr/poll", {"id": self.qr_id}, self.account.poll_qr),
                                           ("qr", {}, self.account.request_qr)):
            with self.subTest(action=action):
                web.Config.write_json(self.config_path, self.config)
                original_revision = web.config_revision(self.config)
                operation.return_value = self.state("authorized", authorized=True, ok=True, credentials_saved=True)
                response = self.client.post("/api/telegram/" + action, json=payload)
                self.assertTrue(response.json["data"]["enabled"])
                self.assertEqual(response.json["previous_config_revision"], original_revision)
                self.assertEqual(response.json["config_revision"], web.config_revision(self.saved()))
                self.assertNotIn("qr", response.json)
                self.assertTrue(self.saved()["source"]["telegram"]["enable"])
        self.manager.assert_not_called()

    def test_qr_poll_password_challenge_and_expired_nonce_never_enable_or_return_new_code(self):
        for result in (self.state("waiting_password", ok=True),
                       self.state("waiting_qr", ok=False, reason="二维码已失效")):
            self.account.poll_qr.return_value = result
            response = self.client.post("/api/telegram/qr/poll", json={"id": self.qr_id})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["success"], result["ok"])
            self.assertEqual(response.json["data"]["state"], result["state"])
            self.assertNotIn("qr", response.json)
            self.assertNotIn("config_revision", response.json)
            self.assertEqual(self.saved(), self.config)

    def test_qr_missing_renderer_or_malformed_sdk_url_is_sanitized(self):
        with patch.object(web, "qrcode", None):
            response = self.client.post("/api/telegram/qr", json={})
            self.assertEqual(response.status_code, 503)
        self.account_class.assert_not_called()
        self.account.request_qr.return_value = self.state(
            "waiting_qr", ok=True, qr_id=self.qr_id, qr_url="https://private-token.example", expires_at=1900000000)
        with self.assertLogs(level="WARNING") as logs:
            response = self.client.post("/api/telegram/qr", json={})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private-token", response.get_data(as_text=True) + "\n".join(logs.output))
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_request_code_does_not_enable_or_persist_credentials(self):
        payload = {**self.credentials, "proxy": "socks5://proxy-user:proxy-password@192.168.1.2:1080"}
        response = self.client.post("/api/telegram/code", json=payload, headers={"Origin": "http://localhost"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["data"]["state"], "waiting_code")
        self.assertFalse(response.json["data"]["enabled"])
        self.account.request_code.assert_called_once_with(**payload)
        self.assertEqual(self.saved(), self.config)
        self.assertNotIn("config_revision", response.json)
        self.manager.assert_not_called()

    def test_empty_proxy_becomes_explicit_direct_connection(self):
        response = self.client.post("/api/telegram/code", json={**self.credentials, "proxy": ""})
        self.assertEqual(response.status_code, 200)
        self.account.request_code.assert_called_once_with(**self.credentials, proxy=None)

    def test_password_challenge_and_failed_code_leave_settings_unchanged(self):
        for result in (self.state("waiting_password"), self.state("waiting_code", ok=False, reason="验证码错误"),
                       self.state("authorized", authorized=True, ok=False, reason="本次登录未完成")):
            self.account.sign_in.return_value = result
            response = self.client.post("/api/telegram/signin", json={"code": "12345"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["success"], result.get("ok", True))
            self.assertEqual(response.json["data"]["reason"], result["reason"])
            self.assertNotIn("config_revision", response.json)
            self.assertEqual(self.saved(), self.config)

    def test_successful_signin_only_enables_telegram_in_latest_config(self):
        current = copy.deepcopy(self.config)
        current["tasklist"] = [{"taskname": "new task saved by worker"}]
        current["source"]["channelsearch"]["telegram_channels"].append("second_channel")
        web.Config.write_json(self.config_path, current)
        previous_revision = web.config_revision(current)
        response = self.client.post("/api/telegram/signin", json={"password": "two-factor-secret"})
        self.assertTrue(response.json["success"])
        self.assertTrue(response.json["data"]["enabled"])
        current["source"]["telegram"]["enable"] = True
        self.assertEqual(self.saved(), current)
        self.assertEqual(response.json["config_revision"], web.config_revision(current))
        self.assertEqual(response.json["previous_config_revision"], previous_revision)
        self.assertEqual(web.config_data, current)
        self.account.sign_in.assert_called_once_with(password="two-factor-secret")
        self.reload.assert_not_called()
        self.manager.assert_not_called()

    def test_logout_local_revocation_disables_only_telegram(self):
        self.config["source"]["telegram"]["enable"] = True
        web.Config.write_json(self.config_path, self.config)
        previous_revision = web.config_revision(self.config)
        self.account.logout.return_value = self.state("disconnected", ok=False, reason="本地已注销；远端撤销未确认")
        response = self.client.delete("/api/telegram/session", json={})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["success"])
        self.assertFalse(response.json["data"]["enabled"])
        self.assertIn("远端撤销未确认", response.json["data"]["reason"])
        self.config["source"]["telegram"]["enable"] = False
        self.assertEqual(self.saved(), self.config)
        self.assertEqual(response.json["config_revision"], web.config_revision(self.config))
        self.assertEqual(response.json["previous_config_revision"], previous_revision)
        self.account.logout.assert_called_once_with()
        self.manager.assert_not_called()

    def test_account_revision_requires_matching_form_snapshot_before_advancing(self):
        for action, method, payload, enabled in (("signin", "post", {"code": "12345"}, True),
                                                 ("session", "delete", {}, False)):
            with self.subTest(action=action):
                original = copy.deepcopy(self.config)
                original["source"]["telegram"]["enable"] = not enabled
                web.Config.write_json(self.config_path, original)
                form = self.client.get("/data").json["data"]
                latest = copy.deepcopy(original)
                latest["tasklist"].append({"taskname": "后台刚新增的任务"})
                web.Config.write_json(self.config_path, latest)
                result = getattr(self.client, method)("/api/telegram/" + action, json=payload).json
                self.assertEqual(result["previous_config_revision"], web.config_revision(latest))
                self.assertNotEqual(form["config_revision"], result["previous_config_revision"])
                # The UI advances only a matching base revision, preserving stale-form conflict detection.
                if form["config_revision"] == result["previous_config_revision"]:
                    form["config_revision"] = result["config_revision"]
                form["source"]["telegram"]["enable"] = result["data"]["enabled"]
                rejected = self.client.post("/update", json=form)
                self.assertEqual(rejected.status_code, 409)
                latest["source"]["telegram"]["enable"] = enabled
                self.assertEqual(self.saved(), latest)

    def test_logout_with_missing_dependency_still_disables_after_local_erasure(self):
        self.config["source"]["telegram"]["enable"] = True
        web.Config.write_json(self.config_path, self.config)
        self.account.logout.return_value = self.state("unavailable", available=False, ok=True)
        response = self.client.delete("/api/telegram/session", json={})
        self.assertTrue(response.json["success"])
        self.assertEqual(response.json["data"]["state"], "unavailable")
        self.assertFalse(response.json["data"]["enabled"])
        self.assertFalse(self.saved()["source"]["telegram"]["enable"])
        self.assertIn("previous_config_revision", response.json)

    def test_sdk_absent_and_exceptions_are_sanitized(self):
        self.config["source"]["telegram"]["enable"] = True
        web.Config.write_json(self.config_path, self.config)
        with patch.object(web, "TelegramAccount", None):
            response = self.client.get("/api/telegram/status")
            self.assertEqual(response.status_code, 503)
            self.assertFalse(response.json["data"]["available"])
            self.assertTrue(response.json["data"]["enabled"])
        self.account.request_code.side_effect = RuntimeError("private-session private-api-hash +8613800138000")
        with self.assertLogs(level="WARNING") as logs:
            response = self.client.post("/api/telegram/code", json=self.credentials)
        self.assertEqual(response.status_code, 503)
        output = response.get_data(as_text=True) + "\n".join(logs.output)
        for secret in ("private-session", "private-api-hash", self.credentials["phone"]):
            self.assertNotIn(secret, output)
        self.assertEqual(self.saved(), self.config)

    def test_account_secrets_never_enter_config_responses_or_run_history(self):
        secret_code, secret_password = "98765", "unique-2fa-password"
        self.client.post("/api/telegram/code", json=self.credentials)
        self.account.sign_in.return_value = self.state("waiting_password")
        self.client.post("/api/telegram/signin", json={"code": secret_code})
        self.account.sign_in.return_value = self.state("authorized", authorized=True, session="unique-session-value")
        result = self.client.post("/api/telegram/signin", json={"password": secret_password})
        self.manager.assert_not_called()
        output = (result.get_data(as_text=True) + self.client.get("/data").get_data(as_text=True)
                  + self.client.get("/api/runs").get_data(as_text=True)
                  + Path(self.config_path).read_text(encoding="utf-8"))
        for secret in (self.credentials["api_hash"], self.credentials["phone"], secret_code,
                       secret_password, "unique-session-value"):
            self.assertNotIn(secret, output)
        self.manager.return_value.start.assert_not_called()

    def test_init_adds_opt_in_defaults_and_preserves_existing_selection(self):
        del self.config["source"]["telegram"]
        del self.config["source"]["channelsearch"]["telegram_folders"]
        web.Config.write_json(self.config_path, self.config)
        with patch.object(web.Config, "load_plugins", return_value=({}, {}, {})):
            web.init()
            self.assertEqual(self.saved()["source"]["telegram"], {"enable": False, "auto_join": False})
            self.assertEqual(self.saved()["source"]["channelsearch"]["telegram_folders"], [])
            self.assertEqual(self.saved()["source"]["channelsearch"]["telegram_channels"], ["custom_channel"])
            saved = self.saved()
            saved["source"]["telegram"]["enable"] = True
            saved["source"]["telegram"]["auto_join"] = True
            saved["source"]["channelsearch"]["telegram_folders"] = ["keep_this_folder"]
            web.Config.write_json(self.config_path, saved)
            web.init()
        self.assertTrue(self.saved()["source"]["telegram"]["enable"])
        self.assertTrue(self.saved()["source"]["telegram"]["auto_join"])
        self.assertEqual(self.saved()["source"]["channelsearch"]["telegram_folders"], ["keep_this_folder"])
        self.account_class.assert_not_called()


if __name__ == "__main__":
    unittest.main()
