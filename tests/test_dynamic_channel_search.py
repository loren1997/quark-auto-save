from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from app.sdk.channel_search import channel_queries
from resource_search import search_web_resources


class ArchivedChannelSearchTests(unittest.TestCase):
    def setUp(self):
        self.web = Mock(errors=[])
        self.web.search.return_value = []
        self.archive = Mock()
        self.archive.search.return_value = {"success": True, "results": []}
        patch("resource_search.WebSearch", return_value=self.web).start()
        self.archive_factory = patch("app.sdk.telegram_web.TelegramWeb", return_value=self.archive).start()
        self.legacy_factory = patch("app.sdk.telegram_account.TelegramAccount").start()
        self.registry_factory = patch("app.sdk.channel_registry.ChannelRegistry").start()
        self.addCleanup(patch.stopall)
        self.config = {"source": {"websearch": {"enable": False}, "channelsearch": {
            "enable": True, "auto_discover": True, "telegram_channels": ["manual_movie"],
            "telegram_folders": ["https://t.me/addlist/SyntheticFolderToken"], "qq_channels": ["pd26778899"]},
            "telegram": {"enable": True, "auto_join": True}, "telegram_web": {"enable": True}}}

    def test_legacy_dynamic_and_manual_channels_never_expand_archived_query(self):
        before = deepcopy(self.config)
        self.assertTrue(search_web_resources("目标作品", self.config)["success"])
        self.archive.search.assert_called_once()
        self.assertEqual(self.archive.search.call_args.args, ("目标作品",))
        self.assertNotIn("channels", self.archive.search.call_args.kwargs)
        self.legacy_factory.assert_not_called()
        self.registry_factory.return_value.active_channels.assert_not_called()
        options = self.web.search.call_args.kwargs["channel_config"]
        self.assertEqual(channel_queries(options, "目标作品"),
                         [("腾讯频道 pd26778899", "https://pd.qq.com/g/pd26778899")])
        self.assertEqual(self.config, before)

    def test_disabled_archive_does_not_use_previous_session_or_discovered_history(self):
        self.config["source"]["telegram_web"]["enable"] = False
        search_web_resources("目标作品", self.config)
        self.archive_factory.assert_not_called()
        self.legacy_factory.assert_not_called()
        self.registry_factory.return_value.active_channels.assert_not_called()

    def test_archive_source_can_run_with_public_channel_search_disabled(self):
        self.config["source"]["channelsearch"]["enable"] = False
        self.assertTrue(search_web_resources("目标作品", self.config)["success"])
        self.archive.search.assert_called_once()
        self.web.search.assert_not_called()
        self.legacy_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
