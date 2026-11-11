from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from app.sdk.channel_registry import ChannelRegistry, DIRECTORY_PAGE, DIRECTORY_URL
from resource_search import search_web_resources


class TelegramChannelDiscoveryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = str(Path(folder.name) / "sources.sqlite")
        self.registry = ChannelRegistry(self.path)
        self.now = 100000
        patch("app.sdk.channel_registry.time.time", side_effect=lambda: self.now).start()
        self.addCleanup(patch.stopall)
        self.web = Mock()
        self.web._fetch.return_value = (json.dumps({"number": 4, "html_url": DIRECTORY_PAGE,
            "user": {"login": "fish2018"}, "body": "CHANNELS=directory_movie"}), DIRECTORY_URL)
        patch("app.sdk.channel_registry.WebSearch", return_value=self.web).start()
        self.account = Mock()
        self.account.status.return_value = {"authorized": True}
        self.account.authorization_generation.return_value = "private-generation-A"
        self.account.discover_channels.return_value = {"ok": True, "channels": [{"name": "account_movie"}]}
        self.account.search.side_effect = self.search
        self.account.join_channel.return_value = {"ok": True, "joined": True, "already_member": False}
        patch("app.sdk.telegram_account.TelegramAccount", return_value=self.account).start()
        self.config = {"source": {"telegram": {"enable": True}, "channelsearch": {
            "telegram_channels": [], "qq_channels": [], "telegram_folders": []}}}

    @staticmethod
    def search(query, channels, **kwargs):
        return {"ok": True, "candidates": [{"taskname": "测试动画 S01E02", "content": "动画 更新至2集",
            "shareurl": "https://pan.quark.cn/s/abc123", "source_url": f"https://t.me/{channels[0]}/1"}]}

    def seed_verified(self, count=8):
        names = [f"verified_movie_{i}" for i in range(count)]
        with self.registry._connect() as db:
            db.execute("UPDATE channel_origins SET names=?, next_check=? WHERE origin='directory'",
                       (json.dumps(names), self.now + 172800))
            db.executemany("INSERT INTO discovered_channels VALUES (?, 'active', '', ?, ?)",
                           [(name, self.now, self.now + 172800) for name in names])
        self.config["source"]["channelsearch"]["auto_discover"] = False
        self.config["source"]["telegram"]["auto_join"] = True
        return names

    def test_account_discovery_verifies_actual_media_and_is_daily_read_only_by_default(self):
        before = deepcopy(self.config)
        state = self.registry.maintain(self.config)
        self.assertIn("account_movie", self.registry.active_channels(check_health=False))
        self.assertIn("account", [row["kind"] for row in state["origins"]])
        self.assertEqual(self.account.discover_channels.call_args.kwargs["cursor"], self.now // 86400)
        self.account.join_channel.assert_not_called()
        self.now += 300
        self.registry.maintain(self.config)
        self.account.discover_channels.assert_called_once()
        self.assertEqual(self.config, before)
        self.assertNotIn("private-generation", json.dumps(state))

    def test_unverified_account_candidates_never_become_searchable_or_joined(self):
        self.config["source"]["telegram"]["auto_join"] = True
        self.account.search.return_value = {"ok": True, "candidates": []}
        self.account.search.side_effect = None
        state = self.registry.maintain(self.config)
        self.assertEqual(self.registry.active_channels(check_health=False), [])
        self.assertTrue(all(row["status"] == "cooldown" for row in state["channels"]))
        self.account.join_channel.assert_not_called()

    def test_verified_account_discovery_reaches_actual_search_without_editing_config(self):
        self.registry.maintain(self.config)
        self.account.search.reset_mock()
        web = Mock(errors=[])
        web.search.return_value = []
        before = deepcopy(self.config)
        with patch("resource_search.WebSearch", return_value=web):
            result = search_web_resources("测试动画", self.config, health_path=self.path)
        self.assertTrue(result["success"])
        self.assertIn("account_movie", self.account.search.call_args.args[1])
        self.assertEqual(self.config, before)
        self.account.join_channel.assert_not_called()

    def test_account_failure_preserves_names_and_short_retry_recovers_without_manual_sources(self):
        self.registry.maintain(self.config)
        self.now += 86401
        self.account.discover_channels.return_value = {"ok": False, "error": "Telegram 账号未登录", "retry_after": 0}
        state = self.registry.maintain(self.config)
        origin = next(row for row in state["origins"] if row["kind"] == "account")
        self.assertEqual(origin["next_check"], self.now + 300)
        self.assertIn("account_movie", [row["name"] for row in state["channels"]])
        self.now += 301
        self.account.discover_channels.return_value = {"ok": True, "channels": [{"name": "new_movie"}]}
        state = self.registry.maintain(self.config)
        self.assertIn("new_movie", [row["name"] for row in state["channels"]])
        self.assertEqual(self.account.discover_channels.call_count, 3)

    def test_disabling_account_removes_only_its_origin_and_auto_discovery_off_does_no_search(self):
        self.registry.maintain(self.config)
        self.account.reset_mock()
        self.config["source"]["telegram"]["enable"] = False
        state = self.registry.maintain(self.config)
        self.assertNotIn("account", [row["kind"] for row in state["origins"]])
        self.assertEqual([row["name"] for row in state["channels"]], ["directory_movie"])
        self.account.discover_channels.assert_not_called()
        self.config["source"]["telegram"]["enable"] = True
        self.config["source"]["channelsearch"]["auto_discover"] = False
        self.registry.maintain(self.config)
        self.account.discover_channels.assert_not_called()

    def test_fresh_discovery_enters_full_origin_and_preserves_verified_seeds(self):
        self.account.discover_channels.return_value = {"ok": True, "channels": [{"name": f"old_movie_{i}"} for i in range(40)]}
        self.registry.maintain(self.config)
        old_verified = set(self.registry.active_channels(check_health=False)) - {"directory_movie"}
        self.now += 86401
        self.account.discover_channels.return_value = {"ok": True, "channels": [{"name": f"new_movie_{i}"} for i in range(40)]}
        state = self.registry.maintain(self.config)
        names = {row["name"] for row in state["channels"]}
        self.assertTrue(old_verified.issubset(names))
        self.assertTrue(any(name.startswith("new_movie_") for name in names))
        with self.registry._connect() as db:
            self.assertEqual(len(json.loads(db.execute("SELECT names FROM channel_origins WHERE origin='account'").fetchone()[0])), 40)
        self.assertTrue(old_verified.issubset(set(self.account.discover_channels.call_args.kwargs["seeds"])))

    def test_join_one_per_round_and_six_successes_per_rolling_day_survive_restart(self):
        self.seed_verified()
        for _ in range(8):
            ChannelRegistry(self.path).maintain(self.config)
            self.now += 300
        self.assertEqual(self.account.join_channel.call_count, 6)
        names = [call.args[0] for call in self.account.join_channel.call_args_list]
        self.assertEqual(len(set(names)), 6)
        state = self.registry.status()
        self.assertEqual(state["joins"]["joined_count"], 6)
        self.assertEqual(state["joins"]["recent_join_count"], 6)
        self.now += 86401
        self.registry.maintain(self.config)
        self.assertEqual(self.account.join_channel.call_count, 7)
        self.assertNotIn("private-generation", json.dumps(state))

    def test_join_error_keeps_search_usable_and_waits_until_retry(self):
        names = self.seed_verified(1)
        self.account.join_channel.return_value = {"ok": False, "code": "FLOOD_WAIT",
            "error": "Telegram 账号请求过于频繁", "retry_after": 7200}
        state = self.registry.maintain(self.config)
        self.assertEqual(state["joins"]["next_check"], self.now + 7200)
        self.assertEqual(self.registry.active_channels(check_health=False), names)
        self.assertEqual(self.registry.health.status(), [])
        self.now += 600
        self.registry.maintain(self.config)
        self.account.join_channel.assert_called_once()
        self.now += 7200
        self.registry.maintain(self.config)
        self.assertEqual(self.account.join_channel.call_count, 2)

    def test_paid_or_approval_channel_join_is_deferred_without_excluding_search(self):
        names = self.seed_verified(1)
        self.account.join_channel.return_value = {"ok": False, "code": "CHANNEL_APPROVAL_REQUIRED",
            "error": "原始内容绝不持久化", "retry_after": 0}
        state = self.registry.maintain(self.config)
        self.assertEqual(state["joins"]["next_check"], self.now + 86400)
        self.assertEqual(state["joins"]["reason"], "频道需要审核或付费，未自动加入")
        self.assertNotIn("原始内容", json.dumps(state))
        self.assertEqual(self.registry.active_channels(check_health=False), names)

    def test_expired_maintenance_deadline_never_starts_join(self):
        self.seed_verified(1)
        with patch("app.sdk.channel_registry.time.monotonic", return_value=100):
            self.registry._join_verified(self.account, 99, self.now)
        self.account.authorization_generation.assert_not_called()
        self.account.join_channel.assert_not_called()

    def test_account_generation_change_rechecks_membership_and_existing_member_does_not_use_quota(self):
        self.seed_verified(1)
        self.registry.maintain(self.config)
        self.account.authorization_generation.return_value = "private-generation-B"
        self.account.join_channel.return_value = {"ok": True, "already_member": True, "joined": False}
        state = self.registry.maintain(self.config)
        self.assertEqual(self.account.join_channel.call_count, 2)
        self.assertEqual(state["joins"]["joined_count"], 1)
        self.assertEqual(state["joins"]["recent_join_count"], 0)
        self.registry.maintain(self.config)
        self.assertEqual(self.account.join_channel.call_count, 2)

    def test_join_toggle_account_and_channel_gates_stop_future_join_actions(self):
        self.seed_verified(1)
        for key, value in (("auto_join", False), ("enable", False)):
            with self.subTest(key=key):
                config = deepcopy(self.config)
                config["source"]["telegram"][key] = value
                self.registry.maintain(config)
        config = deepcopy(self.config)
        config["source"]["channelsearch"]["enable"] = False
        self.registry.maintain(config)
        self.account.status.return_value = {"authorized": False}
        self.registry.maintain(self.config)
        self.account.join_channel.assert_not_called()

    def test_join_never_uses_unverified_expired_removed_or_cooldown_channel(self):
        names = self.seed_verified(4)
        with self.registry._connect() as db:
            db.execute("UPDATE discovered_channels SET status='pending' WHERE name=?", (names[0],))
            db.execute("UPDATE discovered_channels SET next_check=? WHERE name=?", (self.now - 1, names[1]))
            db.execute("UPDATE discovered_channels SET status='cooldown' WHERE name=?", (names[2],))
            db.execute("UPDATE channel_origins SET names=? WHERE origin='directory'", (json.dumps(names[:3]),))
        self.registry.maintain(self.config)
        self.account.join_channel.assert_not_called()

    def test_busy_registry_lease_prevents_duplicate_discovery_and_join(self):
        self.seed_verified(1)
        with self.registry._connect() as db:
            db.execute("UPDATE channel_discovery SET lease=?", (self.now + 30,))
        self.registry.maintain(self.config)
        self.account.join_channel.assert_not_called()
        self.account.discover_channels.assert_not_called()

    def parallel_join_with_reservations(self, history=0, count=8):
        names = self.seed_verified(count)
        with self.registry._connect() as db:
            db.executemany("INSERT INTO channel_joins VALUES (?, ?, 'joined', '', ?, 0, ?)",
                           [("private-generation-A", name, self.now - 10, self.now - 10) for name in names[:history]])
        barrier = threading.Barrier(3)
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()

        def join(name, **kwargs):
            self.assertEqual(kwargs["expected_generation"], "private-generation-A")
            entered.set()
            self.assertTrue(release.wait(3), "Test must release the in-flight join")
            return {"ok": True, "joined": True}

        def run():
            barrier.wait(timeout=3)
            try:
                ChannelRegistry(self.path)._join_verified(self.account, time.monotonic() + 10, self.now)
            finally:
                completed.set()

        self.account.join_channel.side_effect = join
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(run) for _ in range(2)]
            barrier.wait(timeout=3)
            try:
                self.assertTrue(entered.wait(3))
                self.assertTrue(completed.wait(3), "Other instance must observe the committed reservation")
                self.assertEqual(self.account.join_channel.call_count, 1)
                with self.registry._connect() as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM channel_joins WHERE status='joining'").fetchone()[0], 1)
            finally:
                release.set()
            for job in jobs:
                job.result(timeout=3)

    def test_two_registry_instances_reserve_the_sixth_join_atomically(self):
        self.parallel_join_with_reservations(history=5)
        self.assertEqual(self.registry.status()["joins"]["recent_join_count"], 6)

    def test_two_registry_instances_cannot_replace_the_same_inflight_candidate(self):
        self.parallel_join_with_reservations(count=1)
        self.assertEqual(self.registry.status()["joins"]["joined_count"], 1)

    def test_join_exception_retains_quota_reservation_and_retries_after_expiry(self):
        names = self.seed_verified(1)
        self.account.join_channel.side_effect = RuntimeError("simulated process failure")
        with self.assertRaises(RuntimeError):
            self.registry._join_verified(self.account, time.monotonic() + 10, self.now)
        self.registry._join_verified(self.account, time.monotonic() + 10, self.now)
        self.account.join_channel.assert_called_once()
        with self.registry._connect() as db:
            self.assertEqual(db.execute("SELECT status FROM channel_joins").fetchone()[0], "joining")
        self.now += 301
        self.account.join_channel.side_effect = None
        self.registry._join_verified(self.account, time.monotonic() + 10, self.now)
        self.assertEqual(self.account.join_channel.call_count, 2)
        self.assertEqual(self.account.join_channel.call_args.args[0], names[0])

    def test_timed_out_join_rechecks_membership_before_another_new_channel(self):
        names = self.seed_verified(8)
        with self.registry._connect() as db:
            db.executemany("INSERT INTO channel_joins VALUES (?, ?, 'joined', '', ?, 0, ?)",
                           [("private-generation-A", name, self.now - 10, self.now - 10) for name in names[:5]])
        self.account.join_channel.return_value = {"ok": False, "code": "TIMEOUT", "error": "请求超时"}
        self.registry._join_verified(self.account, time.monotonic() + 10, self.now)
        original = self.account.join_channel.call_args.args[0]
        self.registry._join_verified(self.account, time.monotonic() + 10, self.now)
        self.account.join_channel.assert_called_once()
        self.now += 301
        self.account.join_channel.return_value = {"ok": True, "already_member": True}
        self.registry._join_verified(self.account, time.monotonic() + 10, self.now)
        self.assertEqual(self.account.join_channel.call_args.args[0], original)
        self.assertEqual(self.registry.status()["joins"]["recent_join_count"], 6)
        self.registry._join_verified(self.account, time.monotonic() + 10, self.now)
        self.assertEqual(self.account.join_channel.call_count, 2)

    def test_changed_account_cancels_old_reservation_without_recording_success(self):
        self.seed_verified(1)
        self.account.join_channel.return_value = {"ok": False, "code": "ACCOUNT_CHANGED", "error": "private data"}
        self.registry._join_verified(self.account, time.monotonic() + 10, self.now)
        self.assertEqual(self.account.join_channel.call_args.kwargs["expected_generation"], "private-generation-A")
        state = self.registry.status()["joins"]
        self.assertEqual(state["joined_count"], 0)
        self.assertEqual(state["reason"], "Telegram 账号已切换，等待下轮巡检")
        self.account.authorization_generation.return_value = "private-generation-B"
        self.account.join_channel.return_value = {"ok": True, "joined": True}
        self.registry._join_verified(self.account, time.monotonic() + 10, self.now)
        self.assertEqual(self.account.join_channel.call_args.kwargs["expected_generation"], "private-generation-B")
        self.assertEqual(self.registry.status()["joins"]["joined_count"], 1)

    def test_delayed_old_generation_cannot_delete_new_accounts_join_history_or_quota(self):
        names = self.seed_verified(8)
        with self.registry._connect() as db:
            db.executemany("INSERT INTO channel_joins VALUES (?, ?, 'joined', '', ?, 0, ?)",
                           [("private-generation-A", name, self.now - 20, self.now - 20) for name in names[:5]])
        paused, resume = threading.Event(), threading.Event()
        old_account = Mock()

        def old_generation():
            paused.set()
            self.assertTrue(resume.wait(3))
            return "private-generation-A"

        old_account.authorization_generation.side_effect = old_generation
        old_account.join_channel.return_value = {"ok": False, "code": "ACCOUNT_CHANGED"}
        with ThreadPoolExecutor(max_workers=1) as pool:
            old_job = pool.submit(ChannelRegistry(self.path)._join_verified,
                                  old_account, time.monotonic() + 10, self.now)
            try:
                self.assertTrue(paused.wait(3))
                self.account.authorization_generation.return_value = "private-generation-B"
                with self.registry._connect() as db:
                    db.executemany("INSERT INTO channel_joins VALUES (?, ?, 'joined', '', ?, 0, ?)",
                                   [("private-generation-B", name, self.now - 10, self.now - 10) for name in names[:5]])
                self.registry._join_verified(self.account, time.monotonic() + 10, self.now)
                self.assertEqual(self.registry.status()["joins"]["recent_join_count"], 6)
            finally:
                resume.set()
            old_job.result(timeout=3)
        old_account.join_channel.assert_called_once()
        self.assertEqual(old_account.join_channel.call_args.kwargs["expected_generation"], "private-generation-A")
        self.assertEqual(self.registry.status()["joins"]["joined_count"], 6)
        with self.registry._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM channel_joins WHERE generation=? AND status='joined'",
                                        ("private-generation-B",)).fetchone()[0], 6)
        self.registry._join_verified(self.account, time.monotonic() + 10, self.now)
        self.account.join_channel.assert_called_once()

    def test_join_summary_is_empty_without_current_authorization(self):
        self.seed_verified(1)
        self.registry.maintain(self.config)
        self.account.authorization_generation.return_value = ""
        state = self.registry.status()["joins"]
        self.assertEqual(state, {"joined_count": 0, "recent_join_count": 0,
                                 "pending_count": 0, "next_check": 0, "reason": ""})


if __name__ == "__main__":
    unittest.main()
