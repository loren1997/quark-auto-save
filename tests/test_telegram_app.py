import contextlib
import copy
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
with contextlib.redirect_stdout(io.StringIO()):
    import run as web


class LegacyTelegramAppTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config_path = str(Path(directory.name) / "config.json")
        self.config = {"webui": {"username": "test", "password": "test-only"}, "crontab": "0 8 * * *",
                       "tasklist": [], "source": {"telegram": {"enable": True, "auto_join": True},
                       "channelsearch": {"telegram_channels": ["keep_channel"], "telegram_folders": ["keep_folder"]}}}
        for name, value in (("CONFIG_PATH", self.config_path), ("config_data", copy.deepcopy(self.config))):
            patcher = patch.object(web, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        web.Config.write_json(self.config_path, self.config)
        health_path = Path(web.Config.search_health_path(self.config_path))
        self.legacy_file = health_path.with_name("." + health_path.name + ".telegram.json")
        self.secret = '{"session":"legacy-test-session","api_hash":"legacy-test-hash"}'
        self.legacy_file.write_text(self.secret, encoding="utf-8")
        self.client = web.app.test_client()
        with self.client.session_transaction() as session:
            session["token"] = web.get_login_token()

    def test_retired_routes_still_require_login(self):
        anonymous = web.app.test_client()
        for path, method in (("status", "get"), ("code", "post"), ("signin", "post"), ("qr", "post"),
                             ("qr/poll", "post"), ("session", "delete")):
            response = getattr(anonymous, method)("/api/telegram/" + path, json={})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_legacy_status_never_reads_old_session_or_starts_browser(self):
        with patch.object(web, "TelegramWeb") as browser, patch.object(web.Config, "read_json") as read:
            response = self.client.get("/api/telegram/status")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["data"]["available"])
        self.assertFalse(response.json["data"]["authorized"])
        self.assertIn("Telegram Web", response.json["data"]["reason"])
        self.assertNotIn("legacy-test", response.get_data(as_text=True))
        browser.assert_not_called()
        read.assert_not_called()

    def test_old_login_and_logout_return_gone_without_touching_credentials(self):
        with patch.object(web, "TelegramWeb") as browser:
            for path, method in (("code", "post"), ("signin", "post"), ("qr", "post"),
                                 ("qr/poll", "post"), ("session", "delete")):
                response = getattr(self.client, method)("/api/telegram/" + path, json={})
                self.assertEqual(response.status_code, 410)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        browser.assert_not_called()
        self.assertEqual(self.legacy_file.read_text(encoding="utf-8"), self.secret)
        self.assertEqual(web.Config.read_json(self.config_path), self.config)

    def test_init_disables_old_auto_join_and_preserves_old_credentials(self):
        with patch.object(web.Config, "load_plugins", return_value=({}, {}, {})):
            web.init()
        current = web.Config.read_json(self.config_path)
        self.assertFalse(current["source"]["telegram"]["auto_join"])
        self.assertTrue(current["source"]["telegram"]["enable"])
        self.assertFalse(current["source"]["telegram_web"]["enable"])
        self.assertEqual(current["source"]["channelsearch"], self.config["source"]["channelsearch"])
        self.assertEqual(self.legacy_file.read_text(encoding="utf-8"), self.secret)
        current["source"]["telegram_web"]["enable"] = True
        web.Config.write_json(self.config_path, current)
        with patch.object(web.Config, "load_plugins", return_value=({}, {}, {})):
            web.init()
        self.assertTrue(web.Config.read_json(self.config_path)["source"]["telegram_web"]["enable"])


if __name__ == "__main__":
    unittest.main()
