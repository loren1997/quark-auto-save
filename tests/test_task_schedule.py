import contextlib
from copy import deepcopy
from datetime import datetime
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from treelib import Tree

import quark_auto_save as qas


class TaskScheduleTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch.object(qas, "datetime", wraps=datetime).start()
        self.clock.now.return_value = datetime(2026, 11, 11, 8)  # Wednesday.
        patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected HTTP request")).start()
        self.addCleanup(patch.stopall)
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.account = qas.Quark()
        self.task = {"taskname": "测试作品 (2026)", "shareurl": "https://pan.quark.cn/s/oldshare",
                     "shareurl_ban": "分享已取消", "savepath": "/cartoon", "runweek": [5],
                     "pattern": r".*\.mkv$", "replace": "{TASKNAME} S01E{EXX}.{EXT}",
                     "addition": {"aria2": {"auto_download": True}}, "enddate": "2030-12-31"}
        patch.object(qas, "CONFIG_DATA", {"auto_repair": True, "tasklist": [self.task]}).start()
        patch.object(qas, "add_notify").start()

    def test_due_rules_keep_missing_weekday_manual_override_and_empty_weekday_pause(self):
        self.assertFalse(qas.task_is_due(self.task))
        self.assertTrue(qas.task_is_due({**self.task, "runweek": [3]}))
        self.assertFalse(qas.task_is_due({**self.task, "runweek": []}))
        manual = deepcopy(self.task)
        manual.pop("runweek")
        self.assertTrue(qas.task_is_due(manual))
        self.assertFalse(qas.task_is_due({**manual, "enddate": "2026-11-10"}))
        self.assertTrue(qas.task_is_due({**manual, "enddate": "2026-11-11"}))

    def test_off_day_direct_save_replacement_and_gap_repair_make_no_network_call(self):
        self.task.update(resource_id="bangumi:42", resource_type="anime")
        tree = Tree()
        tree.create_node("/cartoon", "root")
        before = deepcopy(self.task)
        with patch.object(self.account, "get_stoken") as token, \
                patch.object(self.account, "find_valid_resource") as find, \
                patch.object(self.account, "ls_dir") as listing:
            self.assertIsNone(self.account.do_save_task(self.task))
            self.assertIsNone(self.account.repair_share(self.task))
            gap_task = {key: value for key, value in self.task.items() if key != "shareurl_ban"}
            self.account.repair_episode_gaps(gap_task, tree)
        token.assert_not_called()
        find.assert_not_called()
        listing.assert_not_called()
        self.assertEqual(self.task, before)
        self.assertIn("待换源", self.output.getvalue())

    def test_friday_rechecks_expired_share_replaces_and_continues_transfer_and_download(self):
        tree = Tree()
        tree.create_node("/cartoon", "root")
        tree.create_node("测试作品 S01E02.mkv", "saved", parent="root")
        candidate = {"shareurl": "https://pan.quark.cn/s/newshare", "pwd_id": "newshare",
                     "pdir_fid": "0", "stoken": "test-token", "source": "test"}
        plugin = SimpleNamespace(is_active=True, run=Mock(return_value=None))
        with patch.object(qas.Config, "load_plugins", return_value=({"aria2": plugin}, {}, {})), \
                patch.object(self.account, "update_savepath_fid") as paths, \
                patch.object(self.account, "get_stoken", return_value={"status": 400, "code": 41001, "message": "分享已取消"}) as token, \
                patch.object(self.account, "find_valid_resource", return_value={"success": True, "candidate": candidate}) as find, \
                patch.object(self.account, "dir_check_and_save", return_value=tree) as transfer, \
                patch.object(self.account, "do_rename"):
            qas.do_save(self.account, [self.task])
            token.assert_not_called()
            find.assert_not_called()
            transfer.assert_not_called()
            plugin.run.assert_not_called()
            paths.assert_called_once_with([])
            self.assertEqual(self.task["shareurl"], "https://pan.quark.cn/s/oldshare")
            self.clock.now.return_value = datetime(2026, 11, 13, 8)  # Friday.
            qas.do_save(self.account, [self.task])
        token.assert_called_once_with("oldshare", "")
        find.assert_called_once()
        transfer.assert_called_once()
        self.assertEqual(transfer.call_args.args[1:4], ("newshare", "test-token", "0"))
        plugin.run.assert_called_once()
        self.assertIs(plugin.run.call_args.kwargs["tree"], tree)
        self.assertEqual(self.task["shareurl"], candidate["shareurl"])
        self.assertEqual(self.task["runweek"], [5])
        self.assertNotIn("shareurl_ban", self.task)


class PendingTaskScheduleTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch.object(qas, "datetime", wraps=datetime).start()
        self.clock.now.return_value = datetime(2026, 11, 11, 8)
        self.addCleanup(patch.stopall)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = str(Path(folder.name) / "config.json")
        self.account = qas.Quark()
        self.account.resource_history_path = qas.Config.resource_history_path(self.path)
        self.candidate = {"success": True, "candidate": {"shareurl": "https://pan.quark.cn/s/verified", "pwd_id": "verified"}}
        patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected HTTP request")).start()
        redirect = contextlib.redirect_stdout(io.StringIO())
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    @staticmethod
    def pending(index, days):
        return {"id": str(index) * 32, "last_attempt_at": f"2026-11-0{index}T08:00:00",
                "created_at": "2026-11-01T08:00:00", "last_error": "请求超时", "task": {
                    "resource_id": f"bangumi:{index}", "resource_type": "anime", "taskname": f"作品{index} (2026)",
                    "savepath": "/cartoon", "runweek": days}}

    def save(self, records):
        qas.Config.write_json(self.path, {"tasklist": [], "pending_subscriptions": records})

    def test_off_day_pending_does_not_use_the_three_retry_slots_or_change_attempt_time(self):
        waiting = [self.pending(index, [5]) for index in (1, 2, 3)]
        self.save(waiting + [self.pending(index, [3]) for index in (4, 5, 6)])
        with patch.object(self.account, "find_valid_resource", return_value=self.candidate) as find:
            promoted = qas.retry_pending_subscriptions(self.account, self.path)
        self.assertEqual(find.call_count, 3)
        self.assertEqual([task["resource_id"] for task in promoted], ["bangumi:4", "bangumi:5", "bangumi:6"])
        self.assertEqual(qas.Config.read_json(self.path)["pending_subscriptions"], waiting)

    def test_latest_schedule_is_rechecked_under_lock_before_search(self):
        self.save([self.pending(1, [3])])
        original_read = qas.Config.read_json
        reads = 0

        def read(path):
            nonlocal reads
            reads += 1
            current = original_read(path)
            if reads == 2:
                current["pending_subscriptions"][0]["task"]["runweek"] = [5]
                qas.Config.write_json(path, current)
            return current

        with patch.object(qas.Config, "read_json", side_effect=read), \
                patch.object(self.account, "find_valid_resource") as find:
            self.assertEqual(qas.retry_pending_subscriptions(self.account, self.path), [])
        find.assert_not_called()
        self.assertEqual(original_read(self.path)["pending_subscriptions"][0]["last_attempt_at"], "2026-11-01T08:00:00")

    def test_schedule_changed_during_search_does_not_promote_task_off_day(self):
        self.save([self.pending(1, [3])])

        def find(*_):
            current = qas.Config.read_json(self.path)
            current["pending_subscriptions"][0]["task"]["runweek"] = [5]
            qas.Config.write_json(self.path, current)
            return self.candidate

        with patch.object(self.account, "find_valid_resource", side_effect=find):
            self.assertEqual(qas.retry_pending_subscriptions(self.account, self.path), [])
        current = qas.Config.read_json(self.path)
        self.assertEqual(current["tasklist"], [])
        self.assertEqual(current["pending_subscriptions"][0]["task"]["runweek"], [5])


if __name__ == "__main__":
    unittest.main()
