from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from app.sdk.common import SearchError
from app.sdk.source_health import SourceHealth
from app.sdk.source_monitor import maintain_sources, record_web_failures


class SourceMonitorTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = str(Path(self.folder.name) / 'sources.sqlite')
        self.health = SourceHealth(self.path)
        self.registry = patch('app.sdk.channel_registry.ChannelRegistry.maintain', return_value={}).start()
        self.addCleanup(patch.stopall)

    def expire(self, url='https://www.google.com/', reason='需要人工验证'):
        with patch('app.sdk.source_health.time.time', return_value=time.time() - 4000):
            self.health.record(url, reason)

    def test_expired_failure_is_retained_until_query_probe_recovers(self):
        self.expire()
        self.health.record('https://other.example/', '请求超时')
        self.assertEqual(len(self.health.status()), 2)
        with patch('app.sdk.websearch.WebSearch._fetch', return_value=('<html></html>', 'https://www.google.com/search')) as fetch, \
                patch('app.sdk.websearch._search_results', return_value=[]) as parse:
            result = maintain_sources({}, self.path)
        self.assertEqual(result, {'checked': 1, 'recovered': ['www.google.com']})
        fetch.assert_called_once()
        parse.assert_called_once()
        self.assertEqual([row['host'] for row in self.health.status()], ['other.example'])

    def test_cooldown_and_disabled_monitor_do_not_probe(self):
        self.health.record('https://www.google.com/', '需要人工验证')
        with patch('app.sdk.websearch.WebSearch._fetch') as fetch:
            self.assertEqual(maintain_sources({}, self.path)['checked'], 0)
            self.expire()
            self.assertEqual(maintain_sources({'source': {'monitor': {'enable': False}}}, self.path)['checked'], 0)
        fetch.assert_not_called()

    def test_challenge_recheck_keeps_source_disabled(self):
        self.expire()
        with patch('app.sdk.websearch.WebSearch._fetch', return_value=('challenge', 'https://www.google.com/search')), \
                patch('app.sdk.websearch._search_results', side_effect=SearchError('需要人工验证')):
            result = maintain_sources({}, self.path)
        self.assertEqual(result['recovered'], [])
        self.assertEqual(self.health.status()[0]['status'], 'cooldown')

    def test_concurrent_new_failure_is_not_cleared_by_old_probe(self):
        self.expire()
        def parse(*_):
            self.health.record('https://www.google.com/', 'HTTP 429')
            return []
        with patch('app.sdk.websearch.WebSearch._fetch', return_value=('', 'https://www.google.com/search')), \
                patch('app.sdk.websearch._search_results', side_effect=parse):
            result = maintain_sources({}, self.path)
        self.assertEqual(result['recovered'], [])
        self.assertEqual(self.health.status()[0]['reason'], 'HTTP 429')

    def test_failed_landing_page_does_not_disable_entire_search_source(self):
        record_web_failures([{'source': '网页搜索 PansoVIP 落地页', 'message': 'HTTP 404'}], self.path)
        self.assertEqual(self.health.status(), [])
        record_web_failures([{'source': '网页搜索 PansoVIP', 'message': 'HTTP 404'}], self.path)
        self.assertEqual(self.health.status()[0]['host'], 'www.panso.vip')

    def test_retry_after_and_dynamic_channel_failure_are_preserved(self):
        self.health.record('https://www.google.com/', 'HTTP 429', '30')
        until = self.health.status()[0]['next_check']
        with patch('app.sdk.channel_registry.ChannelRegistry.record_failure') as failure:
            record_web_failures([{'source': '网页搜索 Google', 'message': 'HTTP 429'},
                                 {'source': '频道索引 Telegram @movie_test', 'message': '请求超时'}], self.path)
        self.assertEqual(self.health.status()[0]['next_check'], until)
        failure.assert_called_once_with('movie_test', '请求超时')

    def test_monitor_checks_at_most_two_due_sources(self):
        for url in ('https://www.google.com/', 'https://meijusou.com/', 'https://www.panso.vip/'):
            self.expire(url)
        with patch('app.sdk.websearch.WebSearch._fetch', return_value=('', 'https://meijusou.com/search')), \
                patch('app.sdk.websearch._search_results', return_value=[]):
            result = maintain_sources({}, self.path)
        self.assertEqual(result['checked'], 2)
        self.assertEqual(len(self.health.status()), 1)


if __name__ == '__main__':
    unittest.main()
