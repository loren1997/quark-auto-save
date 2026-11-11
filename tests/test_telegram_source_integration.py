from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from app.sdk.channel_registry import ChannelRegistry, DIRECTORY_PAGE, DIRECTORY_URL, folder_urls
from app.sdk.common import SearchError
from app.sdk.source_monitor import record_telegram_failures, record_web_failures
from resource_search import search_web_resources


def candidate(name="folder_movie"):
    return {"taskname": "测试动画 S01E02", "shareurl": "https://pan.quark.cn/s/abc123",
            "content": "类型：动画 更新至2集", "source": "Telegram账号", "channel": name,
            "source_url": f"https://t.me/{name}/1"}


class TelegramFolderRegistryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = str(Path(folder.name) / "sources.sqlite")
        self.registry = ChannelRegistry(self.path)
        self.now = 1000
        patch("app.sdk.channel_registry.time.time", side_effect=lambda: self.now).start()
        self.addCleanup(patch.stopall)
        self.web = Mock()
        self.directory = ["directory_movie", "shared_movie"]
        self.web._fetch.side_effect = self.fetch
        patch("app.sdk.channel_registry.WebSearch", return_value=self.web).start()
        self.account = Mock()
        self.account.status.return_value = {"authorized": True}
        self.account.discover_channels.return_value = {"ok": True, "channels": []}
        self.account.read_folder.return_value = {"ok": True, "channels": [{"name": "folder_movie"}, {"name": "shared_movie"}]}
        self.account.search.side_effect = lambda query, channels, **kwargs: {"ok": True, "candidates": [candidate(channels[0])]}
        self.account_class = patch("app.sdk.telegram_account.TelegramAccount", return_value=self.account).start()
        self.url = "https://t.me/addlist/SyntheticFolderToken"
        self.config = {"source": {"telegram": {"enable": True}, "channelsearch": {
            "telegram_channels": [], "qq_channels": [], "telegram_folders": [self.url]}}}

    def fetch(self, url, deadline, **kwargs):
        if url == DIRECTORY_URL:
            body = {"number": 4, "html_url": DIRECTORY_PAGE, "user": {"login": "fish2018"},
                    "body": "CHANNELS=" + ",".join(self.directory)}
            return json.dumps(body), url
        name = url.rsplit("/", 1)[1]
        return (f'<div class="tgme_widget_message" data-post="{name}/1"><div class="tgme_widget_message_text">'
                '测试动画 更新至2集 https://pan.quark.cn/s/abc123</div></div>'), url

    def test_folder_and_directory_union_survives_failed_refresh_and_removed_source(self):
        original = deepcopy(self.config)
        state = self.registry.maintain(self.config)
        self.assertEqual({row["name"] for row in state["channels"]}, {"directory_movie", "folder_movie", "shared_movie"})
        self.assertEqual(self.config, original)
        self.assertNotIn("SyntheticFolderToken", json.dumps(state))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertNotIn("SyntheticFolderToken", "\n".join(db.iterdump()))
        self.now += 86401
        self.account.read_folder.return_value = {"ok": False, "code": "NETWORK_ERROR", "error": "网络连接失败"}
        state = self.registry.maintain(self.config)
        self.assertIn("folder_movie", [row["name"] for row in state["channels"]])
        self.config["source"]["channelsearch"]["telegram_folders"] = []
        state = self.registry.maintain(self.config)
        self.assertEqual({row["name"] for row in state["channels"]}, {"directory_movie", "shared_movie"})

    def test_disabled_or_unauthorized_account_keeps_anonymous_discovery_and_retries_soon(self):
        self.config["source"]["telegram"]["enable"] = False
        state = self.registry.maintain(self.config)
        self.account_class.assert_not_called()
        self.assertTrue(self.registry.active_channels())
        folder = next(row for row in state["origins"] if row["kind"] == "folder")
        self.assertEqual(folder["next_check"], self.now + 300)
        self.now += 300
        self.config["source"]["telegram"]["enable"] = True
        self.account.status.return_value = {"authorized": False}
        self.account.read_folder.return_value = {"ok": False, "code": "AUTH_REQUIRED", "error": "Telegram 账号未登录"}
        state = self.registry.maintain(self.config)
        self.account.search.assert_not_called()
        self.assertTrue(self.registry.active_channels())
        self.assertEqual(next(row for row in state["origins"] if row["kind"] == "folder")["next_check"], self.now + 300)
        self.now += 300
        self.account.status.return_value = {"authorized": True}
        self.account.read_folder.return_value = {"ok": True, "channels": [{"name": "folder_movie"}]}
        self.registry.maintain(self.config)
        self.assertIn("folder_movie", self.registry.active_channels(check_health=False))

    def test_full_active_set_still_verifies_new_folder_candidate_and_rotates_standby(self):
        self.directory = [f"movie_{index}" for index in range(6)]
        self.config["source"]["channelsearch"]["telegram_folders"] = []
        self.registry.maintain(self.config)
        self.now += 300
        self.registry.maintain(self.config)
        self.assertEqual(len(self.registry.active_channels()), 6)
        self.config["source"]["channelsearch"]["telegram_folders"] = [self.url]
        self.account.read_folder.return_value = {"ok": True, "channels": [{"name": "folder_movie"}]}
        self.now += 300
        state = self.registry.maintain(self.config)
        self.assertEqual(next(row["status"] for row in state["channels"] if row["name"] == "folder_movie"), "standby")
        seen = set()
        for _ in range(7):
            seen.update(self.registry.active_channels())
            self.now += 300
        self.assertIn("folder_movie", seen)

    def test_account_validation_does_not_clear_http_cooldown_and_checks_real_quark_media(self):
        self.registry.health.record("https://t.me/", "HTTP 429", "7200")
        before = self.registry.health.status()
        self.registry.maintain(self.config)
        self.assertEqual(self.registry.health.status(), before)
        self.assertEqual(self.registry.active_channels(), [])
        self.assertTrue(self.registry.active_channels(check_health=False))
        for row in ({**candidate(), "shareurl": "https://115.com/s/abc"},
                    {**candidate(), "taskname": "影视软件APP"}):
            with self.subTest(row=row), self.assertRaises(SearchError):
                ChannelRegistry._validate_media([row])

    def test_unavailable_manual_channel_is_filtered_and_rechecked_without_auto_discovery(self):
        config = {"source": {"telegram": {"enable": True}, "channelsearch": {
            "auto_discover": False, "telegram_channels": ["manual_movie"], "qq_channels": []}}}
        record_telegram_failures([], self.path, ["manual_movie"], [{"name": "manual_movie", "retry_after": 3600}])
        self.assertEqual(self.registry.available_channels(["manual_movie"]), [])
        self.assertEqual(self.registry.health.status(), [])
        self.now += 3600
        state = self.registry.maintain(config)
        self.account.search.assert_called_once()
        self.web._fetch.assert_not_called()
        self.assertEqual(state["channels"][0]["status"], "active")
        self.assertEqual(self.registry.available_channels(["manual_movie"]), ["manual_movie"])
        self.assertEqual(config["source"]["channelsearch"]["telegram_channels"], ["manual_movie"])

    def test_folder_validation_and_bad_member_names_cannot_introduce_arbitrary_urls(self):
        for value in (["https://evil.example/addlist/token"], ["https://t.me/addlist/token?secret=1"],
                      ["https://t.me/+invite"], [self.url] * 4, [None]):
            with self.subTest(value=value), self.assertRaises(SearchError):
                folder_urls(value)
        self.account.read_folder.return_value = {"ok": True, "channels": [
            {"name": "https://localhost"}, {"name": "somebot"}, {"name": "addlist"}, {"name": "folder_movie"}]}
        state = self.registry.maintain(self.config)
        self.assertEqual({row["name"] for row in state["channels"]}, {"directory_movie", "shared_movie", "folder_movie"})

    def test_still_unavailable_channel_recheck_preserves_exclusion_and_server_retry(self):
        record_telegram_failures([], self.path, ["manual_movie"], [{"name": "manual_movie", "retry_after": 3600}])
        self.now += 3600
        self.account.search.return_value = None
        self.account.search.side_effect = lambda *args, **kwargs: {"ok": True, "candidates": [],
            "errors": [{"source": "Telegram @manual_movie", "code": "CHANNEL_RESTRICTED"}],
            "unavailable_channels": [{"name": "manual_movie", "retry_after": 86400}]}
        config = {"source": {"telegram": {"enable": True}, "channelsearch": {
            "auto_discover": False, "telegram_channels": ["manual_movie"]}}}
        state = self.registry.maintain(config)
        row = state["channels"][0]
        self.assertEqual((row["reason"], row["next_check"]), ("频道不可访问", self.now + 86400))
        self.assertEqual(self.registry.available_channels(["manual_movie"]), [])

    def test_new_folder_gets_fair_slots_when_eighty_verified_channels_already_exist(self):
        self.directory = [f"directory_{index:02d}" for index in range(40)]
        first = [f"old_folder_{index:02d}" for index in range(40)]
        second = [f"new_folder_{index:02d}" for index in range(40)]
        self.account.read_folder.side_effect = lambda url, **kwargs: {"ok": True, "channels": [
            {"name": name} for name in (first if url == self.url else second)]}
        self.assertEqual(len(self.registry.maintain(self.config)["channels"]), 80)
        with self.registry._connect() as db:
            db.execute("UPDATE discovered_channels SET status='standby'")
            db.executemany("UPDATE discovered_channels SET status='active' WHERE name=?", [(name,) for name in self.directory[:6]])
        self.config["source"]["channelsearch"]["telegram_folders"].append("https://t.me/addlist/SecondSyntheticFolder")
        self.now += 300
        state = self.registry.maintain(self.config)
        names = {row["name"] for row in state["channels"]}
        self.assertEqual(len(names), 80)
        self.assertTrue(names.intersection(second))
        self.assertTrue(names.intersection(first))
        self.assertTrue(set(self.directory[:6]).issubset(names))

    def test_late_transient_failure_cannot_downgrade_confirmed_unavailable_channel(self):
        record_telegram_failures([], self.path, ["manual_movie"], [{"name": "manual_movie", "retry_after": 86400}])
        before = self.registry.status()["channels"][0]
        for reason in ("HTTP 403", "HTTP 404", "请求超时", "网络连接失败"):
            self.now += 1
            record_web_failures([{"source": "频道索引 Telegram @manual_movie", "message": reason}], self.path)
            row = self.registry.status()["channels"][0]
            self.assertEqual((row["reason"], row["next_check"]), ("频道不可访问", before["next_check"]))
            self.assertEqual(self.registry.available_channels(["manual_movie"]), [])
        record_telegram_failures([], self.path, ["manual_movie"], [{"name": "manual_movie", "retry_after": 3600}])
        self.assertEqual(self.registry.status()["channels"][0]["next_check"], before["next_check"])


class TelegramSearchIntegrationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = str(Path(folder.name) / "sources.sqlite")
        self.web = Mock(errors=[])
        self.web.search.return_value = []
        patch("resource_search.WebSearch", return_value=self.web).start()
        self.account = Mock()
        self.account.search.return_value = {"ok": True, "candidates": [candidate()], "errors": []}
        self.account_class = patch("app.sdk.telegram_account.TelegramAccount", return_value=self.account).start()
        self.addCleanup(patch.stopall)
        self.config = {"source": {"websearch": {"enable": False}, "channelsearch": {
            "telegram_channels": ["manual_movie"], "qq_channels": []}, "telegram": {"enable": True}}}

    def test_account_source_is_opt_in_and_respects_channel_disable(self):
        for update in ({}, {"enable": False}):
            config = deepcopy(self.config)
            config["source"]["telegram"] = update
            search_web_resources("测试动画", config, health_path=self.path)
        self.account_class.assert_not_called()
        self.config["source"]["channelsearch"]["enable"] = False
        search_web_resources("测试动画", self.config, health_path=self.path)
        self.account_class.assert_not_called()

    def test_native_candidates_survive_web_failure_and_share_one_bounded_concurrent_window(self):
        started = threading.Event()
        deadlines = []
        original = deepcopy(self.config)

        def native(query, channels, limit, deadline):
            deadlines.append(deadline - time.monotonic())
            started.set()
            return {"ok": True, "candidates": [candidate()]}

        def web(*args, **kwargs):
            self.assertTrue(started.wait(2), "Account source must start while anonymous search is in progress")
            raise SearchError("请求超时")

        self.account.search.side_effect = native
        self.web.search.side_effect = web
        result = search_web_resources("测试动画", self.config, health_path=self.path)
        self.assertTrue(result["success"])
        self.assertEqual(result["data"][0]["shareurl"], candidate()["shareurl"])
        self.assertTrue(0 < deadlines[0] <= 32)
        self.assertEqual(self.config, original)

    def test_account_failure_is_sanitized_and_does_not_break_anonymous_results(self):
        self.web.search.return_value = [candidate("anonymous")]
        self.account.search.return_value = {"ok": False, "code": "AUTH_REQUIRED", "error": "SECRET_PHONE_SESSION", "candidates": []}
        failed = set()
        result = search_web_resources("测试动画", self.config, failed_engines=failed, health_path=self.path)
        self.assertTrue(result["success"])
        self.assertEqual(len(result["data"]), 1)
        self.assertNotIn("SECRET_PHONE_SESSION", json.dumps(result))
        self.assertIn("Telegram账号", failed)
        search_web_resources("其他译名", self.config, failed_engines=failed, health_path=self.path)
        self.account.search.assert_called_once()

    def test_unavailable_manual_channel_is_removed_from_effective_queries_not_config(self):
        self.config["source"]["channelsearch"]["telegram_channels"].append("other_movie")
        self.account.search.return_value = {"ok": True, "candidates": [candidate("other_movie")],
            "errors": [{"source": "Telegram @manual_movie", "code": "CHANNEL_UNAVAILABLE"}],
            "unavailable_channels": [{"name": "manual_movie", "retry_after": 3600}]}
        before = deepcopy(self.config)
        search_web_resources("测试动画", self.config, health_path=self.path)
        self.account.search.reset_mock()
        search_web_resources("另一作品", self.config, health_path=self.path)
        self.assertEqual(self.account.search.call_args.args[1], ["other_movie"])
        self.assertEqual(self.web.search.call_args.kwargs["channel_config"]["telegram_channels"], ["other_movie"])
        self.assertEqual(self.config, before)

    def test_verified_folder_channel_rotates_into_native_queries_even_with_six_manual_sources(self):
        manual = [f"manual_{index}" for index in range(6)]
        self.config["source"]["channelsearch"]["telegram_channels"] = manual
        registry = ChannelRegistry(self.path)
        with registry._connect() as db:
            db.execute("INSERT INTO discovered_channels VALUES ('folder_movie', 'standby', '', 1, 3600)")
        seen = set()
        for index in range(7):
            with patch("resource_search.time.time", return_value=index * 300):
                search_web_resources("测试动画", self.config, health_path=self.path)
            selected = self.account.search.call_args.args[1]
            self.assertLessEqual(len(selected), 6)
            seen.update(selected)
        self.assertIn("folder_movie", seen)
        self.assertEqual(self.config["source"]["channelsearch"]["telegram_channels"], manual)

    def test_all_channel_errors_stop_repeating_account_source_for_each_alias(self):
        self.account.search.return_value = {"ok": True, "candidates": [], "errors": [
            {"source": "Telegram @manual_movie", "code": "TIMEOUT"}]}
        failed = set()
        search_web_resources("测试动画", self.config, failed_engines=failed, health_path=self.path)
        self.assertIn("Telegram账号", failed)
        search_web_resources("其他译名", self.config, failed_engines=failed, health_path=self.path)
        self.account.search.assert_called_once()

    def test_native_verified_candidate_recovers_channel_without_clearing_http_cooldown(self):
        registry = ChannelRegistry(self.path)
        with registry._connect() as db:
            db.execute("INSERT INTO discovered_channels VALUES ('folder_movie', 'active', '', 1, 3600)")
        self.web.errors = ["Telegram @folder_movie: HTTP 403"]
        registry.health.record("https://t.me/", "HTTP 403")
        before = registry.health.status()
        search_web_resources("测试动画", self.config, health_path=self.path)
        self.assertIn("folder_movie", registry.active_channels(check_health=False))
        self.assertEqual(registry.health.status(), before)
        started = time.time() - 1
        registry.record_failure("folder_movie", "频道不可访问", check_health=False)
        registry.record_account_results(["folder_movie"], [candidate()], started)
        self.assertNotIn("folder_movie", registry.active_channels(check_health=False))

    def test_partial_native_candidates_survive_deadline_and_skip_repeating_timed_out_source(self):
        self.web.search.side_effect = SearchError("请求超时")
        self.account.search.return_value = {"ok": False, "code": "TIMEOUT", "error": "请求超时",
                                            "candidates": [candidate()], "errors": []}
        failed = set()
        result = search_web_resources("测试动画", self.config, failed_engines=failed, health_path=self.path)
        self.assertTrue(result["success"])
        self.assertEqual(result["data"], [candidate()])
        self.assertIn({"source": "Telegram账号", "message": "请求超时"}, result["errors"])
        self.assertIn("Telegram账号", failed)
        search_web_resources("其他译名", self.config, failed_engines=failed, health_path=self.path)
        self.account.search.assert_called_once()


if __name__ == "__main__":
    unittest.main()
