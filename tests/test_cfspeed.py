"""Offline regression tests. Provider responses below are explicit test fixtures."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, Mock, MagicMock
from typing import Any
from urllib.error import HTTPError
from urllib.request import urlopen, Request
import ast
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime, timezone

from cfspeed.config import AppError, Config, Target, load_config
from cfspeed.network import HTTP, NoRedirect, parse_ips
from cfspeed.providers import Cloudflare, DNSPod, Record, plan_records, tc3_headers
from cfspeed.runtime import ProcessLock, Runner, State
from cfspeed.server import create_server

CF = Target('cf', 'cloudflare', 'cf.example.com', zone_id='a' * 32)
POD = Target('pod', 'dnspod', 'cf', domain='example.com')
IP = '104.18.41.25'
OLD = '172.64.153.10'
ENV = {'CF_API_TOKEN': 'fixture-token', 'DNSPOD_SECRET_ID': 'fixture-id', 'DNSPOD_SECRET_KEY': 'fixture-key'}


def cfraw(value=OLD, id='1' * 32):
    return {'id': id, 'name': CF.name, 'type': 'A', 'content': value, 'ttl': 600, 'proxied': False,
            'comment': 'preserve me', 'tags': ['owner:test'], 'settings': {}}


def podraw(value=OLD, detail=False, id=123):
    if detail:
        return {'Id': id, 'SubDomain': 'cf', 'RecordType': 'A', 'RecordLineId': '0',
                'RecordLine': '默认', 'Value': value, 'TTL': 600, 'Enabled': 1, 'Weight': None}
    return {'RecordId': id, 'Name': 'cf', 'Type': 'A', 'LineId': '0', 'Line': '默认',
            'Value': value, 'TTL': 600, 'Status': 'ENABLE', 'Weight': None}


class FakeHTTP:
    def __init__(self, responses=None, source=IP.encode()):
        self.responses = list(responses or [])
        self.source = source
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if isinstance(self.source, Exception):
            raise self.source
        return self.source

    def json(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class ConfigTests(unittest.TestCase):
    def load(self, source):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.toml'
            path.write_text(source)
            return load_config(path)

    def test_example_defaults(self):
        config = load_config('config.example.toml')
        self.assertTrue(config.dry_run)
        self.assertEqual(config.source_url, 'https://ip.164746.xyz/ipTop.html')
        self.assertEqual(config.targets, ())

    def test_bad_scalar_types_unknown_keys(self):
        for value in ['dry_run="false"', 'interval_seconds=true', 'port=0', 'interval_seconds=1', 'unknown=1']:
            with self.subTest(value=value), self.assertRaises(AppError):
                self.load('[service]\n' + value)

    def test_apply_requires_targets(self):
        with self.assertRaises(AppError):
            self.load('[service]\ndry_run=false')

    def test_source_must_use_https_no_credentials(self):
        for source in ['http://example.com', 'https://a:b@example.com', 'https://example.com/#key']:
            with self.subTest(source=source), self.assertRaises(AppError):
                self.load(f'[service]\nsource_url="{source}"')

    def test_id_must_not_be_normalized(self):
        for zone in ['A' * 32, 'a' * 31, ' ' + 'a' * 32]:
            with self.subTest(zone=zone), self.assertRaises(AppError):
                self.load(f'[[targets]]\nlabel="x"\nprovider="cloudflare"\nname="cf.example.com"\nzone_id="{zone}"')

    def test_missing_credentials(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(AppError):
            replace(Config(), targets=(CF,)).validate_credentials()

    def test_identifier_scalar_types_rejected_cleanly(self):
        for value in ('42', 'false', '[]'):
            with self.subTest(value=value), self.assertRaises(AppError):
                self.load(f'[[targets]]\nlabel="x"\nprovider="cloudflare"\nname="cf.example.com"\nzone_id={value}\n')
            with self.subTest(line_id=value), self.assertRaises(AppError):
                self.load(f'[[targets]]\nlabel="x"\nprovider="dnspod"\nname="cf"\ndomain="example.com"\nline_id={value}\n')
            with self.subTest(notification=value), self.assertRaises(AppError):
                self.load(f'[service]\npushplus_token_env={value}\n')

    def test_wrong_name_duplicate_scope(self):
        entry = f'[[targets]]\nlabel="x"\nprovider="cloudflare"\nname="cf.example.com"\nzone_id="{"a" * 32}"\n'
        self.assertEqual(len(self.load(entry).targets), 1)
        with self.assertRaises(AppError):
            self.load(entry + entry.replace('label="x"', 'label="y"'))
        with self.assertRaises(AppError):
            self.load(entry.replace('cf.example.com', 'CF.example.com'))


class SourceTests(unittest.TestCase):
    def test_order_dedup_whitespace_bom(self):
        self.assertEqual(parse_ips(('\ufeff' + IP + ', ' + OLD + '\n' + IP).encode()), [IP, OLD])

    def test_invalid_whole_payload_is_rejected(self):
        for bad in ['', '<html>Error</html>', '127.0.0.1', '10.0.0.1', '192.0.2.1', '224.0.0.1', '::1', '104.018.41.25', IP + ',bad', b'\xff']:
            with self.subTest(bad=bad), self.assertRaises(AppError):
                parse_ips(bad)

    def test_max_ips(self):
        with self.assertRaises(AppError):
            parse_ips(IP + ',' + OLD, 1)

    def test_redirects_disabled(self):
        self.assertIsNone(NoRedirect().redirect_request(Mock(), Mock(), 302, '', Mock(), 'https://other.invalid'))

    def test_retry_read_not_write_and_redact(self):
        http = HTTP(attempts=3)
        http.opener = Mock()
        http.opener.open.side_effect = TimeoutError('fixture-secret')
        with patch('cfspeed.network.time.sleep'), self.assertRaises(AppError) as caught:
            http.request('GET', 'https://example.com', retry=True)
        self.assertEqual(http.opener.open.call_count, 3)
        self.assertNotIn('fixture-secret', str(caught.exception))
        http.opener.open.reset_mock()
        with self.assertRaises(AppError):
            http.request('PATCH', 'https://example.com')
        self.assertEqual(http.opener.open.call_count, 1)

    def test_response_limit_and_json_shape(self):
        http = HTTP()
        response = Mock()
        response.status = 200
        response.read1.return_value = b'12345'
        http.opener = Mock()
        http.opener.open.return_value = MagicMock()
        http.opener.open.return_value.__enter__.return_value = response
        with self.assertRaises(AppError):
            http.request('GET', 'https://example.com', limit=4)
        with patch.object(http, 'request', return_value=b'[]'), self.assertRaises(AppError):
            http.json('GET', 'https://example.com')


class PlanTests(unittest.TestCase):
    def test_reorder_is_noop(self):
        records = [Record('2', 'x', OLD, 600), Record('1', 'x', IP, 600)]
        self.assertTrue(all(r.value == ip for r, ip in plan_records(records, [OLD, IP])))

    def test_assignment_deterministic(self):
        records = [Record('2', 'x', '8.8.8.8', 600), Record('1', 'x', OLD, 600)]
        plan = plan_records(records, [IP, OLD])
        self.assertEqual([(r.id, ip) for r, ip in plan], [('1', OLD), ('2', IP)])

    def test_no_records_empty_ips_duplicate_ids(self):
        r = Record('1', 'x', OLD, 600)
        for records, ips in [([], [IP]), ([r], []), ([r, r], [IP, OLD]), ([r, r], [IP])]:
            with self.subTest(records=records), self.assertRaises(AppError):
                plan_records(records, ips)


@patch.dict(os.environ, ENV)
class CloudflareTests(unittest.TestCase):
    def test_paginate_and_filter(self):
        http = FakeHTTP([{'success': True, 'result': [cfraw(id=str(n) * 32)],
                         'result_info': {'total_count': 2, 'total_pages': 2}} for n in (1, 2)])
        result = Cloudflare(CF, http).list_records()
        self.assertEqual(len(result), 2)
        self.assertIn('page=2', http.calls[-1][0][1])
        self.assertIn('name=cf.example.com', http.calls[0][0][1])

    def test_count_mismatch_rejected(self):
        http = FakeHTTP([{'success': True, 'result': [cfraw()], 'result_info': {'total_count': 2, 'total_pages': 1}}])
        with self.assertRaises(AppError):
            Cloudflare(CF, http).list_records()

    def test_api_error_not_success(self):
        http = FakeHTTP([{'success': False, 'errors': [{'code': 10000, 'message': 'fixture-secret'}]}])
        with self.assertRaises(AppError) as caught:
            Cloudflare(CF, http).call('GET')
        self.assertNotIn('fixture-secret', str(caught.exception))

    def test_preserves_options_and_reads_back(self):
        http = FakeHTTP([{'success': True, 'result': cfraw()}, {'success': True}, {'success': True, 'result': cfraw(IP)}])
        client = Cloudflare(CF, http)
        client.update(client.record(cfraw()), IP)
        self.assertEqual([x[0][0] for x in http.calls], ['GET', 'PATCH', 'GET'])
        payload = http.calls[1][1]['payload']
        self.assertEqual(payload['ttl'], 600)
        self.assertFalse(payload['proxied'])
        self.assertEqual(payload['comment'], 'preserve me')

    def test_readback_mismatch_fails(self):
        http = FakeHTTP([{'success': True, 'result': cfraw()}, {'success': True}, {'success': True, 'result': cfraw()}])
        client = Cloudflare(CF, http)
        with self.assertRaises(AppError):
            client.update(client.record(cfraw()), IP)

    def test_concurrent_change_no_write(self):
        http = FakeHTTP([{'success': True, 'result': cfraw('8.8.8.8')}])
        client = Cloudflare(CF, http)
        with self.assertRaises(AppError):
            client.update(client.record(cfraw()), IP)
        self.assertEqual(len(http.calls), 1)

    def test_write_timeout_still_reads_without_rewrite(self):
        for value, fails in [(IP, False), (OLD, True)]:
            http = FakeHTTP([{'success': True, 'result': cfraw()}, AppError('fixture timeout'),
                             {'success': True, 'result': cfraw(value)}])
            client = Cloudflare(CF, http)
            if fails:
                with self.assertRaises(AppError):
                    client.update(client.record(cfraw()), IP)
            else:
                self.assertIsInstance(client.update(client.record(cfraw()), IP), str)
            self.assertEqual([x[0][0] for x in http.calls], ['GET', 'PATCH', 'GET'])

    def test_write_and_read_both_fail(self):
        http = FakeHTTP([{'success': True, 'result': cfraw()}, AppError('fixture timeout'), AppError('fixture read failed')])
        client = Cloudflare(CF, http)
        with self.assertRaisesRegex(AppError, '实际结果未知'):
            client.update(client.record(cfraw()), IP)
        self.assertEqual(len(http.calls), 3)

    def test_out_of_scope_rejected(self):
        client = Cloudflare(CF, FakeHTTP())
        for delta in [{'name': 'other.example.com'}, {'type': 'AAAA'}, {'id': 'oops'}]:
            with self.subTest(delta=delta), self.assertRaises(AppError):
                client.record({**cfraw(), **delta})


@patch.dict(os.environ, ENV)
class DNSPodTests(unittest.TestCase):
    def test_list_detail_normalization(self):
        client = DNSPod(POD, FakeHTTP())
        self.assertEqual(client.record(podraw()), client.record(podraw(detail=True), detail=True))

    def test_pagination(self):
        http = FakeHTTP([{'Response': {'RecordList': [podraw(id=n)], 'RecordCountInfo': {'TotalCount': 2}}} for n in (123, 124)])
        records = DNSPod(POD, http).list_records()
        self.assertEqual(len(records), 2)
        self.assertEqual(json.loads(http.calls[1][1]['body'])['Offset'], 1)

    def test_api_business_error(self):
        client = DNSPod(POD, FakeHTTP([{'Response': {'Error': {'Code': 'AuthFailure', 'Message': 'fixture-secret'}}}]))
        with self.assertRaises(AppError) as caught:
            client.call('ModifyRecord', {})
        self.assertNotIn('fixture-secret', str(caught.exception))

    def test_modify_and_readback(self):
        http = FakeHTTP([{'Response': {'RecordInfo': podraw(detail=True)}}, {'Response': {'RecordId': 123}},
                         {'Response': {'RecordInfo': podraw(IP, detail=True)}}])
        client = DNSPod(POD, http)
        client.update(client.record(podraw()), IP)
        payload = json.loads(http.calls[1][1]['body'])
        self.assertEqual(payload['RecordLineId'], '0')
        self.assertEqual(payload['RecordLine'], '默认')
        self.assertEqual(payload['Status'], 'ENABLE')
        self.assertEqual(payload['TTL'], 600)
        self.assertEqual(http.calls[-1][1]['headers']['X-TC-Action'], 'DescribeRecord')

    def test_update_failure_propagates(self):
        http = FakeHTTP([{'Response': {'RecordInfo': podraw(detail=True)}}, {'Response': {'Error': {'Code': 'FailedOperation'}}}])
        client = DNSPod(POD, http)
        with self.assertRaises(AppError):
            client.update(client.record(podraw()), IP)

    def test_disabled_other_line_rejected(self):
        client = DNSPod(POD, FakeHTTP())
        for delta in [{'Status': 'DISABLE'}, {'LineId': '10=1'}, {'Type': 'CNAME'}]:
            with self.subTest(delta=delta), self.assertRaises(AppError):
                client.record({**podraw(), **delta})

    def test_write_response_errors_read_exact_record(self):
        for response in (AppError('fixture timeout'), {'Response': {'RecordId': 999}}):
            http = FakeHTTP([{'Response': {'RecordInfo': podraw(detail=True)}}, response,
                             {'Response': {'RecordInfo': podraw(IP, detail=True)}}])
            client = DNSPod(POD, http)
            self.assertIsInstance(client.update(client.record(podraw()), IP), str)
            self.assertEqual([x[1]['headers']['X-TC-Action'] for x in http.calls],
                             ['DescribeRecord', 'ModifyRecord', 'DescribeRecord'])

    def test_signer_matches_original_on_exact_body(self):
        # Fixed test vector computed from the legacy signer using dummy credentials.
        payload = {'Domain': 'example.com', 'SubDomain': 'cf', 'RecordLine': '默认'}
        body = json.dumps(payload).encode()
        expected = 'TC3-HMAC-SHA256 Credential=fixture-id/2020-09-13/dnspod/tc3_request, SignedHeaders=content-type;host;x-tc-action, Signature=23769bff9b1cf4f9af19227aaddd130613dfb8fe7ccf8c6744e13967653a478b'
        new = tc3_headers('fixture-id', 'fixture-key', 'ModifyRecord', body, timestamp=1600000000)
        self.assertEqual(new['Authorization'], expected)
        self.assertNotEqual(new['Authorization'], tc3_headers('fixture-id', 'fixture-key', 'ModifyRecord', body + b' ', timestamp=1600000000)['Authorization'])


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = replace(Config(), state_dir=self.tmp.name)
        self.state = State(self.tmp.name)

    def test_real_readonly_http_and_state(self):
        config = replace(self.config, port=0)
        runner = Runner(config, self.state, FakeHTTP())
        server = create_server(runner)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        try:
            with self.assertRaises(HTTPError) as error:
                urlopen(base + '/readyz')
            self.assertEqual(error.exception.code, 503)
            runner.run()
            self.assertEqual(urlopen(base + '/ipTop.html').read().decode().strip(), IP)
            self.assertTrue(json.load(urlopen(base + '/readyz'))['ready'])
            self.assertEqual(json.load(urlopen(base + '/api/status'))['runs'], 1)
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(base + '/run', data=b'', method='POST'))
            self.assertEqual(error.exception.code, 501)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_bad_source_retains_previous_and_never_calls_provider(self):
        factory = Mock()
        runner = Runner(self.config, self.state, FakeHTTP(), provider_factory=factory)
        self.assertEqual(runner.run()['status'], 'ok')
        previous = self.state.snapshot()['last_source_success']
        runner.http = FakeHTTP(source=b'<html>bad gateway</html>')
        self.assertEqual(runner.run()['status'], 'error')
        self.assertEqual(self.state.snapshot()['ips'], [IP])
        self.assertEqual(self.state.snapshot()['last_source_success'], previous)
        self.assertFalse(runner.readiness()[0])
        factory.assert_not_called()

    @patch.dict(os.environ, ENV)
    def test_preview_has_zero_writes_notifications(self):
        provider = Mock()
        provider.list_records.return_value = [Record('1', CF.name, OLD, 600)]
        runner = Runner(replace(self.config, targets=(CF,), pushplus_token_env='CF_API_TOKEN'), self.state, FakeHTTP(), lambda *_: provider)
        result = runner.run()
        self.assertEqual(result['planned'], 1)
        self.assertEqual(result['changed'], 0)
        self.assertEqual(result['notification'], 'disabled')
        provider.update.assert_not_called()

    @patch.dict(os.environ, ENV)
    def test_apply_success_and_partial_error(self):
        good, bad = Mock(), Mock()
        good.list_records.return_value = [Record('1', CF.name, OLD, 600)]
        bad.list_records.return_value = [Record('2', POD.name, OLD, 600)]
        bad.update.side_effect = AppError('fixture: simulated write rejection')
        config = replace(self.config, dry_run=False, targets=(CF, POD))
        runner = Runner(config, self.state, FakeHTTP(), lambda target, _: good if target == CF else bad)
        result = runner.run()
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['changed'], 1)
        self.assertEqual(result['targets'][0]['records'][0]['status'], 'verified')
        self.assertEqual(result['targets'][1]['records'][0]['status'], 'failed_or_unverified')
        self.assertIsNone(self.state.snapshot()['last_success'])

    def test_state_survives_restart_bounded_history_private_file(self):
        runner = Runner(self.config, self.state, FakeHTTP())
        for _ in range(32):
            runner.run()
        recovered = State(self.tmp.name).snapshot()
        self.assertEqual(recovered['ips'], [IP])
        self.assertEqual(len(recovered['history']), 30)
        self.assertEqual(recovered['status'], 'starting')
        self.assertEqual(self.state.path.stat().st_mode & 0o777, 0o600)

    def test_restart_cannot_reuse_previous_success_for_readiness(self):
        Runner(self.config, self.state, FakeHTTP()).run()
        recovered = State(self.tmp.name)
        runner = Runner(self.config, recovered, FakeHTTP())
        recovered.save(status='running')
        self.assertFalse(runner.readiness()[0])
        runner.run()
        self.assertTrue(runner.readiness()[0])

    @patch.dict(os.environ, ENV)
    def test_interrupted_write_preserved_in_history_after_restart(self):
        provider = Mock()
        provider.list_records.return_value = [Record('1', CF.name, OLD, 600)]
        provider.update.side_effect = KeyboardInterrupt()
        runner = Runner(replace(self.config, dry_run=False, targets=(CF,)), self.state,
                        FakeHTTP(), lambda *_: provider)
        with self.assertRaises(KeyboardInterrupt):
            runner.run()
        recovered = State(self.tmp.name)
        Runner(self.config, recovered, FakeHTTP()).run()
        interrupted = recovered.snapshot()['history'][0]
        self.assertEqual(interrupted['status'], 'interrupted')
        self.assertEqual(interrupted['targets'][0]['records'][0]['status'], 'interrupted_unverified')
        self.assertEqual(recovered.snapshot()['runs'], 2)
        self.assertIsNone(recovered.snapshot()['current_run'])

    def test_upgrade_does_not_inherit_stale_version(self):
        self.state.save(version='0.1.0', ips=[IP])
        snapshot = State(self.tmp.name).snapshot()
        self.assertEqual(snapshot['version'], '0.2.0')
        self.assertEqual(snapshot['ips'], [IP])

    def test_process_lock_and_reentry(self):
        with ProcessLock(self.tmp.name):
            with self.assertRaises(AppError):
                with ProcessLock(self.tmp.name):
                    pass
        runner = Runner(self.config, self.state, FakeHTTP())
        with runner.lock, self.assertRaises(AppError):
            runner.run()

    @patch.dict(os.environ, ENV)
    def test_pending_survives_failed_source_and_history_pruning_then_reconciles(self):
        provider = Mock()
        provider.list_records.return_value = [Record('1', CF.name, OLD, 600)]
        provider.update.side_effect = KeyboardInterrupt()
        runner = Runner(replace(self.config, dry_run=False, targets=(CF,)), self.state,
                        FakeHTTP(), lambda *_: provider)
        with self.assertRaises(KeyboardInterrupt):
            runner.run()
        recovered = State(self.tmp.name)
        failed = Runner(self.config, recovered, FakeHTTP(source=b'invalid'))
        for _ in range(31):
            failed.run()
        self.assertEqual(len(recovered.snapshot()['pending_operations']), 1)
        self.assertFalse(failed.readiness()[0])
        provider.list_records.return_value = [Record('1', CF.name, IP, 600)]
        preview = Runner(replace(self.config, targets=(CF,)), recovered, FakeHTTP(), lambda *_: provider)
        result = preview.run()
        self.assertEqual(recovered.snapshot()['pending_operations'], [])
        self.assertEqual(result['targets'][0]['reconciled'][0]['observed'], IP)
        self.assertTrue(preview.readiness()[0])
        self.assertEqual(provider.update.call_count, 1)

    def test_stale_readiness(self):
        runner = Runner(self.config, self.state, FakeHTTP())
        runner.run()
        with patch('cfspeed.runtime.time.time', return_value=time.time() + max(120, self.config.interval_seconds * 2) + 1):
            self.assertFalse(runner.readiness()[0])

    @patch.dict(os.environ, ENV)
    def test_notification_error_does_not_fake_dns_failure(self):
        provider = Mock()
        provider.list_records.return_value = [Record('1', CF.name, OLD, 600)]
        config = replace(self.config, dry_run=False, targets=(CF,), pushplus_token_env='CF_API_TOKEN')
        http = FakeHTTP([{'code': 500}])
        result = Runner(config, self.state, http, lambda *_: provider).run()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['notification'], 'error')
        self.assertEqual(result['changed'], 1)

    def test_scheduler_executes_twice_and_stops(self):
        runner = Runner(replace(self.config, interval_seconds=0.01), self.state, FakeHTTP())
        original = runner.run
        def run():
            result = original()
            if self.state.snapshot()['runs'] == 2:
                runner.stop.set()
            return result
        runner.run = run
        runner.loop()
        self.assertEqual(self.state.snapshot()['runs'], 2)

    def test_cli_error_nonzero(self):
        result = subprocess.run([sys.executable, '-m', 'cfspeed', 'run', '--config', '/does/not/exist'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
