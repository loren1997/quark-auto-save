import contextlib
import copy
import io
import os
from pathlib import Path
import re
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
            "discovery": {"savepath": "/cartoon"}, "auto_repair": True,
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
        self.get_search_aliases = self.patch("get_search_aliases", return_value=[])
        self.real_find = web.Quark.find_valid_resource
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
            self.assertEqual(current["discovery"]["savepath"], "/cartoon")
            self.assertTrue(current["source"]["websearch"]["enable"])
            current["source"]["pansou"]["server"] = "https://custom.example"
            current["source"]["websearch"]["enable"] = False
            web.Config.write_json(self.config_path, current)
            web.init()
            self.assertEqual(self.saved_config()["source"]["pansou"]["server"], "https://custom.example")
            self.assertFalse(self.saved_config()["source"]["websearch"]["enable"])

    def test_update_rejects_malformed_new_settings_without_writing(self):
        for payload in ({"plugins": None}, {"plugins": {"aria2": None}},
                        {"discovery": {"savepath": "/追更", "auto_download": "false"}},
                        {"crontab": "invalid"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post("/update", json=payload).status_code, 400)
                self.assertEqual(self.saved_config(), self.config)

    def test_startup_saves_factory_path_migration_and_preserves_custom_paths(self):
        legacy = {"taskname": "测试动漫 第二季 (2024)", "resource_id": "bangumi:42",
                  "shareurl": "https://pan.quark.cn/s/old", "savepath": "/追更/测试动漫 第二季 (2024)",
                  "pattern": r"(?i).*\.(mp4|mkv|mov|m4v|avi|ts)$", "replace": "",
                  "addition": {"aria2": {"save_path": ""}}}
        manual = {"taskname": "手动任务", "shareurl": "https://pan.quark.cn/s/manual", "savepath": "/追更/手动"}
        custom = {"taskname": "自定义", "resource_id": "bangumi:43", "shareurl": "https://pan.quark.cn/s/custom",
                  "savepath": "/私人目录", "pattern": ".+", "replace": "自定义名称.mp4",
                  "addition": {"aria2": {"save_path": "自定义下载"}}}
        for cloud_path, local_path in (("/追更", "/Downloads"), ("/自定义网盘", "/自定义NAS")):
            with self.subTest(cloud_path=cloud_path):
                self.config["discovery"]["savepath"] = cloud_path
                self.config["plugins"] = {"aria2": {"host_port": "", "secret": "", "dir": local_path}}
                self.config["tasklist"] = copy.deepcopy([legacy, manual, custom])
                self.write_config()
                with patch.object(web.Config, "load_plugins", return_value=({}, {
                    "aria2": {"host_port": "", "secret": "", "dir": "/downloads"},
                }, {})):
                    web.init()
                saved = self.saved_config()
                self.assertEqual(saved["discovery"]["savepath"], "/cartoon" if cloud_path == "/追更" else cloud_path)
                self.assertEqual(saved["plugins"]["aria2"]["dir"], "/downloads" if local_path == "/Downloads" else local_path)
                self.assertEqual(saved["tasklist"][0]["savepath"], "/cartoon")
                self.assertEqual(saved["tasklist"][0]["addition"]["aria2"]["save_path"], "cartoon")
                self.assertEqual(saved["tasklist"][0]["replace"], "{TASKNAME} S02E{EXX}.{EXT}")
                self.assertEqual(saved["tasklist"][1:], [manual, custom])

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
        search.assert_called_once_with("测试动漫", self.config, refresh=True,
                                      health_path=web.Config.search_health_path(web.CONFIG_PATH))

    def test_manual_search_recovers_deleted_task_hints_when_sources_fail(self):
        title = "测试动漫 第二季 (2024)"
        self.config["tasklist"] = [{"taskname": title, "shareurl": "https://pan.quark.cn/s/old",
                                    "cookie": "must-not-copy", "stoken": "private-token",
                                    "addition": {"aria2": {"secret": "private-rpc"}}}]
        self.write_config()
        self.assertTrue(self.client.post("/update", json={"tasklist": []}).json["success"])
        failure = {"success": False, "data": [], "errors": [{"source": "PanSou", "message": "连接超时"}],
                   "message": "PanSou: 连接超时"}
        with patch.object(web, "search_resources", return_value=failure) as search:
            result = self.client.get("/task_suggestions", query_string={"q": title}).json
        self.assertTrue(result["success"])
        self.assertEqual(result["errors"], failure["errors"])
        self.assertEqual(result["message"], failure["message"])
        self.assertEqual(len(result["data"]), 1)
        self.assertEqual(result["data"][0]["shareurl"], "https://pan.quark.cn/s/old")
        self.assertEqual(result["data"][0]["source"], "历史资源（待验证）")
        self.assertTrue(all(secret not in str(result) for secret in ("must-not-copy", "private-token", "private-rpc", "fake-cookie")))
        self.assertEqual(self.saved_config()["tasklist"], [])
        self.find.assert_not_called()
        search.assert_called_once()

    def test_manual_history_search_matches_work_season_and_year(self):
        records = [{"taskname": name, "shareurl": f"https://pan.quark.cn/s/share{index}"}
                   for index, name in enumerate(("测试动漫 第二季 (2024)", "测试动漫 第三季 (2024)",
                                                 "测试动漫 第二季 (2025)", "其他动漫 第二季 (2024)"))]
        web.Config.remember_resources(web.Config.resource_history_path(self.config_path), records)
        with patch.object(web, "search_resources", return_value={"success": True, "data": [], "errors": [], "message": ""}):
            result = self.client.get("/task_suggestions", query_string={"q": "测试动漫 S02 (2024)"}).json
        self.assertEqual([item["shareurl"] for item in result["data"]], [records[0]["shareurl"]])

    def test_manual_history_search_keeps_new_sources_and_deduplicates_with_passcode(self):
        title = "测试动漫 第二季 (2024)"
        web.Config.remember_resources(web.Config.resource_history_path(self.config_path), [
            {"taskname": title, "shareurl": "https://pan.quark.cn/s/old?pwd=AB12"}])
        fresh = {"success": True, "data": [
            {"taskname": title, "shareurl": "https://pan.quark.cn/s/old", "source": "PanSou"},
            {"taskname": title, "shareurl": "https://pan.quark.cn/s/new", "source": "网页搜索"}],
            "errors": [], "message": ""}
        with patch.object(web, "search_resources", return_value=fresh) as search:
            result = self.client.get("/task_suggestions", query_string={"q": title, "d": "1"}).json
        self.assertEqual([item["shareurl"] for item in result["data"]],
                         ["https://pan.quark.cn/s/old?pwd=AB12", "https://pan.quark.cn/s/new"])
        self.assertEqual(result["data"][1]["source"], "网页搜索")
        search.assert_called_once_with(title, self.config, refresh=True,
                                      health_path=web.Config.search_health_path(web.CONFIG_PATH))

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

    def test_no_valid_candidate_keeps_intent_out_of_transfer_tasks(self):
        self.find.return_value = {"success": False, "candidate": None, "error": "没有匹配的有效分享"}
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json["pending"])
        self.assertEqual(response.json["message"], "没有匹配的有效分享")
        saved = self.saved_config()
        self.assertEqual(saved["tasklist"], [])
        self.assertEqual(len(saved["pending_subscriptions"]), 1)
        self.assertNotIn("shareurl", saved["pending_subscriptions"][0]["task"])

    def test_direct_subscription_rejects_invalid_links_without_network_or_writing(self):
        for value in (None, [], "", "not a link", "http://127.0.0.1/s/abc123",
                      "https://pan.quark.cn.attacker.example/s/abc123",
                      "https://user@pan.quark.cn/s/abc123", "https://pan.quark.cn:443/s/abc123",
                      "https://pan.quark.cn/drive", "x" * 2001):
            with self.subTest(value=value):
                response = self.client.post("/api/discovery/subscribe", json={"id": self.entry["id"], "shareurl": value})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.saved_config(), self.config)
        self.find.assert_not_called()

    def test_direct_subscription_verifies_real_files_and_reuses_defaults_without_search(self):
        payload = {"id": self.entry["id"], "shareurl": "分享 http://pan.quark.cn/s/abc123 提取码：abcd"}
        listing = {"code": 0, "data": {"list": [
            {"fid": "video", "file_name": "测试动漫 S02E01 (2024).mkv", "dir": False},
        ]}}
        with patch.object(web.Quark, "get_stoken", return_value={"status": 200, "data": {"stoken": "private-test-token"}}) as token, \
                patch.object(web.Quark, "get_detail", return_value=listing):
            response = self.client.post("/api/discovery/subscribe", json=payload)
            self.assertEqual(response.status_code, 200, response.json)
            task = response.json["data"]
            self.assertEqual(task["shareurl"], "https://pan.quark.cn/s/abc123?pwd=abcd")
            self.assertEqual(task["resource_id"], self.entry["id"])
            self.assertEqual(task["savepath"], "/cartoon")
            self.assertEqual(task["replace"], "{TASKNAME} S02E{EXX}.{EXT}")
            self.assertEqual(task["addition"]["aria2"]["save_path"], "cartoon")
            self.assertEqual(self.saved_config()["tasklist"], [task])
            self.assertNotIn("private-test-token", response.get_data(as_text=True))
            duplicate = self.client.post("/api/discovery/subscribe", json=payload)
            self.assertTrue(duplicate.json["existing"])
            token.assert_called_once_with("abc123", "abcd")
        self.find.assert_not_called()

    def test_direct_subscription_rejects_wrong_title_season_year_and_non_video(self):
        payload = {"id": self.entry["id"], "shareurl": "https://pan.quark.cn/s/abc123"}
        with patch.object(web.Quark, "get_stoken", return_value={"status": 200, "data": {"stoken": "test"}}):
            for filename in ("别的动漫 S02E01 (2024).mp4", "测试动漫 S03E01 (2024).mp4",
                             "测试动漫 S02E01 (2023).mp4", "测试动漫 S02E01 (2024).srt",
                             "测试动漫 S02E01 (2024).mp4.exe"):
                with self.subTest(filename=filename), patch.object(web.Quark, "get_detail", return_value={
                    "code": 0, "data": {"list": [{"fid": "file", "file_name": filename, "dir": False}]},
                }):
                    response = self.client.post("/api/discovery/subscribe", json=payload)
                    self.assertEqual(response.status_code, 422, response.json)
                    self.assertEqual(self.saved_config(), self.config)
        self.find.assert_not_called()

    def test_direct_subscription_uses_only_catalog_aliases(self):
        self.get_search_aliases.return_value = ["官方别名"]
        with patch.object(web.Quark, "get_stoken", return_value={"status": 200, "data": {"stoken": "test"}}), \
                patch.object(web.Quark, "get_detail", return_value={"code": 0, "data": {"list": [
                    {"fid": "video", "file_name": "官方别名 S02E01 (2024).mp4", "dir": False},
                ]}}):
            response = self.client.post("/api/discovery/subscribe", json={
                "id": self.entry["id"], "shareurl": "https://pan.quark.cn/s/abc123",
                "search_aliases": ["客户端不可信别名"],
            })
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["data"]["search_aliases"], ["官方别名"])
        self.find.assert_not_called()

    def test_subscription_persists_only_task_fields_and_keeps_credentials_private(self):
        response = self.subscribe()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["success"])
        task = self.saved_config()["tasklist"][0]
        self.assertEqual(task, response.json["data"])
        self.assertEqual(task["shareurl"], self.candidate["candidate"]["shareurl"])
        self.assertEqual(task["taskname"], "测试动漫 第二季 (2024)")
        self.assertEqual(task["savepath"], "/cartoon")
        self.assertEqual(task["runweek"], list(range(1, 8)))
        self.assertEqual(task["addition"], {"testplugin": {"enabled": False}, "aria2": {"save_path": "cartoon"}})
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

    def test_delete_then_resubscribe_revalidates_remembered_share_without_search(self):
        listing = {"code": 0, "data": {"list": [
            {"fid": "episode", "file_name": "测试动漫 S02E01 (2024).mkv", "dir": False},
        ]}}
        with patch.object(web.Quark, "find_valid_resource", self.real_find), \
                patch("resource_search.search_resources", return_value={"success": True, "data": [self.candidate["candidate"]]}) as search, \
                patch("resource_search.search_web_resources", side_effect=AssertionError("不应依赖网页搜索")), \
                patch.object(web.Quark, "get_stoken", return_value={"status": 200, "data": {"stoken": "private-token"}}) as token, \
                patch.object(web.Quark, "get_detail", return_value=listing):
            first = self.subscribe()
            self.assertEqual(first.status_code, 200, first.json)
            self.assertTrue(self.client.post("/update", json={"tasklist": []}).json["success"])
            self.assertEqual(self.saved_config()["tasklist"], [])
            search.side_effect = AssertionError("外部搜索源故障时仍需复验本地记录")
            second = self.subscribe()
            self.assertEqual(second.status_code, 200, second.json)
            self.assertFalse(second.json["existing"])
            self.assertEqual(second.json["data"]["shareurl"], first.json["data"]["shareurl"])
            self.assertEqual(token.call_count, 2)
            search.assert_called_once()
        history = Path(web.Config.resource_history_path(self.config_path)).read_text(encoding="utf-8")
        self.assertNotIn("private-token", history)
        self.assertNotIn("fake-cookie", history)

    def test_deleting_legacy_task_keeps_only_resource_hint_before_list_is_replaced(self):
        old = {"taskname": "测试动漫 第二季 (2024)", "shareurl": "https://pan.quark.cn/s/old",
               "savepath": "/cartoon", "cookie": "must-not-copy", "addition": {"aria2": {"secret": "private-rpc"}}}
        self.config["tasklist"] = [old]
        self.write_config()
        self.assertTrue(self.client.post("/update", json={"tasklist": []}).json["success"])
        records = web.Config.resource_history(web.Config.resource_history_path(self.config_path))
        self.assertEqual(records, [{"taskname": old["taskname"], "shareurl": old["shareurl"]}])

    def test_same_show_from_another_catalog_is_already_subscribed(self):
        for resource_id in ("tvmaze:99", None):
            with self.subTest(resource_id=resource_id):
                existing = {"taskname": "测试动漫 S02 [2024] [1080p]", "resource_id": resource_id,
                            "shareurl": "https://pan.quark.cn/s/already", "savepath": "/已有",
                            "pattern": r"第(\d+)集\.mkv", "replace": r"自定义\1.mkv"}
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

    def test_donghua_subscription_keeps_source_aliases_for_repair(self):
        entry = dict(self.entry, id="bilibili:123", title="国漫测试", search_title="国漫测试", year="")
        self.calendar["days"][0]["donghua"] = [entry]
        self.get_search_aliases.return_value = ["国漫测试官方别名"]
        response = self.subscribe("bilibili:123")
        self.assertTrue(response.json["success"])
        task = self.saved_config()["tasklist"][0]
        self.assertEqual(task["resource_type"], "anime")
        self.assertEqual(task["search_aliases"], ["国漫测试官方别名"])
        self.assertEqual(self.find.call_args.args[0]["search_aliases"], task["search_aliases"])

    def test_popular_subscription_matches_video_and_subtitles_only(self):
        response = self.subscribe()
        self.assertTrue(response.json["success"])
        pattern = response.json["data"]["pattern"]
        for filename in ("第01集.MP4", "第02集.mkv", "第01集.zh-CN.srt", "第02集.ass"):
            with self.subTest(filename=filename):
                self.assertIsNotNone(re.search(pattern, filename))
        for filename in ("广告.txt", "安装.exe", "原声.mp3", "下载说明.pdf", "视频.mp4.exe"):
            with self.subTest(filename=filename):
                self.assertIsNone(re.search(pattern, filename))
        self.assertEqual(self.find.call_args.args[0]["resource_type"], "anime")

    def test_popular_subscription_returns_and_saves_season_episode_rename_rule(self):
        for title, season in (("测试动漫", "01"), ("测试动漫 第三季", "03"), ("测试动漫 Season 12", "12")):
            with self.subTest(title=title):
                self.entry["search_title"] = title
                self.write_config()
                response = self.subscribe()
                self.assertTrue(response.json["success"])
                task = response.json["data"]
                self.assertEqual(task["replace"], f"{{TASKNAME}} S{season}E{{EXX}}.{{EXT}}")
                self.assertEqual(self.saved_config()["tasklist"][0]["replace"], task["replace"])

    def test_share_preview_excludes_other_files_even_with_unrestricted_pattern(self):
        files = [{"fid": str(index), "file_name": name, "dir": False,
                  "obj_category": "video" if name.endswith(".mp4") else "other"}
                 for index, name in enumerate(("第01集.mp4", "第01集.srt", "广告.txt", "工具.exe"))]
        with patch.object(web.Quark, "get_detail", return_value={"code": 0, "data": {"list": files}}), \
                patch.object(web.Quark, "get_fids", return_value=[]):
            response = self.client.post("/get_share_detail", json={
                "shareurl": "https://pan.quark.cn/s/example", "stoken": "test-token",
                "task": {"taskname": "测试", "savepath": "/追更", "pattern": ".*", "replace": ""},
            })
        self.assertTrue(response.json["success"])
        preview = {item["file_name"]: item for item in response.json["data"]["list"]}
        for filename in ("第01集.mp4", "第01集.srt"):
            self.assertIn("file_name_re", preview[filename])
        for filename in ("广告.txt", "工具.exe"):
            self.assertNotIn("file_name_re", preview[filename])
            self.assertNotIn("file_name_saved", preview[filename])

    def test_subtitle_preview_survives_old_video_rule_and_ignore_extension(self):
        files = [{"fid": "sub", "file_name": "第01集.srt", "dir": False}]
        with patch.object(web.Quark, "get_detail", return_value={"code": 0, "data": {"list": files}}), \
                patch.object(web.Quark, "get_fids", return_value=[{"fid": "saved"}]), \
                patch.object(web.Quark, "ls_dir", return_value={"data": {"list": [
                    {"file_name": "第01集.mp4", "dir": False},
                ]}}):
            response = self.client.post("/get_share_detail", json={
                "shareurl": "https://pan.quark.cn/s/example", "stoken": "test-token",
                "task": {"taskname": "测试", "savepath": "/追更", "pattern": r".*\.mp4$",
                         "replace": "", "ignore_extension": True},
            })
        self.assertTrue(response.json["success"])
        subtitle = response.json["data"]["list"][0]
        self.assertEqual(subtitle["file_name_re"], "第01集.srt")
        self.assertNotIn("file_name_saved", subtitle)

    def test_video_preview_ignores_extension_only_among_existing_videos(self):
        for existing_name, already_saved in (("第01集.srt", False), ("第01集.txt", False), ("第01集.mkv", True)):
            with self.subTest(existing_name=existing_name), \
                    patch.object(web.Quark, "get_detail", return_value={"code": 0, "data": {"list": [
                        {"file_name": "第01集.mp4", "dir": False, "obj_category": "video"},
                    ]}}), patch.object(web.Quark, "get_fids", return_value=[{"fid": "saved"}]), \
                    patch.object(web.Quark, "ls_dir", return_value={"data": {"list": [
                        {"file_name": existing_name, "dir": False},
                    ]}}):
                response = self.client.post("/get_share_detail", json={
                    "shareurl": "https://pan.quark.cn/s/example", "stoken": "test-token",
                    "task": {"taskname": "测试", "savepath": "/cartoon", "pattern": r".*\.mp4$",
                             "replace": "", "ignore_extension": True},
                })
                self.assertTrue(response.json["success"])
                video = response.json["data"]["list"][0]
                if already_saved:
                    self.assertTrue(video.get("file_name_saved"))
                    self.assertNotIn("file_name_re", video)
                else:
                    self.assertEqual(video["file_name_re"], "第01集.mp4")
                    self.assertNotIn("file_name_saved", video)

    def test_preview_preserves_original_name_for_invalid_media_rename(self):
        for filename, replacement in (("第01集.srt", "changed.mp4"), ("第01集.mp4", "changed.txt"),
                                      ("第01集.mp4", "../changed.mp4")):
            with self.subTest(filename=filename), \
                    patch.object(web.Quark, "get_detail", return_value={"code": 0, "data": {"list": [
                        {"file_name": filename, "dir": False, "obj_category": "video"},
                    ]}}), patch.object(web.Quark, "get_fids", return_value=[]):
                response = self.client.post("/get_share_detail", json={
                    "shareurl": "https://pan.quark.cn/s/example", "stoken": "test-token",
                    "task": {"taskname": "测试", "savepath": "/追更", "pattern": ".+", "replace": replacement},
                })
                self.assertTrue(response.json["success"])
                self.assertEqual(response.json["data"]["list"][0]["file_name_re"], filename)

    def test_subscription_failure_returns_reason_and_saves_retry_intent(self):
        self.find.return_value = {"success": False, "candidate": None,
                                  "error": "搜索源暂不可用", "code": "unavailable",
                                  "queries": ["测试动漫", "Test Anime"], "search_complete": False,
                                  "source_errors": [{"source": "网页搜索", "message": "请求超时"}]}
        response = self.subscribe()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["code"], "unavailable")
        self.assertEqual(response.json["queries"], ["测试动漫", "Test Anime"])
        self.assertFalse(response.json["search_complete"])
        self.assertEqual(response.json["source_errors"][0]["message"], "请求超时")
        saved = self.saved_config()
        self.assertEqual(saved["tasklist"], [])
        self.assertEqual(saved["pending_subscriptions"][0]["task"]["resource_id"], self.entry["id"])
        for value in ("fake-cookie", "never-persist-this-token", "stoken", "passcode"):
            self.assertNotIn(value, str(saved["pending_subscriptions"]))

    def test_pending_subscription_survives_calendar_change_and_duplicate_click(self):
        self.find.return_value = {"success": False, "error": "请求超时", "code": "unavailable"}
        self.get_search_aliases.return_value = ["可信作品别名"]
        first = self.subscribe()
        original = self.saved_config()["pending_subscriptions"]
        self.assertEqual(original[0]["task"]["search_aliases"], ["可信作品别名"])
        self.get_calendar.side_effect = AssertionError("已保存的待查找订阅不应重新依赖周历")
        second = self.subscribe()
        self.assertEqual(second.status_code, 202)
        self.assertTrue(second.json["existing"])
        self.assertTrue(second.json["pending"])
        self.assertEqual(self.saved_config()["pending_subscriptions"], original)
        self.assertEqual(first.json["data"]["resource_id"], second.json["data"]["resource_id"])
        self.find.assert_called_once()

    def test_pending_endpoints_require_login_and_cancel_by_id(self):
        self.find.return_value = {"success": False, "error": "暂无匹配资源"}
        self.subscribe()
        anonymous = web.app.test_client()
        self.assertEqual(anonymous.get("/api/discovery/pending").status_code, 401)
        self.assertEqual(anonymous.delete("/api/discovery/pending/bangumi:42").status_code, 401)
        listed = self.client.get("/api/discovery/pending").json["data"]
        self.assertEqual([item["resource_id"] for item in listed], ["bangumi:42"])
        self.assertNotIn("task", listed[0])
        self.assertTrue(self.client.delete("/api/discovery/pending/bangumi:42").json["removed"])
        self.assertFalse(self.client.delete("/api/discovery/pending/bangumi:42").json["removed"])
        self.assertEqual(self.saved_config()["pending_subscriptions"], [])
        self.assertEqual(self.saved_config()["tasklist"], [])

    def test_pending_templates_cannot_be_inserted_via_update(self):
        response = self.client.post("/update", json={"pending_subscriptions": [{"task": {"taskname": "伪造"}}]})
        self.assertTrue(response.json["success"])
        self.assertNotIn("pending_subscriptions", self.saved_config())

    def test_channel_search_defaults_are_added_without_enabling_disabled_source(self):
        with patch.object(web.Config, "load_plugins", return_value=({}, {}, {})):
            web.init()
            defaults = self.saved_config()["source"]["channelsearch"]
            self.assertTrue(defaults["enable"])
            self.assertEqual(defaults["telegram_channels"], ["guoman4K", "kuakedongman"])
            self.assertEqual(defaults["qq_channels"], ["pd26778899"])
            current = self.saved_config()
            current["source"]["channelsearch"] = {"enable": False, "telegram_channels": ["my_channel"]}
            web.Config.write_json(self.config_path, current)
            web.init()
        self.assertEqual(self.saved_config()["source"]["channelsearch"], {"enable": False, "telegram_channels": ["my_channel"]})

    def test_download_setting_change_reaches_pending_subscription(self):
        self.config["plugins"] = {"aria2": {"host_port": "192.0.2.1:6800", "secret": "private-rpc", "dir": "/downloads"}}
        self.config["discovery"]["auto_download"] = True
        self.write_config()
        self.find.return_value = {"success": False, "error": "请求超时"}
        self.subscribe()
        self.assertTrue(self.saved_config()["pending_subscriptions"][0]["task"]["addition"]["aria2"]["auto_download"])
        self.assertTrue(self.client.post("/update", json={"discovery": {
            "savepath": "/cartoon", "auto_download": False}}).json["success"])
        self.assertFalse(self.saved_config()["pending_subscriptions"][0]["task"]["addition"]["aria2"]["auto_download"])

    def test_direct_share_can_replace_pending_intent_only_after_validation(self):
        self.find.return_value = {"success": False, "error": "请求超时"}
        self.subscribe()
        pending = self.saved_config()["pending_subscriptions"]
        with patch.object(web.Quark, "verify_resource_link", return_value={"success": False, "error": "作品不匹配"}):
            failed = self.client.post("/api/discovery/subscribe", json={
                "id": self.entry["id"], "shareurl": "https://pan.quark.cn/s/manual"})
        self.assertEqual(failed.status_code, 422)
        self.assertEqual(self.saved_config()["pending_subscriptions"], pending)
        with patch.object(web.Quark, "verify_resource_link", return_value=self.candidate):
            success = self.client.post("/api/discovery/subscribe", json={
                "id": self.entry["id"], "shareurl": "https://pan.quark.cn/s/manual"})
        self.assertTrue(success.json["success"])
        self.assertEqual(self.saved_config()["pending_subscriptions"], [])
        self.assertEqual(len(self.saved_config()["tasklist"]), 1)

    def test_cancelling_pending_during_manual_verification_does_not_recreate_it(self):
        self.find.return_value = {"success": False, "error": "请求超时"}
        self.subscribe()
        def complete(*_args):
            self.client.delete("/api/discovery/pending/bangumi:42")
            return self.candidate
        with patch.object(web.Quark, "verify_resource_link", side_effect=complete):
            response = self.client.post("/api/discovery/subscribe", json={
                "id": self.entry["id"], "shareurl": "https://pan.quark.cn/s/manual"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.saved_config()["pending_subscriptions"], [])
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
        self.assertEqual(response.json["data"]["addition"]["aria2"], {
            "auto_download": True, "download_subdir": True, "save_path": "cartoon",
        })
        self.assertEqual(web.task_plugins_config_default["aria2"], {"auto_download": False, "download_subdir": False})
        self.assertNotIn("fake-rpc-secret", response.get_data(as_text=True))
        self.assertEqual(self.saved_config()["tasklist"][0], response.json["data"])

    def test_update_enables_existing_popular_tasks_and_preserves_other_settings(self):
        for previous_global in (False, True):
            with self.subTest(previous_global=previous_global):
                self.config["discovery"]["auto_download"] = previous_global
                self.config["tasklist"] = [
                    {"resource_id": "bangumi:42", "addition": {"aria2": {
                        "auto_download": False, "download_subdir": False,
                        "save_path": "动漫", "pause": True,
                    }, "other": {"enabled": True}}},
                    {"resource_id": "bilibili:43"},
                    {"taskname": "手动任务", "addition": {"aria2": {"auto_download": False}}},
                ]
                self.write_config()
                response = self.client.post("/update", json={
                    "discovery": {"savepath": "/追更", "auto_download": True},
                    "plugins": {"aria2": {"host_port": "192.168.66.33:6800",
                                           "secret": "fake-secret", "dir": "/Downloads"}},
                })
                self.assertTrue(response.json["success"])
                tasks = self.saved_config()["tasklist"]
                self.assertEqual(tasks[0]["addition"], {"aria2": {
                    "auto_download": True, "download_subdir": True,
                    "download_existing": True, "save_path": "动漫", "pause": True,
                }, "other": {"enabled": True}})
                self.assertEqual(tasks[1]["addition"]["aria2"], {
                    "auto_download": True, "download_subdir": True, "download_existing": True,
                })
                self.assertEqual(tasks[2], self.config["tasklist"][2])
                # The next UI fetch must contain the updated flags used by manual runs.
                self.assertEqual(self.client.get("/data").json["data"]["tasklist"], tasks)

    def test_saving_enabled_download_does_not_repeat_completed_backfill(self):
        self.config["discovery"]["auto_download"] = True
        self.config["plugins"] = {"aria2": {
            "host_port": "192.168.66.33:6800", "secret": "fake-secret", "dir": "/Downloads",
        }}
        self.config["tasklist"] = [
            {"resource_id": "bangumi:42", "addition": {"aria2": {"auto_download": True}}},
            {"resource_id": "bilibili:43", "addition": {"aria2": {
                "auto_download": True, "download_existing": True,
            }}},
        ]
        self.write_config()
        response = self.client.post("/update", json={"discovery": self.config["discovery"]})
        self.assertTrue(response.json["success"])
        tasks = self.saved_config()["tasklist"]
        self.assertNotIn("download_existing", tasks[0]["addition"]["aria2"])
        self.assertTrue(tasks[1]["addition"]["aria2"]["download_existing"])

    def test_disabling_popular_download_cancels_pending_backfill(self):
        self.config["discovery"]["auto_download"] = True
        self.config["tasklist"] = [{"resource_id": "bangumi:42", "addition": {"aria2": {
            "auto_download": True, "download_existing": True, "download_subdir": True,
            "save_path": "动漫", "pause": True,
        }}}]
        self.write_config()
        response = self.client.post("/update", json={
            "discovery": {"savepath": "/追更", "auto_download": False},
        })
        self.assertTrue(response.json["success"])
        self.assertEqual(self.saved_config()["tasklist"][0]["addition"]["aria2"], {
            "auto_download": False, "download_subdir": True, "save_path": "动漫", "pause": True,
        })

    def test_update_requires_aria2_configuration_before_enabling_download(self):
        complete = {"host_port": "192.168.66.33:6800", "secret": "fake-secret", "dir": "/Downloads"}
        for aria2, flags in (({}, ""), ({"host_port": complete["host_port"]}, ""),
                             ({"host_port": complete["host_port"], "secret": complete["secret"]}, ""),
                             (complete, "-aria2")):
            with self.subTest(aria2=aria2, flags=flags), patch.object(web, "PLUGIN_FLAGS", flags):
                response = self.client.post("/update", json={
                    "discovery": {"savepath": "/新目录", "auto_download": True},
                    "plugins": {"aria2": aria2},
                })
                self.assertEqual(response.status_code, 400)
                self.assertIn("Aria2", response.json["message"])
                self.assertEqual(self.saved_config(), self.config)
                self.assertEqual(web.config_data, self.config)
        self.reload.assert_not_called()

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
        self.config["discovery"]["savepath"] = "/自定义目录/"
        self.write_config()
        response = self.subscribe()
        self.assertTrue(response.json["success"])
        self.assertEqual(response.json["data"]["savepath"], "/自定义目录")
        self.assertEqual(response.json["data"]["addition"]["aria2"]["save_path"], "自定义目录")

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
