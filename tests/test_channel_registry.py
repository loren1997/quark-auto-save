import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.sdk.channel_registry import ChannelRegistry
from app.sdk.common import SearchError


class ChannelRegistryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = str(Path(folder.name) / "sources.sqlite")
        self.registry = ChannelRegistry(self.path)

    def test_no_path_remains_disabled(self):
        registry = ChannelRegistry(None)
        self.assertEqual(registry.maintain({})["channels"], [])
        self.assertEqual(registry.active_channels(), [])

    def test_historical_active_channels_are_not_search_sources(self):
        with self.registry._connect() as db:
            db.execute("INSERT INTO discovered_channels VALUES ('old_movie', 'active', '', 1, 9999999999)")
        self.assertEqual(self.registry.active_channels(), [])
        self.assertEqual(self.registry.active_channels(check_health=False), [])
        self.assertEqual(self.registry.status()["channels"][0]["name"], "old_movie")

    def test_maintenance_preserves_history_without_using_legacy_account_or_network(self):
        with self.registry._connect() as db:
            db.execute("INSERT INTO channel_origins VALUES ('account', ?, 1, 2, '')", (json.dumps(["old_movie"]),))
            db.execute("INSERT INTO channel_joins VALUES ('old-generation', 'old_movie', 'joined', '', 1, 0, 1)")
            before = list(db.iterdump())
        config = {"source": {"telegram": {"enable": True, "auto_join": True}, "channelsearch": {
            "enable": True, "auto_discover": True, "telegram_channels": ["old_movie"],
            "telegram_folders": ["https://t.me/addlist/SyntheticFolderToken"]}}}
        encoded = json.dumps(config)
        with patch("app.sdk.telegram_account.TelegramAccount") as account, patch("requests.Session.get") as get:
            state = self.registry.maintain(config)
        account.assert_not_called()
        get.assert_not_called()
        self.assertNotIn("joins", state)
        self.assertNotIn("old-generation", json.dumps(state))
        self.assertEqual(json.dumps(config), encoded)
        with self.registry._connect() as db:
            self.assertEqual(list(db.iterdump()), before)

    def test_retired_direct_entrypoints_never_discover_probe_or_join(self):
        client, account = Mock(), Mock()
        self.registry._refresh_origins(client, ["https://t.me/addlist/SyntheticFolderToken"], account, 9999999999, 1, set())
        self.registry._join_verified(account, 9999999999, 1)
        for probe, args in ((self.registry._probe, (client, "old_movie", 9999999999)),
                            (self.registry._probe_account, (account, "old_movie", 9999999999))):
            with self.assertRaisesRegex(SearchError, "已归档"):
                probe(*args)
        self.assertEqual(client.mock_calls, [])
        self.assertEqual(account.mock_calls, [])

    def test_local_failure_history_is_sanitized_and_remains_readable(self):
        self.registry.record_failure("old_movie", "频道不可访问 sensitive", check_health=False, register=True, retry_after=86400)
        row = self.registry.status()["channels"][0]
        self.assertEqual(row["reason"], "频道不可访问")
        self.assertEqual(self.registry.available_channels(["old_movie", "other_movie"]), ["other_movie"])
        self.assertEqual(self.registry.active_channels(), [])


if __name__ == "__main__":
    unittest.main()
