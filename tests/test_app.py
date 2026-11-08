import contextlib
import copy
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
with contextlib.redirect_stdout(io.StringIO()):
    import run as web


class AppIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config_path = str(Path(directory.name) / "config.json")
        self.config = {
            "webui": {"username": "test", "password": "test-only"},
            "cookie": ["fake-cookie"], "tasklist": [], "crontab": "0 8 * * *",
            "discovery": {"savepath": "/追更"}, "auto_repair": True,
        }
        self.entry = {"id": "bangumi:42", "title": "测试动漫 第二季",
                      "search_title": "测试动漫 第二季", "year": 2024}
        self.calendar = {"days": [{"weekday": index, "anime": [], "tv": []}
                                  for index in range(1, 8)], "errors": []}
        self.calendar["days"][0]["anime"].append(self.entry)
        self.candidate = {"success": True, "candidate": {
            "taskname": "测试动漫 S02 (2024)", "shareurl": "https://pan.quark.cn/s/validshare?pwd=abcd",
            "stoken": "never-persist-this-token", "passcode": "abcd", "pwd_id": "validshare", "pdir_fid": 0,
        }, "error": ""}
        self.patch("CONFIG_PATH", self.config_path)
        self.patch("config_data", copy.deepcopy(self.config))
        self.patch("task_plugins_config_default", {"testplugin": {"enabled": False}})
        self.patch("scheduler")
        self.reload = self.patch("reload_tasks", return_value=True)
        self.get_calendar = self.patch("get_calendar", return_value=self.calendar)
        find_patcher = patch.object(web.Quark, "find_valid_resource", return_value=self.candidate)
        self.find = find_patcher.start()
        self.addCleanup(find_patcher.stop)
        # Any unexpected real HTTP request fails the test immediately.
        network_patcher = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network request"))
        network_patcher.start()
        self.addCleanup(network_patcher.stop)
        self.write_config()
        flask_config = patch.dict(web.app.config, TESTING=True)
        flask_config.start()
        self.addCleanup(flask_config.stop)
        self.client = web.app.test_client()
        self.login()

    def patch(self, name, *args, **kwargs):
        patcher = patch.object(web, name, *args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def write_config(self):
        web.Config.write_json(self.config_path, self.config)

    def saved_config(self):
        return web.Config.read_json(self.config_path)

    def login(self):
        with self.client.session_transaction() as session:
            session["token"] = web.get_login_token()

    def subscribe(self, entry_id="bangumi:42"):
        return self.client.post("/api/discovery/subscribe", json={"id": entry_id})

    def test_init_sets_local_search_for_new_config_and_preserves_existing_source(self):
        Path(self.config_path).unlink()
        with patch.dict(os.environ, {"PANSOU_SERVER": "http://pansou:8888"}), \
                patch.object(web.Config, "load_plugins", return_value=({}, {}, {})):
            web.init()
            current = self.saved_config()
            self.assertEqual(current["source"]["pansou"]["server"], "http://pansou:8888")
            self.assertEqual(current["tasklist"], [])
            current["source"]["pansou"]["server"] = "https://custom.example"
            web.Config.write_json(self.config_path, current)
            web.init()
            self.assertEqual(self.saved_config()["source"]["pansou"]["server"], "https://custom.example")

    def test_update_rejects_malformed_new_settings_without_writing(self):
        for payload in ({"plugins": None}, {"plugins": {"aria2": None}},
                        {"discovery": {"savepath": "/追更", "auto_download": "false"}},
                        {"crontab": "invalid"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post("/update", json=payload).status_code, 400)
                self.assertEqual(self.saved_config(), self.config)

    def test_new_routes_and_search_require_login(self):
        with self.client.session_transaction() as session:
            session.clear()
        for route, method in (("/task_suggestions?q=测试", "get"),
                              ("/api/discovery", "get"), ("/api/discovery/subscribe", "post")):
            with self.subTest(route=route):
                response = getattr(self.client, method)(route)
                self.assertFalse(response.json["success"])
                self.assertIn("未登录", response.json["message"])
        self.find.assert_not_called()
        self.get_calendar.assert_not_called()

    def test_search_source_errors_remain_visible_and_use_current_config(self):
        self.config["source"] = {"pansou": {"enable": False}}
        self.write_config()
        result = {"success": False, "data": [], "errors": [{"source": "pansou", "message": "连接超时"}], "message": "资源搜索暂不可用"}
        with patch.object(web, "search_resources", return_value=result) as search:
            response = self.client.get("/task_suggestions", query_string={"q": "测试动漫", "d": "1"})
        self.assertEqual(response.json, result)
        search.assert_called_once_with("测试动漫", self.config, refresh=True)

    def test_discovery_returns_seven_days_and_source_errors(self):
        self.calendar["errors"] = [{"source": "tv", "message": "上游暂不可用"}]
        response = self.client.get("/api/discovery")
        self.assertTrue(response.json["success"])
        self.assertEqual(len(response.json["data"]["days"]), 7)
        self.assertEqual(response.json["data"]["days"][0]["anime"][0]["id"], "bangumi:42")
        self.assertEqual(response.json["data"]["errors"], self.calendar["errors"])

    def test_subscription_rejects_malformed_and_unknown_ids(self):
        for value in (None, 42, ["bangumi:42"], {"id": "bangumi:42"}):
            with self.subTest(value=value):
                self.assertEqual(self.subscribe(value).status_code, 400)
        self.assertEqual(self.subscribe("bangumi:missing").status_code, 404)
        self.assertEqual(self.client.post("/api/discovery/subscribe", json=[]).status_code, 400)
        self.find.assert_not_called()
        self.assertEqual(self.saved_config()["tasklist"], [])

    def test_missing_or_placeholder_cookie_is_rejected_without_search(self):
        for cookie in ([], "", None, [""], ["Your Cookie"]):
            with self.subTest(cookie=cookie):
                self.config["cookie"] = cookie
                self.write_config()
                response = self.subscribe()
                self.assertEqual(response.status_code, 400)
                self.assertIn("Cookie", response.json["message"])
        self.find.assert_not_called()

    def test_no_valid_candidate_keeps_config_untouched(self):
        self.find.return_value = {"success": False, "candidate": None, "error": "没有匹配的有效分享"}
        response = self.subscribe()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json["message"], "没有匹配的有效分享")
        self.assertEqual(self.saved_config(), self.config)

    def test_subscription_persists_only_task_fields_and_keeps_credentials_private(self):
        response = self.subscribe()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["success"])
        task = self.saved_config()["tasklist"][0]
        self.assertEqual(task, response.json["data"])
        self.assertEqual(task["shareurl"], self.candidate["candidate"]["shareurl"])
        self.assertEqual(task["taskname"], "测试动漫 第二季 (2024)")
        self.assertEqual(task["savepath"], "/追更/测试动漫 第二季 (2024)")
        self.assertEqual(task["runweek"], list(range(1, 8)))
        self.assertEqual(task["addition"], {"testplugin": {"enabled": False}})
        self.assertNotIn("auto_repair", task)
        for key in ("stoken", "passcode", "pwd_id", "pdir_fid", "cookie"):
            self.assertNotIn(key, task)
        self.assertNotIn("never-persist-this-token", Path(self.config_path).read_text(encoding="utf-8"))
        self.assertNotIn("fake-cookie", response.get_data(as_text=True))
        self.assertEqual(self.find.call_count, 1)
        self.assertEqual(self.find.call_args.args[0]["taskname"], "测试动漫 第二季 (2024)")

    def test_duplicate_subscription_does_not_search_or_append(self):
        self.assertTrue(self.subscribe().json["success"])
        self.find.reset_mock()
        response = self.subscribe()
        self.assertTrue(response.json["existing"])
        self.assertEqual(len(self.saved_config()["tasklist"]), 1)
        self.find.assert_not_called()

    def test_same_show_from_another_catalog_is_already_subscribed(self):
        for resource_id in ("tvmaze:99", None):
            with self.subTest(resource_id=resource_id):
                existing = {"taskname": "测试动漫 S02 [2024] [1080p]", "resource_id": resource_id,
                            "shareurl": "https://pan.quark.cn/s/already", "savepath": "/已有"}
                self.config["tasklist"] = [existing]
                self.write_config()
                response = self.subscribe()
                self.assertTrue(response.json["existing"])
                self.assertEqual(response.json["data"], existing)
                self.assertEqual(self.saved_config()["tasklist"], [existing])
        self.find.assert_not_called()

    def test_different_seasons_or_uncertain_years_are_not_merged_across_catalogs(self):
        for stored_title, year in (("测试动漫 第三季 (2024)", 2024),
                                   ("测试动漫 第二季 (2023)", 2024),
                                   ("测试动漫 第二季", 2024),
                                   ("测试动漫 第二季 (2024)", None)):
            with self.subTest(stored_title=stored_title, year=year):
                self.entry["year"] = year
                self.config["tasklist"] = [{"taskname": stored_title, "resource_id": "tvmaze:99",
                                            "shareurl": "https://pan.quark.cn/s/other", "savepath": "/其它"}]
                self.write_config()
                response = self.subscribe()
                self.assertFalse(response.json["existing"])
                self.assertEqual(len(self.saved_config()["tasklist"]), 2)
        self.assertEqual(self.find.call_count, 4)

    def test_invalid_savepath_is_rejected_without_writing(self):
        for path in ("relative/path", "/safe/../escape", "/safe/./folder", "/safe\\folder", "/safe\nfolder", None, 123):
            with self.subTest(path=path):
                self.config["discovery"]["savepath"] = path
                self.write_config()
                response = self.subscribe()
                self.assertEqual(response.status_code, 400)
                self.assertIn("路径", response.json["message"])
                self.assertEqual(self.saved_config()["tasklist"], [])
        self.find.assert_not_called()

    def test_malformed_discovery_config_is_rejected_without_search(self):
        for settings in (None, [], "not-a-settings-object"):
            with self.subTest(settings=settings):
                self.config["discovery"] = settings
                self.write_config()
                self.assertEqual(self.subscribe().status_code, 400)
        self.find.assert_not_called()

    def test_success_without_candidate_is_not_saved(self):
        self.find.return_value = {"success": True, "candidate": None, "error": ""}
        self.assertEqual(self.subscribe().status_code, 422)
        self.assertEqual(self.saved_config()["tasklist"], [])

    def test_new_subscription_inherits_disabled_global_auto_repair(self):
        self.config["auto_repair"] = False
        self.write_config()
        self.assertTrue(self.subscribe().json["success"])
        saved = self.saved_config()
        self.assertFalse(saved["auto_repair"])
        self.assertNotIn("auto_repair", saved["tasklist"][0])

    def test_auto_download_requires_configured_and_enabled_aria2(self):
        self.config["discovery"]["auto_download"] = True
        for aria2 in ({}, {"host_port": "http://127.0.0.1:6800"},
                      {"host_port": "http://127.0.0.1:6800", "secret": "fake-rpc-secret"}):
            with self.subTest(aria2=aria2):
                self.config["plugins"] = {"aria2": aria2}
                self.write_config()
                response = self.subscribe()
                self.assertEqual(response.status_code, 400)
                self.assertIn("Aria2", response.json["message"])
        self.config["plugins"]["aria2"]["dir"] = "/downloads"
        self.write_config()
        with patch.object(web, "PLUGIN_FLAGS", "-aria2"):
            self.assertEqual(self.subscribe().status_code, 400)
        self.find.assert_not_called()
        self.assertEqual(self.saved_config()["tasklist"], [])

    def test_auto_download_sets_task_plugin_flags_without_rpc_secrets(self):
        self.config["discovery"]["auto_download"] = True
        self.config["plugins"] = {"aria2": {
            "host_port": "http://127.0.0.1:6800", "secret": "fake-rpc-secret", "dir": "/downloads",
        }}
        web.task_plugins_config_default["aria2"] = {"auto_download": False, "download_subdir": False}
        self.write_config()
        with patch.object(web, "PLUGIN_FLAGS", ""):
            response = self.subscribe()
        self.assertTrue(response.json["success"])
        self.assertEqual(response.json["data"]["addition"]["aria2"], {"auto_download": True, "download_subdir": True})
        self.assertEqual(web.task_plugins_config_default["aria2"], {"auto_download": False, "download_subdir": False})
        self.assertNotIn("fake-rpc-secret", response.get_data(as_text=True))
        self.assertEqual(self.saved_config()["tasklist"][0], response.json["data"])

    def test_subscribe_preserves_concurrent_config_changes(self):
        other = {"taskname": "另外一部", "shareurl": "https://pan.quark.cn/s/other", "savepath": "/其它"}

        def concurrent_update(task, config):
            latest = self.saved_config()
            latest["tasklist"].append(other)
            latest["auto_repair"] = False
            web.Config.write_json(self.config_path, latest)
            return self.candidate

        self.find.side_effect = concurrent_update
        self.assertTrue(self.subscribe().json["success"])
        saved = self.saved_config()
        self.assertFalse(saved["auto_repair"])
        self.assertEqual(saved["tasklist"][0], other)
        self.assertEqual(len(saved["tasklist"]), 2)

    def test_simultaneous_subscription_is_not_duplicated_after_search(self):
        other = {"taskname": "测试动漫 第二季", "shareurl": "https://pan.quark.cn/s/already", "savepath": "/已有", "resource_id": self.entry["id"]}

        def concurrent_subscribe(task, config):
            latest = self.saved_config()
            latest["tasklist"].append(other)
            web.Config.write_json(self.config_path, latest)
            return self.candidate

        self.find.side_effect = concurrent_subscribe
        response = self.subscribe()
        self.assertTrue(response.json["existing"])
        self.assertEqual(response.json["data"], other)
        self.assertEqual(self.saved_config()["tasklist"], [other])

    def test_title_characters_cannot_create_extra_directories(self):
        self.entry["search_title"] = '测试/动漫:第"二"季'
        self.config["discovery"]["savepath"] = "/追更/"
        self.write_config()
        response = self.subscribe()
        self.assertTrue(response.json["success"])
        self.assertEqual(response.json["data"]["savepath"], "/追更/测试_动漫_第_二_季 (2024)")

    def test_stale_revision_cannot_overwrite_a_repaired_share(self):
        original = {"taskname": "测试", "shareurl": "https://pan.quark.cn/s/old", "savepath": "/测试"}
        self.config["tasklist"] = [original]
        self.write_config()
        page = self.client.get("/data").json["data"]
        self.assertNotIn("webui", page)
        self.assertIn("config_revision", page)
        self.config["tasklist"][0] = {**original, "shareurl": "https://pan.quark.cn/s/repaired"}
        self.write_config()
        response = self.client.post("/update", json=page)
        self.assertEqual(response.status_code, 409)
        self.assertIn("刷新", response.json["message"])
        self.assertEqual(self.saved_config()["tasklist"][0]["shareurl"], "https://pan.quark.cn/s/repaired")
        self.reload.assert_not_called()

    def test_current_revision_updates_allowed_fields_without_saving_response_metadata(self):
        page = self.client.get("/data").json["data"]
        page.update(auto_repair=False, webui={"username": "injected", "password": "injected"})
        response = self.client.post("/update", json=page)
        self.assertTrue(response.json["success"])
        saved = self.saved_config()
        self.assertFalse(saved["auto_repair"])
        self.assertEqual(saved["webui"], self.config["webui"])
        for key in ("api_token", "config_revision", "task_plugins_config_default"):
            self.assertNotIn(key, saved)
        self.assertEqual(response.json["config_revision"], web.config_revision(saved))
        self.reload.assert_called_once()


if __name__ == "__main__":
    unittest.main()
