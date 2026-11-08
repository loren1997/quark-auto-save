import unittest
from unittest.mock import patch

from quark_auto_save import Quark, _resource_video_title


TITLE = "黑暗召唤师在地下恋爱中 (2026)"
ALIAS = "与暗黑召唤师秘密恋爱中"


class ReleaseTitleTests(unittest.TestCase):
    def verify(self, folder, filenames, title=TITLE, aliases=(ALIAS,)):
        account = Quark()
        root = {"code": 0, "data": {"list": [{"dir": True, "fid": "folder", "file_name": folder}]}}
        files = {"code": 0, "data": {"list": [
            {"dir": False, "fid": str(i), "file_name": name, "obj_category": "video"}
            for i, name in enumerate(filenames)
        ]}}
        with patch.object(account, "get_stoken", return_value={"status": 200, "data": {"stoken": "test"}}), \
                patch.object(account, "get_detail", side_effect=[root, files]):
            return account._find_verified_resource(
                {"taskname": title, "resource_type": "anime"},
                [{"taskname": "腾讯频道", "shareurl": "https://pan.quark.cn/s/example"}],
                set(), aliases, verify_index_title=False,
            )["success"]

    def test_real_release_uses_video_alias_and_enclosing_directory_year(self):
        self.assertTrue(self.verify("H 黑A召唤师在地下恋爱中(2026.10)", [
            "05.mkv", "【10月】与暗黑召唤师秘密恋爱中 05【完全无修版】.mp4",
        ]))

    def test_wrong_or_missing_year_and_unknown_alias_are_rejected(self):
        filename = "【10月】与暗黑召唤师秘密恋爱中 01【完全无修版】.mp4"
        for folder in ("H 黑A召唤师在地下恋爱中(2025.10)", "未注明年份", "合集 (2025-2026)"):
            with self.subTest(folder=folder):
                self.assertFalse(self.verify(folder, [filename]))
        self.assertFalse(self.verify("H 黑A召唤师在地下恋爱中(2026.10)", [filename], aliases=()))

    def test_video_name_must_match_and_cannot_borrow_year_from_sibling_file(self):
        self.assertFalse(self.verify("H 黑A召唤师在地下恋爱中(2026.10)", ["05.mkv", "另一部动漫 01.mp4"]))
        self.assertFalse(self.verify("H 黑A召唤师在地下恋爱中(2026.10)", [f"{ALIAS} 01.mp4", "另一部动漫 01.mp4"]))
        self.assertFalse(self.verify("未注明年份", [f"{ALIAS} 01.mp4", "另一部动漫 (2026).mp4"]))

    def test_declared_wrong_season_or_year_in_video_remains_rejected(self):
        for filename in (f"{ALIAS} 第二季 01.mp4", f"{ALIAS} (2025) 01.mp4"):
            with self.subTest(filename=filename):
                self.assertFalse(self.verify("H 黑A召唤师在地下恋爱中(2026.10)", [filename]))
        self.assertFalse(self.verify("H 黑A召唤师在地下恋爱中(2026.10)", [
            f"{ALIAS} 01.mp4", f"{ALIAS} 第二季 02.mp4",
        ]))

    def test_release_labels_do_not_remove_sequel_or_title_numbers(self):
        for filename, expected in (
            ("【10月】作品 01【完全无修版】.mp4", "作品"),
            ("[4月新番] 作品 - 03 [无删减版].mkv", "作品"),
            ("功夫熊猫 2.mp4", "功夫熊猫 2"),
            ("86 - 01.mkv", "86"),
            ("2001太空漫游.mp4", "2001太空漫游"),
            ("大理寺日志3.mkv", "大理寺日志3"),
            ("作品 S02E01.mkv", "作品 S02E01"),
        ):
            with self.subTest(filename=filename):
                self.assertEqual(_resource_video_title(filename), expected)


if __name__ == "__main__":
    unittest.main()
