import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quark_auto_save import Config
from scripts.recover_resource_history import main, recover_resource_history


class ResourceHistoryImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "history"
        self.root.mkdir()
        self.active = Path(self.temp.name) / "active" / "quark_config.json"
        self.active.parent.mkdir()
        self.active.write_text('{"tasklist": [], "cookie": "active-secret"}', encoding="utf-8")
        self.history = Path(Config.resource_history_path(self.active))

    def write_config(self, directory, title, link, modified=100):
        path = directory / "config" / "quark_config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"cookie": "old-cookie-secret", "tasklist": [
            {"taskname": title, "shareurl": link, "savepath": "/old", "cookie": "task-secret"}
        ]}), encoding="utf-8")
        os.utime(path, (modified, modified))
        return path

    def test_restores_deleted_task_hints_without_secrets_or_active_config_changes(self):
        self.write_config(self.root, "现有作品", "https://pan.quark.cn/s/current", 300)
        self.write_config(self.root / "backup-v3.5-one", "已删除作品", "https://pan.quark.cn/s/old", 200)
        self.write_config(self.root / "backup-v3.4-two", "已删除作品", "https://pan.quark.cn/s/old", 100)
        active_before = self.active.read_bytes()
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(main(["--root", str(self.root), "--config", str(self.active)]), 0)
        self.assertEqual(json.loads(output.getvalue()), {"files_read": 3, "imported_candidates": 2})
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(self.active.read_bytes(), active_before)
        records = json.loads(self.history.read_text(encoding="utf-8"))
        self.assertEqual({record["taskname"] for record in records}, {"现有作品", "已删除作品"})
        self.assertTrue(all(set(record) == {"taskname", "shareurl"} for record in records))
        self.assertNotIn("secret", self.history.read_text(encoding="utf-8"))
        self.assertNotIn("https://", output.getvalue() + errors.getvalue())
        self.assertEqual(recover_resource_history(self.root, self.active)["imported_candidates"], 0)

    def test_only_ten_most_recent_fixed_depth_configs_are_read(self):
        for index in range(12):
            self.write_config(self.root / f"backup-v{index}", f"作品{index}",
                              f"https://pan.quark.cn/s/share{index}", index + 100)
        self.write_config(self.root / "unrelated", "无关", "https://pan.quark.cn/s/unrelated", 999)
        self.write_config(self.root / "backup-vnested" / "child", "嵌套", "https://pan.quark.cn/s/nested", 999)
        result = recover_resource_history(self.root, self.active)
        records = Config.resource_history(self.history)
        self.assertEqual(result, {"files_read": 10, "imported_candidates": 10})
        self.assertEqual([item["taskname"] for item in records], [f"作品{i}" for i in range(11, 1, -1)])

    def test_bad_json_and_wrong_shapes_do_not_block_other_backups(self):
        broken = self.write_config(self.root / "backup-vbad", "坏", "https://pan.quark.cn/s/bad", 300)
        broken.write_text('{"cookie": "do-not-print-secret",', encoding="utf-8")
        wrong = self.write_config(self.root / "backup-vwrong", "错", "https://pan.quark.cn/s/wrong", 200)
        wrong.write_text('{"tasklist": "do-not-print-secret"}', encoding="utf-8")
        self.write_config(self.root / "backup-vgood", "保留", "https://pan.quark.cn/s/good", 100)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            result = recover_resource_history(self.root, self.active)
        self.assertEqual(result, {"files_read": 1, "imported_candidates": 1})
        self.assertIn("JSONDecodeError", errors.getvalue())
        self.assertIn("ValueError", errors.getvalue())
        self.assertNotIn("secret", errors.getvalue())
        self.assertNotIn(str(self.root), errors.getvalue())

    def test_symlinked_backup_or_config_cannot_escape_root(self):
        outside = Path(self.temp.name) / "outside"
        self.write_config(outside, "外部秘密作品", "https://pan.quark.cn/s/outside")
        self.write_config(self.root / "backup-vgood", "保留", "https://pan.quark.cn/s/good")
        try:
            (self.root / "backup-vlink").symlink_to(outside, target_is_directory=True)
            (self.root / "config").symlink_to(outside / "config", target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symlink unavailable: {type(error).__name__}")
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            result = recover_resource_history(self.root, self.active)
        self.assertEqual(result, {"files_read": 1, "imported_candidates": 1})
        self.assertEqual([item["taskname"] for item in Config.resource_history(self.history)], ["保留"])
        self.assertEqual(errors.getvalue().count("ValueError"), 2)

    def test_missing_root_reports_only_exception_type(self):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(main(["--root", str(self.root / "secret-missing"), "--config", str(self.active)]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("FileNotFoundError", errors.getvalue())
        self.assertNotIn("secret", errors.getvalue())
        self.assertFalse(self.history.exists())


if __name__ == "__main__":
    unittest.main()
