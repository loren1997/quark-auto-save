"""Exercise registry -> resource search -> real channel parser without network I/O."""

from copy import deepcopy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from app.sdk.channel_registry import ChannelRegistry
from app.sdk.common import SearchError
from app.sdk.websearch import WebSearch, _cache
from resource_search import search_web_resources


class DynamicChannelSearchTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = str(Path(self.folder.name) / "sources.sqlite")
        self.registry = ChannelRegistry(self.path)
        _cache.clear()
        self.addCleanup(_cache.clear)
        self.fetch = patch.object(WebSearch, "_fetch", side_effect=self.channel_page).start()
        # Public-host validation otherwise resolves DNS even for fixture share URLs.
        patch("app.sdk.websearch._validate_url", return_value="pan.quark.cn").start()
        self.addCleanup(patch.stopall)

    def seed(self, name, status="active"):
        with self.registry._connect() as db:
            db.execute("INSERT INTO discovered_channels VALUES (?, ?, '', ?, ?)",
                       (name, status, time.time(), time.time() + 3600))

    @staticmethod
    def channel_page(url, _deadline):
        parsed = urlsplit(url)
        if parsed.hostname != "t.me" or not parsed.path.startswith("/s/"):
            raise AssertionError(f"Unexpected fixture request: {url}")
        channel = parsed.path.removeprefix("/s/")
        share = "https://pan.quark.cn/s/abc123"
        body = (f'<div class="tgme_widget_message" data-post="{channel}/1">'
                f'<div class="tgme_widget_message_text">名称：目标作品 动画 S01E02<br>'
                f'<a href="{share}">夸克网盘</a></div>'
                f'<a class="tgme_widget_message_date" href="https://t.me/{channel}/1">'
                '<time datetime="2026-11-10T00:00:00+00:00"></time></a></div>')
        return body, url

    @staticmethod
    def config(**channels):
        return {"source": {"websearch": {"enable": False}, "channelsearch": {
            "enable": True, "telegram_channels": ["manual_one", "manual_two"],
            "qq_channels": [], **channels}}}

    def requested_channels(self):
        return [urlsplit(call.args[0]).path.removeprefix("/s/")
                for call in self.fetch.call_args_list]

    def test_verified_dynamic_channels_reach_websearch_preserving_custom_config_and_cap(self):
        for name in ("auto_one", "auto_two", "auto_three", "auto_four", "auto_five"):
            self.seed(name)
        self.seed("inactive_one", "cooldown")
        self.seed("pending_one", "pending")
        for configured in (["manual_one", "manual_two"], "@manual_one, manual_two"):
            with self.subTest(configured=configured):
                self.fetch.reset_mock()
                config = self.config(telegram_channels=configured)
                before = deepcopy(config)
                result = search_web_resources("目标作品", config, refresh=True, health_path=self.path)
                requested = self.requested_channels()
                self.assertTrue(result["success"])
                self.assertTrue(result["data"])
                self.assertEqual(len(requested), 6)
                self.assertTrue({"manual_one", "manual_two"}.issubset(requested))
                self.assertEqual(len([name for name in requested if name.startswith("auto_")]), 4)
                self.assertNotIn("inactive_one", requested)
                self.assertNotIn("pending_one", requested)
                self.assertEqual(config, before)

    def test_auto_discovery_off_keeps_manual_channels_and_channel_off_skips_all(self):
        self.seed("auto_one")
        for toggle in ({"auto_discover": False}, {"auto_discover": "false"}, {"enable": False}):
            with self.subTest(toggle=toggle):
                self.fetch.reset_mock()
                config = self.config(**toggle)
                before = deepcopy(config)
                result = search_web_resources("目标作品", config, refresh=True, health_path=self.path)
                self.assertTrue(result["success"])
                expected = [] if toggle.get("enable") is False else ["manual_one", "manual_two"]
                self.assertCountEqual(self.requested_channels(), expected)
                self.assertEqual(config, before)

    def test_real_search_failure_records_cooldown_and_removes_dynamic_source_from_next_search(self):
        self.seed("auto_one")
        original = ChannelRegistry.record_failure

        def fetch(url, deadline):
            if urlsplit(url).path == "/s/auto_one":
                raise SearchError("请求超时")
            return self.channel_page(url, deadline)

        self.fetch.side_effect = fetch
        config = self.config()
        before = deepcopy(config)
        with patch.object(ChannelRegistry, "record_failure", autospec=True, side_effect=original) as record:
            result = search_web_resources("目标作品", config, refresh=True, health_path=self.path)
        self.assertTrue(result["success"])
        self.assertTrue(result["data"])
        self.assertEqual(record.call_count, 1)
        self.assertEqual(record.call_args.args[1:], ("auto_one", "请求超时"))
        self.assertIn({"source": "频道索引 Telegram @auto_one", "message": "请求超时"}, result["errors"])
        row = self.registry.status()["channels"][0]
        self.assertEqual((row["name"], row["status"], row["reason"]), ("auto_one", "cooldown", "请求超时"))
        self.fetch.reset_mock()
        search_web_resources("目标作品", config, refresh=True, health_path=self.path)
        self.assertCountEqual(self.requested_channels(), ["manual_one", "manual_two"])
        self.assertEqual(config, before)

    def test_channel_disable_leaves_web_engines_enabled_without_dynamic_lookup(self):
        self.seed("auto_one")
        config = self.config(enable=False)
        config["source"]["websearch"]["enable"] = True
        before = deepcopy(config)
        self.fetch.side_effect = lambda url, deadline: ("", url)
        with patch("app.sdk.websearch._search_results", return_value=[]), \
                patch.object(ChannelRegistry, "active_channels") as active:
            result = search_web_resources("目标作品", config, refresh=True, health_path=self.path)
        self.assertTrue(result["success"])
        active.assert_not_called()
        hosts = {urlsplit(call.args[0]).hostname for call in self.fetch.call_args_list}
        self.assertEqual(hosts, {"www.panso.vip", "meijusou.com", "html.duckduckgo.com",
                                 "cn.bing.com", "www.google.com", "www.sogou.com"})
        self.assertEqual(config, before)


if __name__ == "__main__":
    unittest.main()
