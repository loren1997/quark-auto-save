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
        web_search = patch("resource_search.search_web_resources", create=True,
                           return_value={"success": True, "data": [], "errors": []})
        self.web_search = web_search.start()
        self.addCleanup(web_search.stop)

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
        self.assertEqual([call.args[0] for call in search.call_args_list], ["学生会也有洞", "生徒会にも穴はある"])

    def test_alias_query_preserves_original_year_and_season_requirements(self):
        self.task["search_aliases"] = ["Test Show"]
        wrong = self.candidate(title="Test Show 第二季 (2023)")
        right = self.candidate(title="Test Show 第二季 (2024)")
        with patch("resource_search.search_resources", side_effect=[
            {"success": True, "data": []},
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

    def test_failed_query_still_tries_alternate_season_spelling(self):
        with patch("resource_search.search_resources", side_effect=[
            {"success": False, "data": [], "message": "query rejected"},
            {"success": True, "data": [self.candidate()]},
        ]) as search, patch.object(self.account, "get_stoken", return_value=TOKEN), \
                patch.object(self.account, "get_detail", return_value=FILES):
            self.assertTrue(self.account.find_valid_resource(self.task, {})["success"])
            self.assertEqual(search.call_count, 2)

    def test_query_planner_keeps_all_eight_official_aliases_and_deduplicates_punctuation(self):
        aliases = ["alias " + chr(65 + index) for index in range(8)]
        queries = qas._resource_search_queries("作品 第3季 (2026)", aliases)
        self.assertEqual(queries[:9], ["作品 第3季"] + aliases)
        self.assertEqual(queries[9:], ["作品 第三季", "作品 S03", "作品"])
        self.assertEqual(len(queries), 12)
        self.assertEqual(qas._resource_search_queries("Test-Show! (2026)", ["test show", "TEST.Show", "Test—Show"]), ["Test Show"])
        self.assertIn("I'm Dating a Dark Summoner", qas._resource_search_queries(
            "黑暗召唤师在地下恋爱中 (2026)", ["黑暗召唤师在地下恋爱中", "ダークサモナーとデキている",
                                                "Dark Summoner to Dekiteiru", "I'm Dating a Dark Summoner!"]))

    def test_last_official_alias_is_searched_and_can_validate_real_files(self):
        self.task["search_aliases"] = ["alias " + chr(65 + index) for index in range(8)]
        alias_title = "alias H 第二季 (2024)"
        detail = copy.deepcopy(FILES)
        detail["data"]["share"]["title"] = alias_title

        def search(query, *_args, **kwargs):
            self.assertFalse(kwargs["include_web"])
            return {"success": True, "data": [self.candidate(title=alias_title)] if query == "alias H" else []}

        with patch("resource_search.search_resources", side_effect=search), patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", return_value=detail):
            self.assertTrue(self.account.find_valid_resource(self.task, {})["success"])
        self.web_search.assert_not_called()

    def test_base_name_query_does_not_relax_season_verification(self):
        self.task["taskname"] = "罗小黑战记 第3季"
        wrong, right = self.candidate("wrong", "罗小黑战记 第二季"), self.candidate("right", "罗小黑战记 第三季")
        detail = copy.deepcopy(FILES)
        detail["data"]["share"]["title"] = "罗小黑战记 第三季"

        def search(query, *_args, **_kwargs):
            return {"success": True, "data": [wrong, right] if query == "罗小黑战记" else []}

        with patch("resource_search.search_resources", side_effect=search), patch.object(self.account, "get_stoken", return_value=TOKEN) as token, patch.object(self.account, "get_detail", return_value=detail):
            result = self.account.find_valid_resource(self.task, {})
        self.assertTrue(result["success"])
        token.assert_called_once_with("right", "abcd")

    def test_numbered_title_only_means_a_season_when_task_explicitly_requires_one(self):
        self.assertTrue(qas.resource_titles_match("大理寺日志 第3季", "大理寺日志3 (2026) 更新至3集"))
        for name in ("大理寺日志2", "大理寺日志3 电影", "大理寺日志3 第二季", "大理寺日志3 合集"):
            self.assertFalse(qas.resource_titles_match("大理寺日志 第3季", name))
        self.assertFalse(qas.resource_titles_match("大理寺日志", "大理寺日志3"))
        self.assertTrue(qas.resource_titles_match("作品86", "作品86"))

    def test_directory_name_and_plain_episode_season_jointly_verify_same_show(self):
        self.task.update(taskname="大理寺日志 第3季", resource_type="anime")
        for directory, filename, accepted in (("大理寺日志 (2026)", "S03E09 1080p-GyWEB.mp4", True),
                                               ("大理", "S03E09 1080p-GyWEB.mp4", False),
                                               ("大理寺日志 (2026)", "S02E09 1080p-GyWEB.mp4", False),
                                               ("大理寺日志 (2026)", "S03E09 另一部剧.mp4", False)):
            folder = {"code": 0, "data": {"list": [{"fid": "folder", "dir": True, "file_name": directory}]}}
            videos = {"code": 0, "data": {"list": [{"fid": "file", "dir": False, "file_name": filename}]}}
            with self.subTest(directory=directory, filename=filename), self.search([self.candidate(title="大理寺日志3 (2026)")]), patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", side_effect=[folder, videos]):
                self.assertEqual(self.account.find_valid_resource(self.task, {})["success"], accepted)

    def test_other_season_video_or_document_cannot_supply_missing_season_evidence(self):
        self.task.update(taskname="大理寺日志 第3季", resource_type="anime")
        for files in ([{"file_name": "S03E09 1080p-GyWEB.mp4"}, {"file_name": "S02E09 1080p-GyWEB.mp4"}],
                      [{"file_name": "S03E09 1080p-GyWEB.mp4"}, {"file_name": "S02E09 Other Show.mp4"}],
                      [{"file_name": "S03E09.mp3"}, {"file_name": "09.mp4"}]):
            detail = {"code": 0, "data": {"share": {"title": "大理寺日志 (2026)"}, "list": [
                {"fid": str(index), "dir": False, **file} for index, file in enumerate(files)
            ]}}
            with self.subTest(files=files), self.search([self.candidate(title="大理寺日志3 (2026)")]), patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", return_value=detail):
                self.assertFalse(self.account.find_valid_resource(self.task, {})["success"])

    def test_old_movie_and_mixed_season_collection_do_not_validate_a_new_season(self):
        self.task.update(taskname="罗小黑战记 第3季", resource_type="anime")
        for files in ([{"fid": "movie", "dir": False, "file_name": "罗小黑战记 (2019).mp4"}],
                      [{"fid": "old", "dir": True, "file_name": "罗小黑战记 (2019) 电影"},
                       {"fid": "new", "dir": True, "file_name": "罗小黑战记 (2026) 学前篇"}]):
            detail = {"code": 0, "data": {"share": {"title": "罗小黑战记"}, "list": files}}
            self.web_search.return_value = {"success": True, "data": [self.candidate(title="罗小黑战记 第三季资源下载")]}
            with self.subTest(files=files), self.search([]), patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", return_value=detail):
                self.assertFalse(self.account.find_valid_resource(self.task, {})["success"])

    def test_partial_source_errors_with_no_candidates_are_unavailable(self):
        with patch("resource_search.search_resources", return_value={"success": True, "data": [], "errors": [{"source": "backup", "message": "timeout"}]}):
            result = self.account.find_valid_resource(self.task, {})
        self.assertEqual(result["code"], "unavailable")
        self.assertFalse(result["search_complete"])
        self.assertIn("backup：请求超时", result["error"])
        self.assertTrue(result["queries"])

    def test_source_diagnostics_do_not_expose_urls_or_auth_tokens(self):
        error = {"source": "https://private.example?token=SECRET", "message": "HTTP 401 Bearer SECRET"}
        with patch("resource_search.search_resources", return_value={"success": True, "data": [], "errors": [error]}):
            result = self.account.find_valid_resource(self.task, {})
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(result["source_errors"], [{"source": "搜索源", "message": "HTTP 401"}])

    def test_web_challenge_is_reported_as_verification_needed(self):
        self.web_search.return_value = {"success": False, "data": [],
                                       "errors": [{"source": "网页搜索", "message": "DuckDuckGo: 需要人工验证"}]}
        with patch("resource_search.search_resources", return_value={"success": True, "data": []}):
            result = self.account.find_valid_resource(self.task, {})
        self.assertIn("需要人工验证", result["error"])
        self.assertFalse(result["search_complete"])

    def test_web_diagnostic_categories_and_engine_survive_subscription_response(self):
        for message, expected in (
            ("网页域名解析失败", "DNS 域名解析失败"),
            ("网页地址解析到了非公网 IP，已拒绝访问", "地址解析为非公网 IP，已拒绝访问"),
            ("网页请求失败 (ProxyError)", "代理连接失败"),
            ("网页请求失败 (InvalidProxyURL)", "代理配置无效"),
            ("网页请求失败 (SSLError)", "TLS 或证书连接失败"),
            ("TLS 证书验证失败", "TLS 或证书连接失败"),
            ("网页搜索已达到本次时间预算", "达到本次查询时间预算"),
            ("Bing 未返回 RSS，可能需要验证或服务不可用", "响应格式异常"),
        ):
            self.web_search.return_value = {"success": False, "data": [],
                                           "errors": [{"source": "网页搜索 Bing", "message": message}]}
            with self.subTest(message=message), self.search([]):
                result = self.account.find_valid_resource(self.task, {})
            self.assertEqual(result["source_errors"], [{"source": "网页搜索 Bing", "message": expected}])
            self.assertIn("网页搜索 Bing：" + expected, result["error"])
            self.assertFalse(result["search_complete"])

    def test_distinct_engine_failures_are_not_collapsed_in_subscription(self):
        errors = [{"source": "网页搜索 DuckDuckGo", "message": "HTTP 403"},
                  {"source": "网页搜索 Bing", "message": "DNS 域名解析失败"}]
        self.web_search.return_value = {"success": False, "data": [], "errors": errors}
        with self.search([]):
            result = self.account.find_valid_resource(self.task, {})
        self.assertEqual(result["source_errors"], errors)
        self.assertIn("DuckDuckGo：HTTP 403", result["error"])
        self.assertIn("Bing：DNS 域名解析失败", result["error"])

    def test_index_time_budget_still_runs_web_fallback_and_reports_incomplete_search(self):
        with patch("resource_search.search_resources", return_value={"success": True, "data": []}) as index, patch.object(qas.time, "monotonic", side_effect=[0, 1, 61]):
            result = self.account.find_valid_resource(self.task, {})
        self.assertEqual(index.call_count, 1)
        self.web_search.assert_called_once()
        self.assertEqual(result["code"], "unavailable")
        self.assertFalse(result["search_complete"])
        self.assertIn("时间预算", result["error"])

    def test_two_consecutive_index_outages_enter_web_fallback_early(self):
        self.task["search_aliases"] = ["别名甲", "别名乙", "别名丙"]
        self.web_search.return_value = {"success": True, "data": [self.candidate()]}
        with patch("resource_search.search_resources", return_value={"success": False, "data": []}) as index, patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", return_value=FILES):
            self.assertTrue(self.account.find_valid_resource(self.task, {})["success"])
        self.assertEqual(index.call_count, 2)

    def test_web_fallback_verifies_real_share_despite_generic_webpage_title(self):
        candidate = self.candidate(title="网站资源下载 - 今日更新")
        self.web_search.return_value = {"success": True, "data": [candidate]}
        with self.search([]), patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", return_value=FILES):
            self.assertTrue(self.account.find_valid_resource(self.task, {})["success"])
        wrong = copy.deepcopy(FILES)
        wrong["data"]["share"]["title"] = "另外一部剧 第二季 (2024)"
        with self.search([]), patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", return_value=wrong):
            self.assertFalse(self.account.find_valid_resource(self.task, {})["success"])

    def test_web_fallback_retries_arabic_season_with_chinese_numeral(self):
        self.task.update(taskname="大理寺日志 第3季", resource_type="anime")
        candidate = self.candidate(title="大理寺日志 第三季资源网页")
        self.web_search.side_effect = lambda query, *args, **kwargs: {
            "success": True, "data": [candidate] if query == "大理寺日志 第三季" else [],
        }
        files = {"code": 0, "data": {"share": {"title": "大理寺日志3 (2026)"},
                                      "list": [{"file_name": "S03E09.mp4", "dir": False}]}}
        with self.search([]), patch.object(self.account, "get_stoken", return_value=TOKEN), \
                patch.object(self.account, "get_detail", return_value=files):
            self.assertTrue(self.account.find_valid_resource(self.task, {})["success"])
        self.assertEqual([call.args[0] for call in self.web_search.call_args_list],
                         ["大理寺日志 第3季", "大理寺日志 第三季"])

    def test_web_fallback_is_bounded_and_skips_already_checked_index_candidates(self):
        index = [self.candidate(f"index{index}") for index in range(5)]
        self.web_search.return_value = {"success": True, "data": index + [self.candidate("web")]}
        with self.search(index), patch.object(self.account, "get_stoken", side_effect=[EXPIRED] * 5 + [TOKEN]) as token, patch.object(self.account, "get_detail", return_value=FILES):
            self.assertTrue(self.account.find_valid_resource(self.task, {})["success"])
        self.assertEqual(token.call_count, 6)
        self.task["search_aliases"] = ["Other Name", "另一中文译名", "第三个译名"]
        self.web_search.reset_mock()
        self.web_search.return_value = {"success": True, "data": [self.candidate(f"web{index}") for index in range(10)]}
        with self.search([]), patch.object(self.account, "get_stoken", return_value=EXPIRED) as token:
            self.assertFalse(self.account.find_valid_resource(self.task, {})["success"])
        self.assertEqual(token.call_count, 3)
        self.web_search.return_value = {"success": True, "data": []}
        self.web_search.reset_mock()
        with self.search([]):
            self.account.find_valid_resource(self.task, {})
        self.assertEqual([call.args[0] for call in self.web_search.call_args_list], ["测试剧 第二季", "另一中文译名"])

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

    def test_collection_selects_only_the_unique_matching_season_and_scopes_saved_url(self):
        self.task.update(taskname="大理寺日志 第3季", resource_type="anime")
        selected = "c" * 32
        root = {"code": 0, "data": {"list": [{"fid": "a" * 32, "dir": True, "file_name": "大理寺日志"}]}}
        collection = {"code": 0, "metadata": {"_total": 3}, "data": {"list": [
            {"fid": selected, "dir": True, "file_name": "大理寺日志3"},
            {"fid": "b" * 32, "dir": True, "file_name": "大理寺日志1"},
            {"fid": "d" * 32, "dir": True, "file_name": "大理寺日志2"},
        ]}}
        videos = {"code": 0, "data": {"list": [{"fid": "episode", "dir": False, "file_name": "09.mp4"}]}}
        candidate = self.candidate(title="大理寺日志3")
        candidate["shareurl"] += "#/list/share/" + "f" * 32
        with self.search([candidate]), patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", side_effect=[root, collection, videos]) as detail:
            result = self.account.find_valid_resource(self.task, {})
        self.assertTrue(result["success"])
        self.assertEqual([call.args[2] for call in detail.call_args_list], ["f" * 32, "a" * 32, selected])
        expected = "https://pan.quark.cn/s/newshare?pwd=abcd#/list/share/" + selected
        self.assertEqual(result["candidate"]["shareurl"], expected)
        self.assertEqual(result["candidate"]["pdir_fid"], selected)
        self.assertEqual(self.account.extract_url(expected)[:3], ("newshare", "abcd", selected))
        with patch.object(self.account, "find_valid_resource", return_value=result):
            self.account.repair_share(self.task)
        self.assertEqual(self.task["shareurl"], expected)

    def test_pure_season_child_requires_exact_parent_name(self):
        self.task.update(taskname="大理寺日志 第3季", resource_type="anime")
        folders = {"code": 0, "data": {"list": [
            {"fid": "c" * 32, "dir": True, "file_name": "S03"},
            {"fid": "d" * 32, "dir": True, "file_name": "S02"},
        ]}}
        videos = {"code": 0, "data": {"list": [{"fid": "episode", "dir": False, "file_name": "09.mp4"}]}}
        for parent, accepted in (("大理寺日志", True), ("大理", False), ("另一部剧", False)):
            root = {"code": 0, "data": {"list": [{"fid": "a" * 32, "dir": True, "file_name": parent}]}}
            with self.subTest(parent=parent), self.search([self.candidate(title="大理寺日志3")]), patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", side_effect=[root, folders, videos]) as detail:
                result = self.account.find_valid_resource(self.task, {})
                self.assertEqual(result["success"], accepted)
                self.assertEqual(detail.call_count, 3 if accepted else 2)

    def test_ambiguous_incomplete_or_wrong_season_directory_is_never_selected(self):
        self.task.update(taskname="大理寺日志 第3季", resource_type="anime")
        for names, total in ((["大理寺日志1", "大理寺日志2"], 2),
                             (["大理寺日志3", "大理寺日志 第三季"], 2),
                             (["大理寺日志3", "大理寺日志2"], 60)):
            folders = {"code": 0, "metadata": {"_total": total}, "data": {"share": {"title": "大理寺日志"}, "list": [
                {"fid": str(index + 1) * 32, "dir": True, "file_name": name} for index, name in enumerate(names)
            ]}}
            with self.subTest(names=names, total=total), self.search([self.candidate(title="大理寺日志3")]), patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", return_value=folders) as detail:
                self.assertFalse(self.account.find_valid_resource(self.task, {})["success"])
                self.assertEqual(detail.call_count, 1)

    def test_selected_season_cannot_hide_conflicting_ancestor_or_video(self):
        self.task.update(taskname="大理寺日志 第3季", resource_type="anime")
        for parent, video in (("大理寺日志 第2季", "09.mp4"), ("大理寺日志", "S02E09.mp4")):
            folders = {"code": 0, "data": {"share": {"title": parent}, "list": [
                {"fid": "c" * 32, "dir": True, "file_name": "大理寺日志3"},
                {"fid": "d" * 32, "dir": True, "file_name": "大理寺日志2"},
            ]}}
            videos = {"code": 0, "data": {"list": [{"fid": "episode", "dir": False, "file_name": video}]}}
            with self.subTest(parent=parent, video=video), self.search([self.candidate(title="大理寺日志3")]), patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", side_effect=[folders, videos]):
                self.assertFalse(self.account.find_valid_resource(self.task, {})["success"])

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
                                    ("全集.txt", "document"), ("伪视频.exe", "video")):
            detail = {"code": 0, "data": {"share": {"title": self.task["taskname"]}, "list": [
                {"fid": "file", "file_name": file_name, "dir": False, "obj_category": category},
            ]}}
            with self.subTest(file_name=file_name), self.search([self.candidate()]), patch.object(self.account, "get_stoken", return_value=TOKEN), patch.object(self.account, "get_detail", return_value=detail):
                self.assertFalse(self.account.find_valid_resource(self.task, {})["success"])

    def test_generic_subscriptions_also_require_real_video_extensions(self):
        for filename in ("资料.pdf", "01.srt", "01.ass", "广告.exe", "封面.png"):
            detail = {"code": 0, "data": {"share": {"title": self.task["taskname"]}, "list": [
                {"fid": "nonvideo", "file_name": filename, "dir": False, "obj_category": "video"},
            ]}}
            with self.subTest(filename=filename), self.search([self.candidate()]), \
                    patch.object(self.account, "get_stoken", return_value=TOKEN), \
                    patch.object(self.account, "get_detail", return_value=detail):
                self.assertFalse(self.account.find_valid_resource(self.task, {})["success"])

    def test_candidate_with_video_on_second_page_is_verified(self):
        images = [{"fid": f"image-{index}", "file_name": f"封面{index}.png", "dir": False,
                   "obj_category": "image"} for index in range(50)]
        second = [{"fid": "episode", "file_name": "02.MKV", "dir": False, "obj_category": "document"}]
        pages = [{"code": 0, "metadata": {"_total": 51}, "data": {
            "share": {"title": self.task["taskname"]}, "list": files}} for files in (images, second)]
        with self.search([self.candidate()]), patch.object(self.account, "get_stoken", return_value=TOKEN), \
                patch.object(self.account, "_send_request", side_effect=[Mock(json=Mock(return_value=page)) for page in pages]) as request:
            result = self.account.find_valid_resource(self.task, {})
        self.assertTrue(result["success"])
        self.assertEqual([call.kwargs["params"]["_page"] for call in request.call_args_list], [1, 2])

    def test_nonvideo_candidates_are_skipped_until_a_real_video_source_is_found(self):
        candidates = [self.candidate("subtitles"), self.candidate("documents"), self.candidate("videos")]
        def detail(share, *_args, **_kwargs):
            filename = {"subtitles": "01.ass", "documents": "作品.pdf", "videos": "01.mp4"}[share]
            return {"code": 0, "data": {"share": {"title": self.task["taskname"]}, "list": [
                {"fid": "file", "file_name": filename, "dir": False, "obj_category": "video"}]}}
        with self.search(candidates) as search, patch.object(self.account, "get_stoken", return_value=TOKEN), \
                patch.object(self.account, "get_detail", side_effect=detail) as listing:
            result = self.account.find_valid_resource(self.task, {})
        self.assertTrue(result["success"])
        self.assertEqual(result["candidate"]["pwd_id"], "videos")
        self.assertEqual(listing.call_count, 3)
        search.assert_called_once()

    def test_incomplete_candidate_listing_is_unconfirmed_even_when_first_page_has_video(self):
        for incomplete in ({"metadata": {"_total": 200}}, {"truncated": True}):
            detail = copy.deepcopy(FILES)
            if "metadata" in incomplete:
                detail.update(incomplete)
            else:
                detail["data"].update(incomplete)
            with self.subTest(incomplete=incomplete), self.search([self.candidate()]), \
                    patch.object(self.account, "get_stoken", return_value=TOKEN), \
                    patch.object(self.account, "get_detail", return_value=detail) as listing:
                result = self.account.find_valid_resource(self.task, {})
            self.assertFalse(result["success"])
            self.assertTrue(result["validation_limited"])
            self.assertFalse(result["search_complete"])
            self.assertIn("尚未完整确认", result["error"])
            self.assertEqual(listing.call_args.kwargs["max_pages"], 3)

    def test_one_named_folder_beside_images_and_text_keeps_alias_and_season_checks(self):
        self.task.update(taskname="黑色召唤师 (2022)", search_aliases=["Black Summoner"])
        selected = "a" * 32
        videos = {"code": 0, "data": {"list": [{"fid": "episode", "file_name": "01.mkv", "dir": False}]}}
        for folder_name, accepted in (("Black Summoner (2022)", True), ("Black Summoner 第二季 (2022)", False),
                                      ("另一部作品 (2022)", False)):
            root = {"code": 0, "data": {"share": {"title": self.task["taskname"]}, "list": [
                {"fid": selected, "file_name": folder_name, "dir": True},
                {"fid": "cover", "file_name": "封面.png", "dir": False},
                {"fid": "advert", "file_name": "广告.txt", "dir": False},
            ]}}
            with self.subTest(folder_name=folder_name), self.search([self.candidate()]), \
                    patch.object(self.account, "get_stoken", return_value=TOKEN), \
                    patch.object(self.account, "get_detail", side_effect=[root, videos]) as listing:
                result = self.account.find_valid_resource(self.task, {})
            self.assertEqual(result["success"], accepted)
            self.assertEqual(listing.call_count, 2 if accepted else 1)
            if accepted:
                self.assertEqual(result["candidate"]["pdir_fid"], selected)
                self.assertTrue(result["candidate"]["shareurl"].endswith("#/list/share/" + selected))

    def test_no_video_reason_reuses_repair_without_claiming_expiry(self):
        replacement = {**self.candidate(), "pwd_id": "newshare"}
        with patch.object(self.account, "find_valid_resource", return_value={"success": True, "candidate": replacement}), \
                patch.object(qas, "print") as output:
            self.assertEqual(self.account.repair_share(self.task, reason="分享不含视频"), replacement)
        self.assertIn("分享不含视频", output.call_args_list[0].args[0])
        self.assertNotIn("分享已失效", output.call_args_list[0].args[0])

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
        save.assert_called_once_with(self.task, "newshare", "private-token", 0, scan_state={})

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

    def test_terminal_empty_page_accepts_visible_files_despite_stale_total(self):
        first = copy.deepcopy(FILES)
        first["metadata"] = {"_total": 8, "_count": 1, "_page": 1}
        last = {"code": 0, "metadata": {"_total": 8, "_count": 0, "_page": 2}, "data": {"list": []}}
        with patch.object(self.account, "_send_request", side_effect=[
                Mock(json=Mock(return_value=first)), Mock(json=Mock(return_value=last))]):
            detail = self.account.get_detail("newshare", "token", 0, max_pages=3)
        self.assertTrue(qas.share_listing_complete(detail))
        self.assertEqual(detail["metadata"]["_total"], 8)
        self.assertEqual(detail["data"]["share"], FILES["data"]["share"])
        with self.search([self.candidate()]), patch.object(self.account, "get_stoken", return_value=TOKEN), \
                patch.object(self.account, "get_detail", return_value=detail):
            self.assertTrue(self.account.find_valid_resource(self.task, {})["success"])
            self.assertTrue(self.account._scan_share_media(self.task, "newshare", "token", 0)["has_video"])

    def test_limit_or_failed_next_page_never_becomes_complete(self):
        first = copy.deepcopy(FILES)
        first["metadata"] = {"_total": 8}
        with patch.object(self.account, "_send_request", return_value=Mock(json=Mock(return_value=first))):
            detail = self.account.get_detail("newshare", "token", 0, max_pages=1)
        self.assertFalse(qas.share_listing_complete(detail))
        self.assertFalse(detail["data"]["listing_complete"])
        first["data"].pop("truncated")
        with patch.object(self.account, "_send_request", side_effect=[Mock(json=Mock(return_value=first)),
                                                                   requests.Timeout("offline")]):
            self.assertFalse(qas.share_listing_complete(self.account.get_detail("newshare", "token", 0, max_pages=3)))

    def test_manual_link_verifies_real_files_without_any_search(self):
        with self.search([]) as search, patch.object(self.account, "get_stoken", return_value=TOKEN), \
                patch.object(self.account, "get_detail", return_value=FILES):
            result = self.account.verify_resource_link(self.task, "https://pan.quark.cn/s/newshare?pwd=abcd")
        self.assertTrue(result["success"])
        search.assert_not_called()
        self.web_search.assert_not_called()
        wrong = copy.deepcopy(FILES)
        wrong["data"]["share"]["title"] = "另一部剧 第二季 (2024)"
        with patch.object(self.account, "get_stoken", return_value=TOKEN), \
                patch.object(self.account, "get_detail", return_value=wrong):
            self.assertFalse(self.account.verify_resource_link(self.task, "https://pan.quark.cn/s/newshare")["success"])
        with patch.object(self.account, "get_stoken") as token:
            self.assertFalse(self.account.verify_resource_link(self.task, "http://127.0.0.1/private")["success"])
        token.assert_not_called()

    def test_temporary_listing_failures_do_not_abort_later_subscriptions(self):
        later_task = {**self.task, "taskname": "后续任务"}
        response = Mock()
        response.json.side_effect = [ValueError("HTML gateway"), {"code": 1, "status": 503, "message": "分享服务暂不可用"}]
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
