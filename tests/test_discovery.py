import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import requests

from app.sdk import discovery


class CalendarTests(unittest.TestCase):
    def setUp(self):
        discovery._cache = None
        discovery._expires = 0
        discovery._aliases.clear()
        self.clock = patch.object(discovery, "datetime")
        self.clock.start().now.return_value = datetime(2026, 11, 8, 12, tzinfo=discovery.CHINA_TZ)
        self.addCleanup(self.clock.stop)

    def sources(self, url):
        if "bgm.tv" in url:
            return [{"weekday": {"id": 1}, "items": [
                {"id": 1, "type": 2, "name_cn": "低热度动画", "collection": {"doing": 5}},
                {"id": 2, "type": 2, "name_cn": "热门动画", "collection": {"doing": 100}},
                {"id": 2, "type": 2, "name_cn": "热门动画", "collection": {"doing": 100}},
            ]}]
        if "/akas" in url:
            return [{"name": "热门电视剧", "country": {"code": "CN"}}]
        if "2026-11-02" in url:
            show = {"id": 3, "name": "Popular Drama", "type": "Scripted", "weight": 90,
                    "schedule": {"days": ["Sunday"]}}
            return [{"airdate": "2026-11-02", "season": 1, "number": n, "show": show} for n in [1, 2, 2]]
        return []

    @patch.object(discovery, "_get_json")
    def test_week_sorting_real_episode_dates_and_chinese_alias(self, fetch):
        fetch.side_effect = self.sources
        data = discovery.get_calendar()
        self.assertEqual(data["week_start"], "2026-11-02")
        self.assertEqual([day["weekday"] for day in data["days"]], list(range(1, 8)))
        monday = data["days"][0]
        self.assertEqual([item["heat"] for item in monday["anime"]], [100, 5])
        self.assertNotIn("airdate", monday["anime"][0])
        self.assertEqual(monday["tv"][0]["search_title"], "热门电视剧")
        self.assertEqual(monday["tv"][0]["episodes"], [{"season": 1, "number": 1}, {"season": 1, "number": 2}])
        self.assertEqual(data["days"][6]["tv"], [])
        self.assertNotEqual(monday["tv"][0]["heat_label"], monday["anime"][0]["heat_label"])

    @patch.object(discovery, "_get_json")
    def test_cache_is_reused_and_not_mutable_by_caller(self, fetch):
        fetch.side_effect = self.sources
        first = discovery.get_calendar()
        calls = fetch.call_count
        first["days"][0]["anime"].clear()
        second = discovery.get_calendar()
        self.assertTrue(second["cached"])
        self.assertEqual(len(second["days"][0]["anime"]), 2)
        self.assertEqual(fetch.call_count, calls)

    @patch.object(discovery, "_get_json")
    def test_week_changes_at_shanghai_monday_without_reusing_old_dates(self, fetch):
        fetch.side_effect = self.sources
        discovery.get_calendar()
        discovery.datetime.now.assert_called_with(discovery.CHINA_TZ)
        discovery.datetime.now.return_value = datetime(2026, 11, 9, 0, tzinfo=discovery.CHINA_TZ)
        next_week = discovery.get_calendar()
        self.assertEqual(next_week["week_start"], "2026-11-09")
        self.assertFalse(next_week["cached"])
        self.assertEqual(next_week["days"][0]["tv"], [])
        self.assertEqual(next_week["week_end"], "2026-11-15")

    @patch.object(discovery, "_get_json")
    def test_episodes_outside_current_week_are_excluded(self, fetch):
        def outside_week(url):
            if "2026-11-02" in url:
                show = {"id": 3, "name": "Drama", "type": "Scripted", "weight": 90}
                return [{"airdate": "2026-11-09", "season": 1, "number": 1, "show": show}]
            return []
        fetch.side_effect = outside_week
        self.assertFalse(any(day["tv"] for day in discovery.get_calendar()["days"]))

    @patch.object(discovery, "_get_json")
    def test_tvmaze_seasons_have_separate_ids_and_only_first_season_uses_premiere_year(self, fetch):
        def seasons(url):
            if "/akas" in url:
                return [{"name": "测试剧", "country": {"code": "CN"}}]
            if "2026-11-02" in url:
                show = {"id": 3, "name": "Drama", "type": "Scripted", "weight": 90,
                        "premiered": "2017-01-07"}
                return [{"airdate": "2026-11-02", "season": season, "number": 1, "show": show}
                        for season in (1, 5)]
            return []
        fetch.side_effect = seasons
        items = discovery.get_calendar()["days"][0]["tv"]
        self.assertEqual({item["id"] for item in items}, {"tvmaze:3:s1", "tvmaze:3:s5"})
        self.assertEqual({item["search_title"] for item in items}, {"测试剧", "测试剧 第5季"})
        self.assertEqual(next(item["year"] for item in items if item["id"].endswith(":s1")), "2017")
        self.assertFalse(next(item["year"] for item in items if item["id"].endswith(":s5")))
        self.assertTrue(all(len(item["episodes"]) == 1 for item in items))
        self.assertTrue(any("第5季 第1集" in item["schedule_note"] for item in items))

    @patch.object(discovery, "_get_json")
    def test_source_failure_keeps_other_source_and_stale_data(self, fetch):
        fetch.side_effect = self.sources
        discovery.get_calendar()
        discovery._expires = 0

        def failing(url):
            if "bgm.tv" in url:
                raise requests.Timeout("连接超时")
            return self.sources(url)

        fetch.side_effect = failing
        data = discovery.get_calendar()
        self.assertTrue(data["days"][0]["anime"][0]["stale"])
        self.assertEqual(data["days"][0]["tv"][0]["title"], "热门电视剧")
        self.assertTrue(any("连接超时" in warning for warning in data["warnings"]))
        self.assertFalse(any("已降级" in warning for warning in data["warnings"]))

    @patch.object(discovery, "_get_json")
    def test_bangumi_failure_uses_chinese_tvmaze_animation(self, fetch):
        def fallback(url):
            if "bgm.tv" in url:
                raise requests.Timeout("连接超时")
            if "/akas" in url:
                return [{"name": "热门国漫", "country": {"code": "CN"}}]
            if "2026-11-02" in url:
                return [{"airdate": "2026-11-02", "season": 1, "number": 5,
                         "show": {"id": 4, "name": "Chinese Animation", "type": "Animation", "weight": 97}}]
            return []
        fetch.side_effect = fallback
        data = discovery.get_calendar()
        anime = data["days"][0]["anime"][0]
        self.assertEqual(anime["source"], "TVmaze")
        self.assertEqual(anime["title"], "热门国漫")
        self.assertEqual(anime["airdate"], "2026-11-02")
        self.assertIn("TVmaze", anime["heat_label"])
        self.assertTrue(any("已降级" in warning for warning in data["warnings"]))
        self.assertTrue(any("连接超时" in warning for warning in data["warnings"]))

    @patch.object(discovery, "_get_json", side_effect=requests.Timeout("连接超时"))
    def test_total_failure_returns_seven_empty_days(self, fetch):
        data = discovery.get_calendar()
        self.assertEqual(len(data["days"]), 7)
        self.assertTrue(data["warnings"])
        self.assertFalse(any(day["anime"] or day["tv"] for day in data["days"]))

    @patch.object(discovery, "_get_json")
    def test_alias_requests_are_bounded(self, fetch):
        def many(url):
            if "2026-11-02" in url:
                return [{"airdate": "2026-11-02", "season": 1, "number": 1,
                         "show": {"id": i, "name": f"Drama {i}", "type": "Scripted", "weight": i}}
                        for i in range(50)]
            return []
        fetch.side_effect = many
        data = discovery.get_calendar()
        self.assertEqual(len(data["days"][0]["tv"]), discovery.TV_LIMIT)
        self.assertEqual(sum("/akas" in call.args[0] for call in fetch.call_args_list), discovery.TV_LIMIT)

    @patch.object(discovery.requests, "get")
    def test_http_errors_and_invalid_payload_are_reported(self, get):
        response = MagicMock()
        get.return_value.__enter__.return_value = response
        response.raise_for_status.side_effect = requests.HTTPError("429 Too Many Requests")
        result, errors = discovery._fetch_many({"anime": "https://api.bgm.tv/calendar"})
        self.assertEqual(result, {})
        self.assertIn("429", errors["anime"])
        self.assertEqual(get.call_args.kwargs["timeout"], (2, 4))
        response.raise_for_status.side_effect = None
        response.json.return_value = {"message": "not a calendar"}
        with self.assertRaises(ValueError):
            discovery._get_json("https://api.bgm.tv/calendar")

    @patch.object(discovery, "_get_json")
    def test_bad_optional_metadata_does_not_break_calendar(self, fetch):
        def malformed(url):
            if "bgm.tv" in url:
                return [{"weekday": {"id": 1}, "items": [
                    {"id": 2, "type": 2, "name": "Animation", "collection": "bad", "rating": 8,
                     "images": {"medium": "javascript:alert(1)"}},
                ]}, {"weekday": {"id": 1.5}, "items": []}]
            if "2026-11-02" in url:
                return [{"airdate": {}}, {"airdate": "2026-11-02", "season": [],
                         "show": {"id": 3, "name": "Drama", "type": "Scripted"}}]
            return []
        fetch.side_effect = malformed
        item = discovery.get_calendar()["days"][0]["anime"][0]
        self.assertEqual(item["heat"], 0)
        self.assertEqual(item["poster"], "")


if __name__ == "__main__":
    unittest.main()
