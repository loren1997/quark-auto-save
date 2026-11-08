import copy
import re
import unittest

from media_files import MEDIA_PATTERN
import quark_auto_save as qas


class MediaRenameDefaultsTests(unittest.TestCase):
    def task(self, **changes):
        return {"resource_id": "bangumi:example", "taskname": "测试作品", "pattern": "", "replace": "", **changes}

    def test_empty_and_legacy_default_rules_migrate_in_place(self):
        for pattern in (None, "", r"(?i).*\.(mp4|mkv|mov|m4v|avi|ts)$", MEDIA_PATTERN):
            task = self.task(pattern=pattern)
            with self.subTest(pattern=pattern):
                self.assertIs(qas.apply_media_rename_defaults(task), task)
                self.assertEqual(task["pattern"], MEDIA_PATTERN)
                self.assertEqual(task["replace"], "{TASKNAME} S01E{EXX}.{EXT}")
                before = copy.deepcopy(task)
                qas.apply_media_rename_defaults(task)
                self.assertEqual(task, before)

    def test_explicit_third_season_uses_s03(self):
        for title in ("测试作品 第三季", "测试作品 第3季", "测试作品 S03"):
            with self.subTest(title=title):
                task = qas.apply_media_rename_defaults(self.task(taskname=title))
                self.assertEqual(task["replace"], "{TASKNAME} S03E{EXX}.{EXT}")

    def test_custom_rules_ordinary_tasks_and_ambiguous_seasons_are_preserved(self):
        for changes in ({"resource_id": ""}, {"pattern": r".*\.mkv$"},
                        {"replace": "my-custom-name.mkv"}, {"pattern": "$TV"},
                        {"taskname": "测试作品 第一季 第二季"}, {"taskname": "测试作品 第1-2季"}):
            with self.subTest(changes=changes):
                task = self.task(**changes)
                before = copy.deepcopy(task)
                self.assertIs(qas.apply_media_rename_defaults(task), task)
                self.assertEqual(task, before)

    def test_video_and_subtitle_extensions_remain_original(self):
        task = qas.apply_media_rename_defaults(self.task())
        rename = qas.MagicRename()
        rename.set_taskname(task["taskname"])
        for filename, expected in (("01.mp4", "测试作品 S01E01.mp4"),
                                   ("02.mkv", "测试作品 S01E02.mkv"),
                                   ("03.ass", "测试作品 S01E03.ass"),
                                   ("04.srt", "测试作品 S01E04.srt")):
            with self.subTest(filename=filename):
                self.assertIsNotNone(re.fullmatch(task["pattern"], filename))
                self.assertEqual(rename.sub(task["pattern"], task["replace"], filename), expected)
        self.assertIsNone(re.fullmatch(task["pattern"], "advertisement.exe"))

    def test_factory_cloud_directory_and_download_directory_migrate_together(self):
        for path in ("/追更/测试作品", "/追更", "/cartoon"):
            with self.subTest(path=path):
                task = self.task(savepath=path, addition={"aria2": {"auto_download": True}, "other": {"enabled": True}})
                qas.apply_media_rename_defaults(task)
                self.assertEqual(task["savepath"], "/cartoon")
                expected = {"auto_download": True, "save_path": "cartoon"}
                if path != "/cartoon":
                    expected["download_existing"] = True
                self.assertEqual(task["addition"]["aria2"], expected)
                self.assertEqual(task["addition"]["other"], {"enabled": True})
        task = self.task(taskname="测试/作品:第一季 (2026)", savepath="/追更/测试_作品_第一季 (2026)")
        qas.apply_media_rename_defaults(task)
        self.assertEqual(task["savepath"], "/cartoon")

    def test_custom_cloud_directory_is_preserved_and_path_migration_is_independent_of_rename(self):
        task = self.task(savepath="/我的目录/测试作品", addition={"aria2": {"save_path": "personal"}})
        qas.apply_media_rename_defaults(task)
        self.assertEqual(task["savepath"], "/我的目录/测试作品")
        self.assertEqual(task["addition"]["aria2"]["save_path"], "personal")
        task = self.task(savepath="/cartoon", addition={"aria2": {"save_path": "personal"}})
        qas.apply_media_rename_defaults(task)
        self.assertEqual(task["addition"]["aria2"]["save_path"], "personal")
        task = self.task(savepath="/追更/测试作品", pattern="my-pattern", replace="my-replacement")
        qas.apply_media_rename_defaults(task)
        self.assertEqual((task["savepath"], task["pattern"], task["replace"]), ("/cartoon", "my-pattern", "my-replacement"))

    def test_only_actual_factory_path_migration_clears_old_pending_downloads(self):
        for path in ("/追更/测试作品", "/cartoon", "/custom"):
            for enabled in (False, True):
                with self.subTest(path=path, enabled=enabled):
                    task = self.task(savepath=path, aria2_pending=[{"fid": "old-file"}], aria2_pending_dirs=[{"fid": "old-dir"}],
                                     addition={"aria2": {"auto_download": enabled}})
                    qas.apply_media_rename_defaults(task)
                    if path.startswith("/追更/"):
                        self.assertNotIn("aria2_pending", task)
                        self.assertNotIn("aria2_pending_dirs", task)
                        self.assertEqual(bool(task["addition"]["aria2"].get("download_existing")), enabled)
                        task["aria2_pending"] = [{"fid": "new-cartoon-file"}]
                        qas.apply_media_rename_defaults(task)
                        self.assertEqual(task["aria2_pending"], [{"fid": "new-cartoon-file"}])
                    else:
                        self.assertEqual(task["aria2_pending"], [{"fid": "old-file"}])
                        self.assertEqual(task["aria2_pending_dirs"], [{"fid": "old-dir"}])
                        self.assertNotIn("download_existing", task["addition"]["aria2"])

    def test_runtime_migration_uses_resource_identity_when_path_and_link_change(self):
        original = {"tasklist": [self.task(savepath="/追更/测试作品", shareurl="https://pan.quark.cn/s/old",
                                            aria2_pending=[{"fid": "old-file"}], aria2_pending_dirs=[{"fid": "old-dir"}])]}
        current, updated = copy.deepcopy(original), copy.deepcopy(original)
        qas.apply_media_rename_defaults(updated["tasklist"][0])
        updated["tasklist"][0]["shareurl"] = "https://pan.quark.cn/s/new"
        qas.Config.merge_runtime_state(current, original, updated)
        self.assertEqual(current, updated)

    def test_runtime_migration_preserves_concurrent_rules_and_identity_edits(self):
        original = {"tasklist": [self.task(savepath="/追更/测试作品", shareurl="https://pan.quark.cn/s/old",
                                            addition={"aria2": {"save_path": ""}})]}
        current, updated = copy.deepcopy(original), copy.deepcopy(original)
        qas.apply_media_rename_defaults(updated["tasklist"][0])
        current["tasklist"][0].update(pattern="user-filter", replace="user-name")
        current["tasklist"][0]["addition"]["aria2"]["save_path"] = "user-downloads"
        qas.Config.merge_runtime_state(current, original, updated)
        task = current["tasklist"][0]
        self.assertEqual(task["savepath"], "/cartoon")
        self.assertEqual((task["pattern"], task["replace"]), ("user-filter", "user-name"))
        self.assertEqual(task["addition"]["aria2"]["save_path"], "user-downloads")
        for key, value in (("savepath", "/user-cloud"), ("shareurl", "https://pan.quark.cn/s/user"), ("taskname", "user-title")):
            with self.subTest(key=key):
                current = copy.deepcopy(original)
                current["tasklist"][0][key] = value
                before = copy.deepcopy(current)
                qas.Config.merge_runtime_state(current, original, updated)
                self.assertEqual(current, before)

    def test_duplicate_resource_identity_does_not_pair_migrated_tasks(self):
        initial = self.task(savepath="/追更/测试作品", shareurl="https://pan.quark.cn/s/old")
        for duplicate_in in ("original", "updated", "current"):
            with self.subTest(duplicate_in=duplicate_in):
                original = {"tasklist": [copy.deepcopy(initial)]}
                updated, current = copy.deepcopy(original), copy.deepcopy(original)
                qas.apply_media_rename_defaults(updated["tasklist"][0])
                configs = {"original": original, "updated": updated, "current": current}
                configs[duplicate_in]["tasklist"].append({**initial, "shareurl": "https://pan.quark.cn/s/duplicate",
                                                          "savepath": "/other"})
                before = copy.deepcopy(current)
                qas.Config.merge_runtime_state(current, original, updated)
                self.assertEqual(current, before)


if __name__ == "__main__":
    unittest.main()
