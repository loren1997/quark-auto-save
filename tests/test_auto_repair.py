import contextlib
import copy
import io
import json
import os
import unittest
from unittest.mock import Mock, patch

from treelib import Tree

import quark_auto_save as qas
import requests


TOKEN = {"status": 200, "code": 0, "data": {"stoken": "private-token"}}
FILES = {"code": 0, "data": {
    "share": {"title": "测试剧 第二季 (2024)"},
    "list": [{"fid": "episode", "file_name": "01.mkv", "dir": False}],
}}
EXPIRED = {"status": 400, "code": 41001, "message": "分享已取消"}


class AutoRepairTests(unittest.TestCase):
    def setUp(self):
        self.account = qas.Quark()
        self.task = {
            "taskname": "测试剧 第二季 (2024)",
            "shareurl": "https://pan.quark.cn/s/oldshare?pwd=old",
            "savepath": "/追剧/测试剧", "pattern": r".*\.mkv$",
            "replace": "{TASKNAME}.{I}", "runweek": [1, 4],
            "enddate": "2030-12-31", "addition": {"plugin": {"enabled": True}},
        }
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        self.config = patch.object(qas, "CONFIG_DATA", {"auto_repair": True})
        self.config.start()
        self.addCleanup(self.config.stop)

    def candidate(self, share="newshare", title=None):
        return {"taskname": title or self.task["taskname"],
                "shareurl": f"https://pan.quark.cn/s/{share}?pwd=abcd", "source": "test"}

    def search(self, candidates):
        return patch("resource_search.search_resources", return_value={
            "success": True, "data": candidates, "errors": [],
        })

    def test_only_explicit_expiry_is_replaceable(self):
        for message in ("分享已取消", "该分享已失效", "分享链接已过期", "分享不存在", "分享者取消了分享"):
            with self.subTest(message=message):
                self.assertTrue(qas.is_expired_share({"status": 400, "message": message}))
        for response in (
            {"status": 500, "message": "分享已失效"},
            {"status": 403, "message": "分享不存在"},
            {"status": 400, "message": "提取码错误"},
            {"status": 400, "message": "登录已过期"},
            {"status": 400, "message": "分享访问权限不足"},
            {"status": 429, "message": "请求过于频繁"},
            {"status": 400, "message": "分享为空，文件已被分享者删除"},
        ):
            with self.subTest(response=response):
                self.assertFalse(qas.is_expired_share(response))

    def test_titles_require_same_show_season_and_explicit_year(self):
        self.assertTrue(qas.resource_titles_match("测试剧 第二季 (2024)", "测试剧 S02 [2024] [1080p] 更新至20集"))
        self.assertTrue(qas.resource_titles_match("测试剧 第十二季", "测试剧 S12"))
        for other in ("测试剧 第三季 (2024)", "测试剧 第二季 (2023)",
                      "测试剧 第二季", "测试剧 第二季合集 (2024)", "测试剧 第1-2季 (2024)",
                      "测试剧幕后 第二季 (2024)", "测试剧 (2024)"):
            with self.subTest(other=other):
                self.assertFalse(qas.resource_titles_match(self.task["taskname"], other))

    def test_real_release_labels_do_not_hide_an_exact_title(self):
        self.assertTrue(qas.resource_titles_match("庆余年", "庆余年 (2019) 4K 高码 全46集"))
        self.assertTrue(qas.resource_titles_match("庆余年 (2019)", "庆余年 (2019) 4K 高码率 杜比视界 内嵌简中 全46集"))
        self.assertFalse(qas.resource_titles_match("庆余年", "庆余年幕后 (2019) 4K 高码 全46集"))
        self.assertFalse(qas.resource_titles_match("庆余年 第二季", "庆余年 第一季 (2019) 4K 高码 全46集"))

    def test_catalogue_aliases_validate_bilingual_releases_without_accepting_unknown_suffixes(self):
        title = "学生会也有洞！ (2026)"
        aliases = ["生徒会にも穴はある!", "脑洞学生会"]
        for name in ("学生会也有洞! 生徒会にも穴はある! (2026)",
                     "#动漫🗄 学生会也有洞！ 生徒会にも穴はある! (2026) [1080P] [内封简繁] [更至5集]",
                     "学生会也有洞! (2026) 更至第5集 1080P · SDR · 350MB/集 内嵌繁中",
                     "脑洞学生会 (2026)"):
            with self.subTest(name=name):
                self.assertTrue(qas.resource_titles_match(title, name, aliases=aliases))
        for name in ("学生会也有洞幕后 (2026)", "学生会也有洞 漫画 (2026)",
                     "学生会也有洞 另一部作品 (2026)", "脑洞学生会 (2025)",
                     "脑洞学生会 第二季 (2026)"):
            with self.subTest(name=name):
                self.assertFalse(qas.resource_titles_match(title, name, aliases=aliases))

    def test_search_retries_without_year_and_punctuation_then_verifies_bilingual_video(self):
        self.task.update(taskname="学生会也有洞！ (2026)", resource_type="anime",
                         search_aliases=["生徒会にも穴はある!", "脑洞学生会"])
        title = "学生会也有洞！ 生徒会にも穴はある! (2026)"
        folder = {"code": 0, "data": {"share": {"title": title}, "list": [
            {"fid": "folder", "dir": True, "file_name": title},
        ]}}
        videos = {"code": 0, "data": {"list": [
            {"fid": "video", "dir": False, "file_name": "Even.the.Student.Council.Has.Its.Holes.S01E05.mkv"},
        ]}}
        with patch("resource_search.search_resources", side_effect=[
            {"success": True, "data": []},
            {"success": True, "data": [self.candidate(title=title)]},
        ]) as search, patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", side_effect=[folder, videos]):
            result = self.account.find_valid_resource(self.task, {})
        self.assertTrue(result["success"])
        self.assertEqual([call.args[0] for call in search.call_args_list], [self.task["taskname"], "学生会也有洞"])

    def test_alias_query_preserves_original_year_and_season_requirements(self):
        self.task["search_aliases"] = ["Test Show"]
        wrong = self.candidate(title="Test Show 第二季 (2023)")
        right = self.candidate(title="Test Show 第二季 (2024)")
        with patch("resource_search.search_resources", side_effect=[
            {"success": True, "data": []}, {"success": True, "data": []},
            {"success": True, "data": [wrong, right]},
        ]) as search, patch.object(self.account, "get_stoken", return_value=TOKEN) as token, patch.object(self.account, "get_detail", return_value=FILES):
            self.assertTrue(self.account.find_valid_resource(self.task, {})["success"])
        self.assertEqual(search.call_args.args[0], "Test Show")
        token.assert_called_once()

    def test_first_season_english_alias_does_not_turn_valid_video_into_conflict(self):
        english = "Even the Student Council Has Its Holes"
        self.task.update(taskname="学生会也有洞！ (2026)", resource_type="anime",
                         search_aliases=["生徒会にも穴はある!", english])
        for season, accepted in ((1, True), (2, False)):
            detail = {"code": 0, "data": {"share": {"title": self.task["taskname"]}, "list": [
                {"fid": "video", "dir": False, "file_name": f"{english}.S0{season}E05.mkv"},
            ]}}
            with self.subTest(season=season), self.search([self.candidate()]), \
                    patch.object(self.account, "get_stoken", return_value=TOKEN), \
                    patch.object(self.account, "get_detail", return_value=detail):
                self.assertEqual(self.account.find_valid_resource(self.task, {})["success"], accepted)
        self.assertTrue(qas.resource_titles_match(self.task["taskname"], "学生会也有洞 S01 (2026)"))
        self.assertFalse(qas.resource_titles_match(self.task["taskname"], "学生会也有洞 S02 (2026)"))
        self.assertFalse(qas.resource_titles_match(self.task["taskname"], "学生会也有洞 S01 (2025)"))

    def test_failed_query_still_tries_name_without_year(self):
        with patch("resource_search.search_resources", side_effect=[
            {"success": False, "data": [], "message": "query rejected"},
            {"success": True, "data": [self.candidate()]},
        ]) as search, patch.object(self.account, "get_stoken", return_value=TOKEN), \
                patch.object(self.account, "get_detail", return_value=FILES):
            self.assertTrue(self.account.find_valid_resource(self.task, {})["success"])
            self.assertEqual(search.call_count, 2)

    def test_failed_directory_does_not_hide_another_directory_in_same_share(self):
        first, second = self.candidate(), self.candidate()
        first["shareurl"] += "#/list/share/" + "a" * 32
        second["shareurl"] += "#/list/share/" + "b" * 32
        wrong = copy.deepcopy(FILES)
        wrong["data"]["share"]["title"] = "测试剧 第二季 (2023)"
        with self.search([first, second]), patch.object(self.account, "get_stoken", return_value=TOKEN), \
                patch.object(self.account, "get_detail", side_effect=[wrong, FILES]) as detail:
            result = self.account.find_valid_resource(self.task, {})
        self.assertTrue(result["success"])
        self.assertEqual(result["candidate"]["shareurl"], second["shareurl"])
        self.assertEqual(detail.call_count, 2)

    def test_nested_single_folder_video_wrapper_is_verified(self):
        self.task["resource_type"] = "anime"
        folders = [{"code": 0, "data": {"list": [
            {"fid": f"folder{index}", "dir": True, "file_name": self.task["taskname"]},
        ]}} for index in range(2)]
        with self.search([self.candidate()]), patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", side_effect=folders + [FILES]) as detail:
            self.assertTrue(self.account.find_valid_resource(self.task, {})["success"])
        self.assertEqual(detail.call_count, 3)

    def test_recursive_wrappers_remain_bounded_and_cannot_hide_wrong_season(self):
        self.task.update(resource_type="anime", search_aliases=["Test Show"])
        folder = {"code": 0, "data": {"list": [
            {"fid": "folder", "dir": True, "file_name": self.task["taskname"]},
        ]}}
        with self.search([self.candidate()]), patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", return_value=folder) as detail:
            self.assertFalse(self.account.find_valid_resource(self.task, {})["success"])
        self.assertEqual(detail.call_count, 4)
        wrong_folder = copy.deepcopy(folder)
        wrong_folder["data"]["list"][0]["file_name"] = "Test Show 第三季 (2024)"
        with self.search([self.candidate()]), patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", side_effect=[folder, wrong_folder, FILES]):
            self.assertFalse(self.account.find_valid_resource(self.task, {})["success"])

    def test_search_failures_are_distinct_from_no_results_and_unverified_matches(self):
        for payload, code in (({"success": False, "data": []}, "unavailable"),
                              ({"success": True, "data": []}, "no_results"),
                              ({"success": True, "data": [self.candidate(title="另一部剧")]}, "no_match")):
            with self.subTest(code=code), patch("resource_search.search_resources", return_value=payload):
                result = self.account.find_valid_resource(self.task, {})
            self.assertFalse(result["success"])
            self.assertEqual(result["code"], code)
            self.assertNotIn("保留原订阅", result["error"])

    def test_search_skips_old_wrong_season_and_non_quark_candidates(self):
        evil = self.candidate("evil")
        evil["shareurl"] = "https://pan.quark.cn.evil.example/s/evil"
        candidates = [self.candidate("oldshare"), self.candidate("wrong", "测试剧 第三季 (2024)"), evil, self.candidate()]
        original = copy.deepcopy(self.task)
        with self.search(candidates), patch.object(self.account, "get_stoken", return_value=TOKEN) as token, patch.object(self.account, "get_detail", return_value=FILES):
            result = self.account.find_valid_resource(self.task, {})
        self.assertTrue(result["success"])
        self.assertEqual(result["candidate"]["pwd_id"], "newshare")
        token.assert_called_once_with("newshare", "abcd")
        self.assertEqual(self.task, original)

    def test_fifth_valid_candidate_is_accepted_and_attempts_are_bounded(self):
        candidates = [self.candidate(f"share{i}") for i in range(8)]
        with self.search(candidates), patch.object(self.account, "get_stoken", side_effect=[EXPIRED] * 4 + [TOKEN]) as token, patch.object(self.account, "get_detail", return_value=FILES):
            self.assertTrue(self.account.find_valid_resource(self.task, {})["success"])
            self.assertEqual(token.call_count, 5)
        with self.search(candidates), patch.object(self.account, "get_stoken", return_value=EXPIRED) as token:
            self.assertFalse(self.account.find_valid_resource(self.task, {})["success"])
            self.assertEqual(token.call_count, 5)

    def test_missing_year_requires_matching_real_directory(self):
        candidate = self.candidate(title="测试剧 第二季")
        folder = {"code": 0, "data": {"list": [{"fid": "folder", "dir": True, "file_name": self.task["taskname"]}]}}
        with self.search([candidate]), patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", side_effect=[folder, FILES]):
            self.assertTrue(self.account.find_valid_resource(self.task, {})["success"])
        plain_files = {"code": 0, "data": {"list": FILES["data"]["list"]}}
        with self.search([candidate]), patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", return_value=plain_files):
            self.assertFalse(self.account.find_valid_resource(self.task, {})["success"])

    def test_pure_season_directory_conflict_overrides_matching_share_title(self):
        for folder_name in ("S03", "Season 3", "第三季", "2023"):
            folder = {"code": 0, "data": {"share": {"title": self.task["taskname"]},
                      "list": [{"fid": "folder", "dir": True, "file_name": folder_name}]}}
            with self.subTest(folder_name=folder_name), self.search([self.candidate()]), patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", side_effect=[folder, FILES]):
                self.assertFalse(self.account.find_valid_resource(self.task, {})["success"])

    def test_index_title_alone_cannot_validate_another_show_or_numbered_files(self):
        plain_files = {"code": 0, "data": {"list": FILES["data"]["list"]}}
        wrong_show = {"code": 0, "data": {"list": [{"fid": "folder", "dir": True, "file_name": "另外一部剧 第二季 (2024)"}]}}
        wrong_share = {"code": 0, "data": {"list": FILES["data"]["list"], "share": {"title": "另外一部剧 第二季 (2024)"}}}
        for details in ([plain_files], [wrong_show, plain_files], [wrong_share]):
            with self.subTest(details=details), self.search([self.candidate()]), patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", side_effect=details):
                self.assertFalse(self.account.find_valid_resource(self.task, {})["success"])

    def test_real_share_title_or_filename_can_validate_numbered_contents(self):
        named_file = {"code": 0, "data": {"list": [{"fid": "episode", "dir": False, "file_name": self.task["taskname"] + ".mkv"}]}}
        for detail in (FILES, named_file):
            with self.subTest(detail=detail), self.search([self.candidate()]), patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", return_value=detail) as get_detail:
                self.assertTrue(self.account.find_valid_resource(self.task, {})["success"])
                self.assertEqual(get_detail.call_args.kwargs["_fetch_share"], 1)

    def test_audiobook_or_text_only_share_is_not_a_video_replacement(self):
        self.task["resource_type"] = "anime"
        for file_name, category in (("01.mp3", "audio"), ("全集.epub", "document"),
                                    ("全集.txt", "document"), ("伪视频.mp4", "audio")):
            detail = {"code": 0, "data": {"share": {"title": self.task["taskname"]}, "list": [
                {"fid": "file", "file_name": file_name, "dir": False, "obj_category": category},
            ]}}
            with self.subTest(file_name=file_name), self.search([self.candidate()]), patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", return_value=detail):
                self.assertFalse(self.account.find_valid_resource(self.task, {})["success"])

    def test_generic_non_video_subscriptions_can_still_repair(self):
        detail = {"code": 0, "data": {"share": {"title": self.task["taskname"]}, "list": [
            {"fid": "document", "file_name": "资料.pdf", "dir": False, "obj_category": "document"},
        ]}}
        with self.search([self.candidate()]), patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", return_value=detail):
            self.assertTrue(self.account.find_valid_resource(self.task, {})["success"])

    def test_empty_or_inaccessible_share_is_not_a_replacement(self):
        for detail in ({"code": 0, "data": {"list": []}}, {"code": 403, "message": "无权限"}):
            with self.subTest(detail=detail), self.search([self.candidate()]), patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", return_value=detail):
                self.assertFalse(self.account.find_valid_resource(self.task, {})["success"])

    def test_real_directory_cannot_contradict_candidate_season_or_year(self):
        for title in ("测试剧 第三季 (2024)", "测试剧 第二季 (2023)"):
            folder = {"code": 0, "data": {"list": [{"fid": "folder", "dir": True, "file_name": title}]}}
            with self.subTest(title=title), self.search([self.candidate()]), patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", side_effect=[folder, FILES]):
                self.assertFalse(self.account.find_valid_resource(self.task, {})["success"])

    def test_repair_preserves_rules_resumes_save_and_records_no_tokens(self):
        self.task["startfid"] = "old-file-boundary"
        original = copy.deepcopy(self.task)
        with self.search([self.candidate()]), patch.object(self.account, "get_stoken", side_effect=[EXPIRED, TOKEN]), patch.object(self.account, "get_detail", return_value=FILES), patch.object(self.account, "dir_check_and_save", return_value=Tree()) as save:
            self.account.do_save_task(self.task)
        self.assertEqual(self.task["shareurl"], self.candidate()["shareurl"])
        self.assertNotIn("shareurl_ban", self.task)
        self.assertNotIn("startfid", self.task)
        for key in ("taskname", "savepath", "pattern", "replace", "runweek", "enddate", "addition"):
            self.assertEqual(self.task[key], original[key])
        self.assertNotIn("private-token", str(self.task))
        self.assertNotIn("abcd", str(self.task["repair_history"]))
        self.assertEqual(self.task["repair_history"][0]["from_id"], "oldshare")
        save.assert_called_once_with(self.task, "newshare", "private-token", 0)

    def test_temporary_error_never_searches_or_changes_subscription(self):
        for response in ({"status": 500, "message": "request error"}, {"status": 400, "message": "提取码错误"}, {"status": 401, "message": "登录过期"}):
            original = copy.deepcopy(self.task)
            with patch.object(self.account, "get_stoken", return_value=response), patch.object(self.account, "find_valid_resource") as search:
                self.account.do_save_task(self.task)
            search.assert_not_called()
            self.assertEqual(self.task, original)

    def test_historical_ban_is_revalidated(self):
        self.task["shareurl_ban"] = "登录过期"
        with patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "dir_check_and_save", return_value=Tree()) as save:
            self.account.do_save_task(self.task)
        self.assertNotIn("shareurl_ban", self.task)
        save.assert_called_once()

    def test_disabled_or_destructive_resave_never_replaces(self):
        for settings in ({"auto_repair": False}, {"update_subdir_resave_mode": True}):
            task = {**self.task, **settings}
            with patch.object(self.account, "get_stoken", return_value=EXPIRED), patch.object(self.account, "find_valid_resource") as search:
                self.account.do_save_task(task)
            search.assert_not_called()
            self.assertEqual(task["shareurl"], self.task["shareurl"])

    def test_failed_search_keeps_original_and_excludes_previous_failed_ids(self):
        self.task["repair_history"] = [{"from_id": "alreadyfailed", "to_id": "oldshare"}]
        with self.search([self.candidate("alreadyfailed")]), patch.object(self.account, "get_stoken") as token:
            self.assertFalse(self.account.find_valid_resource(self.task, {})["success"])
        token.assert_not_called()

    def test_manual_run_state_merges_without_overwriting_user_rules(self):
        original = copy.deepcopy(self.task)
        updated = {**original, "shareurl": self.candidate()["shareurl"], "repair_history": [{"from_id": "oldshare"}]}
        config = {"cookie": "keep-private", "tasklist": [{**original, "pattern": "user changed this"}]}
        self.assertTrue(qas.Config.merge_task_state(config, original, updated))
        self.assertEqual(config["tasklist"][0]["pattern"], "user changed this")
        self.assertEqual(config["tasklist"][0]["shareurl"], updated["shareurl"])
        self.assertEqual(config["cookie"], "keep-private")
        config["tasklist"][0]["shareurl"] = "https://pan.quark.cn/s/manuallychanged"
        self.assertFalse(qas.Config.merge_task_state(config, original, updated))

    def test_detail_error_does_not_start_file_operations(self):
        with patch.object(self.account, "get_detail", return_value={"code": 503, "message": "暂不可用"}), patch.object(self.account, "get_fids") as fids:
            tree = self.account.dir_check_and_save(self.task, "oldshare", "private-token")
        self.assertEqual(tree.size(), 0)
        fids.assert_not_called()

    def test_expiry_between_token_and_listing_repairs_only_once(self):
        with patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", return_value=EXPIRED), patch.object(self.account, "repair_share", return_value={"pwd_id": "new", "stoken": "token", "pdir_fid": 0}) as repair:
            self.account.do_save_task(self.task)
        repair.assert_called_once_with(self.task)

    def test_detail_gateway_html_and_timeout_are_temporary_errors(self):
        for error in (ValueError("gateway returned HTML"), requests.Timeout("read timeout")):
            with self.subTest(error=type(error).__name__):
                response = Mock()
                response.json.side_effect = error
                with patch.object(self.account, "_send_request", return_value=response):
                    result = self.account.get_detail("oldshare", "token", 0)
                self.assertEqual(result["status"], 503)
                self.assertNotEqual(result["code"], 0)
                self.assertFalse(qas.is_expired_share(result))
        with patch.object(self.account, "_send_request", side_effect=requests.Timeout("request timeout")):
            self.assertEqual(self.account.get_detail("oldshare", "token", 0)["status"], 503)

    def test_bad_detail_shapes_are_temporary_errors(self):
        for payload in (None, [], {}, {"code": 0}, {"code": 1, "status": 500, "message": "request error"}, {"code": 0, "data": []},
                        {"code": 0, "data": {"list": {}}},
                        {"code": 0, "data": {"list": ["not a file"]}},
                        {"code": 0, "data": {"list": [{"fid": "file"}]}},
                        {"code": 0, "data": FILES["data"], "metadata": {"_total": "1"}}):
            with self.subTest(payload=payload), patch.object(self.account, "_send_request", return_value=Mock(json=Mock(return_value=payload))):
                result = self.account.get_detail("oldshare", "token", 0)
                self.assertEqual(result["status"], 503)
                self.assertFalse(qas.is_expired_share(result))

    def test_temporary_listing_failures_do_not_abort_later_subscriptions(self):
        later_task = {**self.task, "taskname": "后续任务"}
        response = Mock()
        response.json.side_effect = [ValueError("HTML gateway"), {"code": 0, "data": {"list": []}}]
        with patch.object(self.account, "get_stoken", return_value=TOKEN) as tokens, patch.object(self.account, "_send_request", return_value=response), patch.object(self.account, "repair_share") as repair:
            self.account.do_save_task(self.task)
            self.account.do_save_task(later_task)
        self.assertEqual(tokens.call_count, 2)
        self.assertNotIn("shareurl_ban", self.task)
        repair.assert_not_called()

    def test_manual_task_execution_persists_replacement_to_config(self):
        config = {"cookie": "fake-cookie", "tasklist": [copy.deepcopy(self.task)]}

        def repair(account, tasks):
            tasks[0]["shareurl"] = self.candidate()["shareurl"]
            tasks[0]["repair_history"] = [{"from_id": "oldshare"}]

        with patch.dict(os.environ, {"TASKLIST": json.dumps([self.task])}, clear=True), patch.object(qas.sys, "argv", ["quark_auto_save.py", "fake-config.json"]), patch.object(qas.os.path, "exists", return_value=True), patch.object(qas.Config, "read_json", return_value=config), patch.object(qas.Config, "write_json") as write, patch.object(qas.Config, "locked", side_effect=lambda _: contextlib.nullcontext()), patch.object(qas, "verify_account", side_effect=lambda account: setattr(account, "is_active", True)), patch.object(qas, "do_save", side_effect=repair), patch.object(qas, "NOTIFYS", []):
            qas.main()
        saved = write.call_args.args[1]
        self.assertEqual(saved["tasklist"][0]["shareurl"], self.candidate()["shareurl"])
        self.assertEqual(saved["tasklist"][0]["pattern"], self.task["pattern"])
        self.assertEqual(saved["cookie"], "fake-cookie")


if __name__ == "__main__":
    unittest.main()
