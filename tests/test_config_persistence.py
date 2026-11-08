import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from copy import deepcopy
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import quark_auto_save as runner
from quark_auto_save import Config


def task(name="作品", url="https://pan.quark.cn/s/old"):
    return {"taskname": name, "shareurl": url, "savepath": "/订阅/" + name,
            "startfid": "old-episode", "addition": {"media": {"enabled": True}}}


def increment_worker(path, iterations):
    print("ready", flush=True)
    sys.stdin.readline()
    for _ in range(iterations):
        with Config.locked(path):
            current = Config.read_json(path)
            current["count"] += 1
            current["payload"] = str(current["count"]) * 8000
            time.sleep(0.002)
            Config.write_json(path, current)


class ConfigPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.json"

    def test_nested_lock_and_atomic_serialization_failure(self):
        original = {"tasklist": [task()], "label": "保持完整"}
        with Config.locked(self.path):
            with Config.locked(str(self.path)):
                Config.write_json(self.path, original)
                self.assertEqual(Config.read_json(self.path), original)

        def interrupted_dump(data, stream, **kwargs):
            stream.write('{"partial":')
            raise TypeError("unserializable")

        with patch.object(runner.json, "dump", side_effect=interrupted_dump):
            with self.assertRaisesRegex(TypeError, "unserializable"):
                Config.write_json(self.path, {"new": True})
        self.assertEqual(Config.read_json(self.path), original)
        self.assertEqual(list(self.path.parent.glob(".quark-config-*.tmp")), [])

    def test_failed_replace_retains_original_and_reports_error(self):
        Config.write_json(self.path, {"before": True})
        with patch.object(runner.os, "replace", side_effect=PermissionError("read only")):
            with self.assertRaises(PermissionError):
                Config.write_json(self.path, {"after": True})
        self.assertEqual(Config.read_json(self.path), {"before": True})
        self.assertEqual(list(self.path.parent.glob(".quark-config-*.tmp")), [])

    def test_two_process_updates_and_complete_reads(self):
        Config.write_json(self.path, {"count": 0, "payload": "0" * 8000})
        workers = []
        try:
            for _ in range(2):
                worker = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "worker", str(self.path), "15"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8",
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                workers.append(worker)
            for worker in workers:
                self.assertEqual(worker.stdout.readline().strip(), "ready")
            for worker in workers:
                worker.stdin.write("start\n")
                worker.stdin.flush()
            reads = 0
            deadline = time.monotonic() + 20
            while any(worker.poll() is None for worker in workers):
                self.assertLess(time.monotonic(), deadline, "configuration workers stalled")
                snapshot = Config.read_json(self.path)
                self.assertEqual(snapshot["payload"], str(snapshot["count"]) * 8000)
                reads += 1
                time.sleep(0.005)
            for worker in workers:
                _, errors = worker.communicate(timeout=5)
                self.assertEqual(worker.returncode, 0, errors)
            self.assertGreater(reads, 0)
            self.assertEqual(Config.read_json(self.path)["count"], 30)
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                worker.communicate(timeout=5)

    def test_merge_preserves_new_deleted_reordered_and_edited_tasks(self):
        original = {"tasklist": [task("A"), task("B"), task("C")], "source": {"server": "old"}}
        updated = deepcopy(original)
        updated["tasklist"][0]["shareurl_ban"] = "分享失效"
        updated["tasklist"][1]["shareurl_ban"] = "分享失效"
        current = {"tasklist": [deepcopy(original["tasklist"][2]), task("new"), deepcopy(original["tasklist"][0])],
                   "source": {"server": "user-selected"}}
        Config.merge_runtime_state(current, original, updated)
        self.assertEqual([item["taskname"] for item in current["tasklist"]], ["C", "new", "A"])
        self.assertEqual(current["tasklist"][-1]["shareurl_ban"], "分享失效")
        self.assertEqual(current["source"]["server"], "user-selected")

    def test_task_identity_changes_are_not_overwritten(self):
        for field, value in (("taskname", "重命名"), ("shareurl", "https://pan.quark.cn/s/user"), ("savepath", "/new")):
            with self.subTest(field=field):
                original = {"tasklist": [task()]}
                updated = deepcopy(original)
                updated["tasklist"][0]["shareurl_ban"] = "expired"
                current = deepcopy(original)
                current["tasklist"][0][field] = value
                expected = deepcopy(current)
                Config.merge_runtime_state(current, original, updated)
                self.assertEqual(current, expected)

    def test_repaired_link_persists_but_conflicting_episode_marker_does_not(self):
        original = {"tasklist": [task()]}
        updated = deepcopy(original)
        updated["tasklist"][0].update(shareurl="https://pan.quark.cn/s/new", repair_history=[{"old": "old"}])
        del updated["tasklist"][0]["startfid"]
        current = deepcopy(original)
        current["tasklist"][0]["startfid"] = "user-episode"
        Config.merge_runtime_state(current, original, updated)
        self.assertEqual(current["tasklist"][0]["shareurl"], "https://pan.quark.cn/s/new")
        self.assertEqual(current["tasklist"][0]["startfid"], "user-episode")
        self.assertEqual(current["tasklist"][0]["repair_history"], [{"old": "old"}])

    def test_plugin_state_merges_without_losing_concurrent_settings(self):
        original = {"tasklist": [task()], "plugins": {"media": {"server": "old", "token": "old-token"}}}
        updated = deepcopy(original)
        updated["plugins"]["media"]["token"] = "new-token"
        updated["tasklist"][0]["addition"]["media"]["id"] = "matched-id"
        current = deepcopy(original)
        current["plugins"]["media"]["server"] = "user-server"
        current["tasklist"][0]["addition"]["media"]["enabled"] = False
        Config.merge_runtime_state(current, original, updated)
        self.assertEqual(current["plugins"]["media"], {"server": "user-server", "token": "new-token"})
        self.assertEqual(current["tasklist"][0]["addition"]["media"], {"enabled": False, "id": "matched-id"})

    def test_runtime_defaults_do_not_replace_user_plugin_settings(self):
        original = {"tasklist": [task()]}
        updated = deepcopy(original)
        updated["plugins"] = {"media": {"server": "default", "token": "new-token"}}
        current = deepcopy(original)
        current["plugins"] = {"media": {"server": "user-server"}}
        Config.merge_runtime_state(current, original, updated)
        self.assertEqual(current["plugins"]["media"], {"server": "user-server", "token": "new-token"})

    def test_ambiguous_or_removed_runtime_tasks_do_not_delete_or_modify_tasks(self):
        original = {"tasklist": [task(), task()]}
        current = deepcopy(original)
        updated = deepcopy(original)
        updated["tasklist"][0]["shareurl_ban"] = "expired"
        Config.merge_runtime_state(current, original, updated)
        self.assertEqual(current, original)
        Config.merge_runtime_state(current, original, {"tasklist": []})
        self.assertEqual(current, original)

    def test_main_merges_user_changes_made_during_network_work(self):
        initial = {"cookie": ["test-cookie"], "tasklist": [task()], "plugins": {"media": {"server": "old", "token": "old-token"}}}
        Config.write_json(self.path, initial)

        def concurrent_change(account, tasks):
            with Config.locked(self.path):
                user_config = Config.read_json(self.path)
                user_config["tasklist"].append(task("用户新订阅"))
                user_config["tasklist"][0]["startfid"] = "user-episode"
                user_config["plugins"]["media"]["server"] = "user-server"
                Config.write_json(self.path, user_config)
            tasks[0]["shareurl_ban"] = "分享失效"
            tasks[0].pop("startfid")
            runner.CONFIG_DATA["plugins"]["media"]["token"] = "new-token"

        with patch.object(sys, "argv", ["quark_auto_save.py", str(self.path)]), \
             patch.dict(os.environ, {"QUARK_TEST": "", "TASKLIST": "", "QUARK_COOKIE": ""}), \
             patch.object(runner, "CONFIG_DATA", {}), patch.object(runner, "NOTIFYS", []), \
             patch.object(runner, "Quark"), patch.object(runner, "verify_account"), \
             patch.object(runner, "do_sign"), patch.object(runner, "do_save", side_effect=concurrent_change), \
             patch("builtins.print"):
            runner.main()
        current = Config.read_json(self.path)
        self.assertEqual(len(current["tasklist"]), 2)
        self.assertEqual(current["tasklist"][0]["startfid"], "user-episode")
        self.assertEqual(current["tasklist"][0]["shareurl_ban"], "分享失效")
        self.assertEqual(current["plugins"]["media"], {"server": "user-server", "token": "new-token"})


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        increment_worker(sys.argv[2], int(sys.argv[3]))
    else:
        unittest.main()
