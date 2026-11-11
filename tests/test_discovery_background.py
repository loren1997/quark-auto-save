from datetime import datetime
from threading import Event, Thread
from time import monotonic
import unittest
from unittest.mock import patch

import requests

from app.sdk import discovery


class BackgroundCalendarTests(unittest.TestCase):
    def setUp(self):
        discovery._cache = None
        discovery._expires = 0
        discovery._source_data = {}
        discovery._snapshot_week = None
        discovery._preview = None
        discovery._refreshing = False
        discovery._refresh_thread = None
        discovery._requested_week = None
        discovery._retry_at = 0
        discovery._aliases.clear()
        discovery._bilibili_media.clear()
        self.release = Event()
        clock = patch.object(discovery, "datetime")
        self.clock = clock.start()
        self.clock.now.return_value = datetime(2026, 11, 8, 12, tzinfo=discovery.CHINA_TZ)
        self.clock.fromtimestamp.side_effect = datetime.fromtimestamp
        self.addCleanup(clock.stop)
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network request"))
        network.start()
        self.addCleanup(network.stop)
        fetch = patch.object(discovery, "_get_json", side_effect=self.sources)
        self.fetch = fetch.start()
        self.addCleanup(fetch.stop)
        self.addCleanup(self.finish_worker)

    def finish_worker(self):
        self.release.set()
        self.assertTrue(discovery._refresh_done.wait(5), "calendar worker did not stop")

    def wait_for(self, predicate):
        until = monotonic() + 3
        while monotonic() < until:
            if predicate():
                return
            Event().wait(0.005)
        self.fail("calendar state did not become ready")

    @staticmethod
    def episode(number, day="2026-11-02", show_id=3, kind="Scripted"):
        return {"airdate": day, "season": 1, "number": number,
                "show": {"id": show_id, "name": f"Original {show_id}", "type": kind, "weight": 90}}

    def sources(self, url, **kwargs):
        if "bgm.tv" in url:
            return [{"weekday": {"id": 1}, "items": [{"id": 2, "type": 2, "name_cn": "测试动画"}]}]
        if url == discovery.BILIBILI_CALENDAR:
            return []
        if "/akas" in url:
            return [{"name": "中文剧名", "country": {"code": "CN"}}]
        if "2026-11-02" in url:
            return [self.episode(1)]
        return []

    def blocked(self, url, **kwargs):
        self.assertTrue(self.release.wait(3))
        return self.sources(url, **kwargs)

    def test_cold_page_is_immediate_and_anime_publishes_before_slow_tv(self):
        started_tv = Event()
        def sources(url, **kwargs):
            if "/schedule" in url:
                started_tv.set()
                self.assertTrue(self.release.wait(3))
            return self.sources(url, **kwargs)
        self.fetch.side_effect = sources
        before = monotonic()
        first = discovery.get_calendar(background=True)
        self.assertLess(monotonic() - before, 0.5)
        self.assertTrue(first["refreshing"])
        self.assertFalse(first["stale"])
        self.assertTrue(started_tv.wait(2))
        self.wait_for(lambda: bool(discovery.get_calendar(background=True)["days"][0]["anime"]))
        for _ in range(10):
            snapshot = discovery.get_calendar(background=True)
            self.assertTrue(snapshot["refreshing"])
            self.assertFalse(snapshot["days"][0]["tv"])
        self.assertEqual(set(call.args[0] for call in self.fetch.call_args_list[:2]),
                         {"https://api.bgm.tv/calendar", discovery.BILIBILI_CALENDAR})
        self.assertLessEqual(self.fetch.call_count, 4)
        self.assertIsNone(discovery._cache)

    def test_slow_aliases_do_not_delay_cards_and_finish_updates_titles(self):
        alias_started = Event()
        def sources(url, **kwargs):
            if "/akas" in url:
                alias_started.set()
                self.assertTrue(self.release.wait(3))
            return self.sources(url, **kwargs)
        self.fetch.side_effect = sources
        discovery.get_calendar(background=True)
        self.assertTrue(alias_started.wait(2))
        snapshot = discovery.get_calendar(background=True)
        self.assertTrue(snapshot["refreshing"])
        self.assertEqual(snapshot["days"][0]["tv"][0]["title"], "Original 3")
        self.assertTrue(any("尚无可用中文名" in note for note in snapshot["notes"]))
        self.assertFalse(any("中文名" in warning for warning in snapshot["warnings"]))
        self.release.set()
        self.assertTrue(discovery._refresh_done.wait(3))
        complete = discovery.get_calendar(background=True)
        self.assertFalse(complete["refreshing"])
        self.assertEqual(complete["days"][0]["tv"][0]["title"], "中文剧名")

    def test_subscription_does_not_consume_unfinished_ui_source(self):
        first_anime = Event()
        def sources(url, **kwargs):
            if "bgm.tv" in url and not first_anime.is_set():
                first_anime.set()
                self.assertTrue(self.release.wait(3))
            return self.sources(url, **kwargs)
        self.fetch.side_effect = sources
        discovery.get_calendar(background=True)
        self.assertTrue(first_anime.wait(2))
        self.assertIsNone(discovery._cache)
        result = discovery.get_calendar(resource_id="bangumi:2")
        self.assertEqual([item["id"] for day in result["days"] for item in day["anime"]], ["bangumi:2"])
        self.assertFalse(result["refreshing"])
        self.assertEqual(sum("bgm.tv" in call.args[0] for call in self.fetch.call_args_list), 2)

    def test_expired_snapshot_is_immediate_stale_and_not_mutable_by_client(self):
        discovery.get_calendar()
        discovery._expires = 0
        self.fetch.reset_mock()
        self.fetch.side_effect = self.blocked
        before = monotonic()
        snapshot = discovery.get_calendar(background=True)
        self.assertLess(monotonic() - before, 0.5)
        self.assertTrue(snapshot["cached"])
        self.assertTrue(snapshot["stale"])
        self.assertTrue(snapshot["days"][0]["tv"][0]["stale"])
        snapshot["days"][0]["anime"].clear()
        other = discovery.get_calendar(background=True)
        self.assertTrue(other["days"][0]["anime"])
        self.assertTrue(other["refreshing"])

    def test_failed_tv_day_retains_only_failed_feed_and_its_episode_numbers(self):
        def original(url, **kwargs):
            if "/schedule/web?" in url and "2026-11-02" in url:
                return [self.episode(2)]
            if "/schedule?" in url and "2026-11-03" in url:
                return [self.episode(9, "2026-11-03")]
            return self.sources(url, **kwargs)
        self.fetch.side_effect = original
        discovery.get_calendar()
        discovery._expires = 0
        def partial(url, **kwargs):
            if "/schedule?" in url and "2026-11-02" in url:
                raise requests.Timeout("timeout")
            if "/schedule/web?" in url and "2026-11-02" in url:
                return [self.episode(3)]
            return self.sources(url, **kwargs)
        self.fetch.side_effect = partial
        refreshed = discovery.get_calendar()
        monday = refreshed["days"][0]["tv"][0]
        self.assertEqual([row["number"] for row in monday["episodes"]], [1, 3])
        self.assertTrue(monday["stale"])
        self.assertTrue(refreshed["stale"])
        self.assertFalse(refreshed["days"][1]["tv"])
        self.assertTrue(any("TVmaze 2026-11-02 获取失败" in warning for warning in refreshed["warnings"]))
        self.fetch.side_effect = lambda url, **kwargs: []
        discovery._expires = 0
        recovered = discovery.get_calendar()
        self.assertFalse(recovered["stale"])
        self.assertFalse(any(day["tv"] for day in recovered["days"]))

    def test_new_week_does_not_present_previous_week_as_current_data(self):
        discovery.get_calendar()
        self.clock.now.return_value = datetime(2026, 11, 9, tzinfo=discovery.CHINA_TZ)
        self.fetch.side_effect = self.blocked
        snapshot = discovery.get_calendar(background=True)
        self.assertEqual(snapshot["week_start"], "2026-11-09")
        self.assertFalse(snapshot["cached"])
        self.assertFalse(any(day[kind] for day in snapshot["days"] for kind in ("anime", "donghua", "tv")))

    def test_old_week_worker_cannot_publish_over_new_week_request(self):
        entered = Event()
        def blocked(url, **kwargs):
            entered.set()
            return self.blocked(url, **kwargs)
        self.fetch.side_effect = blocked
        discovery.get_calendar(background=True)
        self.assertTrue(entered.wait(2))
        self.clock.now.return_value = datetime(2026, 11, 9, tzinfo=discovery.CHINA_TZ)
        current = discovery.get_calendar(background=True)
        self.assertEqual(current["week_start"], "2026-11-09")
        self.release.set()
        self.assertTrue(discovery._refresh_done.wait(3))
        self.assertIsNone(discovery._cache)
        discovery.get_calendar(background=True)
        self.assertTrue(discovery._refresh_done.wait(3))
        self.assertEqual(discovery._cache["week_start"], "2026-11-09")
        self.assertFalse(any(day["tv"] for day in discovery._cache["days"]))

    def test_sync_full_calendar_waits_outside_state_lock_without_second_refresh(self):
        entered = Event()
        def blocked(url, **kwargs):
            entered.set()
            return self.blocked(url, **kwargs)
        self.fetch.side_effect = blocked
        discovery.get_calendar(background=True)
        self.assertTrue(entered.wait(2))
        result = []
        waiter = Thread(target=lambda: result.append(discovery.get_calendar()))
        waiter.start()
        try:
            before = monotonic()
            self.assertTrue(discovery.get_calendar(background=True)["refreshing"])
            self.assertLess(monotonic() - before, 0.5)
        finally:
            self.release.set()
            waiter.join(3)
        self.assertFalse(waiter.is_alive())
        self.assertFalse(result[0]["refreshing"])
        self.assertTrue(result[0]["days"][0]["anime"])
        self.assertEqual(sum("/schedule" in call.args[0] for call in self.fetch.call_args_list), 14)

    def test_explicit_refresh_reuses_one_worker_and_sync_reads_complete_cache(self):
        discovery.get_calendar()
        self.fetch.reset_mock()
        self.assertFalse(discovery.get_calendar(background=True)["refreshing"])
        self.fetch.assert_not_called()
        self.fetch.side_effect = self.blocked
        discovery.get_calendar(background=True, refresh=True)
        worker = discovery._refresh_thread
        for _ in range(3):
            self.assertTrue(discovery.get_calendar(background=True, refresh=True)["refreshing"])
            self.assertIs(discovery._refresh_thread, worker)
        subscription = discovery.get_calendar(resource_id="bangumi:2")
        self.assertFalse(subscription["refreshing"])
        self.assertEqual(subscription["days"][0]["anime"][0]["id"], "bangumi:2")

    def test_background_alias_batch_has_ten_total_requests_and_shared_deadline(self):
        def many(url, **kwargs):
            if "/akas" in url:
                return []
            if "/schedule?" in url and "2026-11-02" in url:
                return [self.episode(1, show_id=i, kind="Animation" if i > 50 else "Scripted") for i in range(1, 111)]
            return []
        self.fetch.side_effect = many
        discovery.get_calendar(background=True)
        self.assertTrue(discovery._refresh_done.wait(3))
        aliases = [call for call in self.fetch.call_args_list if "/akas" in call.args[0]]
        self.assertEqual(len(aliases), 10)
        self.assertEqual(len({call.kwargs["deadline"] for call in aliases}), 1)
        self.assertFalse(discovery._cache["warnings"])
        self.assertTrue(any("尚无可用中文名" in note for note in discovery._cache["notes"]))

    def test_worker_start_failure_does_not_leave_refresh_flag_stuck(self):
        with patch.object(discovery.Thread, "start", side_effect=RuntimeError("cannot start")):
            response = discovery.get_calendar(background=True)
        self.assertFalse(response["refreshing"])
        self.assertFalse(discovery._refreshing)
        self.assertTrue(discovery._refresh_done.is_set())
        self.assertTrue(response["warnings"])
        discovery.get_calendar(background=True, refresh=True)
        self.assertTrue(discovery._refresh_done.wait(3))
        self.assertIsNotNone(discovery._cache)

    def test_unexpected_refresh_error_never_promotes_partial_preview_to_cache(self):
        def crash_after_anime(urls, deadline=None, on_result=None):
            on_result("anime", self.sources("https://api.bgm.tv/calendar"), None)
            raise RuntimeError("worker interrupted")
        with patch.object(discovery, "_fetch_many", side_effect=crash_after_anime):
            discovery.get_calendar(background=True)
            self.assertTrue(discovery._refresh_done.wait(3))
        self.assertIsNone(discovery._cache)
        self.fetch.reset_mock()
        preview = discovery.get_calendar(background=True)
        self.assertFalse(preview["refreshing"])
        self.assertTrue(preview["days"][0]["anime"])
        self.assertTrue(any("未完成" in text for text in preview["warnings"]))
        self.fetch.assert_not_called()
        subscription = discovery.get_calendar(resource_id="tvmaze:3:s1")
        self.assertEqual(subscription["days"][0]["tv"][0]["id"], "tvmaze:3:s1")
        self.assertIsNone(discovery._cache)
        self.assertTrue(all("api.tvmaze.com" in call.args[0] for call in self.fetch.call_args_list))

    def test_missing_card_in_complete_cache_fetches_target_source_again(self):
        self.fetch.return_value = []
        self.fetch.side_effect = None
        discovery.get_calendar()
        self.fetch.reset_mock()
        self.fetch.side_effect = self.sources
        result = discovery.get_calendar(resource_id="bangumi:2")
        self.assertEqual(result["days"][0]["anime"][0]["id"], "bangumi:2")
        self.fetch.assert_called_once_with("https://api.bgm.tv/calendar")
        self.assertFalse(discovery._cache["days"][0]["anime"])
        self.fetch.reset_mock()
        discovery.get_calendar(resource_id="invalid:../../2")
        self.fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
