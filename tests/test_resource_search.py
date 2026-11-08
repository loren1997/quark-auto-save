import unittest
from unittest.mock import Mock, patch

import requests

from app.sdk.cloudsaver import CloudSaver
from app.sdk.common import SearchError, iso_to_cst, normalize_share_url
from app.sdk.pansou import PanSou
from resource_search import DEFAULT_PANSOU_SERVER, search_resources


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


class SharedSearchTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
