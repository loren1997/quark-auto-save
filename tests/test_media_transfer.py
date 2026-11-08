import contextlib
import copy
import io
import unittest
from unittest.mock import Mock, patch

import quark_auto_save as qas
from media_files import is_media_file, is_video_file


def file(fid, name, directory=False):
    return {"fid": fid, "file_name": name, "dir": directory,
            "share_fid_token": "test-token", "obj_category": "video"}


class MediaTransferTests(unittest.TestCase):
    def setUp(self):
        self.account = qas.Quark()
        self.task = {"taskname": "测试剧", "shareurl": "https://pan.quark.cn/s/original",
                     "savepath": "/cartoon", "pattern": r"(?i).*\.mp4$", "replace": ""}
        self.account.savepath_fid = {"/cartoon": "destination"}
        self.saved, self.target = [], []
        self.source = {"": [file("v", "01.mp4"), file("s", "01.srt"), file("j", "广告.jpg")]}
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(patch.object(qas, "CONFIG_DATA", {"auto_repair": True}))
        self.enterContext(patch.object(self.account, "get_stoken", return_value={"status": 200, "data": {"stoken": "token"}}))
        self.enterContext(patch.object(self.account, "get_detail", side_effect=lambda _p, _s, fid, **_kw: {
            "code": 0, "data": {"list": copy.deepcopy(self.source[str(fid or "")])}}))
        self.enterContext(patch.object(self.account, "ls_dir", side_effect=lambda _fid: {
            "code": 0, "data": {"list": copy.deepcopy(self.target)}}))
        self.enterContext(patch.object(self.account, "save_file", side_effect=self.save))
        self.enterContext(patch.object(self.account, "query_task", side_effect=lambda task: {
            "code": 0, "data": {"save_as": {"save_as_top_fids": ["saved-" + fid for fid in self.saved[int(task)][0]]}}}))
        self.renaming = self.enterContext(patch.object(self.account, "rename", side_effect=self.rename))
        self.deletion = self.enterContext(patch.object(self.account, "delete"))

    def save(self, fids, tokens, target, share, stoken):
        self.assertEqual(target, "destination")
        all_files = {item["fid"]: item for files in self.source.values() for item in files}
        for fid in fids:
            self.assertTrue(is_media_file(all_files[fid]))
            self.assertFalse(all_files[fid]["dir"])
            item = copy.deepcopy(all_files[fid])
            item["fid"] = "saved-" + fid
            self.target.append(item)
        self.saved.append((fids, share))
        return {"code": 0, "data": {"task_id": str(len(self.saved) - 1)}}

    def rename(self, fid, name):
        next(item for item in self.target if item["fid"] == fid)["file_name"] = name
        return {"code": 0}

    def test_only_video_and_subtitle_even_with_legacy_video_pattern(self):
        self.source[""] += [file("exe", "a.mp4.exe"), file("zip", "电影.zip"), file("text", "说明.txt")]
        tree = self.account.dir_check_and_save(self.task, "original", "token")
        self.assertEqual(self.saved, [(["v", "s"], "original")])
        self.assertEqual(tree.size(), 3)

    def test_subtitles_are_not_deduplicated_against_same_stem_video(self):
        self.task["ignore_extension"] = True
        self.target = [file("existing", "01.mp4")]
        self.account.dir_check_and_save(self.task, "original", "token")
        self.assertEqual(self.saved, [(["s"], "original")])

    def test_nested_media_are_flattened_and_renamed_without_folder_transfer(self):
        self.source = {"": [file("wrap", "分享包装", True)],
                       "wrap": [file("nested", "更新目录", True), file("ad", "广告.png")],
                       "nested": [file("v", "[10月] 测试剧 02 [完全无修版] .mp4"), file("s", "02.srt"), file("txt", "加群.txt")]}
        self.task.update(pattern=qas.MEDIA_PATTERN, replace="{TASKNAME} S01E{EXX}.{EXT}", update_subdir_resave_mode=True)
        self.account.do_save_task(self.task)
        self.assertEqual(self.saved, [(["v", "s"], "original")])
        self.assertEqual({f["file_name"] for f in self.target}, {"测试剧 S01E02.mp4", "测试剧 S01E02.srt"})
        self.deletion.assert_not_called()

    def test_subtitles_only_are_not_saved_and_trigger_new_resource_lookup(self):
        self.source[""] = [file("s", "01.srt"), file("txt", "说明.txt")]
        with patch.object(self.account, "repair_share", return_value=None) as repair:
            self.account.do_save_task(self.task)
        repair.assert_called_once_with(self.task, reason="分享不含视频")
        self.assertEqual(self.saved, [])

    def test_verified_replacement_is_used_after_no_video(self):
        self.source = {"": [file("s", "01.srt")], "alternative": [file("v", "02.mp4")]}
        candidate = {"pwd_id": "other", "stoken": "token", "pdir_fid": "alternative"}
        with patch.object(self.account, "repair_share", return_value=candidate) as repair:
            self.account.do_save_task(self.task)
        self.assertEqual(self.saved, [(["v"], "other")])
        repair.assert_called_once()

    def test_partial_scan_never_saves_subtitles_or_requests_replacement(self):
        for response in ({"code": 503}, {"code": 0, "data": {"list": [], "truncated": True}}):
            with self.subTest(response=response), patch.object(self.account, "get_detail", side_effect=[
                    {"code": 0, "data": {"list": [file("s", "01.srt"), file("d", "next", True)]}}, response]), \
                    patch.object(self.account, "repair_share") as repair:
                self.account.do_save_task(self.task)
                repair.assert_not_called()
                self.assertEqual(self.saved, [])

    def test_existing_raw_files_are_renamed_without_duplicate_transfer(self):
        self.target = [file("existing-v", "01.mp4"), file("existing-s", "01.srt")]
        for source, existing in zip(self.source[""], self.target):
            source["md5"] = existing["md5"] = "content-hash-" + source["fid"]
        self.task.update(pattern=qas.MEDIA_PATTERN, replace="{TASKNAME} S01E{EXX}.{EXT}")
        tree = self.account.do_save_task(self.task)
        self.assertEqual(self.saved, [])
        self.assertEqual(self.renaming.call_count, 2)
        self.assertEqual({node.data["path"] for node in tree.all_nodes() if not node.is_root()},
                         {"/cartoon/测试剧 S01E01.mp4", "/cartoon/测试剧 S01E01.srt"})
        self.assertFalse(self.account.do_save_task(self.task))
        self.assertEqual(self.renaming.call_count, 2)

    def test_shared_directory_does_not_claim_another_shows_raw_file(self):
        self.source[""] = [file("show-b", "01.mp4")]
        self.target = [file("show-a", "01.mp4")]
        self.task.update(pattern=qas.MEDIA_PATTERN, replace="{TASKNAME} S01E{EXX}.{EXT}")
        self.account.do_save_task(self.task)
        self.assertEqual(self.saved, [])
        self.renaming.assert_not_called()
        self.assertEqual(self.target[0]["file_name"], "01.mp4")

    def test_subtitle_cannot_suppress_video_when_ignoring_extension(self):
        self.task.update(pattern=qas.MEDIA_PATTERN, replace="{TASKNAME} S01E{EXX}.{EXT}", ignore_extension=True)
        self.target = [file("existing-subtitle", "测试剧 S01E01.srt")]
        self.account.do_save_task(self.task)
        self.assertEqual(self.saved, [(["v"], "original")])

    def test_failed_rename_retries_only_its_recorded_file_on_next_run(self):
        self.task.update(pattern=qas.MEDIA_PATTERN, replace="{TASKNAME} S01E{EXX}.{EXT}")
        self.renaming.side_effect = None
        self.renaming.return_value = {"code": 1}
        self.account.do_save_task(self.task)
        self.assertEqual(set(self.task["rename_pending"]), {"saved-v", "saved-s"})
        self.renaming.side_effect = self.rename
        self.account.do_save_task(self.task)
        self.assertNotIn("rename_pending", self.task)
        self.assertEqual(len(self.saved), 1)
        self.assertEqual({f["file_name"] for f in self.target}, {"测试剧 S01E01.mp4", "测试剧 S01E01.srt"})

    def test_video_pattern_in_folder_setting_does_not_hide_nested_videos(self):
        self.source = {"": [file("ad", "广告.txt"), file("folder", "更新", True)],
                       "folder": [file("v", "02.mp4")]}
        self.task["update_subdir"] = self.task["pattern"]
        self.account.do_save_task(self.task)
        self.assertEqual(self.saved, [(["v"], "original")])

    def test_rename_failure_keeps_actual_path_and_is_not_retried_twice_in_same_run(self):
        self.task.update(pattern=qas.MEDIA_PATTERN, replace="{TASKNAME} S01E{EXX}.{EXT}")
        self.renaming.side_effect = None
        self.renaming.return_value = {"code": 1}
        tree = self.account.do_save_task(self.task)
        files = [node.data for node in tree.all_nodes() if not node.is_root()]
        self.assertTrue(all(f["rename_failed"] for f in files))
        self.assertEqual({f["path"] for f in files}, {"/cartoon/01.mp4", "/cartoon/01.srt"})
        self.assertEqual(self.renaming.call_count, 2)

    def test_flattening_does_not_overwrite_same_target_name_from_other_folder(self):
        self.source = {"": [file("a", "A", True), file("b", "B", True)],
                       "a": [file("one", "01.mp4")], "b": [file("two", "01.mp4")]}
        self.account.do_save_task(self.task)
        self.assertEqual(self.saved, [(["one"], "original")])

    def test_whitelist_uses_real_extension_not_server_category_or_folder_name(self):
        for name in ("movie.mp4.exe", "poster.png", "readme.txt", "mp4", ".mp4", "movie.zip"):
            self.assertFalse(is_media_file(file("id", name)))
            self.assertFalse(is_video_file(file("id", name)))
        self.assertFalse(is_media_file(file("id", "folder.mp4", True)))
        self.assertTrue(is_video_file(file("id", "MOVIE.MKV")))
        self.assertTrue(is_media_file(file("id", "字幕.ASS")))


if __name__ == "__main__":
    unittest.main()
