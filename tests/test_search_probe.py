import contextlib
import io
import json
import unittest
from unittest.mock import patch

from app.sdk.common import SearchError
from scripts import probe_web_search


class SearchProbeTests(unittest.TestCase):
    @patch("scripts.probe_web_search.Quark")
    @patch("scripts.probe_web_search.WebSearch")
    def test_diagnostic_passes_explicit_alias_and_year_to_content_verification(self, web, quark):
        web.return_value.search.return_value = [{"page_url": "https://example.com/post"}]
        web.return_value.errors = []
        web.return_value.session.proxies = {}
        quark.return_value._find_verified_resource.return_value = {"success": True, "error": ""}
        output = io.StringIO()
        with patch("sys.argv", ["probe_web_search.py", "作品", "--title", "作品 (2026)",
                                "--alias", "可信别名", "--alias", ""]), contextlib.redirect_stdout(output):
            self.assertEqual(probe_web_search.main(), 0)
        args = quark.return_value._find_verified_resource.call_args.args
        self.assertEqual(args[0]["taskname"], "作品 (2026)")
        self.assertEqual(args[3], ["可信别名"])
        self.assertTrue(json.loads(output.getvalue().splitlines()[-1])["verified"])

    @patch("scripts.probe_web_search.Quark")
    @patch("scripts.probe_web_search.WebSearch")
    def test_engine_outage_returns_diagnostics_without_verifying_or_printing_proxy_credentials(self, web, quark):
        client = web.return_value
        client.errors = ["Bing: 网页域名解析失败", "DuckDuckGo: 网页请求失败 (ProxyError)"]
        client.search.side_effect = SearchError("搜索失败")
        client.session.proxies = {"https": "http://user:SECRET@proxy.example:7890"}
        output = io.StringIO()
        with patch("sys.argv", ["probe_web_search.py", "测试作品"]), contextlib.redirect_stdout(output):
            self.assertEqual(probe_web_search.main(), 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result["candidates"], 0)
        self.assertEqual(result["errors"], client.errors)
        self.assertTrue(result["proxy_configured"])
        self.assertNotIn("SECRET", output.getvalue())
        quark.assert_not_called()
        client.session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
