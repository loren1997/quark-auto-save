import contextlib
import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from treelib import Tree

import quark_auto_save as qas


def video(name, fid=None):
    return {"fid": fid or name, "file_name": name, "dir": False,
            "obj_category": "video", "share_fid_token": "test-token"}


class EpisodeRepairTests(unittest.TestCase):
    def setUp(self):
        self.task = {
            "resource_id": "bangumi:example", "resource_type": "anime",
            "taskname": "黒の召喚士 (2022)", "search_aliases": ["黑色召唤师", "Black Summoner"],
            "shareurl": "https://pan.quark.cn/s/original", "savepath": "/追更/黑色召唤师",
            "pattern": r"(?i).*\.(mp4|mkv)$", "replace": "", "update_subdir": ".*",
        }
        self.account = qas.Quark()
        self.account.savepath_fid = {self.task["savepath"]: "destination"}
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)
        self.config = patch.object(qas, "CONFIG_DATA", {"auto_repair": True})
        self.config.start()
        self.addCleanup(self.config.stop)

    def tree(self):
        tree = Tree()
        tree.create_node(self.task["savepath"], "", data={"is_dir": True})
        return tree

    def files(self, names):
        return {"code": 0, "data": {"list": [video(name) for name in names]}}

    def candidate(self, share):
        return {"taskname": "黑色召唤师 (2022)", "shareurl": f"https://pan.quark.cn/s/{share}"}

    def test_numbers_use_real_episode_markers_or_known_title_aliases(self):
        for filename, number in (
            ("1.mkv", 1), ("05.mkv", 5), ("黑色召唤师 01.mkv", 1),
            ("Black Summoner - 02.mkv", 2), ("Black.Summoner.S01E03.mkv", 3),
            ("黑色召唤师 第4集.mp4", 4), ("E05.mkv", 5), ("EP06.mkv", 6),
            ("S01E07 1080p.mkv", 7), ("黑色召唤师 08【完全无修版】.mkv", 8),
            ("S02E02.mkv", None), ("Other Show 02.mkv", None),
            ("Black Summoner 02 (2021).mkv", None), ("2022.mkv", None),
            ("1080p.mkv", None), ("01-02.mkv", None), ("SP02.mkv", None),
            ("S01E01E02.mkv", None), ("E01-E02.mkv", None), ("第1集第2集.mkv", None),
            ("OVA02.mkv", None), ("预告02.mkv", None), ("02.srt", None),
            ("Black Summoner 2.mkv", None), ("00.mkv", None),
        ):
            with self.subTest(filename=filename):
                self.assertEqual(qas._resource_episode_number(self.task, filename), number)

    def test_explicit_later_season_is_supported_and_unknown_season_is_not(self):
        task = {**self.task, "taskname": "黑色召唤师 第三季"}
        self.assertEqual(qas._resource_episode_number(task, "S03E02.mkv"), 2)
        self.assertIsNone(qas._resource_episode_number(task, "S01E02.mkv"))
        self.assertIsNone(qas._resource_episode_number({**task, "taskname": "黑色召唤师"}, "02.mkv"))

    def test_actual_release_labels_and_spaces_detect_missing_two(self):
        task = {**self.task, "taskname": "黑暗召唤师在地下恋爱中（2026）",
                "search_aliases": ["与暗黑召唤师秘密恋爱中"]}
        names = [f"[10月] 与暗黑召唤师秘密恋爱中 {number:02d} [完全无修版] .mp4" for number in (1, 3, 4, 5)] + ["05.mkv"]
        self.assertEqual({qas._resource_episode_number(task, name) for name in names}, {1, 3, 4, 5})
        files = [video(name) for name in names]
        files += [{"fid": "empty", "file_name": "熟肉晚点", "dir": True}, video("封面.png")]
        with patch.object(self.account, "ls_dir", side_effect=lambda fid: {"code": 0, "data": {"list": [] if fid == "empty" else files}}), \
                patch.object(self.account, "find_valid_resource", return_value={"success": False, "error": "本次未找到"}) as search:
            self.account.repair_episode_gaps(task, self.tree())
        self.assertEqual(search.call_args.kwargs["required_episodes"], {2})
        missing_file = video("[10月] 与暗黑召唤师秘密恋爱中 02 [完全无修版] .mp4", "second")
        self.assertEqual(qas._episode_repair_files(task, [missing_file], {2}), {"second": 2})

    def test_candidate_without_missing_episode_does_not_prevent_later_match(self):
        candidates = [self.candidate("without"), self.candidate("with")]
        def detail(share, *_args, **_kwargs):
            result = self.files(["Black Summoner 03.mkv"] if share == "without" else ["Black Summoner 02.mkv", "02.mp4"])
            result["data"]["share"] = {"title": "黑色召唤师 (2022)"}
            return result
        with patch("resource_search.search_resources", return_value={"success": True, "data": candidates}), \
                patch.object(self.account, "get_stoken", return_value={"status": 200, "data": {"stoken": "token"}}), \
                patch.object(self.account, "get_detail", side_effect=detail):
            result = self.account.find_valid_resource(self.task, {}, required_episodes={2})
        self.assertTrue(result["success"])
        self.assertEqual(result["candidate"]["pwd_id"], "with")
        self.assertEqual(list(result["candidate"]["episode_fids"].values()), [2])

    def test_wrong_season_year_and_unrelated_work_are_never_supplements(self):
        for title, filename in (("黑色召唤师 (2022)", "S02E02.mkv"),
                                ("黑色召唤师 (2021)", "02.mkv"),
                                ("另一部作品 (2022)", "02.mkv")):
            with self.subTest(title=title, filename=filename):
                detail = self.files([filename])
                detail["data"]["share"] = {"title": title}
                with patch.object(self.account, "get_stoken", return_value={"status": 200, "data": {"stoken": "token"}}), \
                        patch.object(self.account, "get_detail", return_value=detail):
                    result = self.account._find_verified_resource(
                        self.task, [self.candidate("other")], set(), self.task["search_aliases"], required_episodes={2})
                self.assertFalse(result["success"])

    def test_supplement_respects_filter_and_preserves_episode_after_rename(self):
        files = [video("Black Summoner 02.mkv")]
        self.assertEqual(qas._episode_repair_files(self.task, files, {2}), {files[0]["fid"]: 2})
        excluded = {**self.task, "pattern": r".*\.mp4$"}
        self.assertEqual(qas._episode_repair_files(excluded, files, {2}), {})
        renamed = {**self.task, "pattern": r".*? (\d+)\.mkv$", "replace": r"E\1.mkv"}
        self.assertEqual(qas._episode_repair_files(renamed, files, {2}), {files[0]["fid"]: 2})
        self.assertEqual(qas._episode_repair_files({**renamed, "replace": "E09.mkv"}, files, {2}), {})

    def test_supplement_uses_media_extensions_even_with_wrong_server_category(self):
        task = {**self.task, "pattern": qas.MEDIA_PATTERN, "replace": "{TASKNAME} S01E{EXX}.{EXT}"}
        for extension in ("mp4", "mts", "mpg", "rmvb"):
            for category in (None, "document", "video"):
                with self.subTest(extension=extension, category=category):
                    item = {**video("Black Summoner 02." + extension, "second"), "obj_category": category}
                    self.assertEqual(qas._episode_repair_files(task, [item], {2}), {"second": 2})
        for filename in ("02.srt", "02.exe", "封面02.png"):
            self.assertEqual(qas._episode_repair_files(task, [video(filename)], {2}), {})

    def test_gap_queries_try_numbered_alias_and_broad_pack_before_giving_up(self):
        self.task["taskname"] = "黑色召唤师 (2022)"
        self.task["search_aliases"] = ["另一中文译名"]
        expected = ["黑色召唤师 02", "另一中文译名 02", "黑色召唤师", "另一中文译名"]
        for successful_query in expected[1:]:
            with self.subTest(query=successful_query):
                def search(query, *_args, **_kwargs):
                    return {"success": True, "data": [{"taskname": "另一中文译名 02", "shareurl":
                            "https://pan.quark.cn/s/second"}] if query == successful_query else []}
                detail = self.files(["02.mkv"])
                detail["data"]["share"] = {"title": "黑色召唤师 (2022)"}
                with patch("resource_search.search_resources", side_effect=search) as lookup, \
                        patch.object(self.account, "get_stoken", return_value={"status": 200, "data": {"stoken": "token"}}), \
                        patch.object(self.account, "get_detail", return_value=detail):
                    result = self.account.find_valid_resource(self.task, {}, required_episodes={2})
                self.assertTrue(result["success"])
                self.assertEqual([call.args[0] for call in lookup.call_args_list],
                                 expected[:expected.index(successful_query) + 1])

    def test_one_query_cannot_use_all_gap_candidate_slots(self):
        self.task["taskname"] = "黑色召唤师 (2022)"
        self.task["search_aliases"] = ["另一中文译名"]
        def search(query, *_args, **_kwargs):
            return {"success": True, "data": [self.candidate("with")] if query == "另一中文译名 02"
                    else [self.candidate("without" + str(n)) for n in range(10)]}
        def detail(share, *_args, **_kwargs):
            result = self.files(["02.mkv" if share == "with" else "03.mkv"])
            result["data"]["share"] = {"title": "黑色召唤师 (2022)"}
            return result
        with patch("resource_search.search_resources", side_effect=search) as lookup, \
                patch.object(self.account, "get_stoken", return_value={"status": 200, "data": {"stoken": "token"}}) as token, \
                patch.object(self.account, "get_detail", side_effect=detail):
            result = self.account.find_valid_resource(self.task, {}, required_episodes={2})
        self.assertTrue(result["success"])
        self.assertEqual(lookup.call_count, 2)
        self.assertEqual(token.call_count, 3)

    def test_valid_gap_candidate_returned_after_search_deadline_is_still_verified(self):
        detail = self.files(["02.mkv"])
        detail["data"]["share"] = {"title": "黑色召唤师 (2022)"}
        clock = {"now": 0}
        def slow_search(*_args, **_kwargs):
            clock["now"] = 61
            return {"success": True, "data": [self.candidate("second")],
                    "errors": [{"source": "PanSou 聚合 1", "message": "ReadTimeout"}]}
        with patch("resource_search.search_resources", side_effect=slow_search), \
                patch.object(qas.time, "monotonic", side_effect=lambda: clock["now"]), \
                patch.object(self.account, "get_stoken", return_value={"status": 200, "data": {"stoken": "token"}}) as token, \
                patch.object(self.account, "get_detail", return_value=detail):
            result = self.account.find_valid_resource(self.task, {}, required_episodes={2})
        self.assertTrue(result["success"])
        token.assert_called_once()

    def test_later_candidate_is_deferred_for_aliases_but_never_discarded(self):
        candidates = [self.candidate("without0"), self.candidate("without1"), self.candidate("with")]
        calls = []
        def search(query, *_args, **_kwargs):
            calls.append(query)
            return {"success": True, "data": candidates if len(calls) == 1 else []}
        def detail(share, *_args, **_kwargs):
            result = self.files(["02.mkv" if share == "with" else "03.mkv"])
            result["data"]["share"] = {"title": "黑色召唤师 (2022)"}
            return result
        with patch("resource_search.search_resources", side_effect=search), \
                patch.object(self.account, "get_stoken", return_value={"status": 200, "data": {"stoken": "token"}}), \
                patch.object(self.account, "get_detail", side_effect=detail):
            result = self.account.find_valid_resource(self.task, {}, required_episodes={2})
        self.assertTrue(result["success"])
        self.assertEqual(result["candidate"]["pwd_id"], "with")
        self.assertGreater(len(calls), 1)

    def test_exhausted_validation_budget_does_not_start_another_network_query(self):
        def candidates():
            yield self.candidate("first")
            self.fail("验证名额用完后不应继续取候选或发起网络查询")
        detail = self.files(["03.mkv"])
        detail["data"]["share"] = {"title": "黑色召唤师 (2022)"}
        with patch.object(self.account, "get_stoken", return_value={"status": 200, "data": {"stoken": "token"}}), \
                patch.object(self.account, "get_detail", return_value=detail):
            result = self.account._find_verified_resource(
                self.task, candidates(), set(), self.task["search_aliases"], max_checked=1, required_episodes={2})
        self.assertFalse(result["success"])
        self.assertTrue(result["validation_limited"])

    def test_numbered_index_titles_still_require_real_matching_share_files(self):
        for filename in ("黑色召唤师 02", "黑色召唤师 02.mkv", "黑色召唤师 EP02.mp4"):
            with self.subTest(filename=filename):
                detail = self.files(["02.mkv"])
                detail["data"]["share"] = {"title": "黑色召唤师 (2022)"}
                candidate = {**self.candidate("second"), "taskname": filename}
                with patch.object(self.account, "get_stoken", return_value={"status": 200, "data": {"stoken": "token"}}), \
                        patch.object(self.account, "get_detail", return_value=detail):
                    result = self.account._find_verified_resource(
                        self.task, [candidate], set(), self.task["search_aliases"], required_episodes={2})
                self.assertTrue(result["success"])
        for title in ("另一部作品 (2022)", "黑色召唤师 (2021)", "黑色召唤师 第二季 (2022)"):
            with self.subTest(title=title):
                detail = self.files(["02.mkv"])
                detail["data"]["share"] = {"title": title}
                candidate = {**self.candidate("second"), "taskname": "黑色召唤师 EP02"}
                with patch.object(self.account, "get_stoken", return_value={"status": 200, "data": {"stoken": "token"}}) as token, \
                        patch.object(self.account, "get_detail", return_value=detail):
                    result = self.account._find_verified_resource(
                        self.task, [candidate], set(), self.task["search_aliases"], required_episodes={2})
                self.assertFalse(result["success"])
                token.assert_called_once()

    def test_missing_episode_failure_logs_queries_and_verified_candidate_reason(self):
        target = self.files(["01.mkv", "03.mkv"])
        candidate_detail = self.files(["01.mkv", "03.mkv"])
        candidate_detail["data"]["share"] = {"title": "黑色召唤师 (2022)"}
        with patch.object(self.account, "ls_dir", return_value=target), \
                patch("resource_search.search_resources", return_value={"success": True, "data": [self.candidate("missing")]}), \
                patch("resource_search.search_web_resources", return_value={"success": True, "data": []}), \
                patch.object(self.account, "get_stoken", return_value={"status": 200, "data": {"stoken": "token"}}), \
                patch.object(self.account, "get_detail", return_value=candidate_detail):
            self.account.repair_episode_gaps(self.task, self.tree())
        self.assertIn("补集已查询关键词", self.output.getvalue())
        self.assertIn("已验证 1 个同作品候选", self.output.getvalue())
        self.assertNotIn("补集转存成功", self.output.getvalue())

    def test_candidate_file_changed_before_transfer_is_not_saved(self):
        for changed_name in ("03.mkv", "S02E02.mkv", "Other Show 02.mkv"):
            with self.subTest(filename=changed_name), \
                    patch.object(self.account, "get_detail", return_value={"code": 0, "data": {
                        "list": [video(changed_name, "second")]}}), \
                    patch.object(self.account, "ls_dir", return_value=self.files(["01.mkv", "03.mkv"])), \
                    patch.object(self.account, "save_file") as saving:
                tree = self.account.dir_check_and_save(self.task, "other", "token", allowed_episode_fids={"second": 2})
                saving.assert_not_called()
                self.assertEqual(tree.size(), 1)

    def test_existing_renamed_episode_and_duplicate_five_do_not_create_a_gap(self):
        target = self.files(["01.mkv", "S01E02.mkv", "03.mkv", "04.mkv", "5.mkv", "05.mkv"])
        with patch.object(self.account, "ls_dir", return_value=target), patch.object(self.account, "find_valid_resource") as search:
            self.account.repair_episode_gaps(self.task, self.tree())
        search.assert_not_called()

    def test_shared_cartoon_directory_does_not_mix_other_titles_or_subtitles(self):
        self.task["savepath"] = "/cartoon"
        self.account.savepath_fid = {"/cartoon": "destination"}
        target = self.files(["Black Summoner S01E01.mkv", "Black Summoner S01E03.mkv",
                             "其他作品 S01E02.mkv", "02.mkv", "Black Summoner S01E02.srt"])
        target["data"]["list"].append({"fid": "foreign", "file_name": "其他作品", "dir": True})
        with patch.object(self.account, "ls_dir", return_value=target) as listing, \
                patch.object(self.account, "find_valid_resource", return_value={"success": False, "error": "未找到"}) as search:
            self.account.repair_episode_gaps(self.task, self.tree())
        self.assertEqual(search.call_args.kwargs["required_episodes"], {2})
        listing.assert_called_once_with("destination")

    def test_only_middle_gaps_are_requested_and_partial_success_is_reported(self):
        target = self.files(["03.mkv", "05.mkv", "07.mkv"])
        supplemental = self.tree()
        supplemental.create_node("04.mkv", "four", parent="", data={
            "file_name": "04.mkv", "file_name_re": "04.mkv", "fid": "saved-four",
            "path": self.task["savepath"] + "/04.mkv", "is_dir": False, "obj_category": "video"})
        result = {"success": True, "candidate": {"pwd_id": "other", "stoken": "token", "pdir_fid": "", "episode_fids": {"four": 4}}}
        tree = self.tree()
        with patch.object(self.account, "ls_dir", return_value=target), \
                patch.object(self.account, "find_valid_resource", side_effect=[result, {"success": False, "error": "未找到"}]) as search, \
                patch.object(self.account, "dir_check_and_save", return_value=supplemental):
            self.account.repair_episode_gaps(self.task, tree)
        self.assertEqual([call.kwargs["required_episodes"] for call in search.call_args_list], [{4, 6}, {6}])
        self.assertEqual(tree.size(), 2)
        self.assertEqual(tree.children(tree.root)[0].data["fid"], "saved-four")
        self.assertIn("仍有中间缺集 [6]", self.output.getvalue())
        self.assertEqual(self.task["shareurl"], "https://pan.quark.cn/s/original")

    def test_title_only_shares_combine_missing_files_and_reach_download_plugin(self):
        existing = [video(f"Black Summoner {episode:02d}.mkv") for episode in (1, 3, 5)]
        original = copy.deepcopy(existing)
        sources = {"two": [video("Black Summoner 02.mkv", "second"), video("01.mkv")],
                   "four": [video("Black Summoner 04.mkv", "fourth"), video("03.mkv")]}
        def detail(share, *_args, **_kwargs):
            return {"code": 0, "data": {"share": {"title": "黑色召唤师 (2022)"},
                    "list": copy.deepcopy(original if share == "original" else sources[share])}}
        def save(fids, _tokens, _destination, share, _stoken):
            self.assertEqual(fids, [sources[share][0]["fid"]])
            existing.append({**sources[share][0], "fid": "saved-" + share})
            return {"code": 0, "data": {"task_id": share}}
        with patch.object(self.account, "get_stoken", return_value={"status": 200, "data": {"stoken": "token"}}), \
                patch.object(self.account, "get_detail", side_effect=detail), \
                patch.object(self.account, "ls_dir", side_effect=lambda *_a, **_k: {"code": 0, "data": {"list": copy.deepcopy(existing)}}), \
                patch.object(self.account, "save_file", side_effect=save) as saving, \
                patch.object(self.account, "query_task", side_effect=lambda share: {"code": 0, "data": {
                    "save_as": {"save_as_top_fids": ["saved-" + share]}}}), \
                patch("resource_search.search_resources", return_value={"success": True, "data": [
                    self.candidate("two"), self.candidate("four")]}) as search:
            tree = self.account.do_save_task(self.task)
            self.assertEqual(tree.size(), 3)
            self.assertEqual({node.data["fid"] for node in tree.children(tree.root)}, {"saved-two", "saved-four"})
            self.assertFalse(self.account.do_save_task(self.task))
        self.assertEqual(saving.call_count, 2)
        self.assertEqual(search.call_count, 2)
        self.assertEqual(self.task["shareurl"], "https://pan.quark.cn/s/original")
        plugin = Mock(spec=["is_active", "run"])
        plugin.is_active, plugin.run.return_value = True, None
        self.account.nickname = "test"
        with patch.object(qas.Config, "load_plugins", return_value=({"aria2": plugin}, {}, {"aria2": {"auto_download": True}})), \
                patch.object(self.account, "update_savepath_fid"), patch.object(self.account, "do_save_task", return_value=tree):
            qas.do_save(self.account, [self.task])
        plugin.run.assert_called_once()
        received = plugin.run.call_args.kwargs["tree"]
        self.assertEqual({node.data["fid"] for node in received.children(received.root)}, {"saved-two", "saved-four"})

    def test_history_alias_survives_gap_file_revalidation_and_next_run(self):
        self.task.pop("search_aliases")
        existing = [video(f"黒の召喚士 {episode:02d}.mkv") for episode in (1, 3)]
        missing = video("Black Summoner 02.mkv", "second")
        detail = {"code": 0, "data": {"share": {"title": "Black Summoner (2022)"}, "list": [missing]}}

        def save(fids, *_args):
            self.assertEqual(fids, ["second"])
            existing.append({**missing, "fid": "saved-second"})
            return {"code": 0, "data": {"task_id": "save"}}

        with tempfile.TemporaryDirectory() as directory:
            self.account.resource_history_path = str(Path(directory) / "history.json")
            qas.Config.remember_resources(self.account.resource_history_path, [{
                "taskname": self.task["taskname"], "shareurl": "https://pan.quark.cn/s/history",
                "search_aliases": ["Black Summoner"],
            }])
            with patch.object(self.account, "get_stoken", return_value={"status": 200, "data": {"stoken": "token"}}) as token, \
                    patch.object(self.account, "get_detail", return_value=detail), \
                    patch.object(self.account, "ls_dir", side_effect=lambda *_a, **_k: {"code": 0, "data": {"list": copy.deepcopy(existing)}}), \
                    patch.object(self.account, "save_file", side_effect=save) as saving, \
                    patch.object(self.account, "query_task", return_value={"code": 0, "data": {"save_as": {"save_as_top_fids": ["saved-second"]}}}), \
                    patch("resource_search.search_resources", side_effect=AssertionError("history should fill the gap")):
                tree = self.tree()
                self.account.repair_episode_gaps(self.task, tree)
                self.assertEqual([node.data["fid"] for node in tree.children(tree.root)], ["saved-second"])
                self.assertEqual(self.task["search_aliases"], ["Black Summoner"])
                self.account.repair_episode_gaps(self.task, self.tree())
            saving.assert_called_once()
            token.assert_called_once()
        self.assertIn("补集转存成功：[2]", self.output.getvalue())
        self.assertNotIn("仍有中间缺集", self.output.getvalue())

    def test_failed_supplement_is_excluded_without_banning_original_and_pending_rename_survives(self):
        candidate = {"success": True, "candidate": {"pwd_id": "other", "pdir_fid": "wrapped", "stoken": "token",
                     "shareurl": "https://pan.quark.cn/s/other?pwd=code", "passcode": "code", "episode_fids": {"second": 2}}}
        pending = {"saved-second": {"original": "02.mkv", "renamed": "Black Summoner S01E02.mkv", "path": self.task["savepath"]}}
        def failed_save(task, *_args, **_kwargs):
            task["shareurl_ban"] = "备用分享已失效"
            task["rename_pending"] = pending
            return Tree()
        with patch.object(self.account, "ls_dir", return_value=self.files(["01.mkv", "03.mkv"])), \
                patch.object(self.account, "find_valid_resource", side_effect=[candidate, {"success": False, "error": "未找到"}]) as search, \
                patch.object(self.account, "dir_check_and_save", side_effect=failed_save) as saving:
            self.account.repair_episode_gaps(self.task, self.tree())
        saving.assert_called_once()
        self.assertEqual(search.call_args.kwargs["required_episodes"], {2})
        self.assertEqual(search.call_args.kwargs["exclude_candidates"], {("other", 0, "code"), ("other", "wrapped", "code")})
        self.assertEqual(self.task["rename_pending"], pending)
        self.assertEqual(self.task["shareurl"], "https://pan.quark.cn/s/original")
        self.assertNotIn("shareurl_ban", self.task)

    def test_partial_save_exception_keeps_pending_rename(self):
        candidate = {"success": True, "candidate": {"pwd_id": "other", "pdir_fid": "", "stoken": "token", "episode_fids": {"second": 2}}}
        pending = {"saved-second": {"original": "02.mkv", "renamed": "Black Summoner S01E02.mkv", "path": self.task["savepath"]}}
        def failed_save(task, *_args, **_kwargs):
            task["rename_pending"] = pending
            raise requests.Timeout()
        with patch.object(self.account, "ls_dir", return_value=self.files(["01.mkv", "03.mkv"])), \
                patch.object(self.account, "find_valid_resource", return_value=candidate), \
                patch.object(self.account, "dir_check_and_save", side_effect=failed_save):
            self.account.repair_episode_gaps(self.task, self.tree())
        self.assertEqual(self.task["rename_pending"], pending)

    def test_supplement_only_file_rename_retries_next_run_then_reaches_plugin(self):
        self.task.update(taskname="黑色召唤师 (2022)", savepath="/cartoon",
                         pattern=qas.MEDIA_PATTERN, replace="{TASKNAME} S01E{EXX}.{EXT}")
        self.account.savepath_fid = {"/cartoon": "destination"}
        existing = [video(f"黑色召唤师 S01E{episode:02d}.mkv") for episode in (1, 3)]
        original = copy.deepcopy(existing)
        def detail(share, *_args, **_kwargs):
            return {"code": 0, "data": {"share": {"title": "黑色召唤师 (2022)"},
                    "list": copy.deepcopy(original if share == "original" else [video("02.mkv", "second")])}}
        def save(fids, *_args):
            self.assertEqual(fids, ["second"])
            existing.append(video("02.mkv", "saved-second"))
            return {"code": 0, "data": {"task_id": "save"}}
        def rename(fid, name):
            self.assertEqual((fid, name), ("saved-second", "黑色召唤师 (2022) S01E02.mkv"))
            existing[-1]["file_name"] = name
            return {"code": 0}
        with patch.object(self.account, "get_stoken", return_value={"status": 200, "data": {"stoken": "token"}}), \
                patch.object(self.account, "get_detail", side_effect=detail), \
                patch.object(self.account, "ls_dir", side_effect=lambda *_a, **_k: {"code": 0, "data": {"list": copy.deepcopy(existing)}}), \
                patch.object(self.account, "save_file", side_effect=save) as saving, \
                patch.object(self.account, "query_task", return_value={"code": 0, "data": {"save_as": {"save_as_top_fids": ["saved-second"]}}}), \
                patch.object(self.account, "rename", return_value={"code": 1}) as renaming, \
                patch("resource_search.search_resources", return_value={"success": True, "data": [self.candidate("other")]}) as search:
            first = self.account.do_save_task(self.task)
            self.assertTrue(first.children(first.root)[0].data["rename_failed"])
            self.assertEqual(set(self.task["rename_pending"]), {"saved-second"})
            renaming.side_effect = rename
            retried = self.account.do_save_task(self.task)
            self.assertEqual(retried.children(retried.root)[0].data["path"], "/cartoon/黑色召唤师 (2022) S01E02.mkv")
            self.assertNotIn("rename_pending", self.task)
        saving.assert_called_once()
        search.assert_called_once()
        self.assertEqual(renaming.call_count, 2)
        plugin = Mock(spec=["is_active", "run"])
        plugin.is_active, plugin.run.return_value = True, None
        self.account.nickname = "test"
        with patch.object(qas.Config, "load_plugins", return_value=({"aria2": plugin}, {}, {"aria2": {"auto_download": True}})), \
                patch.object(self.account, "update_savepath_fid"), patch.object(self.account, "do_save_task", return_value=retried):
            qas.do_save(self.account, [self.task])
        plugin.run.assert_called_once()
        received = plugin.run.call_args.kwargs["tree"].children(retried.root)[0].data
        self.assertEqual(received["fid"], "saved-second")
        self.assertEqual(received["path"], "/cartoon/黑色召唤师 (2022) S01E02.mkv")
        self.assertNotIn("rename_failed", received)

    def test_pending_supplement_rename_requires_exact_identity_path_and_media_filter(self):
        for filename, fid, recorded, pattern in (
            ("02.mkv", "other-fid", {}, self.task["pattern"]),
            ("02.mkv", "second", {"original": "03.mkv"}, self.task["pattern"]),
            ("02.mkv", "second", {"path": "/other"}, self.task["pattern"]),
            ("02.mkv", "second", {}, r".*\.mp4$"),
            ("02.exe", "second", {"original": "02.exe"}, ".*"),
        ):
            with self.subTest(filename=filename, fid=fid, recorded=recorded, pattern=pattern):
                task = {**self.task, "pattern": pattern, "rename_pending": {"second": {
                    "original": "02.mkv", "renamed": "Black Summoner S01E02.mkv", "path": self.task["savepath"], **recorded}}}
                tree = self.tree()
                with patch.object(self.account, "ls_dir", return_value={"code": 0, "data": {"list": [video(filename, fid)]}}), \
                        patch.object(self.account, "find_valid_resource") as search:
                    self.account.repair_episode_gaps(task, tree)
                self.assertEqual(tree.size(), 1)
                search.assert_not_called()

    def test_gap_search_stops_after_three_distinct_shares_or_elapsed_budget(self):
        for slow in (False, True):
            with self.subTest(slow=slow):
                clock, calls = {"now": 0}, []
                def search(*_args, **kwargs):
                    calls.append(kwargs)
                    if slow:
                        clock["now"] = 121
                    return {"success": True, "candidate": {"pwd_id": "other" + str(len(calls)),
                            "stoken": "token", "pdir_fid": "", "episode_fids": {"second": 2}}}
                with patch.object(qas.time, "monotonic", side_effect=lambda: clock["now"]), \
                        patch.object(self.account, "ls_dir", return_value=self.files(["01.mkv", "03.mkv"])), \
                        patch.object(self.account, "find_valid_resource", side_effect=search), \
                        patch.object(self.account, "dir_check_and_save", return_value=Tree()) as saving:
                    self.account.repair_episode_gaps(self.task, self.tree())
                self.assertEqual(len(calls), 1 if slow else 3)
                self.assertEqual(saving.call_count, len(calls))
                self.assertTrue(all(call["required_episodes"] == {2} for call in calls))
                self.assertEqual(len(calls[-1]["exclude_candidates"]), len(calls) - 1)

    def test_normal_save_fills_only_two_then_does_not_repeat(self):
        existing = [video(name) for name in ("黑色召唤师 01.mkv", "黑色召唤师 03.mkv", "黑色召唤师 04.mkv", "黑色召唤师 05.mkv", "05.mkv")]
        existing += [{"fid": "empty-folder", "file_name": "熟肉晚点", "dir": True},
                     {"fid": "cover", "file_name": "封面.png", "dir": False, "obj_category": "image"}]
        original = copy.deepcopy(existing)
        other = [video("Black Summoner 02.mkv", "second"), video("02.mp4", "second-duplicate"), video("06.mkv", "sixth")]
        def detail(share, _token, folder, **_kwargs):
            files = [] if folder == "empty-folder" else (original if share == "original" else other)
            return {"code": 0, "data": {"share": {"title": "黑色召唤师 (2022)"}, "list": copy.deepcopy(files)}}
        def save(fids, *_args):
            self.assertEqual(fids, ["second"])
            existing.append(video("Black Summoner 02.mkv", "saved-second"))
            return {"code": 0, "data": {"task_id": "save"}}
        before = copy.deepcopy(self.task)
        with patch.object(self.account, "get_stoken", return_value={"status": 200, "data": {"stoken": "token"}}), \
                patch.object(self.account, "get_detail", side_effect=detail), \
                patch.object(self.account, "ls_dir", side_effect=lambda fid, **_k: {"code": 0, "data": {
                    "list": [] if fid == "empty-folder" else copy.deepcopy(existing)}}), \
                patch.object(self.account, "save_file", side_effect=save) as saving, \
                patch.object(self.account, "query_task", return_value={"code": 0, "data": {"save_as": {"save_as_top_fids": ["saved-second"]}}}), \
                patch("resource_search.search_resources", return_value={"success": True, "data": [self.candidate("other")]}) as search:
            tree = self.account.do_save_task(self.task)
            self.assertEqual(tree.size(), 2)
            self.assertEqual(tree.children(tree.root)[0].data["fid"], "saved-second")
            self.assertFalse(self.account.do_save_task(self.task))
        saving.assert_called_once()
        search.assert_called_once()
        self.assertEqual(self.task, before)

    def test_supplement_tree_reaches_aria2_with_saved_destination_fid(self):
        target = self.files(["01.mkv", "03.mkv"])
        supplemental = self.tree()
        supplemental.create_node("02.mkv", "source-second", parent="", data={
            "file_name": "02.mkv", "file_name_re": "02.mkv", "fid": "destination-second",
            "path": self.task["savepath"] + "/02.mkv", "is_dir": False, "obj_category": "video"})
        result = {"success": True, "candidate": {"pwd_id": "other", "stoken": "token", "pdir_fid": "", "episode_fids": {"source-second": 2}}}
        tree = self.tree()
        with patch.object(self.account, "ls_dir", return_value=target), \
                patch.object(self.account, "find_valid_resource", return_value=result), \
                patch.object(self.account, "dir_check_and_save", return_value=supplemental):
            self.account.repair_episode_gaps(self.task, tree)
        plugin = Mock(spec=["is_active", "run"])
        plugin.is_active = True
        plugin.run.return_value = None
        self.account.nickname = "test"
        with patch.object(qas.Config, "load_plugins", return_value=({"aria2": plugin}, {}, {"aria2": {"auto_download": True}})), \
                patch.object(self.account, "update_savepath_fid"), patch.object(self.account, "do_save_task", return_value=tree):
            qas.do_save(self.account, [self.task])
        plugin.run.assert_called_once()
        received = plugin.run.call_args.kwargs["tree"]
        self.assertEqual(received.children(received.root)[0].data["fid"], "destination-second")
        self.assertEqual(received.children(received.root)[0].data["path"], self.task["savepath"] + "/02.mkv")

    def test_directory_checks_are_bounded_and_do_not_traverse_nonempty_folders(self):
        folder = {"fid": "sub", "file_name": "子目录", "dir": True}
        for child in ({"code": 1}, self.files(["02.mkv"]), {"code": 0, "data": {"list": [folder]}}):
            with self.subTest(child=child), patch.object(self.account, "ls_dir", side_effect=[{
                    "code": 0, "data": {"list": [video("01.mkv"), video("03.mkv"), folder]}}, child]) as listing, \
                    patch.object(self.account, "find_valid_resource") as search:
                self.account.repair_episode_gaps(self.task, self.tree())
                self.assertEqual(listing.call_count, 2)
                search.assert_not_called()
        with patch.object(self.account, "ls_dir", return_value={"code": 0, "data": {"list": [folder] * 4}}) as listing:
            self.account.repair_episode_gaps(self.task, self.tree())
            listing.assert_called_once()

    def test_lookup_or_transfer_failure_keeps_existing_tree_and_subscription(self):
        target = self.files(["01.mkv", "03.mkv"])
        candidate = {"success": True, "candidate": {"pwd_id": "other", "stoken": "token", "pdir_fid": "", "episode_fids": {"second": 2}}}
        for lookup in ({"success": False, "error": "没有经过验证的缺集资源"}, candidate, requests.Timeout()):
            with self.subTest(lookup=type(lookup).__name__):
                tree, before = self.tree(), copy.deepcopy(self.task)
                with patch.object(self.account, "ls_dir", return_value=target), \
                        patch.object(self.account, "find_valid_resource", side_effect=lookup if isinstance(lookup, Exception) else None,
                                     return_value=lookup), patch.object(self.account, "dir_check_and_save", return_value=Tree()):
                    self.account.repair_episode_gaps(self.task, tree)
                self.assertEqual(tree.size(), 1)
                self.assertEqual(self.task, before)
        self.assertNotIn("补集转存成功", self.output.getvalue())

    def test_complex_tasks_and_ambiguous_target_files_are_left_alone(self):
        for changes in ({"resource_id": ""}, {"resource_type": "document"}, {"auto_repair": False},
                        {"startfid": "boundary"}, {"replace": "{I}.mkv"}, {"update_subdir_resave_mode": True}):
            with self.subTest(changes=changes), patch.object(self.account, "ls_dir") as listing:
                self.account.repair_episode_gaps({**self.task, **changes}, self.tree())
                listing.assert_not_called()
        for extra in (video("unrecognized.mkv"), {"fid": "sub", "file_name": "另一季", "dir": True}):
            with self.subTest(extra=extra["file_name"]), patch.object(self.account, "ls_dir", return_value={"code": 0, "data": {
                    "list": [video("01.mkv"), video("03.mkv"), extra]}}), patch.object(self.account, "find_valid_resource") as search:
                self.account.repair_episode_gaps(self.task, self.tree())
                search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
