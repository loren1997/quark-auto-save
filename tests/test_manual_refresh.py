import contextlib
from copy import deepcopy
import io
import unittest
from unittest.mock import patch

import requests
from treelib import Tree

import quark_auto_save as qas
from resource_search import candidate_timestamp


class ManualRefreshTests(unittest.TestCase):
    def setUp(self):
        self.account = qas.Quark()
        self.task = {"taskname": "测试作品", "shareurl": "https://pan.quark.cn/s/original1234",
                     "savepath": "/cartoon", "pattern": r".*\.mkv$", "replace": "{TASKNAME} S04E{E}.{EXT}",
                     "startfid": "old-boundary", "auto_repair": False,
                     "addition": {"aria2": {"auto_download": True}}}
        self.candidate = {"shareurl": "https://pan.quark.cn/s/replacement1", "pwd_id": "replacement1",
                          "pdir_fid": "0", "stoken": "verified-token", "source": "fixture"}
        self.token = {"status": 200, "code": 0, "data": {"stoken": "original-token"}}
        self.tree = Tree()
        self.tree.create_node("/cartoon", "root")
        self.output = io.StringIO()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network")))
        self.stack.enter_context(patch.object(qas, "CONFIG_DATA", {"auto_repair": False}))
        self.stack.enter_context(patch.object(qas, "add_notify"))
        self.stack.enter_context(patch.object(self.account, "do_rename"))
        self.stack.enter_context(patch.object(self.account, "repair_episode_gaps"))
        self.transfer = self.stack.enter_context(patch.object(self.account, "dir_check_and_save", return_value=self.tree))

    def test_manual_valid_original_is_replaced_before_transfer_even_when_auto_repair_disabled(self):
        before = deepcopy(self.task)
        with patch.object(self.account, "find_valid_resource", return_value={"success": True, "candidate": self.candidate}) as find, \
                patch.object(self.account, "get_stoken") as token:
            self.account.do_save_task(self.task, latest=True)
        find.assert_called_once_with(self.task, qas.CONFIG_DATA, latest=True)
        token.assert_not_called()
        self.assertEqual(self.transfer.call_args.args[1:4], ("replacement1", "verified-token", 0))
        self.assertEqual(self.task["shareurl"], self.candidate["shareurl"])
        self.assertNotIn("startfid", self.task)
        for field in ("pattern", "replace", "savepath", "addition", "auto_repair"):
            self.assertEqual(self.task[field], before[field])

    def test_missing_or_failed_latest_search_retains_original_and_runs_it(self):
        for outcome in ({"success": False, "error": "未找到"}, requests.Timeout("private-error")):
            with self.subTest(outcome=type(outcome).__name__):
                before = deepcopy(self.task)
                with patch.object(self.account, "find_valid_resource", return_value=outcome,
                                  side_effect=outcome if isinstance(outcome, Exception) else None) as find, \
                        patch.object(self.account, "get_stoken", return_value=self.token):
                    self.account.do_save_task(self.task, latest=True)
                find.assert_called_once()
                self.assertEqual(self.task, before)
                self.assertEqual(self.transfer.call_args.args[1:4], ("original1234", "original-token", 0))
        self.assertNotIn("private-error", self.output.getvalue())

    def test_failed_search_and_expired_original_do_not_trigger_second_search(self):
        with patch.object(self.account, "find_valid_resource", return_value={"success": False, "error": "未找到"}) as find, \
                patch.object(self.account, "get_stoken", return_value={"status": 404, "code": 41012, "message": "分享已取消"}):
            self.account.do_save_task(self.task, latest=True)
        find.assert_called_once()
        self.transfer.assert_not_called()
        self.assertEqual(self.task["shareurl"], "https://pan.quark.cn/s/original1234")

    def test_rediscovered_current_link_keeps_file_boundary_and_history(self):
        candidate = {**self.candidate, "shareurl": self.task["shareurl"], "pwd_id": "original1234"}
        before = deepcopy(self.task)
        with patch.object(self.account, "find_valid_resource", return_value={"success": True, "candidate": candidate}):
            self.account.do_save_task(self.task, latest=True)
        self.assertEqual(self.task, before)

    def test_scheduled_valid_original_does_not_search(self):
        with patch.object(self.account, "find_valid_resource") as find, \
                patch.object(self.account, "get_stoken", return_value=self.token):
            self.account.do_save_task(self.task)
        find.assert_not_called()
        self.assertEqual(self.transfer.call_args.args[1], "original1234")

    def row(self, share, date):
        return {"taskname": self.task["taskname"], "shareurl": f"https://pan.quark.cn/s/{share}", "datetime": date}

    def search_setup(self, index, web=None, archive=None):
        empty = {"success": True, "data": [], "errors": []}
        indexed = self.stack.enter_context(patch("resource_search.search_resources", return_value={**empty, "data": index}))
        webpage = self.stack.enter_context(patch("resource_search.search_web_resources", return_value={**empty, "data": web or []}))
        telegram = self.stack.enter_context(patch("resource_search.search_telegram_resources",
                                                side_effect=archive or [{**empty, "partial": False}]))
        self.stack.enter_context(patch.object(qas.Config, "resource_history", return_value=[self.row("historyshare", "2030-01-01")]))
        self.stack.enter_context(patch.object(qas.Config, "remember_resources"))
        self.stack.enter_context(patch.object(self.account, "_video_leaf_folders", return_value=[("0", [{"fid": "v", "file_name": "01.mkv"}])]))
        return indexed, webpage, telegram

    def test_fresh_sources_sorted_together_newest_invalid_skipped_without_history_shortcut(self):
        index, web, _ = self.search_setup([self.row("olderindex12", "2026-11-20T10:00:00Z")],
                                        [self.row("invalidnew12", "2026-11-22T10:00:00Z"),
                                         self.row("validnew1234", "2026-11-21T10:00:00Z")])
        def token(share, passcode):
            return {"status": 404, "code": 41012} if share == "invalidnew12" else self.token
        with patch.object(self.account, "get_stoken", side_effect=token) as tokens:
            result = self.account.find_valid_resource(self.task, {}, latest=True)
        self.assertEqual(result["candidate"]["pwd_id"], "validnew1234")
        self.assertEqual([call.args[0] for call in tokens.call_args_list], ["invalidnew12", "validnew1234"])
        self.assertTrue(all(call.kwargs["refresh"] for call in index.call_args_list + web.call_args_list))

    def test_latest_search_allows_current_and_previously_used_share_from_fresh_index(self):
        self.search_setup([self.row("original1234", "2026-11-22")])
        self.task["repair_history"] = [{"from_id": "original1234"}]
        with patch.object(self.account, "get_stoken", return_value=self.token):
            result = self.account.find_valid_resource(self.task, {}, latest=True)
        self.assertEqual(result["candidate"]["pwd_id"], "original1234")

    def test_archive_refresh_only_first_page_and_timestamp_competes_with_other_sources(self):
        _, _, archive = self.search_setup([self.row("olderindex12", "2026-11-20")], archive=[
            {"success": True, "data": [self.row("olderarchive", "2026-11-19")], "partial": True},
            {"success": True, "data": [self.row("newarchive12", 1795305600)], "partial": False},
        ])
        with patch.object(self.account, "get_stoken", return_value=self.token):
            result = self.account.find_valid_resource(self.task, {"source": {"telegram_web": {"enable": True}}}, latest=True)
        self.assertEqual(result["candidate"]["pwd_id"], "newarchive12")
        self.assertTrue(archive.call_args_list[0].kwargs["refresh"])
        self.assertNotIn("refresh", archive.call_args_list[1].kwargs)

    def test_recency_handles_timezones_unix_milliseconds_and_unknown_dates(self):
        same = ["2026-11-22T08:00:00+08:00", "2026-11-22T00:00:00Z", "2026-11-22 08:00:00", 1795305600, 1795305600000]
        self.assertEqual(len({candidate_timestamp({"datetime": value}) for value in same}), 1)
        for value in (None, "unknown", {}, True, float("nan"), float("inf")):
            self.assertEqual(candidate_timestamp({"datetime": value}), 0)
        self.assertEqual(candidate_timestamp({"datetime": "2026-11-22"}),
                         candidate_timestamp({"datetime": "2026-11-21T16:00:00Z"}))

    def test_irrelevant_bulk_links_do_not_hide_a_newer_later_batch(self):
        junk = [{**self.row(f"other{i:08d}", "2026-11-20"), "taskname": "其他短剧"} for i in range(80)]
        _, _, archive = self.search_setup([], archive=[
            {"success": True, "data": [self.row("original1234", "2026-11-19"), *junk[:39]], "partial": True},
            {"success": True, "data": junk[39:], "partial": True},
            {"success": True, "data": [self.row("newarchive12", 1795305600)], "partial": False},
        ])
        with patch.object(self.account, "get_stoken", return_value=self.token) as token:
            result = self.account.find_valid_resource(self.task, {"source": {"telegram_web": {"enable": True}}}, latest=True)
        self.assertEqual(result["candidate"]["pwd_id"], "newarchive12")
        token.assert_called_once_with("newarchive12", "")
        self.assertEqual(archive.call_count, 3)


if __name__ == "__main__":
    unittest.main()
