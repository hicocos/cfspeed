"""Carrier sources preserve upstream first-entry order and fail closed."""
import json
import unittest
from dataclasses import replace
from unittest.mock import Mock

from cfspeed.config import AppError, Config, V2TOO_SOURCE_URL
from cfspeed.network import fetch_ips


class CarrierSourceTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(source_url=V2TOO_SOURCE_URL)
        self.rows = [[{'ip': ip, 'carrier': carrier}, {'ip': '9.9.9.9', 'speed': 999}]
                     for carrier, ip in zip(('ct', 'cm', 'cu'), ('1.1.1.1', '8.8.8.8', '208.67.222.222'))]

    def fetch(self, rows=None, config=None):
        http = Mock()
        http.request.side_effect = [json.dumps(row).encode() for row in (rows or self.rows)]
        result = fetch_ips(config or self.config, http)
        return result, http

    def test_first_entry_from_each_exact_endpoint(self):
        result, http = self.fetch()
        self.assertEqual(result, ['1.1.1.1', '8.8.8.8', '208.67.222.222'])
        from urllib.parse import urlsplit, parse_qs
        urls = []
        for carrier, call in zip(('ct', 'cm', 'cu'), http.request.call_args_list):
            self.assertEqual(call.args[0], 'GET')
            parts = urlsplit(call.args[1])
            self.assertEqual(f'{parts.scheme}://{parts.netloc}{parts.path}', V2TOO_SOURCE_URL)
            query = parse_qs(parts.query)
            self.assertEqual(set(query), {'carrier', '_cfspeed_nonce'})
            self.assertEqual(query['carrier'], [carrier])
            self.assertRegex(query['_cfspeed_nonce'][0], r'^[0-9a-f]{32}$')
            urls.append(call.args[1])
            self.assertEqual(call.kwargs, {'limit': 65536, 'retry': True, 'headers': {
                'Cache-Control': 'no-cache, no-store, max-age=0', 'Pragma': 'no-cache'}})
        _, second = self.fetch()
        self.assertTrue(set(urls).isdisjoint(call.args[1] for call in second.request.call_args_list))

    def test_duplicate_first_ips_are_deduplicated_without_replacement(self):
        self.rows[1][0]['ip'] = '1.1.1.1'
        self.assertEqual(self.fetch()[0], ['1.1.1.1', '208.67.222.222'])

    def test_invalid_first_entry_never_falls_back(self):
        for bad in ([], {}, [None], [{}], [{'ip': None}], [{'ip': 123}],
                    [{'ip': '10.0.0.1'}, {'ip': '1.1.1.1'}],
                    [{'ip': '1.1.1.1,8.8.8.8'}], [{'ip': '::1'}],
                    [{'ip': '1.1.1.1', 'carrier': 'cu'}]):
            with self.subTest(bad=bad), self.assertRaisesRegex(AppError, 'cm'):
                self.fetch([self.rows[0], bad, self.rows[2]])

    def test_http_and_json_errors_fail_whole_fetch(self):
        for bad in (AppError('timeout'), b'not json', b'\xff'):
            http = Mock()
            http.request.side_effect = [json.dumps(self.rows[0]).encode(), bad]
            with self.subTest(bad=bad), self.assertRaisesRegex(AppError, 'cm'):
                fetch_ips(self.config, http)

    def test_max_ips_still_enforced(self):
        with self.assertRaises(AppError):
            self.fetch(config=replace(self.config, max_ips=2))

    def test_original_source_unchanged(self):
        http = Mock()
        http.request.return_value = b'1.1.1.1,8.8.8.8'
        self.assertEqual(fetch_ips(Config(), http), ['1.1.1.1', '8.8.8.8'])
        self.assertEqual(http.request.call_count, 1)
