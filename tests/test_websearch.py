import socket
import os
import time
import unittest
from unittest.mock import Mock, patch

import requests
from urllib3.exceptions import ReadTimeoutError

from app.sdk.common import SearchError
from app.sdk.websearch import (MAX_BYTES, WebSearch, _Page, _HTTPSConnection,
                               _cache, _page_links, _public_addresses, _search_results, _validate_url)


URL = "https://pan.quark.cn/s/abc123"
GOOGLE_EMPTY = '<div>Your search did not match any documents.</div>'


def response(body=b"<html></html>", status=200, headers=None):
    result = Mock(status_code=status, headers={"Content-Type": "text/html; charset=utf-8", **(headers or {})}, encoding="utf-8")
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.iter_content.return_value = [body]
    result.raw.read1.side_effect = [body, b""]
    return result


class WebSearchTests(unittest.TestCase):
    def setUp(self):
        _cache.clear()
        self.client = WebSearch()
        self.addCleanup(self.client.session.close)

    def test_parse_real_search_engine_shapes(self):
        html = '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fshow&amp;rut=x">作品 <b>第三季</b></a>'
        rows = _search_results("DuckDuckGo", html, "https://html.duckduckgo.com/html/")
        self.assertEqual(rows, [{"title": "作品 第三季", "url": "https://example.com/show", "snippet": ""}])
        rss = '<rss><channel><item><title>作品</title><link>https://example.com/show</link><description>资源</description></item></channel></rss>'
        self.assertEqual(_search_results("Bing", rss, "https://bing.com")[0]["title"], "作品")

    def test_duck_snippets_stay_with_their_matching_result(self):
        html = f'''
            <a class="result__snippet" href="https://example.com/a">孤立摘要 {URL}</a>
            <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=heading">作品 A</a>
            <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=snippet"><b>作品 A</b> {URL} 提取码 AA11</a>
            <a class="result__a" href="https://example.com/b">作品 B</a>
            <a class="result__snippet" href="https://example.com/a">其他条目 提取码 XX99</a>
            <a class="result__snippet" href="https://example.com/b">作品 B 提取码 BB22</a>
        '''
        rows = _search_results("DuckDuckGo", html, "https://html.duckduckgo.com/html/")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["snippet"], f"作品 A {URL} 提取码 AA11")
        self.assertEqual(rows[1]["snippet"], "作品 B 提取码 BB22")

    def test_challenges_and_changed_pages_are_errors_not_empty(self):
        for engine, body in (("DuckDuckGo", '<form id="challenge-form"></form>'),
                             ("DuckDuckGo", "<html>unexpected</html>"),
                             ("Google", '<form id="captcha-form"></form>'),
                             ("Google", 'Before you continue to Google'),
                             ("Google", '<noscript>Please enable JavaScript</noscript>'),
                             ("Bing", "<html>captcha</html>"), ("Bing", "<root/>")):
            with self.subTest(engine=engine, body=body), self.assertRaises(SearchError):
                _search_results(engine, body, "https://example.com")
        self.assertEqual(_search_results("DuckDuckGo", '<div class="no-results">No results found</div>', "https://example.com"), [])
        self.assertEqual(_search_results("Bing", "<rss><channel/></rss>", "https://example.com"), [])
        self.assertEqual(_search_results("Google", GOOGLE_EMPTY, "https://www.google.com/search"), [])

    def test_google_result_headings_and_snippets_stay_in_their_own_blocks(self):
        html = f'''
            <a href="https://unrelated.example"><h2>导航</h2></a>
            <div class="MjjYud"><div><a href="/url?q=https%3A%2F%2Fexample.com%2Fa&amp;sa=U">站点<h3>作品 <b>第三季</b></h3></a></div>
                <div>资源 {URL} 提取码 AA11</div></div>
            <div class="g"><a href="https://example.com/b"><h3>另一部作品</h3></a>
                <div>https://pan.quark.cn/s/other 提取码 BB22</div></div>
            <a href="/search?q=other"><h3>Google 内部链接</h3></a>
        '''
        rows = _search_results("Google", html, "https://www.google.com/search")
        self.assertEqual([row["url"] for row in rows], ["https://example.com/a", "https://example.com/b"])
        self.assertEqual(rows[0]["title"], "作品 第三季")
        self.assertIn(URL, rows[0]["snippet"])
        self.assertNotIn("BB22", rows[0]["snippet"])
        self.assertNotIn("AA11", rows[1]["snippet"])

    def test_google_container_with_multiple_results_does_not_share_snippets(self):
        html = f'''<div class="MjjYud">
            <a href="https://example.com/a"><h3>作品 A</h3></a><p>{URL}</p>
            <a href="https://example.com/b"><h3>作品 B</h3></a><p>提取码 BB22</p></div>'''
        rows = _search_results("Google", html, "https://www.google.com/search")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["snippet"] == "" for row in rows))

    @patch("app.sdk.websearch.socket.getaddrinfo")
    def test_nonpublic_addresses_and_credentials_are_blocked(self, resolve):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "100.64.0.1", "224.0.0.1", "ff02::1", "2002:7f00:1::", "64:ff9b::7f00:1"):
            resolve.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))]
            with self.subTest(address=address), self.assertRaises(SearchError):
                _validate_url("http://example.com/path")
        for url in ("file:///etc/passwd", "http://user:password@example.com", "http://example.com:5005", "http://[bad"):
            with self.subTest(url=url), self.assertRaises(SearchError):
                _validate_url(url)
        resolve.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
                                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with self.assertRaises(SearchError):
            _public_addresses("example.com", 443)

    @patch("app.sdk.websearch.create_connection")
    @patch("app.sdk.websearch._public_addresses", return_value=["1.1.1.1"])
    def test_connection_uses_validated_ip_and_preserves_tls_hostname(self, addresses, connect):
        connection = _HTTPSConnection("example.com", port=443, timeout=3)
        connection._new_conn()
        self.assertEqual(connect.call_args.args[0], ("1.1.1.1", 443))
        self.assertEqual(connection.host, "example.com")
        self.assertFalse(self.client.session.trust_env)

    @patch("app.sdk.websearch._validate_url")
    def test_redirects_are_revalidated_and_never_forward_credentials(self, validate):
        validate.side_effect = ["example.com", SearchError("非公网")]
        self.client.session.get = Mock(return_value=response(status=302, headers={"Location": "http://127.0.0.1/secret"}))
        with self.assertRaises(SearchError):
            self.client._fetch("https://example.com", time.monotonic() + 5)
        self.assertEqual(self.client.session.get.call_count, 1)
        self.assertEqual(validate.call_args.args[0], "http://127.0.0.1/secret")
        kwargs = self.client.session.get.call_args.kwargs
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["headers"], {"Cookie": "", "Authorization": None})

    @patch("app.sdk.websearch._validate_url", return_value="example.com")
    def test_response_size_and_redirect_limits(self, validate):
        self.client.session.get = Mock(return_value=response(b"a" * (MAX_BYTES + 1)))
        with self.assertRaisesRegex(SearchError, "上限"):
            self.client._fetch("https://example.com", time.monotonic() + 5)
        self.client.session.get = Mock(return_value=response(status=302, headers={"Location": "/next"}))
        with self.assertRaisesRegex(SearchError, "重定向"):
            self.client._fetch("https://example.com", time.monotonic() + 5)
        self.assertEqual(self.client.session.get.call_count, 3)

    @patch("app.sdk.websearch._validate_url", return_value="example.com")
    def test_raw_transport_errors_are_reported_as_search_errors(self, validate):
        result = response()
        result.raw.read1.side_effect = ReadTimeoutError(None, "/", "timed out")
        self.client.session.get = Mock(return_value=result)
        with self.assertRaisesRegex(SearchError, "ReadTimeoutError"):
            self.client._fetch("https://example.com", time.monotonic() + 5)

    @patch("app.sdk.websearch._validate_url", return_value="example.com")
    def test_aggregates_landing_pages_keeps_real_titles_and_caches(self, validate):
        duck = ''.join(f'<a class="result__a" href="https://example.com/{number}">作品 第三季 页面{number}</a>' for number in range(7))
        bing = "<rss><channel><item><title>无关网页</title><link>https://other.example/ignore</link></item></channel></rss>"
        def fetch(url, deadline):
            if "duckduckgo.com" in url:
                return duck, url
            if "bing.com" in url:
                return bing, url
            if "google.com" in url:
                return GOOGLE_EMPTY, url
            number = url.rsplit("/", 1)[1]
            return f'<title>真实作品页面 {number}</title><a href="{URL}">下载</a> 提取码: ab12', url
        self.client._fetch = Mock(side_effect=fetch)
        results = self.client.search("作品 第3季")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["shareurl"], URL + "?pwd=ab12")
        self.assertEqual(results[0]["taskname"], "真实作品页面 0")
        self.assertEqual(results[0]["page_url"], "https://example.com/0")
        self.assertEqual(results[0]["channel"], "DuckDuckGo · example.com")
        self.assertEqual(self.client._fetch.call_count, 7)
        results[0]["taskname"] = "changed"
        cached = self.client.search("作品 第3季")
        self.assertEqual(cached[0]["taskname"], "真实作品页面 0")
        self.assertEqual(self.client._fetch.call_count, 7)

    @patch("app.sdk.websearch._validate_url", return_value="example.com")
    def test_one_engine_failure_keeps_other_results_and_all_failures_raise(self, validate):
        def fetch(url, deadline):
            if "duckduckgo.com" in url:
                raise SearchError("需要验证")
            if "google.com" in url:
                return GOOGLE_EMPTY, url
            return f'<rss><channel><item><title>作品</title><link>{URL}</link></item></channel></rss>', url
        self.client._fetch = Mock(side_effect=fetch)
        self.assertEqual(self.client.search("作品")[0]["shareurl"], URL)
        self.assertIn("DuckDuckGo", self.client.errors[0])
        self.client._fetch.side_effect = SearchError("offline")
        with self.assertRaisesRegex(SearchError, "offline"):
            self.client.search("作品", refresh=True)

    @patch("app.sdk.websearch._validate_url", return_value="example.com")
    def test_landing_page_403_keeps_snippet_share_and_other_page_results(self, validate):
        other = "https://pan.quark.cn/s/other"
        duck = f'''
            <a class="result__a" href="https://example.com/blocked">作品 A</a>
            <a class="result__snippet" href="https://example.com/blocked">{URL} 提取码 AA11</a>
            <a class="result__a" href="https://example.com/working">作品 B</a>
            <a class="result__snippet" href="https://example.com/working">作品 B 提取码 BB22</a>
        '''
        def fetch(url, deadline):
            if "duckduckgo.com" in url:
                return duck, url
            if "bing.com" in url:
                return "<rss><channel/></rss>", url
            if "google.com" in url:
                return GOOGLE_EMPTY, url
            if url.endswith("blocked"):
                raise SearchError("网页 HTTP 403")
            return f'<title>作品 B</title><p><a href="{other}">下载</a> 提取码 BB22</p>', url
        self.client._fetch = Mock(side_effect=fetch)
        rows = self.client.search("作品")
        self.assertEqual({row["shareurl"] for row in rows}, {URL + "?pwd=AA11", other + "?pwd=BB22"})
        self.assertEqual(rows[0]["taskname"], "作品 A")
        self.assertIn("DuckDuckGo 落地页: 网页 HTTP 403", self.client.errors)

    @patch("app.sdk.websearch._validate_url", return_value="example.com")
    def test_failed_empty_search_is_retried_without_refresh(self, validate):
        def fetch(url, deadline):
            if "duckduckgo.com" in url:
                return '<a class="result__a" href="https://example.com/blocked">作品</a>', url
            if "bing.com" in url:
                return "<rss><channel/></rss>", url
            if "google.com" in url:
                return GOOGLE_EMPTY, url
            raise SearchError("网页 HTTP 403")
        self.client._fetch = Mock(side_effect=fetch)
        self.assertEqual(self.client.search("作品"), [])
        self.assertNotIn("作品", _cache)
        self.assertEqual(self.client.search("作品"), [])
        self.assertEqual(self.client._fetch.call_count, 8)

    def test_successful_empty_search_is_cached(self):
        def fetch(url, deadline):
            if "google.com" in url:
                return GOOGLE_EMPTY, url
            return ('<div class="no-results">No results found</div>' if "duckduckgo.com" in url
                    else "<rss><channel/></rss>"), url
        self.client._fetch = Mock(side_effect=fetch)
        self.assertEqual(self.client.search("作品"), [])
        self.assertEqual(self.client.search("作品"), [])
        self.assertEqual(self.client._fetch.call_count, 3)

    @patch("app.sdk.websearch._validate_url", return_value="example.com")
    def test_google_still_checks_landing_page_after_other_engine_uses_slots(self, validate):
        def fetch(url, deadline):
            if "duckduckgo.com" in url:
                return ''.join(f'<a class="result__a" href="https://example.com/empty{n}">作品 夸克</a>' for n in range(6)), url
            if "bing.com" in url:
                raise SearchError("offline")
            if "google.com" in url:
                return '<div class="g"><a href="https://example.com/found"><h3>作品 合集</h3></a></div>', url
            if url.endswith("found"):
                return f'<title>作品 合集</title><p>{URL}</p>', url
            return '<p>没有分享链接</p>', url
        self.client._fetch = Mock(side_effect=fetch)
        rows = self.client.search("作品")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["shareurl"], URL)
        self.assertEqual(rows[0]["channel"], "Google · example.com")
        self.assertEqual(self.client._fetch.call_count, 7)

    @patch("app.sdk.websearch._validate_url", return_value="example.com")
    def test_google_snippet_survives_blocked_landing_page_and_failed_other_engines(self, validate):
        def fetch(url, deadline):
            if "google.com" in url:
                return f'<div class="g"><a href="https://example.com/blocked"><h3>作品</h3></a><p>{URL} 提取码 AA11</p></div>', url
            raise SearchError("网页 HTTP 403")
        self.client._fetch = Mock(side_effect=fetch)
        rows = self.client.search("作品")
        self.assertEqual(rows[0]["shareurl"], URL + "?pwd=AA11")
        self.assertIn("Google 落地页: 网页 HTTP 403", self.client.errors)

    def test_visible_text_and_anchor_links_do_not_execute_or_parse_scripts(self):
        page = _Page(f'<title>作品</title><script>"https://pan.quark.cn/s/evil"</script><a href="{URL}">下载</a> 提取码: ab12')
        self.assertNotIn("/evil", "".join(page.text))
        self.assertIn(URL, "".join(page.text))

    def test_explicit_proxy_environment_is_supported_without_netrc(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:7890", "HTTP_PROXY": "http://127.0.0.1:7890"}):
            client = WebSearch()
            self.addCleanup(client.session.close)
            self.assertEqual(client.session.proxies["https"], "http://127.0.0.1:7890")
            self.assertFalse(client.session.trust_env)

    def test_passwords_do_not_cross_html_blocks(self):
        other = "https://pan.quark.cn/s/other"
        page = _Page(f'<p>A<a href="{URL}">下载A</a></p><p>B 提取码: BB22 <a href="{other}">下载B</a></p>')
        self.assertEqual(_page_links("".join(page.text)), [URL, other + "?pwd=BB22"])
        page = _Page(f'<p><a href="{URL}">下载</a><br>提取码: AA11</p>')
        self.assertEqual(_page_links("".join(page.text)), [URL + "?pwd=AA11"])
        page = _Page(f'<table><tr><td><a href="{URL}">下载A</a></td></tr><tr><td>提取码: BB22</td><td><a href="{other}">下载B</a></td></tr></table>')
        self.assertEqual(_page_links("".join(page.text)), [URL, other + "?pwd=BB22"])


if __name__ == "__main__":
    unittest.main()
