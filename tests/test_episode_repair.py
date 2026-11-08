import contextlib
import copy
import io
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
                patch.object(self.account, "find_valid_resource", return_value=result) as search, \
                patch.object(self.account, "dir_check_and_save", return_value=supplemental):
            self.account.repair_episode_gaps(self.task, tree)
        self.assertEqual(search.call_args.kwargs["required_episodes"], {4, 6})
        self.assertEqual(tree.size(), 2)
        self.assertEqual(tree.children(tree.root)[0].data["fid"], "saved-four")
        self.assertIn("仍有中间缺集 [6]", self.output.getvalue())
        self.assertEqual(self.task["shareurl"], "https://pan.quark.cn/s/original")

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
