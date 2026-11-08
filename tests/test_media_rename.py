import copy
import unittest
from unittest.mock import patch

from quark_auto_save import MagicRename


class MediaRenameTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(MagicRename, "magic_variable", copy.deepcopy(MagicRename.magic_variable)))
        self.rename = MagicRename()
        self.rename.set_taskname("黑暗召唤师在地下恋爱中")
        self.pattern = r"(?i)^.*\.(?:mp4|mkv)$"
        self.template = "{TASKNAME}.{SXX}E{E}.{EXT}"

    def test_release_month_is_not_used_as_episode(self):
        for label in ("[10月]", "【10月】", " [10月新番] "):
            for episode in ("01", "02", "10"):
                with self.subTest(label=label, episode=episode):
                    name = f"{label} 与暗黑召唤师秘密恋爱中 {episode} [完全无修版] .mp4"
                    self.assertEqual(self.rename.media_name(self.pattern, self.template, name),
                                     f"黑暗召唤师在地下恋爱中.S01E{episode}.mp4")

    def test_existing_name_is_stable_and_explicit_episode_beats_title_numbers(self):
        for source, season, episode in (
            ("黑暗召唤师在地下恋爱中.S01E02.mkv", "S01", "02"),
            ("[10月] 86 -不存在的战区- EP03.mkv", "S01", "03"),
            ("3月的狮子 第02集.mp4", "S01", "02"),
            ("[10月] 作品 S02E05.mp4", "S02", "05"),
        ):
            with self.subTest(source=source):
                self.assertEqual(self.rename.media_name(self.pattern, self.template, source),
                                 f"黑暗召唤师在地下恋爱中.{season}E{episode}.{source.rsplit('.', 1)[1]}")

    def test_exx_pads_episode_without_changing_e_or_truncating_long_numbers(self):
        for raw, padded in (("1", "01"), ("01", "01"), ("123", "123")):
            source = f"第{raw}集.mp4"
            self.assertEqual(self.rename.sub(self.pattern, "E{E}.{EXT}", source), f"E{raw}.mp4")
            self.assertEqual(self.rename.sub(self.pattern, "E{EXX}.{EXT}", source), f"E{padded}.mp4")

    def test_episode_preprocessing_does_not_change_date_or_other_variables(self):
        self.assertEqual(self.rename.sub(self.pattern, "{DATE}.{SXX}E{EXX}.{EXT}",
                                        "[10月] 剧名 2026-11-09 S03E2.mp4"),
                         "20261109.S03E02.mp4")
        self.assertEqual(self.rename.sub(self.pattern, "E{II}.{EXT}", "第2集.mp4"), "E{II}.mp4")

    def test_subtitle_not_matching_video_pattern_keeps_its_name(self):
        name = "作品.S01E02.zh-Hans.ass"
        self.assertEqual(self.rename.media_name(self.pattern, self.template, name), name)
        self.assertEqual(self.rename.media_name(r"^.*\.ass$", "E{EXX}.{EXT}", name), "E02.zh-Hans.ass")

    def test_requested_format_uses_only_reliable_episode_numbers(self):
        template = "{TASKNAME} S01E{EXX}.{EXT}"
        for source, episode in (
            ("[10月] 与暗黑召唤师秘密恋爱中 02 [完全无修版] .mp4", "02"),
            ("【10月】作品 03【无修版】[1080p].mp4", "03"),
            ("作品.S01E4.1080p.mkv", "04"), ("作品 EP05.mp4", "05"),
            ("作品 第6集.mp4", "06"), ("1.mp4", "01"), ("86 -不存在的战区- 07.mp4", "07"),
            ("作品 第123话.mp4", "123"),
        ):
            with self.subTest(source=source):
                self.assertEqual(self.rename.media_name(self.pattern, template, source),
                                 f"黑暗召唤师在地下恋爱中 S01E{episode}.{source.rsplit('.', 1)[1]}")

    def test_missing_ambiguous_or_multiple_episodes_keep_original(self):
        for source in ("[10月] 与暗黑召唤师秘密恋爱中.mp4", "3月的狮子.mp4", "复仇者联盟 2.mp4",
                       "作品 2026.mp4", "1080p.mp4", "2026.mp4", "作品 00.mp4", "01-02.mp4",
                       "作品 01-02.mp4", "S01E01E02.mp4", "S01E01-E02.mp4", "第1集第2集.mp4",
                       "作品 SP 02.mp4"):
            with self.subTest(source=source):
                self.assertEqual(self.rename.media_name(self.pattern, "{TASKNAME} S01E{EXX}.{EXT}", source), source)

    def test_subtitle_language_and_accessibility_suffixes_are_preserved(self):
        for source, suffix in (("作品 02.zh-Hans.ass", ".zh-Hans"),
                               ("作品.S01E02.en.forced.srt", ".en.forced"),
                               ("作品 第2集.chs.eng.srt", ".chs.eng")):
            with self.subTest(source=source):
                self.assertEqual(self.rename.media_name(r"^.*$", "{TASKNAME} S01E{EXX}.{EXT}", source),
                                 f"黑暗召唤师在地下恋爱中 S01E02{suffix}.{source.rsplit('.', 1)[1]}")

    def test_output_must_keep_video_or_subtitle_type(self):
        for source, replacement in (("E01.mp4", "E01.ass"), ("E01.srt", "E01.mp4"),
                                    ("E01.mp4", "广告.jpg"), ("广告.jpg", "E01.mp4")):
            with self.subTest(source=source, replacement=replacement):
                self.assertEqual(self.rename.media_name(r"^.*$", replacement, source), source)
        self.assertEqual(self.rename.media_name(r"^.*$", "E01.mkv", "E01.mp4"), "E01.mkv")

    def test_unsafe_output_or_invalid_regex_keeps_original(self):
        source = "E01.mp4"
        for replacement in ("../E01.mp4", "folder/E01.mp4", r"folder\E01.mp4", "C:E01.mp4",
                            "E01..mp4", "E01\x00.mp4", "E01\n.mp4", "E01\u202e.mp4",
                            " E01.mp4", "E01.mp4 ", "E01.mp4.", 'E01?.mp4', r"\9.mp4"):
            with self.subTest(replacement=replacement):
                self.assertEqual(self.rename.media_name(r"^.*$", replacement, source), source)
        self.assertEqual(self.rename.media_name("[", "E02.mp4", source), source)

    def test_magic_regex_is_expanded_by_safe_wrapper(self):
        self.assertEqual(self.rename.media_name("$TV", "", "作品.S02E03.mkv"), "S02E03.mkv")

    def test_titles_with_slashes_are_valid_and_do_not_leak_between_tasks(self):
        from plugins.aria2 import Aria2
        other = MagicRename()
        other.set_taskname("乱马1/2 第三季 (2026)")
        template = "{TASKNAME} S03E{EXX}.{EXT}"
        self.assertEqual(other.media_name(self.pattern, template, "01.mp4"),
                         "乱马1_2 第三季 (2026) S03E01.mp4")
        self.assertEqual(self.rename.media_name(self.pattern, template, "01.mp4"),
                         "黑暗召唤师在地下恋爱中 S03E01.mp4")
        self.assertTrue(Aria2._belongs_to_task(
            {"taskname": "乱马1/2 第三季 (2026)", "replace": template},
            "/cartoon/乱马1_2 第三季 (2026) S03E01.mp4"))


if __name__ == "__main__":
    unittest.main()
