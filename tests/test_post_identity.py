import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from quark_auto_save import Config, Quark


NAME = "测试魔法师的第二人生"
SHARE = "https://pan.quark.cn/s/postidentity"
POST = {"page_url": "https://meijusou.com/post/abc123", "title": NAME, "shareurl": SHARE}


class PostIdentityTests(unittest.TestCase):
    def verify(self, *, task=None, post=POST, folder=NAME, videos=None, history=None, required=None, groups=None):
        task = task or {"taskname": NAME + " (2026)", "resource_type": "anime", "resource_id": "bangumi:123"}
        account = Quark()
        account.resource_history_path = history
        groups = groups if groups is not None else [
            {"dir": True, "fid": "b" * 32, "file_name": "[测试组][1080P][繁中]（05）"},
            {"dir": True, "fid": "c" * 32, "file_name": "漫画"}]
        listings = {
            "0": [{"dir": True, "fid": "a" * 32, "file_name": folder}],
            "a" * 32: groups,
            "b" * 32: [{"dir": False, "fid": str(i), "file_name": name}
                       for i, name in enumerate(videos or [NAME + " 01.mp4", NAME + " 02.mp4"])],
        }
        def detail(_pwd, _token, parent="0", **_kwargs):
            return {"code": 0, "data": {"list": listings[str(parent or "0")], "share": {"title": folder}}}
        candidate = {"taskname": "资源正文", "shareurl": SHARE}
        if post is not None:
            candidate["source_post"] = post
        with patch.object(account, "get_stoken", return_value={"status": 200, "data": {"stoken": "test"}}), \
                patch.object(account, "get_detail", side_effect=detail) as listing:
            result = account._find_verified_resource(task, [candidate], set(), [], verify_index_title=False,
                                                     required_episodes=required)
        return result, listing.call_args_list

    def test_named_subtitle_group_and_post_confirm_missing_year_without_touching_comics(self):
        result, calls = self.verify(required={2})
        self.assertTrue(result["success"], result)
        self.assertEqual(result["candidate"]["episode_fids"], {"1": 2})
        self.assertEqual(result["candidate"]["pdir_fid"], "b" * 32)
        self.assertNotIn("c" * 32, [call.args[2] for call in calls])

    def test_single_release_group_with_cover_and_text_is_still_verified(self):
        groups = [{"dir": True, "fid": "b" * 32, "file_name": "[测试组][1080P][繁中]（05）"},
                  {"dir": False, "fid": "cover", "file_name": "封面.png"},
                  {"dir": False, "fid": "text", "file_name": "广告.txt"}]
        result, calls = self.verify(groups=groups)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["candidate"]["pdir_fid"], "b" * 32)
        self.assertIn("b" * 32, [call.args[2] for call in calls])
        self.assertFalse(self.verify(groups=groups, folder="另一部作品")[0]["success"])

    def test_missing_year_requires_catalogue_and_bound_full_post_title(self):
        for post in (None, {**POST, "title": "推荐 " + NAME + "的漫画"},
                     {**POST, "title": NAME + " (2025)"}, {**POST, "title": NAME + " 第二季"},
                     {**POST, "shareurl": "https://pan.quark.cn/s/another"},
                     {**POST, "page_url": "https://meijusou.com.evil.test/post/abc"}):
            with self.subTest(post=post):
                self.assertFalse(self.verify(post=post)[0]["success"])
        for task in ({"taskname": NAME + " (2026)", "resource_type": "anime"},
                     {"taskname": NAME + " (2026)", "resource_type": "tv", "resource_id": "tvmaze:123"}):
            with self.subTest(task=task):
                self.assertFalse(self.verify(task=task)[0]["success"])

    def test_post_cannot_override_wrong_or_ambiguous_actual_files(self):
        for videos in ([NAME + " (2025) 01.mp4"], [NAME + " S02E01.mp4"],
                       [NAME + " 01.mp4", "另一部作品 02.mp4"], ["01.mp4", "02.mp4"]):
            with self.subTest(videos=videos):
                self.assertFalse(self.verify(videos=videos)[0]["success"])
        self.assertFalse(self.verify(folder=NAME + " (2025)")[0]["success"])

    def test_verified_post_binding_survives_history_without_extra_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "history.json")
            post = {**POST, "cookie": "must-not-persist"}
            result, _ = self.verify(post=post, history=path)
            self.assertTrue(result["success"], result)
            records = Config.resource_history(path)
            self.assertEqual(records[0]["source_post"], POST)
            self.assertNotIn("must-not-persist", Path(path).read_text(encoding="utf-8"))
            self.assertTrue(self.verify(post=copy.deepcopy(records[0]["source_post"]))[0]["success"])

    def test_task_resave_keeps_bound_history_post_but_not_for_another_share(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "history.json")
            result, _ = self.verify(history=path)
            task = {"taskname": NAME + " (2026)", "shareurl": result["candidate"]["shareurl"]}
            Config.remember_resources(path, [{**task, "shareurl": task["shareurl"].replace("https:", "http:")}])
            records = Config.resource_history(path)
            self.assertEqual(records[0]["source_post"], POST)
            Config.remember_resources(path, [{**task, "shareurl": "https://pan.quark.cn/s/another"}])
            records = Config.resource_history(path)
            self.assertNotIn("source_post", records[0])
            self.assertEqual(records[1]["source_post"], POST)

    def test_task_resave_revalidates_old_post_binding(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "history.json")
            task = {"taskname": NAME + " (2026)", "shareurl": SHARE}
            for invalid in ({**POST, "shareurl": "https://pan.quark.cn/s/another"},
                            {**POST, "title": "另一部作品"},
                            {**POST, "page_url": "https://meijusou.com.evil.test/post/abc"}):
                with self.subTest(post=invalid):
                    Config.write_json(path, [{**task, "source_post": invalid}])
                    Config.remember_resources(path, [task])
                    self.assertNotIn("source_post", Config.resource_history(path)[0])


if __name__ == "__main__":
    unittest.main()
