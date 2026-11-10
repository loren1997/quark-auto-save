from pathlib import Path
import os
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from app.sdk.common import SearchError
from app.sdk.source_health import SourceHealth
from app.sdk.source_monitor import maintain_sources, record_web_failures
from resource_search import DEFAULT_PANSOU_SERVER, search_resources


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

    def test_configured_telegram_recovers_when_auto_discovery_is_off(self):
        self.expire('https://t.me/s/manual_movies')
        config = {'source': {'channelsearch': {'enable': True, 'auto_discover': False,
                  'telegram_channels': ['manual_movies'], 'qq_channels': []}}}
        body = ('<div class="tgme_widget_message" data-post="manual_movies/1">'
                '<div class="tgme_widget_message_text">电视剧 目标作品 S01E02 '
                'https://pan.quark.cn/s/abc123</div></div>')
        with patch('app.sdk.websearch.WebSearch._fetch',
                   side_effect=lambda url, deadline: (body, url)) as fetch:
            result = maintain_sources(config, self.path)
        self.assertEqual(result, {'checked': 1, 'recovered': ['t.me']})
        self.assertTrue(fetch.call_args.args[0].startswith('https://t.me/s/manual_movies?q='))
        self.assertEqual(self.health.status(), [])
        self.expire('https://t.me/s/manual_movies')
        config['source']['channelsearch']['enable'] = False
        with patch('app.sdk.websearch.WebSearch._fetch') as fetch:
            self.assertEqual(maintain_sources(config, self.path)['checked'], 0)
        fetch.assert_not_called()

    def test_blank_pansou_server_uses_same_default_as_search_not_environment(self):
        self.expire(DEFAULT_PANSOU_SERVER)
        config = {'source': {'pansou': {'server': '', 'token': 'main-token'}}}
        with patch.dict(os.environ, {'PANSOU_SERVER': 'https://environment.example',
                                    'PANSOU_FALLBACK_SERVERS': ''}), \
                patch('app.sdk.pansou.PanSou') as monitor_client, \
                patch('resource_search.PanSou') as search_client:
            monitor_client.return_value.search.return_value = []
            search_client.return_value.search.return_value = []
            result = maintain_sources(config, self.path)
            search_resources('电视剧', config, include_web=False)
        self.assertEqual(result['checked'], 1)
        monitor_client.assert_called_once_with(DEFAULT_PANSOU_SERVER, token='main-token')
        self.assertEqual(monitor_client.call_args_list, search_client.call_args_list)

    def test_pansou_duplicates_are_removed_before_three_server_limit_like_search(self):
        first, second, third, fourth = [f'https://{name}.example' for name in ('first', 'second', 'third', 'fourth')]
        for fallback in ([first + '/', second], first + '/\n' + second):
            with self.subTest(fallback=fallback):
                for url in (first, second, third, fourth):
                    self.expire(url)
                config = {'source': {'pansou': {'server': first, 'token': 'main-token',
                                               'fallback_servers': fallback}}}
                with patch.dict(os.environ, {'PANSOU_FALLBACK_SERVERS': '\n'.join((second, third, fourth))}), \
                        patch('app.sdk.pansou.PanSou') as monitor_client, \
                        patch('resource_search.PanSou') as search_client:
                    monitor_client.return_value.search.return_value = []
                    search_client.return_value.search.return_value = []
                    self.assertEqual(maintain_sources(config, self.path)['checked'], 2)
                    self.assertEqual(maintain_sources(config, self.path)['checked'], 1)
                    search_resources('电视剧', config, include_web=False)
                self.assertCountEqual(monitor_client.call_args_list, search_client.call_args_list)
                self.assertEqual({call.args[0] for call in monitor_client.call_args_list}, {first, second, third})
                tokens = {call.args[0]: call.kwargs['token'] for call in monitor_client.call_args_list}
                self.assertEqual(tokens, {first: 'main-token', second: '', third: ''})
                self.assertEqual([row['host'] for row in self.health.status()], ['fourth.example'])

    def test_unregistered_telegram_transport_failure_cools_host(self):
        for reason in ('请求超时', '网络连接失败'):
            with self.subTest(reason=reason):
                with self.health._connect() as db:
                    db.execute('DELETE FROM cooldowns')
                record_web_failures([{'source': '频道索引 Telegram @manual_movies', 'message': reason}], self.path)
                row = self.health.status()[0]
                self.assertEqual((row['host'], row['reason'], row['status']), ('t.me', reason, 'cooldown'))
                with self.assertRaises(SearchError):
                    self.health.check('https://t.me/s/another_channel?q=test')
                with self.health._connect() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM discovered_channels').fetchone()[0], 0)

    def test_telegram_transport_and_http_feedback_preserve_existing_retry_after(self):
        self.health.record('https://t.me/s/manual_movies', 'HTTP 429', '7200')
        before = self.health.status()
        for reason in ('请求超时', '网络连接失败', 'HTTP 429', 'HTTP 403'):
            record_web_failures([{'source': '频道索引 Telegram @manual_movies', 'message': reason}], self.path)
            self.assertEqual(self.health.status(), before)

    def test_telegram_channel_content_failures_do_not_cool_the_whole_host(self):
        for reason in ('HTTP 404', 'HTTP 410', '未发现影视夸克链接', '响应格式异常',
                       '频道未公开', 'HTTP 403', 'HTTP 429'):
            with self.subTest(reason=reason):
                record_web_failures([{'source': '频道索引 Telegram @manual_movies', 'message': reason}], self.path)
                self.assertEqual(self.health.status(), [])


if __name__ == '__main__':
    unittest.main()
