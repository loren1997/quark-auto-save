import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from app.sdk.channel_registry import ChannelRegistry, DIRECTORY_PAGE, DIRECTORY_URL
from app.sdk.common import SearchError


def catalogue(names):
    return json.dumps({"number": 4, "html_url": DIRECTORY_PAGE, "user": {"login": "fish2018"},
                       "body": "```sh\nexport CHANNELS=" + ",".join(names) + "\n```"})


def message(name, title="名称：测试动画 (2026) 更新至02集", body="类型：动画", share=True):
    link = '<a href="https://pan.quark.cn/s/abcdef123456">夸克</a>' if share else '<a href="https://115.com/s/abc">115</a>'
    return f'<div class="tgme_widget_message" data-post="{name}/123"><div class="tgme_widget_message_text">{title}<br>{body}<br>{link}</div></div>'


class ChannelRegistryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = str(Path(folder.name) / "health.sqlite")
        self.registry = ChannelRegistry(self.path)
        self.now = 1000
        self.clock = patch("app.sdk.channel_registry.time.time", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def client(self, names=("ChannelAlpha", "ChannelBravo"), bodies=None):
        client = MagicMock()
        def fetch(url, deadline, **kwargs):
            self.registry.health.check(url)
            if url == DIRECTORY_URL:
                self.assertTrue(kwargs.get("allow_json"))
                return catalogue(names), url
            self.assertRegex(url, r"^https://t\.me/s/[A-Za-z][A-Za-z0-9_]{3,31}$")
            name = url.rsplit("/", 1)[1]
            body = (bodies or {}).get(name, message(name))
            if isinstance(body, Exception):
                raise body
            return body, url
        client._fetch.side_effect = fetch
        return client

    def maintain(self, client, config=None):
        with patch("app.sdk.channel_registry.WebSearch", return_value=client):
            return self.registry.maintain(config or {})

    def test_no_path_or_disabled_setting_never_fetches(self):
        with patch("app.sdk.channel_registry.WebSearch") as client, patch("app.sdk.channel_registry.sqlite3.connect") as connect:
            registry = ChannelRegistry()
            self.assertEqual(registry.maintain({})["channels"], [])
            self.assertEqual(registry.active_channels(), [])
            registry.record_failure("ChannelAlpha", "HTTP 429")
            client.assert_not_called()
            connect.assert_not_called()
        for field in ("enable", "auto_discover"):
            with patch("app.sdk.channel_registry.WebSearch") as client:
                self.registry.maintain({"source": {"channelsearch": {field: False}}})
                client.assert_not_called()

    def test_only_owner_catalogue_is_accepted_and_candidates_are_bounded(self):
        names = ["SomeBot", "joinchat", "@BadName", "Channel_A"] + [f"channel_{i:02d}" for i in range(60)]
        client = self.client(names)
        state = self.maintain(client)
        self.assertEqual(len(state["channels"]), 40)
        self.assertNotIn("SomeBot", [row["name"] for row in state["channels"]])
        self.assertNotIn("joinchat", [row["name"] for row in state["channels"]])
        self.assertNotIn("@BadName", [row["name"] for row in state["channels"]])
        self.assertEqual(sum(row["status"] == "active" for row in state["channels"]), 3)
        self.assertLessEqual(client._fetch.call_count, 4)
        self.assertEqual(client._fetch.call_args_list[0].args[0], DIRECTORY_URL)
        for change in ({"user": {"login": "someone"}}, {"number": 5}, {"html_url": "https://evil.test"}, {"user": "bad"}):
            data = json.loads(catalogue(["ChannelAlpha"]))
            data.update(change)
            with self.subTest(change=change), self.assertRaises(SearchError):
                ChannelRegistry._directory(json.dumps(data))
        with self.assertRaises(SearchError):
            ChannelRegistry._directory(catalogue(["ChannelAlpha", "https://t.me/+invite"]))

    def test_custom_channels_are_untouched_and_excluded_from_extras(self):
        config = {"source": {"channelsearch": {"telegram_channels": "ChannelAlpha,@OtherCustom"}}}
        original = json.dumps(config)
        client = self.client()
        self.maintain(client, config)
        self.assertEqual(json.dumps(config), original)
        self.assertEqual(self.registry.active_channels(["CHANNELBRAVO"]), [])
        self.assertEqual(self.registry.active_channels(), ["ChannelBravo"])
        self.assertNotIn("https://t.me/s/ChannelAlpha", [call.args[0] for call in client._fetch.call_args_list])

    def test_plain_http_success_and_software_or_other_disks_do_not_enable_channel(self):
        client = self.client(("ChannelAlpha", "ChannelBravo", "ChannelThird"), {
            "ChannelAlpha": message("ChannelAlpha", share=False),
            "ChannelBravo": message("ChannelBravo", title="影视软件 APP", body="电视剧电影软件"),
            "ChannelThird": "<html>loading</html>",
        })
        state = self.maintain(client)
        self.assertEqual(self.registry.active_channels(), [])
        self.assertEqual({row["status"] for row in state["channels"]}, {"cooldown"})
        self.assertIn("未发现影视夸克链接", {row["reason"] for row in state["channels"]})

    def test_catalogue_is_read_at_most_once_a_day_and_state_survives_restart(self):
        client = self.client()
        self.maintain(client)
        self.now += 300
        self.maintain(client)
        self.assertEqual(sum(call.args[0] == DIRECTORY_URL for call in client._fetch.call_args_list), 1)
        self.assertEqual(ChannelRegistry(self.path).active_channels(), ["ChannelAlpha", "ChannelBravo"])
        self.now += 86400
        self.maintain(client)
        self.assertEqual(sum(call.args[0] == DIRECTORY_URL for call in client._fetch.call_args_list), 2)

    def test_failure_is_sanitized_and_due_channel_is_restored(self):
        client = self.client(("ChannelAlpha",))
        self.maintain(client)
        self.registry.record_failure("ChannelAlpha", "HTTP 500 SECRET_COOKIE=never-store")
        row = self.registry.status()["channels"][0]
        self.assertEqual((row["status"], row["reason"], row["next_check"]), ("cooldown", "HTTP 500", self.now + 300))
        self.assertEqual(self.registry.active_channels(), [])
        self.now += 299
        self.maintain(client)
        self.assertEqual(self.registry.active_channels(), [])
        self.now += 1
        self.maintain(client)
        self.assertEqual(self.registry.active_channels(), ["ChannelAlpha"])
        with closing(sqlite3.connect(self.path)) as db:
            dump = "\n".join(db.iterdump())
        self.assertNotIn("SECRET_COOKIE", dump)
        self.registry.record_failure("NotRegistered", "HTTP 500")
        self.assertEqual(len(self.registry.status()["channels"]), 1)

    def test_retry_after_host_cooldown_is_not_shortened(self):
        client = self.client(("ChannelAlpha",))
        self.maintain(client)
        self.registry.health.record("https://t.me/s/ChannelAlpha", "HTTP 429", retry_after="3600")
        self.registry.record_failure("ChannelAlpha", "HTTP 429")
        self.assertGreaterEqual(self.registry.status()["channels"][0]["next_check"], self.now + 3600)
        self.now += 300
        self.maintain(client)
        self.assertEqual(self.registry.active_channels(), [])
        self.now += 3301
        self.maintain(client)
        self.assertEqual(self.registry.active_channels(), ["ChannelAlpha"])
        self.assertEqual(self.registry.health.status(), [])

    def test_older_successful_probe_cannot_clear_a_newer_search_failure(self):
        client = self.client(("ChannelAlpha",))
        self.maintain(client)
        self.now += 3601
        original = client._fetch.side_effect
        def newer_failure(url, deadline, **kwargs):
            result = original(url, deadline, **kwargs)
            if url.startswith("https://t.me/"):
                self.now += 1
                self.registry.record_failure("ChannelAlpha", "HTTP 500")
            return result
        client._fetch.side_effect = newer_failure
        self.maintain(client)
        self.assertEqual(self.registry.active_channels(), [])
        self.assertEqual(self.registry.status()["channels"][0]["reason"], "HTTP 500")

    def test_catalogue_redirect_or_malformed_owner_data_is_not_accepted(self):
        client = self.client()
        client._fetch.side_effect = lambda url, deadline, **kwargs: (catalogue(["ChannelAlpha"]), "https://evil.test/catalogue")
        state = self.maintain(client)
        self.assertEqual(state["channels"], [])
        self.assertEqual(state["discovery_reason"], "频道地址不符合访问规则")

    def test_active_limit_and_successful_recovery_standby(self):
        names = [f"channel_{i:02d}" for i in range(8)]
        client = self.client(names)
        self.maintain(client)
        self.now += 300
        self.maintain(client)
        self.assertEqual(len(self.registry.active_channels()), 6)
        self.registry.record_failure(names[6], "HTTP 500")
        self.now += 300
        self.maintain(client)
        state = {row["name"]: row["status"] for row in self.registry.status()["channels"]}
        self.assertEqual(state[names[6]], "standby")
        self.assertEqual(len(self.registry.active_channels()), 6)
        self.registry.record_failure(names[0], "HTTP 500")
        self.maintain(client)
        self.assertIn(names[6], self.registry.active_channels())
        self.assertEqual(len(self.registry.active_channels()), 6)

    def test_an_existing_maintenance_lease_prevents_parallel_network_requests(self):
        entered, release = threading.Event(), threading.Event()
        client = self.client(("ChannelAlpha",))
        original = client._fetch.side_effect
        def blocking(url, deadline, **kwargs):
            if url == DIRECTORY_URL:
                entered.set()
                self.assertTrue(release.wait(5))
            return original(url, deadline, **kwargs)
        client._fetch.side_effect = blocking
        with patch("app.sdk.channel_registry.WebSearch", return_value=client):
            worker = threading.Thread(target=self.registry.maintain, args=({},))
            worker.start()
            self.assertTrue(entered.wait(3))
            try:
                ChannelRegistry(self.path).maintain({})
                self.assertEqual(client._fetch.call_count, 1)
            finally:
                release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(self.registry.active_channels(), ["ChannelAlpha"])


if __name__ == "__main__":
    unittest.main()
