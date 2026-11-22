import json
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
import requests

from app.sdk.common import SearchError
from app.sdk.index_catalog import IndexCatalog
from app.sdk.pansou import PanSou
from app.sdk.source_monitor import maintain_sources
from resource_search import search_resources, pansou_server, DEFAULT_PANSOU_SERVER, LEGACY_PANSOU_SERVER


class IndexCatalogTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = str(Path(folder.name) / 'health.sqlite')
        self.server = 'http://pansou:8888'
        self.catalog = IndexCatalog(self.path, self.server)
        self.client = Mock()
        self.client.plugins.return_value = ['pansearch', 'quarktv']
        self.client.search.return_value = []
        clock = patch('app.sdk.index_catalog.time.time', return_value=10000)
        self.clock = clock.start()
        self.addCleanup(clock.stop)

    def test_new_plugins_are_discovered_added_and_removed_without_config_edits(self):
        self.catalog.discover(self.client)
        self.assertEqual(set(self.catalog.select([])), {'pansearch', 'quarktv'})
        self.assertEqual({row['state'] for row in self.catalog.status()['plugins']}, {'pending'})
        self.catalog.discover(self.client)
        self.client.plugins.assert_called_once()
        self.clock.return_value += 3601
        self.client.plugins.return_value = ['pansearch', 'new_site']
        self.catalog.discover(self.client)
        self.assertEqual(set(self.catalog.select([])), {'pansearch', 'new_site'})
        self.assertEqual(self.catalog.select(['pansearch']), ['pansearch'])
        with self.assertRaisesRegex(SearchError, '未在 PanSou'):
            self.catalog.select(['quarktv'])

    def test_empty_result_is_not_failure_and_timeout_is_rechecked_then_recovers(self):
        self.catalog.discover(self.client)
        self.client.search.side_effect = [[], SearchError('网络请求失败 (ReadTimeout)')]
        self.catalog.probe(self.client)
        states = {row['name']: row['state'] for row in self.catalog.status()['plugins']}
        self.assertEqual(states, {'pansearch': 'empty', 'quarktv': 'cooldown'})
        self.assertEqual(self.catalog.select([]), ['pansearch'])
        self.client.search.reset_mock()
        self.catalog.probe(self.client)
        self.client.search.assert_not_called()
        self.clock.return_value += 301
        self.client.search.side_effect = None
        self.client.search.return_value = [{'shareurl': 'https://pan.quark.cn/s/test'}]
        self.catalog.probe(self.client)
        self.client.search.assert_called_once()
        self.assertEqual(set(self.catalog.select([])), {'pansearch', 'quarktv'})
        self.assertEqual(self.catalog.status()['plugins'][1]['state'], 'active')

    def test_all_disabled_does_not_send_empty_selection_which_would_query_everything(self):
        self.catalog.discover(self.client)
        for name in ['pansearch', 'quarktv']:
            self.catalog.record(name, reason='需要人工验证')
        with self.assertRaisesRegex(SearchError, '暂无|没有已启用'):
            self.catalog.select([])

    def test_rate_limit_respects_server_retry_after_and_stops_remaining_probes(self):
        self.catalog.discover(self.client)
        response = Mock(headers={'Retry-After': '7200'}, status_code=429)
        error = SearchError('HTTP 429')
        error.__cause__ = requests.HTTPError(response=response)
        self.client.search.side_effect = error
        self.catalog.probe(self.client)
        self.client.search.assert_called_once()
        self.clock.return_value += 1000
        self.catalog.probe(self.client)
        self.client.search.assert_called_once()

    def test_older_success_does_not_override_new_failure(self):
        self.catalog.discover(self.client)
        started = self.clock.return_value
        self.clock.return_value += 1
        self.catalog.record('pansearch', reason='需要人工验证')
        self.catalog.record('pansearch', hits=5, started=started)
        self.assertEqual(self.catalog.status()['plugins'][0]['state'], 'cooldown')

    def test_failed_inventory_preserves_previous_adapters_and_does_not_leak_response(self):
        self.catalog.discover(self.client)
        self.clock.return_value += 3601
        self.client.plugins.side_effect = SearchError('private-token')
        self.catalog.discover(self.client)
        self.assertEqual(set(self.catalog.select([])), {'pansearch', 'quarktv'})
        self.assertNotIn('private-token', str(self.catalog.status()))

    def test_search_uses_catalog_excludes_cooldown_and_records_actual_hits(self):
        self.catalog.discover(self.client)
        self.catalog.record('quarktv', reason='需要人工验证')
        self.client.search.return_value = [{'shareurl': 'https://pan.quark.cn/s/test',
                                            'channel': 'plugin:pansearch', 'taskname': '作品'}]
        config = {'source': {'pansou': {'enable': True, 'server': self.server, 'auto_discover': True}}}
        with patch('resource_search.PanSou', return_value=self.client), patch.dict('os.environ', {'PANSOU_FALLBACK_SERVERS': ''}):
            result = search_resources('作品', config, include_web=False, health_path=self.path)
        self.assertTrue(result['success'])
        self.assertEqual(self.client.search.call_args.kwargs['plugins'], ['pansearch'])
        self.assertEqual(self.catalog.status()['plugins'][0]['hits'], 1)

    def test_disabled_monitor_does_not_discover_and_custom_selection_is_preserved(self):
        config = {'source': {'monitor': {'enable': False}, 'pansou': {'server': self.server, 'auto_discover': True}}}
        with patch('app.sdk.pansou.PanSou') as constructor:
            maintain_sources(config, self.path)
        constructor.assert_not_called()
        config['source']['monitor']['enable'] = True
        config['source']['pansou']['plugins'] = ['quarktv']
        with patch('app.sdk.pansou.PanSou', return_value=self.client), patch.dict('os.environ', {'PANSOU_FALLBACK_SERVERS': ''}):
            maintain_sources(config, self.path)
        self.assertEqual(self.client.search.call_args.kwargs['plugins'], ['quarktv'])

    def test_server_resolution_preserves_custom_addresses_and_credentials(self):
        with patch.dict('os.environ', {'PANSOU_SERVER': 'http://local:8888'}):
            self.assertEqual(pansou_server({}), 'http://local:8888')
            self.assertEqual(pansou_server({'server': LEGACY_PANSOU_SERVER}), 'http://local:8888')
            self.assertEqual(pansou_server({'server': LEGACY_PANSOU_SERVER, 'token': 'secret'}), LEGACY_PANSOU_SERVER)
            self.assertEqual(pansou_server({'server': 'https://custom.example'}), 'https://custom.example')

    def test_health_is_a_bounded_directory_not_proof_of_search_success(self):
        client = PanSou(self.server + '/api/search')
        self.addCleanup(client.session.close)
        response = MagicMock()
        response.__enter__.return_value = response
        client.session.get = Mock(return_value=response)
        for payload in ({'status': 'ok', 'plugins': ['pansearch', 'new_site']},
                        {'status': 'ok', 'plugins': ['https://untrusted.example/code.py']},
                        {'status': 'ok', 'plugin_count': 5}, {'status': 'ok', 'plugins': ['x'] * 129}):
            response.iter_content.return_value = [json.dumps(payload).encode()]
            if payload.get('plugins') == ['pansearch', 'new_site']:
                self.assertEqual(client.plugins(), ['pansearch', 'new_site'])
            else:
                with self.assertRaises(SearchError):
                    client.plugins()
        self.assertEqual(client.session.get.call_args.args[0], self.server + '/api/health')
        response.iter_content.return_value = [b'x' * (512 * 1024 + 1)]
        with self.assertRaises(SearchError):
            client.plugins()

    def test_incomplete_cloudsaver_is_explicit_and_never_sends_credentials(self):
        with patch('resource_search.CloudSaver') as client:
            result = search_resources('作品', {'source': {'pansou': {'enable': False},
                'cloudsaver': {'enable': True, 'server': '', 'password': 'private-secret'}}}, include_web=False)
        client.assert_not_called()
        self.assertIn('CloudSaver 未配置', result['message'])
        self.assertNotIn('private-secret', str(result))

    def test_readonly_probe_reports_counts_without_untrusted_channel_text(self):
        from scripts import probe_index_search
        self.client.search.return_value = [{'channel': 'plugin:pansearch'}, {'channel': 'private-secret'}]
        output = io.StringIO()
        with patch.object(probe_index_search, 'PanSou', return_value=self.client), \
                patch('sys.argv', ['probe_index_search.py', '作品']), contextlib.redirect_stdout(output):
            self.assertEqual(probe_index_search.main(), 0)
        self.assertNotIn('private-secret', output.getvalue())
        self.assertEqual(json.loads(output.getvalue().splitlines()[1])['candidates'], 2)
        self.client.session.close.assert_called_once()

    def test_readonly_probe_does_not_treat_empty_directory_as_working(self):
        from scripts import probe_index_search
        self.client.plugins.return_value = []
        with patch.object(probe_index_search, 'PanSou', return_value=self.client), \
                patch('sys.argv', ['probe_index_search.py', '作品']), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(probe_index_search.main(), 2)
        self.client.search.assert_not_called()

    def test_readonly_probe_waits_once_for_cold_index_without_refresh_loop(self):
        from scripts import probe_index_search
        self.client.search.side_effect = [[], [{'channel': 'plugin:pansearch'}]]
        with patch.object(probe_index_search, 'PanSou', return_value=self.client), \
                patch.object(probe_index_search.time, 'sleep') as wait, \
                patch('sys.argv', ['probe_index_search.py', '作品']), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(probe_index_search.main(), 0)
        wait.assert_called_once_with(12)
        self.assertEqual([call.kwargs['refresh'] for call in self.client.search.call_args_list], [True, False])


if __name__ == '__main__':
    unittest.main()
