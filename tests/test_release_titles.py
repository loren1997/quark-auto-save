import unittest
from unittest.mock import patch

from quark_auto_save import (MagicRename, Quark, _resource_episode_number, _resource_title_parts,
                             _resource_video_title, resource_titles_match)


TITLE = "黑暗召唤师在地下恋爱中 (2026)"
ALIAS = "与暗黑召唤师秘密恋爱中"


class ReleaseTitleTests(unittest.TestCase):
    def verify(self, folder, filenames, title=TITLE, aliases=(ALIAS,), folder_id="folder"):
        account = Quark()
        root = {"code": 0, "data": {"list": [{"dir": True, "fid": folder_id, "file_name": folder}]}}
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

    def test_traditional_release_title_and_five_labels_match_actual_episode(self):
        title = "被放逐的作弊赋予魔术师享受随心所欲的第二人生。"
        traditional = "被放逐的作弊賦予魔術師享受隨心所欲的第二人生。"
        filename = f"[ANi] {traditional} - 02 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"
        self.assertEqual(_resource_video_title(filename), traditional)
        self.assertEqual(_resource_episode_number({"taskname": title + " (2026)"}, filename), 2)
        self.assertTrue(self.verify(title + " (2026)", [filename], title=title + " (2026)", aliases=()))
        self.assertEqual(_resource_title_parts("被流放的開挂魔術師讴歌随心所欲第二人生"),
                         _resource_title_parts("被流放的开挂魔术师讴歌随心所欲第二人生"))

    def test_script_normalization_keeps_identity_year_season_and_non_chinese_names(self):
        self.assertTrue(resource_titles_match("魔术师 第二季 (2026)", "魔術師 第二季 (2026)"))
        for other in ("魔術師 第二季 (2025)", "魔術師 第三季 (2026)", "魔術師的學生 第二季 (2026)"):
            with self.subTest(other=other):
                self.assertFalse(resource_titles_match("魔术师 第二季 (2026)", other))
        japanese = "追放されたチート付与魔術師は気ままなセカンドライフを謳歌する"
        self.assertEqual(_resource_title_parts(japanese), (japanese, set(), set()))
        self.assertEqual(_resource_title_parts("Dark Summoner"), ("darksummoner", set(), set()))

    def test_release_labels_keep_unknown_labels_and_declared_conflicts(self):
        for label in ("另一部作品", "2025", "S02"):
            filename = f"[ANi] {ALIAS} - 01 [{label}][Baha][WEB-DL][AAC AVC][CHT].mp4"
            with self.subTest(label=label):
                self.assertIn(label, _resource_video_title(filename))
                self.assertFalse(self.verify("作品目录 (2026)", [filename]))

    def test_dot_separated_bilingual_release_requires_catalogue_alias_and_exact_metadata(self):
        title = "庆余年 第二季 (2024)"
        folder = "[庆余年 第二季].Joy.of.Life.2024.S02.2160p.WEB-DL.HEVC.DDP.2Audios-QHstudIo"
        filename = folder.replace(".S02.", ".S02E36.") + ".mp4"
        self.assertEqual(_resource_title_parts(folder), ("庆余年joyoflife", {2}, {"2024"}))
        self.assertFalse(resource_titles_match(title, folder))
        self.assertTrue(resource_titles_match(title, folder, aliases=["Joy of Life"]))
        self.assertTrue(self.verify(folder, [filename], title=title, aliases=["Joy of Life"]))
        self.assertFalse(self.verify(folder, [filename, filename.replace("Joy.of.Life", "Other.Show")],
                                     title=title, aliases=["Joy of Life"], folder_id="a" * 32))
        task = {"taskname": title, "search_aliases": ["Joy of Life"]}
        self.assertEqual(_resource_episode_number(task, filename), 36)
        rename = MagicRename({})
        rename.set_taskname(title)
        output = rename.media_name(r"(?i).*\.(?:mp4|mkv)$", "{TASKNAME} S02E{EXX}.{EXT}", filename)
        self.assertEqual(_resource_episode_number(task, output), 36)
        self.assertTrue(output.endswith(" S02E36.mp4"), output)

    def test_release_tail_does_not_hide_unknown_words_or_identity_conflicts(self):
        title = "庆余年 第二季 (2024)"
        folder = "[庆余年 第二季].Joy.of.Life.2024.S02.2160p.WEB-DL.HEVC.DDP.2Audios-QHstudIo"
        candidates = [
            folder.replace("Joy.of.Life", "Other.Show"),
            folder.replace("2024", "2025"),
            folder.replace(".S02.", ".S03."),
            folder.replace(".HEVC.", ".UnknownCodec."),
            folder + ".UnknownSuffix", folder + ".Another.Show", folder + " 另一部作品",
            folder.replace("-QHstudIo", "-S03"),
            folder.replace("-QHstudIo", "-QH2025Studio"),
            "[庆余年 第二季].Another.Show.2024.S02.2160p.WEB-DL.HEVC.DDP.2Audios-QHstudIo",
        ]
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.assertFalse(resource_titles_match(title, candidate, aliases=["Joy of Life"]))
                self.assertIsNone(_resource_episode_number(
                    {"taskname": title, "search_aliases": ["Joy of Life"]},
                    candidate.replace(".S02.", ".S02E36.") + ".mp4"))


if __name__ == "__main__":
    unittest.main()
