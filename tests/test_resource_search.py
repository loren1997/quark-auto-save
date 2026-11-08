import unittest
import os
import threading
import time
from functools import partial
from unittest.mock import Mock, patch

import requests

from app.sdk.cloudsaver import CloudSaver
from app.sdk.common import SearchError, extract_share_urls, iso_to_cst, normalize_share_url
from app.sdk.pansou import PanSou
from resource_search import DEFAULT_PANSOU_SERVER, search_resources as aggregate_search_resources, search_web_resources

# Existing adapter cases isolate the index providers; web aggregation is tested below.
search_resources = partial(aggregate_search_resources, include_web=False)


URL = "https://pan.quark.cn/s/abc123"


def entry(url=URL, title="测试作品", date=""):
    return {"shareurl": url, "taskname": title, "datetime": date}


class SearchAdapterTests(unittest.TestCase):
    def pansou(self, payload):
        client = PanSou("https://search.example/")
        client.session = Mock()
        client.session.post.return_value.json.return_value = payload
        return client

    def test_native_and_proxy_api_shapes(self):
        data = {"merged_by_type": {"quark": [{"url": URL, "note": "作品 简介：内容", "password": "aB12", "datetime": "2026-01-01T00:00:00Z"}]}}
        for payload in (data, {"code": 0, "data": data}):
            with self.subTest(payload=payload):
                client = self.pansou(payload)
                result = client.search("作品", True)
                self.assertEqual(result[0]["shareurl"], URL + "?pwd=aB12")
                self.assertEqual(result[0]["taskname"], "作品")
                self.assertEqual(result[0]["datetime"], "2026-01-01 08:00:00")
                call = client.session.post.call_args
                self.assertEqual(call.args[0], "https://search.example/api/search")
                self.assertIs(call.kwargs["json"]["refresh"], True)
                self.assertEqual(call.kwargs["timeout"], (5, 25))

    def test_empty_results_are_successful(self):
        self.assertEqual(self.pansou({"merged_by_type": {}}).search("不存在"), [])
        self.assertEqual(self.pansou({"total": 0}).search("不存在"), [])

    def test_all_channels_and_raw_text_are_requested_and_parsed(self):
        payload = {"results": [{"title": "作品", "content": f"{URL} 提取码: ab12\nhttps://pan.quark.cn/s/def456 密码: cd34", "channel": "tg:anime"}]}
        client = self.pansou(payload)
        result = client.search("作品", channels=["anime"], plugins=["labi"])
        self.assertEqual([item["shareurl"] for item in result], [URL + "?pwd=ab12", "https://pan.quark.cn/s/def456?pwd=cd34"])
        self.assertEqual(result[0]["channel"], "tg:anime")
        params = client.session.post.call_args.kwargs["json"]
        self.assertEqual((params["src"], params["res"], params["channels"], params["plugins"]), ("all", "all", ["anime"], ["labi"]))

    def test_raw_links_keep_per_work_title(self):
        client = self.pansou({"results": [{"title": "本周作品合集", "links": [{"type": "quark", "url": URL, "password": "ab12", "work_title": "作品第二季"}]}]})
        result = client.search("作品")
        self.assertEqual(result[0]["taskname"], "作品第二季")
        self.assertEqual(result[0]["shareurl"], URL + "?pwd=ab12")

    def test_raw_telegram_noise_is_filtered_before_sniffing(self):
        client = self.pansou({"results": [{"title": "学生会也有洞！ 生徒会にも穴はある (2026)", "content": URL}, {"title": "家庭教育学习书籍", "content": "https://pan.quark.cn/s/wrong"}]})
        self.assertEqual([item["shareurl"] for item in client.search("学生会也有洞 (2026)")], [URL])

    def test_password_from_another_link_is_not_assigned_to_structured_url(self):
        other = "https://pan.quark.cn/s/other"
        text = f"作品 {other} 提取码: ab12"
        pansou = self.pansou({"merged_by_type": {"quark": [{"url": URL, "note": text}]}})
        cloudsaver = CloudSaver("https://search.example")
        for results in (pansou.search("作品"), cloudsaver.clean_search_results([{"list": [{"title": "作品", "content": text, "cloudLinks": [{"cloudType": "quark", "link": URL}]}]}])):
            with self.subTest(results=results):
                self.assertEqual([item["shareurl"] for item in results], [URL, other + "?pwd=ab12"])

    def test_unscoped_password_is_not_assigned_to_multiple_structured_links(self):
        other = "https://pan.quark.cn/s/other"
        pansou = self.pansou({"results": [{"title": "作品", "content": "提取码: ab12", "links": [{"type": "quark", "url": URL}, {"type": "quark", "url": other}]}]})
        cloudsaver = CloudSaver("https://search.example")
        for results in (pansou.search("作品"), cloudsaver.clean_search_results([{"list": [{"content": "提取码: ab12", "cloudLinks": [{"cloudType": "quark", "link": URL}, {"cloudType": "quark", "link": other}]}]}])):
            self.assertEqual([item["shareurl"] for item in results], [URL, other])

    def test_text_sniffer_handles_html_emoji_and_separate_passwords(self):
        text = f'<a href="{URL}?pwd=ab12&amp;utm=x">作品</a>\nhttps://pan.quark.cn/s/def456📁 提取码: cd34\nhttps://evil.test/s/nope'
        self.assertEqual(extract_share_urls(text), [URL + "?pwd=ab12", "https://pan.quark.cn/s/def456?pwd=cd34"])
        self.assertEqual(extract_share_urls("https://pan.quark.cn.evil.test/s/abc123"), [])
        self.assertEqual(extract_share_urls(f"{URL}\nhttps://other.example/download 密码: ab12"), [URL])

    def test_failures_do_not_become_empty_results(self):
        for payload in ({"code": 401, "message": "登录失效"}, {"error": "未授权"}, {"hello": "world"}, [], {"merged_by_type": {"quark": {}}}):
            with self.subTest(payload=payload), self.assertRaises(SearchError):
                self.pansou(payload).search("作品")

    def test_network_timeout_is_explicit(self):
        client = self.pansou({})
        client.session.post.side_effect = requests.Timeout("secret URL")
        with self.assertRaisesRegex(SearchError, "Timeout") as error:
            client.search("作品")
        self.assertNotIn("secret", str(error.exception))

    def test_bad_date_does_not_drop_valid_links(self):
        client = self.pansou({"merged_by_type": {"quark": [{"url": URL, "datetime": "unknown", "note": None}]}})
        self.assertEqual(client.search("作品")[0]["datetime"], "")
        self.assertEqual(iso_to_cst("2026-01-01T00:00:00"), "2026-01-01 08:00:00")

    def test_normalize_quark_link_and_password(self):
        self.assertEqual(normalize_share_url(f"链接：{URL} 提取码：bA12"), URL + "?pwd=bA12")
        self.assertEqual(normalize_share_url(URL + "?utm_source=x&amp;pwd=ab12", "zz99"), URL + "?pwd=ab12")
        self.assertEqual(normalize_share_url(URL, context="密码: 1234"), URL + "?pwd=1234")
        self.assertEqual(normalize_share_url(URL + "#/list/share/" + "a" * 32, "abcd"), URL + "?pwd=abcd#/list/share/" + "a" * 32)

    def test_reject_other_hosts_and_invalid_urls(self):
        for url in ("javascript:alert(1)", "https://pan.quark.cn.evil.test/s/abc", "https://evil.test/s/abc", "https://user@pan.quark.cn/s/abc", "https://pan.quark.cn/not-share", None):
            with self.subTest(url=url):
                self.assertEqual(normalize_share_url(url), "")

    def test_cloudsaver_refreshes_expired_login(self):
        client = CloudSaver("https://search.example/")
        client.search = Mock(side_effect=[{"success": False, "auth_error": True}, {"success": True, "data": []}])
        client.login = Mock(return_value={"success": True, "token": "new"})
        result = client.auto_login_search("作品")
        self.assertTrue(result["success"])
        self.assertEqual(result["new_token"], "new")
        self.assertEqual(client.search.call_count, 2)

    def test_cloudsaver_normalizes_password(self):
        data = [{"list": [{"title": "名称：作品", "content": "提取码：4321", "pubDate": "bad date", "cloudLinks": [{"cloudType": "quark", "link": URL}]}]}]
        result = CloudSaver("https://search.example").clean_search_results(data)
        self.assertEqual(result[0]["shareurl"], URL + "?pwd=4321")
        self.assertEqual(result[0]["taskname"], "作品")

    def test_cloudsaver_skips_null_channel_and_link_lists(self):
        data = [{"list": None}, {"list": [{"cloudLinks": None}]}]
        self.assertEqual(CloudSaver("https://search.example").clean_search_results(data), [])

    def test_cloudsaver_sniffs_text_without_structured_links(self):
        data = [{"list": [{"title": "作品", "content": f"{URL} 提取码: ab12\nhttps://pan.quark.cn/s/def456 提取码: cd34", "cloudLinks": None, "channelId": "anime"}]}]
        result = CloudSaver("https://search.example").clean_search_results(data)
        self.assertEqual([item["shareurl"] for item in result], [URL + "?pwd=ab12", "https://pan.quark.cn/s/def456?pwd=cd34"])
        self.assertEqual(result[0]["channel"], "anime")

    def test_malformed_optional_link_lists_do_not_discard_text(self):
        self.assertEqual(self.pansou({"results": [{"title": "作品", "content": URL, "links": 5}]}).search("作品")[0]["shareurl"], URL)
        self.assertEqual(CloudSaver("https://search.example").clean_search_results([{"list": [{"content": URL, "cloudLinks": 5}]}])[0]["shareurl"], URL)


class SharedSearchTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"PANSOU_FALLBACK_SERVERS": ""})
        env.start()
        self.addCleanup(env.stop)

    @patch("resource_search.PanSou")
    def test_default_source_works_without_configuration(self, pansou):
        pansou.return_value.search.return_value = [entry()]
        result = search_resources("作品", {})
        self.assertTrue(result["success"])
        self.assertEqual(result["data"][0]["shareurl"], URL)
        pansou.assert_called_once_with(DEFAULT_PANSOU_SERVER, token="")
        pansou.return_value.session.close.assert_called_once()

    @patch("resource_search.PanSou")
    def test_fallback_retains_primary_error_without_forwarding_token(self, pansou):
        pansou.return_value.search.side_effect = [SearchError("offline"), [entry()]]
        result = search_resources("作品", {"source": {"pansou": {"server": "https://primary.example", "token": "private", "fallback_servers": "https://backup.example"}}})
        self.assertTrue(result["success"])
        self.assertEqual(len(result["data"]), 1)
        self.assertEqual(result["errors"][0]["message"], "offline")
        self.assertEqual(pansou.call_args_list[1].kwargs["token"], "")

    @patch("resource_search.PanSou")
    def test_empty_primary_also_uses_configured_fallback(self, pansou):
        pansou.return_value.search.side_effect = [[], [entry()]]
        result = search_resources("作品", {"source": {"pansou": {"fallback_servers": ["http://pansou:8888"]}}})
        self.assertTrue(result["data"])

    @patch("resource_search.CloudSaver")
    @patch("resource_search.PanSou")
    def test_nonempty_sources_are_aggregated_and_merge_provenance(self, pansou, cloudsaver):
        clients = {url: Mock() for url in ("https://primary.example", "https://backup.example")}
        clients["https://primary.example"].search.return_value = [{**entry(), "channel": "tg:first"}]
        clients["https://backup.example"].search.return_value = [{**entry(URL + "?pwd=1234"), "channel": "plugin:labi"}, entry("https://pan.quark.cn/s/extra")]
        pansou.side_effect = lambda server, **kwargs: clients[server]
        cloudsaver.return_value.auto_login_search.return_value = {"success": True, "data": []}
        cloudsaver.return_value.clean_search_results.return_value = [entry("https://pan.quark.cn/s/cloudsaver")]
        result = search_resources("作品", {"source": {"pansou": {"server": "https://primary.example", "fallback_servers": ["https://backup.example"]}, "cloudsaver": {"server": "https://cloudsaver.example"}}})
        self.assertEqual(len(result["data"]), 3)
        merged = result["data"][0]
        self.assertEqual(merged["shareurl"], URL + "?pwd=1234")
        self.assertEqual(merged["channel"], "tg:first / plugin:labi")
        self.assertIn("PanSou 聚合 1", merged["source"])

    @patch("resource_search.PanSou")
    def test_environment_sources_are_deduplicated_bounded_and_disabled_together(self, pansou):
        pansou.return_value.search.return_value = []
        with patch.dict(os.environ, {"PANSOU_FALLBACK_SERVERS": DEFAULT_PANSOU_SERVER + "\nhttps://second.example\nhttps://third.example\nhttps://fourth.example"}):
            result = search_resources("作品", {})
            self.assertEqual(pansou.call_count, 3)
            self.assertIn("前三个", result["message"])
            pansou.reset_mock()
            search_resources("作品", {"source": {"pansou": {"enable": False}}})
            pansou.assert_not_called()

    @patch("resource_search.PanSou")
    def test_selection_is_validated_and_passed_to_every_server(self, pansou):
        pansou.return_value.search.return_value = []
        search_resources("作品", {"source": {"pansou": {"channels": "@anime, guoman\n@anime", "plugins": "labi zhizhen"}}})
        self.assertEqual(pansou.return_value.search.call_args.kwargs["channels"], ["anime", "guoman"])
        self.assertEqual(pansou.return_value.search.call_args.kwargs["plugins"], ["labi", "zhizhen"])
        pansou.reset_mock()
        result = search_resources("作品", {"source": {"pansou": {"plugins": "https://not-a-plugin.test"}}})
        self.assertFalse(result["success"])
        pansou.assert_not_called()

    @patch("resource_search.PanSou")
    def test_provider_queries_run_concurrently_with_at_most_two_workers(self, pansou):
        lock, gate = threading.Lock(), threading.Event()
        active, maximum = 0, 0
        def run(*args, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                if active == 2:
                    gate.set()
            self.assertTrue(gate.wait(2), "search sources were run serially")
            time.sleep(0.02)
            with lock:
                active -= 1
            return []
        pansou.return_value.search.side_effect = run
        search_resources("作品", {"source": {"pansou": {"fallback_servers": ["https://second.example", "https://third.example"]}}})
        self.assertEqual(maximum, 2)

    @patch("resource_search.PanSou")
    def test_all_failed_is_not_reported_as_zero_results(self, pansou):
        pansou.return_value.search.side_effect = SearchError("offline")
        result = search_resources("作品", {})
        self.assertFalse(result["success"])
        self.assertIn("offline", result["message"])

    @patch("resource_search.PanSou")
    def test_all_disabled_does_not_contact_public_source(self, pansou):
        result = search_resources("作品", {"source": {"pansou": {"enable": False}}})
        self.assertFalse(result["success"])
        pansou.assert_not_called()

    @patch("resource_search.PanSou")
    def test_dedup_prefers_password_even_if_older(self, pansou):
        pansou.return_value.search.return_value = [entry(date="2026-11-08"), entry(URL + "?pwd=1234", date="2026-11-01")]
        result = search_resources("作品", {})
        self.assertEqual(len(result["data"]), 1)
        self.assertEqual(result["data"][0]["shareurl"], URL + "?pwd=1234")

    @patch("resource_search.PanSou")
    def test_invalid_input_and_server_are_rejected(self, pansou):
        self.assertFalse(search_resources("a" * 201, {})["success"])
        self.assertTrue(search_resources(" ", {})["success"])
        result = search_resources("作品", {"source": {"pansou": {"server": "file:///secret"}}})
        self.assertFalse(result["success"])
        pansou.assert_not_called()


class WebAggregationTests(unittest.TestCase):
    @patch("resource_search.WebSearch")
    def test_web_search_defaults_on_and_preserves_partial_engine_failure(self, web):
        web.return_value.search.return_value = [{**entry(), "source": "DuckDuckGo", "page_url": "https://public.example/post"}]
        web.return_value.errors = ["Bing: 连接超时"]
        result = aggregate_search_resources("作品", {"source": {"pansou": {"enable": False}}})
        self.assertTrue(result["success"])
        self.assertEqual(result["data"][0]["source"], "DuckDuckGo")
        self.assertEqual(result["data"][0]["page_url"], "https://public.example/post")
        self.assertIn("Bing", result["message"])
        web.return_value.session.close.assert_called_once()

    @patch("resource_search.WebSearch")
    def test_disabled_web_never_contacts_engines(self, web):
        result = search_web_resources("作品", {"source": {"websearch": {"enable": False}}})
        self.assertEqual(result["data"], [])
        web.assert_not_called()

    @patch("resource_search.WebSearch")
    def test_web_failure_remains_a_source_error(self, web):
        web.return_value.search.side_effect = SearchError("搜索引擎要求验证码")
        result = search_web_resources("作品", {})
        self.assertFalse(result["success"])
        self.assertIn("验证码", result["errors"][0]["message"])
        web.return_value.session.close.assert_called_once()

    @patch("resource_search.WebSearch")
    @patch("resource_search.PanSou")
    def test_web_and_index_duplicates_keep_both_sources_and_password(self, pansou, web):
        pansou.return_value.search.return_value = [entry()]
        web.return_value.search.return_value = [{**entry(URL + "?pwd=1234"), "source": "DuckDuckGo"}]
        web.return_value.errors = []
        result = aggregate_search_resources("作品", {})
        self.assertEqual(len(result["data"]), 1)
        self.assertEqual(result["data"][0]["shareurl"], URL + "?pwd=1234")
        self.assertIn("PanSou", result["data"][0]["source"])
        self.assertIn("DuckDuckGo", result["data"][0]["source"])


if __name__ == "__main__":
    unittest.main()
