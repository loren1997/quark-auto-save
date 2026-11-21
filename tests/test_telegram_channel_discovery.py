from copy import deepcopy
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

from app.sdk.channel_search import channel_queries
from app.sdk.common import SearchError
from app.sdk.websearch import WebSearch, _cache, _validate_url


class TelegramArchiveBoundaryTests(unittest.TestCase):
    def setUp(self):
        _cache.clear()

    def test_old_public_channel_configuration_is_ignored_without_changing_qq(self):
        config = {"enable": True, "auto_discover": True, "telegram_channels": ["old_public_movie"],
                  "telegram_folders": ["https://t.me/addlist/SyntheticFolderToken"], "qq_channels": ["pd26778899"]}
        before = deepcopy(config)
        self.assertEqual(channel_queries(config, "作品"), [("腾讯频道 pd26778899", "https://pd.qq.com/g/pd26778899")])
        self.assertEqual(config, before)

    def test_all_telegram_public_aliases_are_blocked_before_dns(self):
        with patch("app.sdk.websearch._public_addresses") as dns:
            for host in ("t.me", "T.ME.", "telegram.me", "telegram.dog", "web.telegram.org", "sub.t.me"):
                with self.subTest(host=host), self.assertRaisesRegex(SearchError, "已归档"):
                    _validate_url("https://" + host + "/some_movie/1")
            dns.assert_not_called()

    @patch("app.sdk.websearch._public_addresses", return_value=["8.8.8.8"])
    def test_redirect_to_public_telegram_is_not_followed(self, dns):
        client = WebSearch()
        self.addCleanup(client.session.close)
        response = MagicMock(status_code=302, headers={"Location": "https://t.me/public_movie/1"})
        response.__enter__.return_value = response
        client.session.get = Mock(return_value=response)
        with self.assertRaisesRegex(SearchError, "已归档"):
            client._fetch("https://example.org/redirect", time.monotonic() + 10)
        client.session.get.assert_called_once()

    @patch("app.sdk.websearch._public_addresses", return_value=["8.8.8.8"])
    def test_google_public_telegram_snippet_and_page_are_skipped_but_other_sites_work(self, dns):
        client = WebSearch()
        self.addCleanup(client.session.close)
        good_share = "https://pan.quark.cn/s/abc123"
        bad_share = "https://pan.quark.cn/s/def456"
        client._fetch = Mock(side_effect=lambda url, deadline: (
            (f"<title>目标作品</title><p>目标作品 {good_share}</p>", url)
            if url == "https://example.org/movie" else ("search", url)))
        def results(engine, body, url):
            if engine != "Google":
                return []
            return [{"title": "目标作品", "url": "https://t.me/public_movie/1", "snippet": bad_share},
                    {"title": "目标作品", "url": "https://example.org/movie", "snippet": "夸克资源"}]
        with patch("app.sdk.websearch._search_results", side_effect=results):
            rows = client.search("目标作品", channel_config={"telegram_channels": ["public_movie"], "qq_channels": []})
        self.assertEqual([row["shareurl"] for row in rows], [good_share])
        self.assertFalse(any("t.me" in call.args[0] for call in client._fetch.call_args_list))
        self.assertIn("已归档", " ".join(client.errors))


if __name__ == "__main__":
    unittest.main()
