import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from app.sdk.channel_search import channel_queries, channel_results
from app.sdk.common import SearchError
from app.sdk.websearch import WebSearch, _cache
from quark_auto_save import Config


SHARE = "https://pan.quark.cn/s/correct"


def telegram_message(post, content):
    return f'''<div class="tgme_widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="{post}">
        <div class="tgme_widget_message_author">频道名称</div>
        <div class="tgme_widget_message_text js-message_text" dir="auto">{content}</div>
        <div><a class="tgme_widget_message_date" href="https://t.me/{post}"><time datetime="2026-11-10T00:00:00+00:00">08:00</time></a></div>
        </div></div>'''


class ChannelSearchTests(unittest.TestCase):
    def setUp(self):
        _cache.clear()

    def test_channel_configuration_only_constructs_bounded_fixed_domain_urls(self):
        jobs = channel_queries({"telegram_channels": "@Anime_Test, Anime_Test", "qq_channels": ["pd26778899"]}, "作品 02")
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0][0], "Telegram @Anime_Test")
        self.assertEqual(parse_qs(urlsplit(jobs[0][1]).query), {"q": ["作品 02"]})
        self.assertEqual(jobs[1][1], "https://pd.qq.com/g/pd26778899")
        self.assertEqual(channel_queries({"enable": False}, "作品"), [])
        for config in ({"telegram_channels": ["http://127.0.0.1"]}, {"telegram_channels": ["../secret"]},
                       {"qq_channels": ["pd12345/../../secret"]}, {"telegram_channels": ["Anime_Test"] * 7},
                       {"telegram_channels": [1]}, {"qq_channels": "user:password@private"}):
            with self.subTest(config=config), self.assertRaises(SearchError):
                channel_queries(config, "作品")

    def test_retired_channel_is_removed_only_from_exact_legacy_defaults(self):
        legacy = ["Q_dongman", "guoman4K", "kuakedongman"]
        for channels in (legacy, ",".join(legacy), "\n".join(legacy)):
            with self.subTest(channels=channels):
                config = {"source": {"channelsearch": {"enable": False, "telegram_channels": channels,
                                                        "qq_channels": ["pd123456"]}}}
                Config.breaking_change_update(config)
                self.assertEqual(config["source"]["channelsearch"], {
                    "enable": False, "telegram_channels": ["guoman4K", "kuakedongman"],
                    "qq_channels": ["pd123456"]})
        for channels in ([], ["Q_dongman"], legacy + ["my_channel"], "Q_dongman,my_channel"):
            with self.subTest(custom=channels):
                config = {"source": {"channelsearch": {"telegram_channels": channels}}}
                Config.breaking_change_update(config)
                self.assertEqual(config["source"]["channelsearch"]["telegram_channels"], channels)

    def test_telegram_messages_keep_titles_links_and_passwords_separate(self):
        html = (telegram_message("Anime_Test/1", f'名称：目标作品 2026<br>夸克：<a href="{SHARE}">网盘</a> 提取码 AA11')
                + telegram_message("Anime_Test/2", '名称：其他作品<br>https://pan.quark.cn/s/other 提取码 BB22')
                + telegram_message("Other_Channel/3", f'名称：目标作品<br>{SHARE}'))
        rows = channel_results("Telegram @Anime_Test", html, "https://t.me/s/Anime_Test?q=test", "目标作品 2026")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], SHARE + "?pwd=AA11")
        self.assertEqual(rows[0]["page_url"], "https://t.me/Anime_Test/1")
        self.assertEqual(rows[0]["title"], "名称：目标作品 2026")
        self.assertNotIn("BB22", rows[0]["snippet"])

    def test_telegram_query_filters_a_message_that_only_contains_an_unrelated_link(self):
        html = telegram_message("Anime_Test/1", '另一部片名<br>https://pan.quark.cn/s/other')
        self.assertEqual(channel_results("Telegram @Anime_Test", html, "https://t.me/s/Anime_Test", "目标作品"), [])

    def test_qq_preview_only_uses_each_post_title_and_does_not_borrow_neighbor_links(self):
        html = f'''<a href="/g/pd26778899/post/first">目标作品 2026</a><p>其他内容 {SHARE}</p>
            <a href="/g/pd26778899/post/second">其他作品</a>
            <a href="https://evil.example/g/pd26778899/post/third">目标作品</a>'''
        rows = channel_results("腾讯频道 pd26778899", html, "https://pd.qq.com/g/pd26778899", "目标作品")
        self.assertEqual(rows, [{"title": "目标作品 2026", "url": "https://pd.qq.com/g/pd26778899/post/first", "snippet": ""}])

    def test_verified_empty_pages_are_distinct_from_login_javascript_and_challenges(self):
        self.assertEqual(channel_results("Telegram @Anime_Test", '<section class="tgme_channel_history">No posts found</section>', "https://t.me/s/Anime_Test", "作品"), [])
        self.assertEqual(channel_results("腾讯频道 pd26778899", '<p>暂无帖子</p>', "https://pd.qq.com/g/pd26778899", "作品"), [])
        for engine, url in (("Telegram @Anime_Test", "https://t.me/s/Anime_Test"), ("腾讯频道 pd26778899", "https://pd.qq.com/g/pd26778899")):
            for body in ('<script>document.cookie="EO_Bot_Ssid=123"</script>', '<div id="captcha">验证</div>',
                         '<div id="root"></div><script src="app.js"></script>', '<title>请登录</title>'):
                with self.subTest(engine=engine, body=body), self.assertRaises(SearchError):
                    channel_results(engine, body, url, "作品")
            with self.assertRaises(SearchError):
                channel_results(engine, "暂无帖子", "https://other.example", "作品")

    @patch("app.sdk.websearch._validate_url", return_value="pan.quark.cn")
    def test_telegram_direct_share_does_not_require_general_search_or_an_extra_landing_fetch(self, validate):
        client = WebSearch()
        self.addCleanup(client.session.close)
        body = telegram_message("Anime_Test/1", f'名称：目标作品 2026<br>{SHARE}')
        client._fetch = Mock(return_value=(body, "https://t.me/s/Anime_Test"))
        config = {"telegram_channels": ["Anime_Test"], "qq_channels": []}
        rows = client.search("目标作品", channel_config=config, include_engines=False)
        self.assertEqual(rows[0]["shareurl"], SHARE)
        self.assertEqual(rows[0]["source"], "频道索引")
        self.assertEqual(rows[0]["page_url"], "https://t.me/Anime_Test/1")
        client._fetch.assert_called_once()
        validate.assert_called_with(SHARE)

    def test_failed_channels_are_skipped_for_aliases_and_retried_in_next_run(self):
        client = WebSearch()
        self.addCleanup(client.session.close)
        client._fetch = Mock(side_effect=SearchError("网页 HTTP 429"))
        config = {"telegram_channels": ["Anime_Test"], "qq_channels": []}
        failed = set()
        for query in ("目标作品", "另一个译名"):
            with self.assertRaises(SearchError):
                client.search(query, channel_config=config, include_engines=False, failed_engines=failed)
        self.assertEqual(client._fetch.call_count, 1)
        self.assertEqual(failed, {"Telegram @Anime_Test"})
        with self.assertRaises(SearchError):
            client.search("目标作品", channel_config=config, include_engines=False, failed_engines=set())
        self.assertEqual(client._fetch.call_count, 2)

    def test_channels_and_engines_share_deadline_and_at_most_two_workers(self):
        client = WebSearch()
        self.addCleanup(client.session.close)
        lock = threading.Lock()
        active, maximum, deadlines = 0, 0, []
        def fetch(url, deadline):
            nonlocal active, maximum
            with lock:
                deadlines.append(deadline)
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.01)
            with lock:
                active -= 1
            raise SearchError("网页 HTTP 403")
        client._fetch = Mock(side_effect=fetch)
        with self.assertRaises(SearchError):
            client.search("目标作品", channel_config={})
        self.assertEqual(len(deadlines), 9)
        self.assertEqual(len(set(deadlines)), 1)
        self.assertEqual(maximum, 2)
        urls = [call.args[0] for call in client._fetch.call_args_list]
        self.assertIn("panso.vip", urls[0])
        self.assertIn("t.me/s/guoman4K", urls[1])
        self.assertIn("meijusou.com", urls[2])
        self.assertIn("t.me/s/kuakedongman", urls[3])

    @patch("app.sdk.websearch._validate_url")
    def test_channel_requests_reuse_public_address_validation(self, validate):
        client = WebSearch()
        self.addCleanup(client.session.close)
        validate.side_effect = SearchError("网页地址解析到了非公网 IP，已拒绝访问")
        client.session.get = Mock()
        with self.assertRaises(SearchError):
            client.search("目标作品", channel_config={"telegram_channels": ["Anime_Test"], "qq_channels": []}, include_engines=False)
        client.session.get.assert_not_called()
        self.assertIn("非公网 IP", client.errors[0])

    @patch("app.sdk.websearch._validate_url", return_value="pan.quark.cn")
    def test_channel_cache_is_partitioned_by_enabled_channels(self, validate):
        client = WebSearch()
        self.addCleanup(client.session.close)
        client._fetch = Mock(return_value=(telegram_message("Anime_Test/1", f'目标作品<br>{SHARE}'), "https://t.me/s/Anime_Test"))
        options = {"channel_config": {"telegram_channels": ["Anime_Test"], "qq_channels": []}, "include_engines": False}
        self.assertTrue(client.search("目标作品", **options))
        self.assertTrue(client.search("目标作品", **options))
        self.assertEqual(client._fetch.call_count, 1)
        client._fetch.side_effect = SearchError("网页 HTTP 403")
        with self.assertRaises(SearchError):
            client.search("目标作品", channel_config={"telegram_channels": ["Other_Channel"], "qq_channels": []}, include_engines=False)


if __name__ == "__main__":
    unittest.main()
