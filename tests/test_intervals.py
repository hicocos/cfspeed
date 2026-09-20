"""Independent schedules; all I/O is temporary state or explicit offline fixtures."""
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import tempfile
import threading
import time
import unittest

from cfspeed.admin import Admin, load_saved_config
from cfspeed.config import AppError, BusyError, Config, Target, parse_config
from cfspeed.runtime import Runner, State
from cfspeed.providers import Record


class Source:
    def __init__(self):
        self.calls = 0
        self.fail = False

    def request(self, method, url, **kwargs):
        assert method == 'GET' and url.startswith('https://source.example/')
        self.calls += 1
        if self.fail:
            raise AppError('fixture source failure')
        return b'1.1.1.1'

    def json(self, *args, **kwargs):
        raise AssertionError('Network forbidden')


class Provider:
    def __init__(self):
        self.calls = 0
        self.writes = []
        self.ip = '8.8.8.8'

    def list_records(self):
        self.calls += 1
        return [Record('fixture-id', 'cdn.example.com', self.ip, 600)]

    def update(self, record, ip):
        self.writes.append(ip)
        self.ip = ip


class IntervalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = Config(state_dir=temp.name, source_url='https://source.example/ips',
                             interval_seconds=7200, source_interval_seconds=3600, dry_run=False,
                             targets=(Target('fixture', 'cloudflare', 'cdn.example.com', zone_id='a'*32),),
                             credential_values={'CF_API_TOKEN': 'fixture-only'})
        self.source, self.provider = Source(), Provider()
        self.runner = Runner(self.config, State(temp.name), self.source, lambda *_: self.provider)

    def dns(self):
        self.runner.next_due = 0
        self.runner.schedule_context.active = True
        try:
            return self.runner.run()
        finally:
            self.runner.schedule_context.active = False

    def test_legacy_toml_migration_and_validation(self):
        config = parse_config({'service': {'interval_seconds': 43200}})
        self.assertEqual(config.source_interval_seconds, 43200)
        self.assertEqual(Config(interval_seconds=1234).source_interval_seconds, 1234)
        for bad in (None, True, 29, 604801, 30.5):
            with self.subTest(bad=bad), self.assertRaises(AppError):
                parse_config({'service': {'source_interval_seconds': bad}})

    def test_source_only_and_dns_cache_and_independent_deadlines(self):
        original_dns = self.runner.next_due
        result = self.runner.refresh_source()
        self.assertEqual(result['kind'], 'source')
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.provider.writes, [])
        self.assertEqual(self.runner.next_due, original_dns)
        source_due = self.runner.next_source_due
        self.assertFalse(self.runner.readiness()[0])
        self.assertEqual(self.dns()['changed'], 1)
        self.assertEqual(self.source.calls, 1)
        self.assertEqual(self.runner.next_source_due, source_due)
        dns_due = self.runner.next_due
        self.runner.refresh_source()
        self.assertEqual(self.runner.next_due, dns_due)
        self.assertTrue(self.runner.readiness()[0])
        state = self.runner.state.snapshot()
        self.assertEqual(state['next_run_at'], state['next_dns_run_at'])
        self.assertNotEqual(state['next_source_run_at'], state['next_dns_run_at'])
        self.assertEqual(state['history'][1]['source_fetched_at'], state['last_source_success'])

    def test_failure_revokes_cache_preserves_display_and_pending(self):
        self.runner.run()
        pending = [{'fixture': 'never resend'}]
        self.runner.state.save(pending_operations=pending)
        self.source.fail = True
        self.assertEqual(self.runner.refresh_source()['status'], 'error')
        calls = self.provider.calls
        self.assertEqual(self.dns()['status'], 'error')
        self.assertEqual(self.provider.calls, calls)
        state = self.runner.state.snapshot()
        self.assertEqual(state['ips'], ['1.1.1.1'])
        self.assertFalse(state['source_valid'])
        self.assertEqual(state['pending_operations'], pending)
        self.assertFalse(self.runner.readiness()[0])
        self.source.fail = False
        self.runner.refresh_source()
        self.assertTrue(self.runner.state.snapshot()['source_valid'])
        self.assertFalse(self.runner.readiness()[0])

    def test_source_switch_and_max_ips_invalidate_without_network(self):
        for changes in ({'source_url': 'https://source.example/other'}, {'max_ips': 1}):
            self.runner.refresh_source()
            calls = self.source.calls
            with self.runner.lock:
                self.runner.configure(replace(self.runner.config, **changes))
            self.assertEqual(self.source.calls, calls)
            self.assertIsNone(self.runner.source_ips)
            self.assertLessEqual(self.runner.next_source_due, time.monotonic())
            self.assertEqual(self.dns()['status'], 'error')
            self.assertEqual(self.provider.calls, 0)

    def test_restart_keeps_display_history_not_dns_authorization(self):
        self.runner.refresh_source()
        self.dns()
        restarted = Runner(self.config, State(self.config.state_dir), self.source, lambda *_: self.provider)
        self.assertFalse(restarted.readiness()[0])
        self.assertEqual(restarted.state.snapshot()['ips'], ['1.1.1.1'])
        self.assertEqual(len(restarted.state.snapshot()['history']), 2)
        self.runner = restarted
        self.assertEqual(self.dns()['status'], 'error')
        self.assertEqual(self.source.calls, 1)
        self.assertEqual(self.provider.writes, ['1.1.1.1'])

    def test_hot_save_each_deadline_and_manual_both_clocks(self):
        self.runner.run()
        a, b = self.runner.last_completed, self.runner.last_source_completed
        with self.runner.lock:
            self.runner.configure(replace(self.config, interval_seconds=10800))
        self.assertEqual(self.runner.next_due, a + 10800)
        self.assertEqual(self.runner.next_source_due, b + 3600)
        with self.runner.lock:
            self.runner.configure(replace(self.runner.config, source_interval_seconds=1800))
        self.assertEqual(self.runner.next_due, a + 10800)
        self.assertEqual(self.runner.next_source_due, b + 1800)
        self.runner.start_async(True)
        self.runner.worker.join(3)
        self.assertFalse(self.runner.worker.is_alive())
        self.assertEqual(self.source.calls, 2)
        self.assertGreater(self.runner.last_completed, a)
        self.assertGreater(self.runner.last_source_completed, b)
        self.assertEqual(self.provider.writes, ['1.1.1.1'])

    def test_shared_lock_and_stale_scheduled_decision(self):
        with self.runner.lock:
            with self.assertRaises(BusyError):
                self.runner.refresh_source()
            with self.assertRaises(BusyError):
                self.runner.run()
        self.runner.refresh_source()
        self.assertIsNone(self.runner.refresh_source(scheduled=True))
        self.assertEqual(self.source.calls, 1)

    def test_pending_write_reconciled_by_dns_not_source(self):
        self.runner.refresh_source()
        def uncertain(record, ip):
            self.provider.ip = ip
            self.provider.writes.append(ip)
            raise AppError('fixture unverified write')
        with patch.object(self.provider, 'update', side_effect=uncertain):
            self.assertEqual(self.dns()['status'], 'error')
        pending = self.runner.state.snapshot()['pending_operations']
        self.assertEqual(len(pending), 1)
        self.runner.refresh_source()
        self.assertEqual(self.runner.state.snapshot()['pending_operations'], pending)
        self.assertEqual(self.dns()['status'], 'ok')
        self.assertEqual(self.provider.writes, ['1.1.1.1'])
        self.assertEqual(self.runner.state.snapshot()['pending_operations'], [])
        self.assertEqual(self.source.calls, 2)

    def test_source_deadline_persistence_failure_revokes_cache(self):
        with patch.object(self.runner, '_completed', side_effect=OSError('fixture disk error')):
            self.assertEqual(self.runner.refresh_source()['status'], 'error')
        self.assertIsNone(self.runner.source_ips)
        self.assertFalse(self.runner.readiness()[1]['source_valid'])
        self.assertEqual(self.dns()['status'], 'error')
        self.assertEqual(self.provider.calls, 0)

    def test_source_readiness_expires_on_its_own_clock(self):
        self.runner.run()
        self.assertTrue(self.runner.readiness()[0])
        self.runner.source_success_time = time.monotonic() - 7201
        self.assertFalse(self.runner.readiness()[0])

    def test_public_status_and_retained_text_flag(self):
        from http.client import HTTPConnection
        from cfspeed.server import create_server
        import json
        self.runner.config = replace(self.config, port=0)
        server = create_server(self.runner)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        def get(path):
            connection = HTTPConnection('127.0.0.1', server.server_port, timeout=2)
            try:
                connection.request('GET', path)
                response = connection.getresponse()
                return response.status, response.getheader('X-CFSPEED-Ready'), response.read()
            finally:
                connection.close()
        try:
            self.runner.run()
            status = json.loads(get('/api/status')[2])
            self.assertTrue(status['source_valid'])
            self.assertEqual(status['source_interval_seconds'], 3600)
            self.assertEqual(status['next_run_at'], status['next_dns_run_at'])
            self.assertIn('next_source_run_at', status)
            self.source.fail = True
            self.runner.refresh_source()
            self.assertEqual(get('/readyz')[0], 503)
            self.assertEqual(get('/ipTop.html'), (200, 'false', b'1.1.1.1\n'))
            status = json.loads(get('/api/status')[2])
            self.assertFalse(status['source_valid'])
            self.assertEqual(status['history'][-1]['kind'], 'source')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_startup_loop_fetches_before_dns_only_once(self):
        thread = threading.Thread(target=self.runner.loop)
        thread.start()
        try:
            deadline = time.monotonic() + 3
            while self.runner.state.snapshot()['runs'] < 2 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(self.runner.state.snapshot()['runs'], 2)
            self.assertEqual(self.source.calls, 1)
            self.assertEqual(self.provider.writes, ['1.1.1.1'])
        finally:
            self.runner.stop.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_saved_legacy_override_and_first_patch_freezes_inheritance(self):
        base = replace(self.config, dry_run=True, targets=(), credential_values={})
        runner = Runner(base, State(base.state_dir), self.source)
        admin = Admin(runner, bootstrap_password='fixture-password-only')
        # Simulate an encrypted legacy administrator override without the new field.
        admin.data['service'].pop('source_interval_seconds')
        admin.data['service']['interval_seconds'] = 43200
        admin._persist(admin.data)
        loaded = load_saved_config(base)
        self.assertEqual(loaded.source_interval_seconds, 43200)
        restarted = Runner(loaded, State(base.state_dir), self.source)
        admin = Admin(restarted, bootstrap_password='fixture-password-only')
        result = admin.update({'revision': admin.data['revision'], 'service': {'interval_seconds': 7200}})
        self.assertEqual(result['service']['source_interval_seconds'], 43200)
        self.assertEqual(result['service']['interval_seconds'], 7200)
        result = admin.update({'revision': result['revision'], 'service': {'source_interval_seconds': 1800}})
        saved = load_saved_config(base)
        self.assertEqual((saved.interval_seconds, saved.source_interval_seconds), (7200, 1800))
        self.assertEqual(self.source.calls, 0)


if __name__ == '__main__':
    unittest.main()
