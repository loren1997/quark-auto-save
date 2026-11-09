import contextlib
from copy import deepcopy
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests
import quark_auto_save as qas
from media_files import MEDIA_PATTERN


class PendingSubscriptionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "config.json")
        self.task = {"resource_id": "bangumi:42", "taskname": "测试作品 (2026)", "resource_type": "anime",
                     "search_aliases": ["Trusted Alias"], "savepath": "/cartoon", "pattern": MEDIA_PATTERN,
                     "replace": "{TASKNAME} S01E{EXX}.{EXT}", "update_subdir": ".*", "runweek": list(range(1, 8)),
                     "addition": {"aria2": {"auto_download": True, "download_subdir": True, "save_path": "cartoon"}}}
        self.pending = {"id": "a" * 32, "task": deepcopy(self.task), "created_at": "2026-11-01T08:00:00",
                        "last_attempt_at": "2026-11-01T08:00:00", "last_error": "请求超时"}
        self.config = {"cookie": ["__uid=fake-cookie"], "tasklist": [], "pending_subscriptions": [self.pending],
                       "discovery": {"savepath": "/cartoon", "auto_download": True},
                       "plugins": {"aria2": {"host_port": "192.0.2.1:6800", "secret": "private-rpc", "dir": "/downloads"}}}
        qas.Config.write_json(self.path, self.config)
        self.account = qas.Quark("__uid=fake-cookie")
        self.account.resource_history_path = qas.Config.resource_history_path(self.path)
        self.success = {"success": True, "candidate": {"shareurl": "https://pan.quark.cn/s/verified",
                                                        "stoken": "must-not-persist", "pwd_id": "verified",
                                                        "search_aliases": ["Trusted Alias"]}}
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected HTTP request"))
        network.start()
        self.addCleanup(network.stop)

    def read(self):
        return qas.Config.read_json(self.path)

    def retry(self, result=None, side_effect=None):
        with patch.object(self.account, "find_valid_resource", return_value=result, side_effect=side_effect) as find, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            promoted = qas.retry_pending_subscriptions(self.account, self.path)
        return promoted, find, output.getvalue()

    def test_saved_intent_promotes_after_restart_without_calendar_and_without_secrets(self):
        promoted, find, _ = self.retry(self.success)
        self.assertEqual(len(promoted), 1)
        self.assertEqual(promoted[0]["shareurl"], self.success["candidate"]["shareurl"])
        self.assertEqual(promoted[0]["savepath"], "/cartoon")
        self.assertEqual(promoted[0]["replace"], "{TASKNAME} S01E{EXX}.{EXT}")
        self.assertEqual(promoted[0]["search_aliases"], ["Trusted Alias"])
        self.assertEqual(self.read()["tasklist"], promoted)
        self.assertEqual(self.read()["pending_subscriptions"], [])
        find.assert_called_once()
        self.assertEqual(find.call_args.args[0], self.task)
        self.assertNotIn("must-not-persist", Path(self.path).read_text(encoding="utf-8"))
        self.assertNotIn("stoken", str(promoted))
        again, find, _ = self.retry(self.success)
        self.assertEqual(again, [])
        find.assert_not_called()

    def test_unavailable_or_wrong_candidate_keeps_intent_for_next_run(self):
        for result in ({"success": False, "code": "unavailable", "error": "Google 需要人工验证"},
                       {"success": False, "code": "no_match", "error": "候选作品和季度不匹配"},
                       {"success": True, "candidate": None}):
            with self.subTest(result=result):
                promoted, _, output = self.retry(result)
                self.assertEqual(promoted, [])
                self.assertEqual(self.read()["tasklist"], [])
                self.assertEqual(self.read()["pending_subscriptions"][0]["id"], self.pending["id"])
                self.assertIn("下次定时运行", output)
        promoted, _, output = self.retry(side_effect=requests.Timeout("private-request"))
        self.assertEqual(promoted, [])
        self.assertNotIn("private-request", output)
        self.assertEqual(self.read()["pending_subscriptions"][0]["task"], self.task)

    def test_cancellation_during_search_wins_for_success_and_failure(self):
        for result in (self.success, {"success": False, "error": "请求超时"}):
            with self.subTest(result=result):
                qas.Config.write_json(self.path, self.config)
                def cancel(*_args):
                    current = self.read()
                    current["pending_subscriptions"] = []
                    qas.Config.write_json(self.path, current)
                    return result
                promoted, _, output = self.retry(side_effect=cancel)
                self.assertEqual(promoted, [])
                self.assertEqual(self.read()["tasklist"], [])
                self.assertEqual(self.read()["pending_subscriptions"], [])
                self.assertIn("已取消", output)

    def test_cancel_then_readd_same_show_does_not_apply_old_search_result(self):
        def readd(*_args):
            current = self.read()
            current["pending_subscriptions"][0]["id"] = "b" * 32
            qas.Config.write_json(self.path, current)
            return self.success
        promoted, _, _ = self.retry(side_effect=readd)
        self.assertEqual(promoted, [])
        self.assertEqual(self.read()["tasklist"], [])
        self.assertEqual(self.read()["pending_subscriptions"][0]["id"], "b" * 32)

    def test_existing_formal_subscription_deduplicates_without_search(self):
        self.config["tasklist"] = [dict(self.task, shareurl="https://pan.quark.cn/s/existing")]
        qas.Config.write_json(self.path, self.config)
        promoted, find, _ = self.retry(self.success)
        self.assertEqual(promoted, [])
        find.assert_not_called()
        self.assertEqual(self.read()["tasklist"], self.config["tasklist"])
        self.assertEqual(self.read()["pending_subscriptions"], [])

    def test_disabled_downloader_keeps_intent_without_search_or_transfer(self):
        self.config["plugins"]["aria2"].pop("secret")
        qas.Config.write_json(self.path, self.config)
        promoted, find, output = self.retry(self.success)
        self.assertEqual(promoted, [])
        find.assert_not_called()
        self.assertEqual(self.read()["tasklist"], [])
        self.assertIn("Aria2 配置不完整", output)
        self.assertEqual(len(self.read()["pending_subscriptions"]), 1)

    def test_retry_batch_is_bounded_and_next_run_starts_with_oldest_untried_show(self):
        records = []
        for index in range(4):
            record = deepcopy(self.pending)
            record["id"] = str(index + 1) * 32
            record["task"].update(resource_id=f"bangumi:{index + 1}", taskname=f"测试作品{index + 1} (2026)")
            records.append(record)
        self.config["pending_subscriptions"] = records
        qas.Config.write_json(self.path, self.config)
        promoted, find, output = self.retry({"success": False, "error": "暂未找到资源"})
        self.assertEqual(promoted, [])
        self.assertEqual(find.call_count, 3)
        self.assertIn("最久未查找的 3 部", output)
        self.assertEqual([call.args[0]["resource_id"] for call in find.call_args_list],
                         ["bangumi:1", "bangumi:2", "bangumi:3"])
        self.assertEqual(self.read()["pending_subscriptions"][3]["last_attempt_at"], self.pending["last_attempt_at"])
        _, find, _ = self.retry({"success": False, "error": "暂未找到资源"})
        self.assertEqual(find.call_count, 3)
        self.assertEqual(find.call_args_list[0].args[0]["resource_id"], "bangumi:4")

    def test_one_failed_pending_does_not_block_next_verified_subscription(self):
        second = deepcopy(self.pending)
        second["id"] = "b" * 32
        second["task"].update(resource_id="bangumi:43", taskname="另一作品 (2026)")
        self.config["pending_subscriptions"].append(second)
        qas.Config.write_json(self.path, self.config)
        promoted, find, _ = self.retry(side_effect=[requests.Timeout(), self.success])
        self.assertEqual(find.call_count, 2)
        self.assertEqual([task["resource_id"] for task in promoted], ["bangumi:43"])
        self.assertEqual([item["task"]["resource_id"] for item in self.read()["pending_subscriptions"]], ["bangumi:42"])

    def test_runtime_state_merge_cannot_recreate_cancelled_pending_intent(self):
        original = deepcopy(self.config)
        updated = deepcopy(original)
        updated["pending_subscriptions"][0]["last_error"] = "新状态"
        latest = deepcopy(original)
        latest["pending_subscriptions"] = []
        qas.Config.merge_runtime_state(latest, original, updated)
        self.assertEqual(latest["pending_subscriptions"], [])

    def test_scheduled_main_resolves_pending_and_invokes_transfer_and_download_in_same_run(self):
        self.account.is_active = True
        self.account.nickname = "test"
        self.account.update_savepath_fid = Mock()
        tree = qas.Tree()
        tree.create_node("/cartoon", "root")
        tree.create_node("测试作品 S01E01.mkv", "video", parent="root")
        self.account.do_save_task = Mock(return_value=tree)
        def submit(task, **_kwargs):
            task["aria2_pending"] = [{"fid": "video", "file_name": "测试作品 S01E01.mkv"}]
            return task
        plugin = SimpleNamespace(is_active=True, run=Mock(side_effect=submit))
        with patch.object(qas.sys, "argv", ["quark_auto_save.py", self.path]), \
                patch.dict(os.environ, {"TASKLIST": "", "QUARK_TEST": ""}), \
                patch.object(qas, "CONFIG_DATA", {}), patch.object(qas, "NOTIFYS", []), \
                patch.object(qas, "Quark", return_value=self.account), \
                patch.object(qas, "verify_account"), patch.object(qas, "do_sign"), \
                patch.object(self.account, "find_valid_resource", return_value=self.success), \
                patch.object(qas.Config, "load_plugins", return_value=({"aria2": plugin}, self.config["plugins"], {})), \
                contextlib.redirect_stdout(io.StringIO()):
            qas.main()
        self.account.do_save_task.assert_called_once()
        task = self.account.do_save_task.call_args.args[0]
        self.assertEqual(task["shareurl"], self.success["candidate"]["shareurl"])
        self.assertEqual(task["savepath"], "/cartoon")
        plugin.run.assert_called_once()
        self.assertEqual(plugin.run.call_args.args[0]["addition"]["aria2"]["save_path"], "cartoon")
        self.assertEqual(self.read()["pending_subscriptions"], [])
        self.assertEqual(len(self.read()["tasklist"]), 1)
        self.assertEqual(self.read()["tasklist"][0]["aria2_pending"][0]["fid"], "video")

    def test_missing_cookie_skips_pending_without_dropping_intent(self):
        self.config["cookie"] = []
        qas.Config.write_json(self.path, self.config)
        with patch.object(qas.sys, "argv", ["quark_auto_save.py", self.path]), \
                patch.dict(os.environ, {"TASKLIST": "", "QUARK_TEST": ""}), \
                patch.object(qas, "CONFIG_DATA", {}), patch.object(qas, "NOTIFYS", []), \
                patch.object(qas, "retry_pending_subscriptions") as retry, \
                contextlib.redirect_stdout(io.StringIO()):
            qas.main()
        retry.assert_not_called()
        self.assertEqual(self.read()["pending_subscriptions"], [self.pending])

    def test_main_reloads_deleted_or_edited_promoted_tasks_and_plugins_after_search_batch(self):
        for delete_first in (False, True):
            with self.subTest(delete_first=delete_first):
                config = deepcopy(self.config)
                second = deepcopy(self.pending)
                second["id"] = "b" * 32
                second["task"].update(resource_id="bangumi:43", taskname="另一作品 (2026)")
                config["pending_subscriptions"].append(second)
                qas.Config.write_json(self.path, config)
                self.account.is_active = True
                def find(task, _config):
                    if task["resource_id"] == "bangumi:42":
                        return self.success
                    latest = self.read()
                    if delete_first:
                        latest["tasklist"] = []
                    else:
                        latest["tasklist"][0]["savepath"] = "/用户修改路径"
                        latest["tasklist"][0]["addition"]["aria2"]["save_path"] = "changed"
                    latest["plugins"]["aria2"]["dir"] = "/updated-downloads"
                    qas.Config.write_json(self.path, latest)
                    return {"success": False, "error": "暂未找到资源"}
                def transfer(account, tasks):
                    self.assertIs(account, self.account)
                    self.assertEqual(qas.CONFIG_DATA["plugins"]["aria2"]["dir"], "/updated-downloads")
                    self.assertEqual(len(tasks), 0 if delete_first else 1)
                    if tasks:
                        self.assertEqual(tasks[0]["savepath"], "/用户修改路径")
                        self.assertEqual(tasks[0]["addition"]["aria2"]["save_path"], "changed")
                        tasks[0]["aria2_pending"] = [{"fid": "video"}]
                with patch.object(qas.sys, "argv", ["quark_auto_save.py", self.path]), \
                        patch.dict(os.environ, {"TASKLIST": "", "QUARK_TEST": ""}), \
                        patch.object(qas, "CONFIG_DATA", {}), patch.object(qas, "NOTIFYS", []), \
                        patch.object(qas, "Quark", return_value=self.account), \
                        patch.object(qas, "verify_account"), patch.object(qas, "do_sign"), \
                        patch.object(self.account, "find_valid_resource", side_effect=find), \
                        patch.object(qas, "do_save", side_effect=transfer) as save, \
                        contextlib.redirect_stdout(io.StringIO()):
                    qas.main()
                save.assert_called_once()
                self.assertEqual(len(self.read()["tasklist"]), 0 if delete_first else 1)
                if not delete_first:
                    self.assertEqual(self.read()["tasklist"][0]["aria2_pending"], [{"fid": "video"}])

    def test_cookie_changed_during_pending_search_stops_transfer_without_exposing_it(self):
        self.account.is_active = True
        def change_cookie(*_args):
            current = self.read()
            current["cookie"] = ["__uid=new-private-cookie"]
            qas.Config.write_json(self.path, current)
            return self.success
        with patch.object(qas.sys, "argv", ["quark_auto_save.py", self.path]), \
                patch.dict(os.environ, {"TASKLIST": "", "QUARK_TEST": ""}), \
                patch.object(qas, "CONFIG_DATA", {}), patch.object(qas, "NOTIFYS", []), \
                patch.object(qas, "Quark", return_value=self.account), \
                patch.object(qas, "verify_account"), patch.object(qas, "do_sign"), \
                patch.object(self.account, "find_valid_resource", side_effect=change_cookie), \
                patch.object(qas, "do_save") as save, contextlib.redirect_stdout(io.StringIO()) as output:
            qas.main()
        save.assert_not_called()
        self.assertEqual(self.account.cookie, "__uid=fake-cookie")
        self.assertEqual(self.read()["cookie"], ["__uid=new-private-cookie"])
        self.assertNotIn("new-private-cookie", output.getvalue())
        self.assertIn("下次运行将重新验证账号", output.getvalue())

    def test_channel_source_error_keeps_safe_handle_but_rejects_control_text(self):
        labels = ["频道索引 Telegram @Q_dongman", "bad\nsource", "<script>bad</script>"]
        failure = {"success": False, "data": [], "errors": [
            {"source": source, "message": "请求超时"} for source in labels]}
        with patch("resource_search.search_resources", return_value=failure), \
                patch("resource_search.search_web_resources", return_value={"success": True, "data": []}), \
                contextlib.redirect_stdout(io.StringIO()):
            result = self.account.find_valid_resource(self.task, {})
        self.assertEqual({item["source"] for item in result["source_errors"]},
                         {"频道索引 Telegram @Q_dongman", "搜索源"})


if __name__ == "__main__":
    unittest.main()
