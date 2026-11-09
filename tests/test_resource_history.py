import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import quark_auto_save as qas


class ResourceHistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.history_path = str(Path(directory.name) / "resources.json")
        self.account = qas.Quark()
        self.account.resource_history_path = self.history_path
        self.task = {"taskname": "测试作品 (2026)", "pattern": qas.MEDIA_PATTERN, "replace": ""}
        self.old = {"taskname": self.task["taskname"], "shareurl": "https://pan.quark.cn/s/old"}
        self.new = {**self.old, "shareurl": "https://pan.quark.cn/s/new"}
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def detail(self, episode="02", title="测试作品 (2026)"):
        return {"code": 0, "data": {"share": {"title": title}, "list": [
            {"fid": episode, "file_name": episode + ".mkv", "dir": False},
        ]}}

    def test_records_survive_new_account_and_are_revalidated_without_credentials(self):
        with patch.object(self.account, "get_stoken", return_value={"status": 200, "data": {"stoken": "sensitive"}}), \
                patch.object(self.account, "get_detail", return_value=self.detail()):
            self.assertTrue(self.account.verify_resource_link(self.task, self.old["shareurl"])["success"])
        stored = Path(self.history_path).read_text(encoding="utf-8")
        self.assertNotIn("sensitive", stored)
        restarted = qas.Quark()
        restarted.resource_history_path = self.history_path
        with patch.object(restarted, "get_stoken", return_value={"status": 200, "data": {"stoken": "fresh"}}) as token, \
                patch.object(restarted, "get_detail", return_value=self.detail()), \
                patch("resource_search.search_resources", side_effect=AssertionError("search outage")):
            self.assertTrue(restarted.find_valid_resource(self.task, {})["success"])
        token.assert_called_once_with("old", "")

    def test_expired_or_changed_history_continues_to_live_search(self):
        qas.Config.remember_resources(self.history_path, [self.old])
        for stale in ({"code": 1, "message": "分享已取消"}, self.detail(title="另一部作品 (2026)"), self.detail(title="测试作品 (2025)")):
            with self.subTest(stale=stale), \
                    patch.object(self.account, "get_stoken", return_value={"status": 200, "data": {"stoken": "t"}}), \
                    patch.object(self.account, "get_detail", side_effect=lambda share, *_a, **_k: stale if share == "old" else self.detail()), \
                    patch("resource_search.search_resources", return_value={"success": True, "data": [self.new]}) as search:
                qas.Config.write_json(self.history_path, [self.old])
                result = self.account.find_valid_resource(self.task, {})
                self.assertTrue(result["success"])
                self.assertEqual(result["candidate"]["pwd_id"], "new")
                search.assert_called_once()

    def test_remembered_catalog_alias_can_revalidate_after_catalog_alias_lookup_fails(self):
        qas.Config.remember_resources(self.history_path, [{**self.old, "search_aliases": ["官方译名"]}])
        with patch.object(self.account, "get_stoken", return_value={"status": 200, "data": {"stoken": "t"}}), \
                patch.object(self.account, "get_detail", return_value=self.detail(title="官方译名 (2026)")), \
                patch("resource_search.search_resources", side_effect=AssertionError("no live search needed")):
            result = self.account.find_valid_resource(self.task, {})
        self.assertTrue(result["success"])
        self.assertEqual(result["candidate"]["search_aliases"], ["官方译名"])

    def test_history_pack_without_gap_episode_does_not_stop_searching_other_packs(self):
        qas.Config.remember_resources(self.history_path, [self.old])
        with patch.object(self.account, "get_stoken", return_value={"status": 200, "data": {"stoken": "t"}}), \
                patch.object(self.account, "get_detail", side_effect=lambda share, *_a, **_k: self.detail("03" if share == "old" else "02")), \
                patch("resource_search.search_resources", return_value={"success": True, "data": [self.old, self.new]}) as search:
            result = self.account.find_valid_resource(self.task, {}, required_episodes={2})
        self.assertTrue(result["success"])
        self.assertEqual(result["candidate"]["episode_fids"], {"02": 2})
        self.assertEqual(result["candidate"]["pwd_id"], "new")
        search.assert_called_once()

    def test_history_keeps_alternatives_but_not_credentials_or_nonquark_urls(self):
        task = {**self.old, "cookie": "cookie-secret", "stoken": "token-secret", "addition": {"aria2": {"secret": "rpc-secret"}}}
        qas.Config.remember_resources(self.history_path, [task, self.new, task,
            {**task, "shareurl": "https://127.0.0.1/s/private"}])
        self.assertEqual(qas.Config.resource_history(self.history_path), [self.old, self.new])

    def test_corrupted_history_does_not_block_live_search(self):
        Path(self.history_path).write_text("{broken", encoding="utf-8")
        with patch.object(self.account, "get_stoken", return_value={"status": 200, "data": {"stoken": "t"}}), \
                patch.object(self.account, "get_detail", return_value=self.detail()), \
                patch("resource_search.search_resources", return_value={"success": True, "data": [self.new]}):
            self.assertTrue(self.account.find_valid_resource(self.task, {})["success"])
        self.assertIn("资源记录读取失败", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
