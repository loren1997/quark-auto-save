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
        discovery._bilibili_media.clear()
        self.clock = patch.object(discovery, "datetime")
        clock = self.clock.start()
        clock.now.return_value = datetime(2026, 11, 8, 12, tzinfo=discovery.CHINA_TZ)
        clock.fromtimestamp.side_effect = datetime.fromtimestamp
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
    def test_subscription_loads_only_its_calendar_and_does_not_replace_full_cache(self, fetch):
        fetch.side_effect = self.sources
        data = discovery.get_calendar(resource_id="bangumi:2")
        fetch.assert_called_once_with("https://api.bgm.tv/calendar")
        self.assertEqual([item["id"] for day in data["days"] for item in day["anime"]], ["bangumi:2"])
        self.assertIsNone(discovery._cache)
        full = discovery.get_calendar()
        self.assertEqual(len(full["days"][0]["anime"]), 2)
        self.assertTrue(full["days"][0]["tv"])
        fetch.reset_mock()
        cached = discovery.get_calendar(resource_id="bangumi:2")
        fetch.assert_not_called()
        self.assertTrue(cached["cached"])
        self.assertEqual(len(cached["days"][0]["anime"]), 1)
        self.assertEqual(len(discovery.get_calendar()["days"][0]["anime"]), 2)

    @patch.object(discovery, "_get_json")
    def test_tv_subscription_looks_up_only_target_show_aliases(self, fetch):
        def sources(url):
            data = self.sources(url)
            if "/schedule?" in url and "2026-11-02" in url:
                data.append({"airdate": "2026-11-02", "season": 1, "number": 1,
                             "show": {"id": 95314, "name": "Unrelated", "type": "Scripted", "weight": 99}})
            if "/shows/95314/" in url:
                raise requests.ConnectionError("Network unreachable")
            return data
        fetch.side_effect = sources
        data = discovery.get_calendar(resource_id="tvmaze:3:s1")
        self.assertEqual([item["id"] for day in data["days"] for item in day["tv"]], ["tvmaze:3:s1"])
        urls = [call.args[0] for call in fetch.call_args_list]
        self.assertEqual([url for url in urls if "/akas" in url], ["https://api.tvmaze.com/shows/3/akas"])
        self.assertTrue(all(url.startswith("https://api.tvmaze.com/") for url in urls))
        self.assertEqual(data["warnings"], [])

    @patch.object(discovery, "_get_json")
    def test_bilibili_subscription_does_not_fetch_other_sources_or_identity(self, fetch):
        fetch.side_effect = self.official_duplicate_sources
        data = discovery.get_calendar(resource_id="bilibili:1733")
        fetch.assert_called_once_with(discovery.BILIBILI_CALENDAR)
        self.assertEqual([item["id"] for day in data["days"] for item in day["donghua"]], ["bilibili:1733"])

    @patch.object(discovery, "_get_json")
    def test_subscription_calendar_failure_is_not_cached_as_missing_resource(self, fetch):
        fetch.side_effect = requests.Timeout("timeout")
        failed = discovery.get_calendar(resource_id="bangumi:2")
        self.assertFalse(any(day["anime"] for day in failed["days"]))
        self.assertTrue(failed["warnings"])
        fetch.side_effect = self.sources
        recovered = discovery.get_calendar(resource_id="bangumi:2")
        self.assertEqual(len(recovered["days"][0]["anime"]), 1)
        self.assertEqual(fetch.call_count, 2)

    @patch.object(discovery, "_get_json")
    def test_unknown_subscription_id_does_not_trigger_calendar_fetches(self, fetch):
        for resource_id in ("unknown:123", "bangumi:../123", "tvmaze:3", "", 1):
            data = discovery.get_calendar(resource_id=resource_id)
            self.assertFalse(any(day[kind] for day in data["days"] for kind in ("anime", "donghua", "tv")))
        fetch.assert_not_called()

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
    def test_bangumi_failure_does_not_remove_chinese_tvmaze_animation(self, fetch):
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
        anime = data["days"][0]["donghua"][0]
        self.assertEqual(anime["source"], "TVmaze")
        self.assertEqual(anime["title"], "热门国漫")
        self.assertEqual(anime["airdate"], "2026-11-02")
        self.assertIn("TVmaze", anime["heat_label"])
        self.assertTrue(any("连接超时" in warning for warning in data["warnings"]))

    @patch.object(discovery, "_get_json", side_effect=requests.Timeout("连接超时"))
    def test_total_failure_returns_seven_empty_days(self, fetch):
        data = discovery.get_calendar()
        self.assertEqual(len(data["days"]), 7)
        self.assertTrue(data["warnings"])
        self.assertFalse(any(day["anime"] or day["donghua"] or day["tv"] for day in data["days"]))

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
        self.assertEqual(sum("/akas" in call.args[0] for call in fetch.call_args_list), 10)

    @patch.object(discovery, "_get_json")
    def test_tv_alias_batches_progress_after_refresh_without_caching_deferred_names(self, fetch):
        failed = {15}
        def sources(url):
            if "/akas" in url:
                key = int(url.split("/")[-2])
                if key in failed:
                    raise requests.Timeout("timeout")
                return [{"name": f"剧目{key}", "country": {"code": "CN"}}]
            if "2026-11-02" in url:
                return [{"airdate": "2026-11-02", "season": 1, "number": 1,
                         "show": {"id": key, "name": f"Drama {key}", "type": "Scripted", "weight": key}}
                        for key in range(1, 16)]
            return []
        fetch.side_effect = sources
        first = discovery.get_calendar()
        self.assertEqual(len(first["days"][0]["tv"]), 15)
        self.assertEqual(set(discovery._aliases), set(range(6, 15)))
        self.assertEqual(first["days"][0]["tv"][-1]["title"], "Drama 1")
        initial_calls = fetch.call_count
        discovery.get_calendar()
        self.assertEqual(fetch.call_count, initial_calls)
        failed.clear()
        discovery._expires = 0
        fetch.reset_mock()
        refreshed = discovery.get_calendar()
        self.assertEqual(set(discovery._aliases), set(range(1, 16)))
        self.assertEqual(refreshed["days"][0]["tv"][-1]["title"], "剧目1")
        self.assertEqual({int(call.args[0].split("/")[-2]) for call in fetch.call_args_list if "/akas" in call.args[0]},
                         {1, 2, 3, 4, 5, 15})

    @patch.object(discovery, "_get_json")
    def test_tv_subscription_fetches_deferred_aliases_once_and_keeps_season(self, fetch):
        fetch.return_value = [{"name": "中文剧名", "country": {"code": "CN"}},
                              {"name": "English Alias", "country": {"code": "US"}}]
        aliases = discovery.get_search_aliases({"id": "tvmaze:42:s3", "title": "Drama 第3季", "series_title": "Drama"})
        self.assertIn("中文剧名 第3季", aliases)
        self.assertNotIn("中文剧名", aliases)
        self.assertEqual(fetch.call_args.args[0], "https://api.tvmaze.com/shows/42/akas")
        self.assertIn("deadline", fetch.call_args.kwargs)
        self.assertEqual(discovery._aliases[42], ["中文剧名"])
        cached = discovery.get_search_aliases({"id": "tvmaze:42:s2", "title": "Drama 第2季", "series_title": "Drama"})
        self.assertIn("中文剧名 第2季", cached)
        self.assertNotIn("中文剧名 第3季", cached)
        self.assertEqual(fetch.call_count, 1)

    @patch.object(discovery, "_get_json")
    def test_failed_tv_subscription_alias_fetch_does_not_poison_cache(self, fetch):
        entry = {"id": "tvmaze:42:s1", "title": "Drama", "series_title": "Drama"}
        fetch.side_effect = requests.Timeout("timeout")
        self.assertEqual(discovery.get_search_aliases(entry), ["Drama"])
        self.assertNotIn(42, discovery._aliases)
        fetch.side_effect = None
        fetch.return_value = [{"name": "中文剧名", "country": {"code": "CN"}}]
        self.assertIn("中文剧名", discovery.get_search_aliases(entry))

    @patch.object(discovery, "_get_json")
    def test_tvmaze_animation_year_season_is_not_added_to_subscription_aliases(self, fetch):
        fetch.return_value = [{"name": "国漫译名", "country": {"code": "CN"}}]
        aliases = discovery.get_search_aliases({"id": "tvmaze:42:s2026", "title": "国漫年番", "series_title": "国漫年番"})
        self.assertIn("国漫译名", aliases)
        self.assertNotIn("国漫译名 第2026季", aliases)

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

    @patch.object(discovery, "_get_json")
    def test_domestic_animation_is_always_included_without_top_ten_limit(self, fetch):
        def sources(url):
            if "/akas" in url:
                show_id = url.split("/")[-2]
                return [{"name": f"国漫{show_id}", "country": {"code": "CN"}}]
            if "/schedule/web" in url and "2026-11-02" in url:
                return [{"airdate": "2026-11-02", "season": 2026, "number": 88,
                         "_embedded": {"show": {"id": i, "name": f"Animation {i}", "type": "Animation", "weight": i}}}
                        for i in range(1, 18)]
            return self.sources(url) if "bgm.tv" in url else []
        fetch.side_effect = sources
        data = discovery.get_calendar()
        items = data["days"][0]["donghua"]
        self.assertEqual(len(items), 17)
        self.assertTrue(data["days"][0]["anime"])
        self.assertEqual(items[0]["title"], "国漫17")
        self.assertEqual(items[0]["search_title"], "国漫17")
        self.assertNotIn("第2026季", items[0]["search_title"])
        self.assertIn("第2026季", items[0]["schedule_note"])
        self.assertEqual(items[0]["year"], "")
        self.assertEqual(items[0]["search_aliases"], ["国漫17"])

    @patch.object(discovery, "_get_json")
    def test_tv_calendar_expands_real_scheduled_dramas_with_a_visible_fifty_show_cap(self, fetch):
        for count in (22, 55):
            with self.subTest(count=count):
                discovery._cache = None
                discovery._aliases.clear()
                def sources(url):
                    if "/akas" in url:
                        return [{"name": "电视剧" + url.split("/")[-2], "country": {"code": "CN"}}]
                    if "/schedule/web" in url and "2026-11-02" in url:
                        episodes = [{"airdate": "2026-11-02", "season": 1, "number": 3,
                                     "_embedded": {"show": {"id": number, "name": f"Drama {number}",
                                                            "type": "Scripted", "weight": number}}}
                                    for number in range(1, count + 1)]
                        return episodes + [episodes[-1], {**episodes[-1], "airdate": "2026-11-09"}]
                    return []
                fetch.side_effect = sources
                calendar = discovery.get_calendar()
                items = calendar["days"][0]["tv"]
                self.assertEqual(len(items), min(count, 50))
                self.assertEqual([item["heat"] for item in items], list(range(count, max(0, count - 50), -1)))
                self.assertTrue(all(item["airdate"] == "2026-11-02" and item["episodes"] == [{"season": 1, "number": 3}]
                                    for item in items))
                self.assertTrue(all(not day["tv"] for day in calendar["days"][1:]))
                self.assertEqual(any("电视剧 TVmaze 来源本周超过 50 部" in note for note in calendar["warnings"]), count > 50)
                self.assertIn("本周热度前 50 部", " ".join(calendar["notes"]))

    @patch.object(discovery, "_get_json")
    def test_domestic_numbered_seasons_cannot_fall_back_to_unqualified_series_aliases(self, fetch):
        def sources(url):
            if "/akas" in url:
                return [{"name": name, "country": {"code": "CN"}} for name in ("一念永恒", "一念永恒动画")]
            if "2026-11-02" in url:
                return [{"airdate": "2026-11-02", "season": 4, "number": 19,
                         "show": {"id": 49652, "name": "Yi Nian Yong Heng", "type": "Animation", "weight": 90}}]
            return []
        fetch.side_effect = sources
        item = discovery.get_calendar()["days"][0]["donghua"][0]
        self.assertEqual(item["title"], "一念永恒 第4季")
        self.assertEqual(item["search_title"], "一念永恒 第4季")
        self.assertEqual(item["search_aliases"], ["一念永恒 第4季", "一念永恒动画 第4季"])
        self.assertNotIn("一念永恒", discovery.get_search_aliases(item))
        self.assertTrue(all("第4季" in name for name in discovery.get_search_aliases(item)))

    @patch.object(discovery, "_get_json")
    def test_domestic_distinct_numbered_seasons_are_not_merged(self, fetch):
        def sources(url):
            if url == discovery.BILIBILI_CALENDAR:
                return [{"date_ts": 1793980800, "episodes": [
                    {"season_id": 1, "title": "测试动画 第2季", "follows": "10万追番"}]}]
            if "/akas" in url:
                return [{"name": "测试动画", "country": {"code": "CN"}}]
            if "2026-11-07" in url:
                return [{"airdate": "2026-11-07", "season": 3, "number": 1,
                         "show": {"id": 3, "name": "Test Animation", "type": "Animation"}}]
            return []
        fetch.side_effect = sources
        items = discovery.get_calendar()["days"][5]["donghua"]
        self.assertEqual({item["title"] for item in items}, {"测试动画 第2季", "测试动画 第3季"})

    @patch.object(discovery, "_get_json")
    def test_foreign_animation_broadcast_in_china_is_not_called_domestic(self, fetch):
        fetch.side_effect = lambda url: [{"airdate": "2026-11-02", "season": 1, "number": 1,
            "show": {"id": 1, "name": "Foreign Animation", "type": "Animation", "language": "Japanese"}}] if "2026-11-02" in url else []
        self.assertFalse(any(day["donghua"] for day in discovery.get_calendar()["days"]))

    @patch.object(discovery, "_get_json")
    def test_official_domestic_dates_popularity_and_cross_source_deduplication(self, fetch):
        def sources(url):
            if url == discovery.BILIBILI_CALENDAR:
                return [{"date_ts": 1793980800, "episodes": [
                    {"season_id": 28747, "title": "凡人修仙传！", "follows": "1804.8万追番", "published": 1,
                     "pub_index": "第199话", "pub_time": "11:00", "cover": "https://example.com/poster.jpg"},
                    {"season_id": 2, "title": "动画第二季", "follows": "4805追番", "published": 0},
                    {"season_id": 3, "title": "动画第三季", "follows": "2.5万系列追番", "delay": 1,
                     "delay_reason": "本周停播"},
                ]}, {"date_ts": 1794585600, "episodes": [
                    {"season_id": 4, "title": "下周动画", "follows": "5万追番"}]}]
            if "/akas" in url:
                return [{"name": "凡人修仙传", "country": {"code": "CN"}}]
            if "2026-11-07" in url:
                return [{"airdate": "2026-11-07", "season": 1, "number": 22,
                         "show": {"id": 49302, "name": "Fanren", "type": "Animation", "weight": 98}}]
            return []
        fetch.side_effect = sources
        data = discovery.get_calendar()
        items = data["days"][5]["donghua"]
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["source"], "Bilibili")
        self.assertEqual(items[0]["heat"], 18048000)
        self.assertEqual(items[0]["airdate"], "2026-11-07")
        self.assertIn("第199话", items[0]["schedule_note"])
        self.assertEqual(items[1]["heat"], 25000)
        self.assertIn("系列", items[1]["heat_label"])
        self.assertIn("延期", items[1]["schedule_note"])
        self.assertFalse(any(item["title"] == "下周动画" for day in data["days"] for item in day["donghua"]))

    @patch.object(discovery, "_get_json")
    def test_domestic_source_failure_keeps_its_cached_entries_and_other_source(self, fetch):
        bili = [{"date_ts": 1793980800, "episodes": [{"season_id": 1, "title": "官方国创", "follows": "10万追番"}]}]
        fetch.side_effect = lambda url: bili if url == discovery.BILIBILI_CALENDAR else self.sources(url)
        discovery.get_calendar()
        discovery._expires = 0
        def failed(url):
            if url == discovery.BILIBILI_CALENDAR:
                raise requests.Timeout("B站超时")
            return self.sources(url)
        fetch.side_effect = failed
        data = discovery.get_calendar()
        self.assertTrue(data["days"][5]["donghua"][0]["stale"])
        self.assertTrue(data["days"][0]["anime"])
        self.assertTrue(data["days"][0]["tv"])

    @patch.object(discovery, "_get_json")
    def test_future_premieres_are_not_listed_as_available_animation(self, fetch):
        fetch.return_value = []
        fetch.side_effect = lambda url: [{"weekday": {"id": 6}, "items": [
            {"id": 1, "type": 2, "name_cn": "已开播", "air_date": "2026-10-03"},
            {"id": 2, "type": 2, "name_cn": "明年首播", "air_date": "2027-01-01"},
        ]}] if "bgm.tv" in url else []
        items = discovery.get_calendar()["days"][5]["anime"]
        self.assertEqual([item["title"] for item in items], ["已开播"])

    @patch.object(discovery.requests, "get")
    def test_bilibili_payload_is_unwrapped_and_invalid_shape_is_rejected(self, get):
        response = get.return_value.__enter__.return_value
        response.json.return_value = {"code": 0, "result": {"timeline": [{"date_ts": 1, "episodes": []}]}}
        self.assertEqual(len(discovery._get_json(discovery.BILIBILI_CALENDAR)), 1)
        for payload in ({"code": -412}, {"code": 0, "result": {"timeline": {}}}, []):
            response.json.return_value = payload
            with self.assertRaises(ValueError):
                discovery._get_json(discovery.BILIBILI_CALENDAR)

    @patch.object(discovery.requests, "get")
    def test_bangumi_aliases_are_fetched_on_demand_with_failure_fallback(self, get):
        entry = {"id": "bangumi:123", "title": "作品中文名", "search_title": "作品中文名",
                 "search_aliases": ["日文原名"]}
        response = get.return_value.__enter__.return_value
        response.json.side_effect = [{"id": 123, "type": 2, "name_cn": "作品中文名", "name": "日文原名", "infobox": [
            {"key": "别名", "value": [{"v": "常见译名"}, {"v": "常见译名"}, {"v": []}]},
            {"key": "中文名", "value": "另一个中文名"}, {"key": "作者", "value": "不能用来搜资源"},
        ]}, []]
        aliases = discovery.get_search_aliases(entry)
        self.assertEqual(aliases, ["作品中文名", "日文原名", "常见译名", "另一个中文名"])
        self.assertEqual(get.call_count, 2)
        self.assertIn("/v0/subjects/123/subjects", get.call_args.args[0])
        get.side_effect = requests.Timeout("超时")
        self.assertEqual(discovery.get_search_aliases(entry), ["作品中文名", "日文原名"])
        get.reset_mock()
        self.assertEqual(discovery.get_search_aliases({"id": "bilibili:123", "title": "国创"}), ["国创"])
        get.assert_not_called()

    @patch.object(discovery.requests, "get")
    def test_same_named_source_book_adds_chinese_alias_before_romanized_names(self, get):
        response = get.return_value.__enter__.return_value
        response.json.side_effect = [
            {"id": 630160, "type": 2, "name_cn": "黑暗召唤师在地下恋爱中", "name": "ダークサモナーとデキている",
             "infobox": [{"key": "别名", "value": [{"v": "Dark Summoner to Dekiteiru"}, {"v": "I'm Dating a Dark Summoner!"}]}]},
            [{"type": 1, "relation": "书籍", "name": "ダークサモナーとデキている", "name_cn": "与暗黑召唤师秘密恋爱中"},
             {"type": 2, "relation": "衍生", "name": "ダークサモナーとデキている", "name_cn": "迷你动画"},
             {"type": 1, "relation": "书籍", "name": "别的原作", "name_cn": "另一本书"},
             {"type": 1, "relation": "片头曲", "name": "ダークサモナーとデキている", "name_cn": "同名音乐"}],
        ]
        aliases = discovery.get_search_aliases({"id": "bangumi:630160", "title": "黑暗召唤师在地下恋爱中"})
        self.assertEqual(aliases[:2], ["黑暗召唤师在地下恋爱中", "与暗黑召唤师秘密恋爱中"])
        self.assertEqual(get.call_count, 2)
        self.assertFalse({"迷你动画", "另一本书", "同名音乐"}.intersection(aliases))

    @patch.object(discovery.requests, "get")
    def test_unrelated_subject_response_does_not_supply_names(self, get):
        response = get.return_value.__enter__.return_value
        response.json.return_value = {"id": 555605, "type": 2, "name_cn": "另一部作品", "name": "Wrong Subject"}
        aliases = discovery.get_search_aliases({"id": "bangumi:630160", "title": "黑暗召唤师在地下恋爱中"})
        self.assertEqual(aliases, ["黑暗召唤师在地下恋爱中"])
        get.assert_called_once()

    @patch.object(discovery.requests, "get")
    def test_failed_relations_preserve_subject_aliases(self, get):
        response = get.return_value.__enter__.return_value
        response.json.side_effect = [{"id": 123, "type": 2, "name": "Original", "name_cn": "中文名"},
                                     requests.Timeout("timeout"), requests.Timeout("timeout")]
        self.assertEqual(discovery.get_search_aliases({"id": "bangumi:123", "title": "中文名"}), ["中文名", "Original"])

    @patch.object(discovery.requests, "get")
    def test_bangumi_aliases_retry_one_transient_failure_and_keep_trusted_names(self, get):
        response = get.return_value.__enter__.return_value
        response.json.side_effect = [
            {"id": 123, "type": 2, "name_cn": "作品", "name": "Original"},
            [{"type": 1, "relation": "原作", "name": "Original", "name_cn": "原作译名"}],
        ]
        get.side_effect = [requests.Timeout("read timeout"), get.return_value, get.return_value]
        self.assertEqual(discovery.get_search_aliases({"id": "bangumi:123", "title": "作品"}),
                         ["作品", "原作译名", "Original"])
        self.assertEqual(get.call_count, 3)

    @patch.object(discovery.requests, "get")
    def test_alias_http_and_tls_errors_are_not_retried(self, get):
        for error in (requests.HTTPError(response=MagicMock(status_code=429)),
                      requests.HTTPError(response=MagicMock(status_code=403)),
                      requests.exceptions.SSLError("certificate verification failed")):
            get.reset_mock()
            get.side_effect = error
            self.assertEqual(discovery.get_search_aliases({"id": "bangumi:123", "title": "作品"}), ["作品"])
            get.assert_called_once()

    @patch.object(discovery, "monotonic", side_effect=[0, 0, 6, 10.1])
    @patch.object(discovery.requests, "get")
    def test_alias_detail_and_relations_share_a_ten_second_retry_budget(self, get, clock):
        response = get.return_value.__enter__.return_value
        response.json.return_value = {"id": 123, "type": 2, "name": "Original", "name_cn": "作品"}
        get.side_effect = [requests.Timeout("read timeout"), get.return_value]
        self.assertEqual(discovery.get_search_aliases({"id": "bangumi:123", "title": "作品"}), ["作品", "Original"])
        self.assertEqual(get.call_count, 2)
        self.assertLessEqual(sum(get.call_args_list[1].kwargs["timeout"]), 4)

    def official_duplicate_sources(self, url):
        if url == discovery.BILIBILI_CALENDAR:
            return [{"date_ts": 1793980800, "episodes": [{"season_id": 1733, "title": "罗小黑战记", "follows": "983万追番"}]}]
        if url.startswith(discovery.BILIBILI_SEASON):
            return [{"season_id": 1733, "media_id": 9999}]
        if "/akas" in url:
            return [{"name": "罗小黑战记", "country": {"code": "CN"}}]
        if "2026-11-07" in url:
            return [{"airdate": "2026-11-07", "season": 3, "number": 3,
                     "show": {"id": 7750, "name": "The Legend of Luoxiaohei", "type": "Animation",
                              "officialSite": "https://www.bilibili.com/bangumi/media/md9999/"}}]
        return []

    @patch.object(discovery, "_get_json")
    def test_exact_bilibili_media_mapping_prefers_official_continuous_numbering(self, fetch):
        fetch.side_effect = self.official_duplicate_sources
        items = discovery.get_calendar()["days"][5]["donghua"]
        self.assertEqual([item["id"] for item in items], ["bilibili:1733"])
        self.assertEqual(items[0]["search_title"], "罗小黑战记")
        self.assertEqual(sum(call.args[0].startswith(discovery.BILIBILI_SEASON) for call in fetch.call_args_list), 1)
        discovery._expires = 0
        discovery.get_calendar()
        self.assertEqual(sum(call.args[0].startswith(discovery.BILIBILI_SEASON) for call in fetch.call_args_list), 1)

    @patch.object(discovery, "_get_json")
    def test_official_season_link_needs_no_extra_lookup(self, fetch):
        def sources(url):
            data = self.official_duplicate_sources(url)
            if "2026-11-07" in url:
                data[0]["show"]["officialSite"] = "https://www.bilibili.com/bangumi/play/ss1733/?from=source"
            return data
        fetch.side_effect = sources
        items = discovery.get_calendar()["days"][5]["donghua"]
        self.assertEqual([item["id"] for item in items], ["bilibili:1733"])
        self.assertFalse(any(call.args[0].startswith(discovery.BILIBILI_SEASON) for call in fetch.call_args_list))

    @patch.object(discovery, "_get_json")
    def test_catalog_lookup_budget_is_bounded_and_unverified_rows_are_preserved(self, fetch):
        items = []
        for number in range(1, 15):
            items.extend([
                {"id": f"bilibili:{number}", "title": f"国漫{number}", "source": "Bilibili"},
                {"id": f"tvmaze:{number}:s3", "title": f"国漫{number} 第3季", "series_title": f"国漫{number}",
                 "source": "TVmaze", "official_site": f"https://www.bilibili.com/bangumi/media/md{number + 100}/"}])
        fetch.side_effect = lambda url: [{"season_id": int(url.split("=")[-1]), "media_id": int(url.split("=")[-1]) + 100}]
        days = [{"date": "2026-11-13", "donghua": items}]
        self.assertTrue(discovery._prefer_official_bilibili(days, []))
        self.assertEqual(fetch.call_count, 12)
        self.assertEqual(sum(item["source"] == "TVmaze" for item in days[0]["donghua"]), 2)

    @patch.object(discovery, "_get_json")
    def test_unknown_or_failed_bilibili_identity_keeps_tvmaze_and_caches_failure(self, fetch):
        def failed(url):
            if url.startswith(discovery.BILIBILI_SEASON):
                raise requests.Timeout("timeout")
            return self.official_duplicate_sources(url)
        fetch.side_effect = failed
        data = discovery.get_calendar()
        self.assertEqual(len(data["days"][5]["donghua"]), 2)
        self.assertTrue(any("身份" in warning for warning in data["warnings"]))
        discovery._expires = 0
        discovery.get_calendar()
        self.assertEqual(sum(call.args[0].startswith(discovery.BILIBILI_SEASON) for call in fetch.call_args_list), 1)

    @patch.object(discovery, "_get_json")
    def test_bilibili_ids_are_not_assumed_equal_and_other_seasons_stay_separate(self, fetch):
        def mismatch(url):
            if url.startswith(discovery.BILIBILI_SEASON):
                return [{"season_id": 1733, "media_id": 1733}]
            return self.official_duplicate_sources(url)
        fetch.side_effect = mismatch
        self.assertEqual(len(discovery.get_calendar()["days"][5]["donghua"]), 2)
        discovery._cache = None
        def distinct_season(url):
            data = self.official_duplicate_sources(url)
            if url == discovery.BILIBILI_CALENDAR:
                data[0]["episodes"][0]["title"] = "罗小黑战记 第2季"
            return data
        fetch.reset_mock()
        fetch.side_effect = distinct_season
        self.assertEqual(len(discovery.get_calendar()["days"][5]["donghua"]), 2)
        self.assertFalse(any(call.args[0].startswith(discovery.BILIBILI_SEASON) for call in fetch.call_args_list))

    @patch.object(discovery, "_get_json")
    def test_official_identity_is_date_specific_and_rejects_other_hosts(self, fetch):
        for site in ("https://www.bilibili.com.evil.example/bangumi/media/md9999/", "https://www.bilibili.com/bangumi/media/md9999/"):
            discovery._cache = None
            def sources(url):
                data = self.official_duplicate_sources(url)
                if "2026-11-07" in url:
                    data[0]["show"]["officialSite"] = site
                    if site.endswith("md9999/") and "evil" not in site:
                        data[0]["airdate"] = "2026-11-06"
                return data
            fetch.reset_mock()
            fetch.side_effect = sources
            data = discovery.get_calendar()
            self.assertEqual(sum(len(day["donghua"]) for day in data["days"]), 2)
            self.assertFalse(any(call.args[0].startswith(discovery.BILIBILI_SEASON) for call in fetch.call_args_list))


if __name__ == "__main__":
    unittest.main()
