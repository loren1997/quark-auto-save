from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from email.utils import formatdate
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import requests

from app.sdk.common import SearchError
from app.sdk.source_health import SourceHealth
from app.sdk.websearch import WebSearch, _cache


def response(body="<html></html>", status=200, headers=None):
    result = Mock(status_code=status, headers={"Content-Type": "text/html; charset=utf-8", **(headers or {})}, encoding="utf-8")
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.raw.read1.side_effect = [body.encode(), b""]
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(response=result)
    return result


class SourceHealthTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / "health.sqlite3")
        self.health = SourceHealth(self.path)
        _cache.clear()

    def client(self):
        client = WebSearch(health_path=self.path)
        self.addCleanup(client.session.close)
        return client

    def test_persists_across_worker_process_without_query_or_credentials(self):
        self.health.record("https://example.com/post?secret=never-store", "需要人工验证")
        program = "from app.sdk.source_health import SourceHealth; import sys; SourceHealth(sys.argv[1]).check('https://example.com/another')"
        result = subprocess.run([sys.executable, "-c", program, self.path], capture_output=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"SearchError", result.stderr)
        self.assertNotIn(b"never-store", Path(self.path).read_bytes())
        self.health.check("https://unrelated.example.com/")

    def test_retry_after_seconds_and_http_date_expire_without_refresh(self):
        with patch("app.sdk.source_health.time.time", return_value=10000):
            self.health.record("https://seconds.example/", "HTTP 429", "90")
            self.health.record("https://date.example/", "HTTP 429", formatdate(10120, usegmt=True))
            with self.assertRaisesRegex(SearchError, "90 秒"):
                SourceHealth(self.path).check("https://seconds.example/alias")
            with self.assertRaisesRegex(SearchError, "120 秒"):
                self.health.check("https://date.example/")
        with patch("app.sdk.source_health.time.time", return_value=10121):
            self.health.check("https://seconds.example/")
            self.health.check("https://date.example/")

    @patch("app.sdk.source_health.time.time", return_value=10000)
    def test_concurrent_updates_are_bounded_and_shorter_retry_does_not_shorten_block(self, clock):
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda index: SourceHealth(self.path).record(f"https://site{index}.example/", "HTTP 429"), range(130)))
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM cooldowns").fetchone()[0], 128)
        self.health.record("https://source.example/", "HTTP 429", "600")
        self.health.record("https://source.example/", "HTTP 429", "10")
        with self.assertRaisesRegex(SearchError, "600 秒"):
            self.health.check("https://source.example/")

    def test_corrupt_store_reports_warning_and_does_not_stop_sources(self):
        Path(self.path).write_text("bad sqlite", encoding="utf-8")
        with self.assertLogs(level="WARNING") as logs:
            self.health.check("https://example.com/")
            self.health.record("https://example.com/", "HTTP 403")
        self.assertEqual(len(logs.output), 2)

    def test_malformed_rows_are_reported_without_values_and_removed_for_recovery(self):
        self.health.record("https://example.com/", "HTTP 403")
        for reason, until in (("HTTP 403", "private-invalid-value"), ("HTTP 403", float("inf")),
                              ("HTTP 403", float("-inf")), ("HTTP 403", "nan"),
                              ("private-invalid-reason", time.time() + 100)):
            with self.subTest(reason=reason, until=until):
                with closing(sqlite3.connect(self.path)) as connection, connection:
                    connection.execute("INSERT OR REPLACE INTO cooldowns VALUES (?, ?, ?)", ("example.com", reason, until))
                with self.assertLogs(level="WARNING") as logs:
                    self.health.check("https://example.com/")
                self.assertNotIn("private-invalid", " ".join(logs.output))
                self.health.record("https://example.com/", "HTTP 429")
                with self.assertRaisesRegex(SearchError, "HTTP 429"):
                    self.health.check("https://example.com/")

    @patch("app.sdk.websearch._validate_url", return_value="example.com")
    def test_http_429_suppresses_next_worker_request_and_preserves_reason(self, validate):
        client = self.client()
        client.session.get = Mock(return_value=response(status=429, headers={"Retry-After": "120"}))
        with self.assertRaisesRegex(SearchError, "HTTP 429"):
            client._fetch("https://limited.example/", time.monotonic() + 10)
        another = self.client()
        another.session.get = Mock()
        with self.assertRaisesRegex(SearchError, "来源冷却中.*HTTP 429"):
            another._fetch("https://limited.example/different-query", time.monotonic() + 10)
        another.session.get.assert_not_called()

    @patch("app.sdk.websearch._validate_url", return_value="example.com")
    def test_redirect_failure_cools_only_actual_target(self, validate):
        client = self.client()
        client.session.get = Mock(side_effect=[
            response(status=302, headers={"Location": "https://target.example/post"}),
            response(status=403),
        ])
        with self.assertRaisesRegex(SearchError, "HTTP 403"):
            client._fetch("https://search.example/redirect", time.monotonic() + 10)
        SourceHealth(self.path).check("https://search.example/other-result")
        with self.assertRaisesRegex(SearchError, "HTTP 403"):
            SourceHealth(self.path).check("https://target.example/post")

    @patch("app.sdk.websearch._validate_url", return_value="example.com")
    def test_captcha_cools_google_but_other_engines_continue_on_next_query(self, validate):
        def get(url, **kwargs):
            host = urlsplit(url).hostname
            bodies = {"meijusou.com": "0 条结果", "html.duckduckgo.com": '<div class="no-results">No results found</div>',
                      "cn.bing.com": "<rss><channel/></rss>", "www.google.com": '<form id="captcha-form"></form>',
                      "www.sogou.com": "没有找到相关结果"}
            return response(bodies[host])
        client = self.client()
        client.session.get = Mock(side_effect=get)
        self.assertEqual(client.search("作品"), [])
        self.assertNotIn("作品", _cache)
        another = self.client()
        another.session.get = Mock(side_effect=get)
        self.assertEqual(another.search("其他作品"), [])
        called_hosts = [urlsplit(call.args[0]).hostname for call in another.session.get.call_args_list]
        self.assertNotIn("www.google.com", called_hosts)
        self.assertIn("cn.bing.com", called_hosts)
        self.assertTrue(any("Google: 来源冷却中" in error for error in another.errors))
        self.assertNotIn("其他作品", _cache)

    @patch("app.sdk.websearch._validate_url", return_value="example.com")
    def test_blocked_landing_domain_does_not_disable_its_search_engine_or_snippet(self, validate):
        share = "https://pan.quark.cn/s/abc123"
        self.health.record("https://blocked.example/other", "HTTP 403")
        def get(url, **kwargs):
            host = urlsplit(url).hostname
            bodies = {"meijusou.com": "0 条结果", "html.duckduckgo.com": '<div class="no-results">No results found</div>',
                      "cn.bing.com": "<rss><channel/></rss>", "www.sogou.com": "没有找到相关结果",
                      "www.google.com": f'<div class="g"><a href="https://blocked.example/post"><h3>作品 网盘</h3></a><p>{share}</p></div>'}
            return response(bodies[host])
        client = self.client()
        client.session.get = Mock(side_effect=get)
        rows = client.search("作品")
        self.assertEqual(rows[0]["shareurl"], share)
        self.assertTrue(any("Google 落地页: 来源冷却中" in error for error in client.errors))
        client.search("作品 网盘")
        called_hosts = [urlsplit(call.args[0]).hostname for call in client.session.get.call_args_list]
        self.assertEqual(called_hosts.count("www.google.com"), 2)
        self.assertNotIn("blocked.example", called_hosts)


if __name__ == "__main__":
    unittest.main()
