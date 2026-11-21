import contextlib
import io
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import quark_auto_save as qas


def listing(*files, **extra):
    return {"code": 0, "data": {"list": list(files), **extra}}


def folder(fid, name="任意包装目录"):
    return {"fid": fid, "file_name": name, "dir": True}


def video(name="S04E20_4K.mp4", fid="video"):
    return {"fid": fid, "file_name": name, "dir": False}


class DescriptionResourceTests(unittest.TestCase):
    def setUp(self):
        self.q = qas.Quark()
        self.task = {"taskname": "一念永恒 (2020)", "replace": "{TASKNAME}.S04E{E}.{EXT}",
                     "pattern": r".*\.(mp4|mkv)$", "resource_type": "anime"}
        self.row = {"taskname": "一念永恒 第4季 (2026)", "shareurl": "https://pan.quark.cn/s/abcdef123456?pwd=abcd",
                    "source_type": "telegram_web"}
        self.token = patch.object(self.q, "get_stoken", return_value={"status": 200, "code": 0, "data": {"stoken": "SECRET"}}).start()
        self.addCleanup(patch.stopall)
        self.logs = io.StringIO()
        self.output = contextlib.redirect_stdout(self.logs)
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def find(self, rows=None, **kwargs):
        return self.q._find_verified_resource(self.task, rows or [self.row], set(), [], description_only=True, **kwargs)

    def test_description_matches_title_alias_or_hashtag_but_not_other_work(self):
        for row in (self.row, {"taskname": "网站资源", "content": "名称：一念永恒 第4季"},
                    {"content": "简介\n#国漫 #一念永恒"}, {"taskname": "A Will Eternal (2026)"}):
            self.assertTrue(qas.resource_description_matches(self.task, row, ["A Will Eternal"]))
        for row in ({"taskname": "一念永恒幕后"}, {"content": "其他作品 第4季"}, {"content": ""}):
            self.assertFalse(qas.resource_description_matches(self.task, row, []))

    def test_obfuscated_nested_title_uses_innermost_video_folder(self):
        first, leaf = "a" * 32, "b" * 32
        tree = {"0": listing(folder(first, "Y【一!@念!#永!&忄亘】")), first: listing(folder(leaf, "S04")),
                leaf: listing(video())}
        with patch.object(self.q, "get_detail", side_effect=lambda _p, _t, fid, **kw: tree[str(fid)]):
            result = self.find()
        self.assertTrue(result["success"])
        self.assertEqual(result["candidate"]["pdir_fid"], leaf)
        self.assertTrue(result["candidate"]["shareurl"].endswith("?pwd=abcd#/list/share/" + leaf))

    def test_file_identity_and_year_are_not_checked(self):
        with patch.object(self.q, "get_detail", return_value=listing(video("改写的文件名 (1999) S02E01.mkv"))):
            self.assertTrue(self.find()["success"])

    def test_nonmatching_description_does_not_request_token(self):
        result = self.find([{**self.row, "taskname": "其他作品"}])
        self.assertFalse(result["success"])
        self.token.assert_not_called()

    def test_empty_images_and_subtitles_without_video_skip_to_next_share(self):
        non_video = listing(video("广告.png"), video("01.srt", "subtitle"))
        with patch.object(self.q, "get_detail", side_effect=[non_video, listing(video())]):
            result = self.find([self.row, {**self.row, "shareurl": "https://pan.quark.cn/s/nextshare1234"}])
        self.assertIn("nextshare", result["candidate"]["shareurl"])

    def test_deeper_video_wins_over_parent_video_but_empty_child_does_not(self):
        leaf = "c" * 32
        for children, expected in ((listing(video()), leaf), (listing(video("poster.jpg")), 0)):
            with patch.object(self.q, "get_detail", side_effect=[listing(video(), folder(leaf)), children]):
                self.assertEqual(self.find()["candidate"]["pdir_fid"], expected)

    def test_parallel_leaf_without_missing_episode_does_not_hide_another(self):
        a, b = "a" * 32, "b" * 32
        with patch.object(self.q, "get_detail", side_effect=[listing(folder(a), folder(b)),
                          listing(video("S04E01.mp4", "one")), listing(video("S04E02.mp4", "two"))]):
            result = self.find(required_episodes={2})
        self.assertEqual(result["candidate"]["pdir_fid"], b)
        self.assertEqual(result["candidate"]["episode_fids"], {"two": 2})

    def test_rename_season_is_used_only_for_episode_numbers(self):
        self.assertEqual(qas._resource_episode_number(self.task, "S04E20_4K.mp4"), 20)
        self.assertIsNone(qas._resource_episode_number({**self.task, "taskname": "一念永恒 第二季"}, "S04E20.mp4"))

    def test_incomplete_subtree_never_falls_back_to_parent(self):
        with patch.object(self.q, "get_detail", side_effect=[listing(video(), folder("a" * 32)),
                          {"status": 503, "code": 1, "message": "temporary"}]):
            result = self.find()
        self.assertFalse(result["success"])
        self.assertTrue(result["validation_limited"])

    def test_cycle_and_deep_nesting_are_bounded(self):
        with patch.object(self.q, "get_detail", return_value=listing(folder("a" * 32))) as fetch:
            self.assertFalse(self.find()["success"])
        self.assertEqual(fetch.call_count, 2)
        def nested(_p, _t, fid, **kwargs):
            return listing(folder(str(int(str(fid)) + 1).zfill(32)))
        with patch.object(self.q, "get_detail", side_effect=nested) as fetch:
            result = self.find()
        self.assertFalse(result["success"])
        self.assertTrue(result["validation_limited"])
        self.assertLessEqual(fetch.call_count, 13)

    def test_deleted_task_can_reuse_history_leaf_without_outer_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            self.q.resource_history_path = os.path.join(directory, "history.json")
            leaf = "b" * 32
            with patch.object(self.q, "get_detail", side_effect=[listing(folder(leaf)), listing(video())]):
                self.assertTrue(self.find()["success"])
            with patch.object(self.q, "get_detail", return_value=listing(video())) as fetch, \
                    patch("resource_search.search_resources", side_effect=AssertionError("history should suffice")):
                result = self.q.find_valid_resource(self.task, {})
            self.assertTrue(result["success"])
            self.assertEqual(fetch.call_args.args[2], leaf)

    def test_api_errors_are_distinct_and_never_echo_secrets(self):
        for response, expected in (({"status": 400, "code": 41001, "message": "分享已取消 SECRET"}, "明确取消"),
                                   ({"status": 401, "message": "SECRET"}, "认证失败"),
                                   ({"status": 429, "message": "SECRET"}, "限流"),
                                   ({"status": 400, "message": "提取码错误 SECRET"}, "提取码"),
                                   ({"status": 503, "message": "request error SECRET"}, "网络请求失败")):
            result = qas.share_validation_error(response)
            self.assertIn(expected, result)
            self.assertNotIn("SECRET", result)


class ShareTokenProtocolTests(unittest.TestCase):
    def test_http_error_and_non_json_are_not_successful_tokens(self):
        q = qas.Quark()
        for status, payload in ((403, {"status": 200, "data": {"stoken": "SECRET"}}), (502, None)):
            response = Mock(status_code=status)
            if payload is None:
                response.json.side_effect = ValueError("private body")
            else:
                response.json.return_value = payload
            with patch.object(q, "_send_request", return_value=response):
                result = q.get_stoken("abcdef")
            self.assertEqual(result["status"], status)
            self.assertEqual(result["http_status"], status)

    def test_unexpected_token_data_does_not_crash_candidate_validation(self):
        response = Mock(status_code=200)
        response.json.return_value = {"status": 200, "data": ["SECRET"]}
        with patch.object(qas.Quark, "_send_request", return_value=response):
            result = qas.Quark().get_stoken("abcdef")
        self.assertIsNone(result["data"])
