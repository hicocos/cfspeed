"""Offline management HTTP/runtime tests; explicit source/provider fixtures only."""
from dataclasses import replace
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch
from typing import Any
import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest

from cfspeed.admin import Admin, AdminError, load_saved_config
from cfspeed.config import AppError, Config, load_config, parse_config
from cfspeed.network import HTTP
from cfspeed.providers import Cloudflare, DNSPod, Record
from cfspeed.runtime import Runner, State, target_scope
from cfspeed.server import create_server

PASSWORD = 'isolated-test-password-only'
NEW_PASSWORD = 'changed-test-password-only'
CF = {'label': 'CF A', 'provider': 'cloudflare', 'name': 'cdn.example.com',
      'zone_id': 'a' * 32, 'token_env': 'TEST_CF_TOKEN'}
DP = {'label': 'DNS A', 'provider': 'dnspod', 'name': 'cdn', 'domain': 'example.net',
      'line_id': '0', 'secret_id_env': 'TEST_DP_ID', 'secret_key_env': 'TEST_DP_KEY'}


class SourceFixture:
    def __init__(self):
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.error = False

    def request(self, method, url, **kwargs):
        assert method == 'GET' and url == 'https://ip.164746.xyz/ipTop.html'
        self.calls += 1
        self.started.set()
        if not self.release.wait(5):
            raise AssertionError('fixture timeout')
        if self.error:
            raise AppError('fixture source failure')
        return b'1.1.1.1,8.8.8.8'

    def json(self, *args, **kwargs):
        raise AssertionError('external provider/notification forbidden')


class ProviderFixture:
    def __init__(self, target):
        self.target = target
        self.writes = []
        self.value = '9.9.9.9'

    def list_records(self):
        return [Record('1', self.target.name, self.value, 600)]

    def update(self, record, ip):
        self.writes.append((record.id, ip))
        self.value = ip


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.base = Config(state_dir=str(self.directory), port=0)
        self.http = SourceFixture()
        self.providers = {}
        def provider(target, http):
            self.assertIs(http, self.http)
            self.assertTrue(all(http.credentials[name].startswith('fixture-') for name in target.credential_names()))
            return self.providers.setdefault(target.label, ProviderFixture(target))
        self.runner = Runner(self.base, State(self.directory), self.http, provider)
        # Direct Config(port=0) is used only to request an OS-assigned test port.
        # The shared strict parser quite correctly rejects zero in persisted service settings.
        self.runner.config = replace(self.base, port=8788)
        self.admin = Admin(self.runner, bootstrap_password=PASSWORD)
        self.runner.config = replace(self.runner.config, port=0)
        self.dist = self.directory / 'web' / 'dist'
        (self.dist / 'assets').mkdir(parents=True)
        (self.dist / 'index.html').write_text('<!doctype html><title>fixture app</title>')
        (self.dist / 'assets' / 'app-aBcD1234.js').write_text('export const fixture = true;')
        (self.dist / 'assets' / 'font-aBcD1234.woff2').write_bytes(b'fixture-font')
        (self.dist / 'assets' / 'app-aBcD1234.js.map').write_text('{"fixture":true}')
        (self.dist / 'leak.js').symlink_to(self.admin.path)
        self.server = create_server(self.runner, static_dir=self.dist)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = f'127.0.0.1:{self.server.server_port}'
        self.cookie = ''
        self.csrf = ''
        self.addCleanup(self.close)

    def close(self):
        self.http.release.set()
        self.runner.stop.set()
        if self.runner.worker:
            self.runner.worker.join(5)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def request(self, method, path, body=None, headers=None, *, authorized=True, raw=False) -> tuple[int, Any, dict]:
        request_headers = {}
        if method in ('POST', 'PATCH', 'DELETE'):
            request_headers = {'Content-Type': 'application/json', 'Origin': 'http://' + self.host}
            if authorized:
                request_headers['X-CSRF-Token'] = self.csrf
        if self.cookie and authorized:
            request_headers['Cookie'] = self.cookie
        if headers:
            for key, value in headers.items():
                if value is None:
                    request_headers.pop(key, None)
                else:
                    request_headers[key] = value
        data = body if raw else (json.dumps(body).encode() if body is not None else None)
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=data, headers=request_headers)
            response = connection.getresponse()
            payload = response.read()
            result = json.loads(payload) if payload and 'application/json' in response.getheader('Content-Type', '') else payload
            return response.status, result, dict(response.getheaders())
        finally:
            connection.close()

    def login(self, password=PASSWORD, headers=None):
        status, view, response_headers = self.request('POST', '/api/auth/login',
                                                      {'username': 'admin', 'password': password}, headers,
                                                      authorized=False)
        if status == 200:
            self.cookie = response_headers['Set-Cookie'].split(';')[0]
            self.csrf = view['csrf']
        return status, view, response_headers

    def save(self, **changes):
        revision = self.admin.view()['revision']
        return self.request('PATCH', '/api/admin/config', {'revision': revision, **changes})

    def add_cf(self, apply=False):
        payload = {'targets': [CF], 'secrets': {'TEST_CF_TOKEN': 'fixture-cf-token'}}
        if apply:
            payload.update(service={'dry_run': False}, confirm_apply=True)
        response = self.save(**payload)
        self.assertEqual(response[0], 200, response)
        return response[1]

    def test_source_refresh_requires_auth_csrf_origin_and_empty_payload(self):
        path = '/api/admin/source/refresh'
        self.assertEqual(self.request('POST', path, {}, authorized=False)[0], 401)
        self.login()
        self.assertEqual(self.request('POST', path, {}, {'X-CSRF-Token': None})[0], 403)
        self.assertEqual(self.request('POST', path, {}, {'Origin': 'https://other.example'})[0], 403)
        self.assertEqual(self.request('POST', path, {'dry_run': False})[0], 400)
        self.assertEqual(self.http.calls, 0)

    def test_source_refresh_reserves_without_dns_or_deadline_changes(self):
        self.login()
        self.add_cf(apply=True)
        self.runner.state.save(pending_operations=[{'fixture': 'preserved'}])
        before = self.runner.state.snapshot()
        deadline = self.runner.next_due
        self.http.release.clear()
        self.assertEqual(self.request('POST', '/api/admin/source/refresh', {})[0], 202)
        self.assertTrue(self.http.started.wait(2))
        self.assertEqual(self.runner.state.snapshot()['source_status'], 'running')
        self.assertEqual(self.request('POST', '/api/admin/source/refresh', {})[0], 409)
        self.assertEqual(self.request('POST', '/api/admin/run', {'dry_run': True})[0], 409)
        self.assertEqual(self.save(service={'max_ips': 5})[0], 409)
        self.http.release.set()
        self.runner.worker.join(5)
        after = self.runner.state.snapshot()
        self.assertEqual(after['ips'], ['1.1.1.1', '8.8.8.8'])
        self.assertEqual(after['history'][-1]['kind'], 'source')
        self.assertEqual(after['source_status'], 'ok')
        self.assertEqual(after['pending_operations'], before['pending_operations'])
        self.assertEqual(after['next_dns_run_at'], before['next_dns_run_at'])
        self.assertEqual(self.runner.next_due, deadline)
        self.assertFalse(self.providers)
        self.assertTrue(self.runner.lock.acquire(blocking=False))
        self.runner.lock.release()

    def test_source_refresh_failure_retains_ips_and_allows_retry(self):
        self.login()
        self.runner.refresh_source()
        before = self.runner.state.snapshot()
        self.http.error = True
        self.assertEqual(self.request('POST', '/api/admin/source/refresh', {})[0], 202)
        self.runner.worker.join(5)
        failed = self.runner.state.snapshot()
        self.assertEqual(failed['source_status'], 'error')
        self.assertIn('fixture source failure', failed['source_error'])
        self.assertEqual(failed['ips'], before['ips'])
        self.assertFalse(failed['source_valid'])
        self.assertEqual(failed['next_dns_run_at'], before['next_dns_run_at'])
        self.http.error = False
        self.assertEqual(self.request('POST', '/api/admin/source/refresh', {})[0], 202)
        self.runner.worker.join(5)
        self.assertEqual(self.runner.state.snapshot()['source_status'], 'ok')
        self.assertFalse(self.providers)

    def test_retired_history_setting_loads_without_rewriting_authenticated_config(self):
        from cfspeed.admin import persist_admin_data, read_admin_data
        self.login()
        self.add_cf(apply=True)
        data = read_admin_data(self.admin.path, store=self.admin.secret_store)
        data['service']['history_retention_days'] = 7
        persist_admin_data(self.admin.path, data, self.admin.secret_store)
        before = self.admin.path.read_bytes()
        loaded = load_saved_config(replace(self.base, port=8788))
        self.assertFalse(hasattr(loaded, 'history_retention_days'))
        self.assertEqual(loaded.targets, self.runner.config.targets)
        self.assertEqual(loaded.credential_values, self.runner.config.credential_values)
        self.assertEqual(loaded.source_url, self.runner.config.source_url)
        self.assertFalse(loaded.dry_run)
        restart = Runner(loaded, State(self.directory), SourceFixture())
        again = Admin(restart)
        self.assertNotIn('history_retention_days', again.view()['service'])
        self.assertEqual(self.admin.path.read_bytes(), before)
        self.assertEqual(self.save(service={'history_retention_days': 1})[0], 400)

    def test_removed_history_api_and_methods_leave_state_untouched(self):
        self.login()
        self.runner.run()
        before = self.runner.state.snapshot()
        self.assertEqual(self.request('DELETE', '/api/admin/history/' + 'a'*32, {})[0], 501)
        self.assertEqual(self.runner.state.snapshot(), before)
        for obj, names in ((self.runner, ('delete_history', 'maintain_history')),
                           (self.runner.state, ('delete_history', 'prune_history'))):
            for name in names:
                self.assertFalse(hasattr(obj, name))

    def test_original_history_bounds_preserve_pending_journal_and_old_rows(self):
        state = self.runner.state
        pending = [{'record_id': 'unverified'}]
        rows = [{'started_at': str(i), 'finished_at': '2000-01-01T00:00:00+00:00'} for i in range(35)]
        state.save(history=rows, pending_operations=pending)
        self.assertEqual(state.snapshot()['history'], rows[-30:])
        self.assertEqual(State(self.directory).snapshot()['history'], rows[-30:])
        large = [{'started_at': str(i), 'error': 'x' * 800000} for i in range(4)]
        state.save(history=large)
        self.assertEqual(state.snapshot()['history'], large[-2:])
        self.assertEqual(State(self.directory).snapshot()['pending_operations'], pending)

    def test_bootstrap_private_hash_only_and_restart(self):
        self.assertEqual(self.admin.initial_path.read_text().strip(), PASSWORD)
        self.assertNotIn(PASSWORD, self.admin.path.read_text())
        self.assertEqual(self.admin.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.admin.initial_path.stat().st_mode & 0o777, 0o600)
        before = self.admin.path.read_bytes()
        restart = Runner(replace(self.base, port=8788), State(self.directory), SourceFixture())
        again = Admin(restart, bootstrap_password='ignored-other-password')
        again.login({'username': 'admin', 'password': PASSWORD})
        self.assertEqual(again.path.read_bytes(), before)
        self.assertFalse(restart.readiness()[0])

    def test_configuration_change_clears_readiness_and_reports_current_mode(self):
        self.login()
        self.add_cf()
        self.runner.run()
        self.assertTrue(self.runner.readiness()[0])
        self.save(service={'dry_run': False}, confirm_apply=True)
        self.assertFalse(self.runner.readiness()[0])
        view = self.request('GET', '/api/admin/status')[1]
        self.assertFalse(view['dry_run'])
        self.assertFalse(view['ready'])
        self.runner.start_async(True)
        assert self.runner.worker is not None
        self.runner.worker.join(5)
        self.assertFalse(self.runner.readiness()[0], 'manual preview cannot certify live mode readiness')
        self.assertFalse(self.request('GET', '/api/admin/status')[1]['dry_run'])
        self.assertEqual(self.request('GET', '/api/status', authorized=False)[0], 401)

    def test_random_password_when_no_test_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Config(state_dir=directory), State(directory), SourceFixture())
            admin = Admin(runner)
            password = admin.initial_path.read_text().strip()
            self.assertGreaterEqual(len(password), 24)
            self.assertNotEqual(password, PASSWORD)
            self.assertNotIn(password, admin.path.read_text())
            admin.login({'username': 'admin', 'password': password})

    def test_auth_required_wrong_password_cookie_session_logout(self):
        self.assertEqual(self.request('GET', '/api/admin/status')[0], 401)
        self.assertEqual(self.request('GET', '/api/admin/config')[0], 401)
        self.assertEqual(self.request('GET', '/api/auth/session')[1], {'authenticated': False, 'username': '', 'csrf': ''})
        self.assertEqual(self.login('incorrect-password')[0], 401)
        status, view, headers = self.login()
        self.assertEqual(status, 200)
        self.assertTrue(view['authenticated'])
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.assertEqual(self.request('GET', '/api/auth/session')[1], view)
        self.assertEqual(self.request('POST', '/api/auth/logout', {})[0], 200)
        self.assertEqual(self.request('GET', '/api/admin/config')[0], 401)

    def test_origin_csrf_and_secure_cookie(self):
        for origin in (None, 'null', 'http://evil.example', 'http://' + self.host + '/path'):
            self.assertEqual(self.login(headers={'Origin': origin})[0], 403)
        self.assertEqual(self.login(headers={'Origin': 'https://' + self.host})[0], 200)
        self.assertIn('Secure', self.login(headers={'Origin': 'https://' + self.host})[2]['Set-Cookie'])
        for headers in ({'Origin': None}, {'Origin': 'https://evil.example'}, {'X-CSRF-Token': None},
                        {'X-CSRF-Token': 'wrong'}, {'Sec-Fetch-Site': 'cross-site'},
                        {'Origin': 'https://evil.example', 'X-Forwarded-Host': 'evil.example'}):
            self.assertEqual(self.request('POST', '/api/admin/run', {'dry_run': True}, headers)[0], 403)
        self.assertEqual(self.http.calls, 0)

    def test_password_change_eight_character_minimum(self):
        self.login()
        before = self.admin.path.read_bytes()
        result = self.request('POST', '/api/auth/password',
                              {'current_password': PASSWORD, 'new_password': '1234567'})
        self.assertEqual(result[0], 400)
        self.assertIn('8–256', result[1]['error'])
        self.assertEqual(self.admin.path.read_bytes(), before)
        for new_password in ('Test8!ab', 'Test9!abc'):
            current = PASSWORD if new_password == 'Test8!ab' else 'Test8!ab'
            self.assertEqual(self.request('POST', '/api/auth/password',
                             {'current_password': current, 'new_password': new_password})[0], 200)
            self.assertEqual(self.request('GET', '/api/admin/config')[0], 401)
            self.assertEqual(self.login(new_password)[0], 200)
            self.assertNotIn(new_password, self.admin.path.read_text())
        restart = Runner(replace(self.base, port=8788), State(self.directory), SourceFixture())
        Admin(restart).login({'username': 'admin', 'password': 'Test9!abc'})

    def test_password_change_revokes_all_sessions_and_removes_initial_file(self):
        self.login()
        first_cookie = self.cookie
        self.login()
        self.assertEqual(self.request('POST', '/api/auth/password', {'current_password': 'bad', 'new_password': NEW_PASSWORD})[0], 401)
        self.assertEqual(self.request('POST', '/api/auth/password', {'current_password': PASSWORD, 'new_password': 'short'})[0], 400)
        status, view, headers = self.request('POST', '/api/auth/password', {'current_password': PASSWORD, 'new_password': NEW_PASSWORD})
        self.assertEqual((status, view), (200, {'ok': True}))
        self.assertIn('Max-Age=0', headers['Set-Cookie'])
        self.assertFalse(self.admin.initial_path.exists())
        self.assertIsNone(self.admin.session(first_cookie.split('=', 1)[1]))
        self.assertEqual(self.request('GET', '/api/admin/config')[0], 401)
        self.assertEqual(self.login(PASSWORD)[0], 401)
        self.assertEqual(self.login(NEW_PASSWORD)[0], 200)
        self.assertNotIn(NEW_PASSWORD, self.admin.path.read_text())
        restart = Runner(replace(self.base, port=8788), State(self.directory), SourceFixture())
        Admin(restart).login({'username': 'admin', 'password': NEW_PASSWORD})

    def test_revision_scoped_save_preserves_fields_and_no_run(self):
        self.login()
        original = self.admin.view()
        status, saved, _ = self.save(service={'interval_seconds': 123, 'attempts': 2})
        self.assertEqual(status, 200)
        self.assertTrue(saved['service']['dry_run'])
        self.assertEqual(saved['service']['timeout_seconds'], original['service']['timeout_seconds'])
        self.assertEqual(saved['revision'], original['revision'] + 1)
        self.assertEqual(self.request('PATCH', '/api/admin/config', {'revision': original['revision']})[0], 409)
        self.assertEqual(self.http.calls, 0)
        self.assertEqual(self.runner.state.snapshot()['runs'], 0)
        self.assertEqual(self.runner.config.host, self.base.host)

    def test_secret_redaction_override_clear_preserve_and_persistence(self):
        self.login()
        saved = self.add_cf()
        self.assertEqual(saved['credentials'], [{'name': 'TEST_CF_TOKEN', 'configured': True}])
        self.assertNotIn('fixture-cf-token', json.dumps(saved))
        self.assertNotIn('fixture-cf-token', repr(self.runner.config))
        persisted = json.loads(self.admin.path.read_bytes())
        self.assertEqual(persisted['version'], 2)
        self.assertEqual(persisted['secrets']['algorithm'], 'AES-256-GCM')
        self.assertNotIn('fixture-cf-token', self.admin.path.read_text())
        self.assertNotIn('TEST_CF_TOKEN', json.dumps(persisted['secrets']))
        self.assertNotIn('fixture-cf-token', json.dumps(self.request('GET', '/api/admin/config')[1]))
        self.assertEqual(self.save(service={'max_ips': 12})[0], 200)
        self.assertEqual(self.runner.config.credential('TEST_CF_TOKEN'), 'fixture-cf-token')
        self.assertEqual(self.save(secrets={'TEST_CF_TOKEN': None})[0], 400)
        self.assertEqual(self.save(secrets={'TEST_CF_TOKEN': ''})[0], 400)
        saved = load_saved_config(replace(self.base, port=8788))
        self.assertEqual(saved.max_ips, 12)
        self.assertEqual(saved.credential('TEST_CF_TOKEN'), 'fixture-cf-token')
        self.runner.run()
        for path in ('/api/admin/status', '/api/status', '/readyz'):
            self.assertNotIn('fixture-cf-token', json.dumps(self.request('GET', path)[1]))
        with patch.dict(os.environ, {'TEST_CF_TOKEN': 'environment-secret'}):
            status, view, _ = self.save(targets=[], secrets={'TEST_CF_TOKEN': None})
            self.assertEqual(status, 200)
            self.assertEqual(view['credentials'], [{'name': 'TEST_CF_TOKEN', 'configured': False}])
            self.assertEqual(self.runner.config.credential('TEST_CF_TOKEN'), '')
        self.assertFalse(any(provider.writes for provider in self.providers.values()))

    def test_targets_add_edit_remove_both_providers_and_accounts(self):
        self.login()
        self.add_cf()
        second = {**DP, 'label': 'DNS B', 'secret_id_env': 'TEST_DP_B_ID', 'secret_key_env': 'TEST_DP_B_KEY'}
        status, saved, _ = self.save(targets=[CF, DP, second], secrets={
            'TEST_DP_ID': 'fixture-dp-id', 'TEST_DP_KEY': 'fixture-dp-key',
            'TEST_DP_B_ID': 'fixture-dp-b-id', 'TEST_DP_B_KEY': 'fixture-dp-b-key'})
        self.assertEqual(status, 200)
        self.assertEqual(len(saved['targets']), 3)
        self.assertNotEqual(target_scope(self.runner.config.targets[1]), target_scope(self.runner.config.targets[2]))
        edited = {**CF, 'label': 'Changed', 'name': 'new.example.com'}
        self.assertEqual(self.save(targets=[edited, DP])[0], 200)
        self.assertEqual(self.admin.view()['targets'][0]['name'], 'new.example.com')
        self.assertEqual(self.save(targets=[])[1]['targets'], [])
        self.assertEqual(self.http.calls, 0)

    def test_source_selection_persists_and_forces_preview(self):
        self.login()
        self.add_cf()
        self.assertEqual(self.save(service={'dry_run': False}, confirm_apply=True)[0], 200)
        source = 'https://ip.v2too.top/api/nodes'
        status, view, _ = self.save(service={'source_url': source})
        self.assertEqual(status, 200)
        self.assertEqual(view['service']['source_url'], source)
        self.assertTrue(view['service']['dry_run'])
        self.assertEqual(self.request('GET', '/api/admin/config')[1]['service']['source_url'], source)
        self.assertEqual(load_saved_config(replace(self.base, port=8788)).source_url, source)
        self.assertEqual(self.save(service={'max_ips': 10})[1]['service']['source_url'], source)
        self.assertEqual(self.save(service={'source_url': self.base.source_url})[0], 200)
        self.assertEqual(load_saved_config(replace(self.base, port=8788)).source_url, self.base.source_url)
        self.assertEqual(self.http.calls, 0)
        self.assertFalse(any(provider.writes for provider in self.providers.values()))

    def test_strict_validation_source_allowlist_and_bounds(self):
        self.login()
        invalid = [dict(service={'source_url': 'https://other.example/'}), dict(service={'dry_run': 'false'}),
                   dict(service={'port': 0}), dict(service={'interval_seconds': 1}), dict(service={'max_ips': True}),
                   dict(targets=[{**CF, 'zone_id': 'A' * 32}]), dict(targets=[{**CF, 'name': 'UPPER.example'}]),
                   dict(targets=[CF, {**CF, 'label': 'Duplicate'}]), dict(targets=[{**CF, 'token': 'bad'}]),
                   dict(targets=[{**DP, 'domain': ''}]), dict(targets=[{**DP, 'line_id': []}]),
                   dict(targets=[CF] * 33), dict(secrets={'bad-name': 'secret'}),
                   dict(secrets={'GOOD_NAME': ' white '}), dict(secrets={'GOOD_NAME': 'x' * 4097}),
                   dict(service=None), dict(confirm_apply='true')]
        original = self.admin.view()
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertEqual(self.save(**payload)[0], 400)
        self.assertEqual(self.admin.view(), original)
        self.assertEqual(self.save(service={'source_url': self.base.source_url})[0], 200)

    def test_apply_requires_explicit_confirmation_scope_changes_force_preview(self):
        self.login()
        self.add_cf()
        self.assertEqual(self.save(service={'dry_run': False})[0], 400)
        self.assertEqual(self.save(service={'dry_run': False}, confirm_apply=True)[0], 200)
        self.assertFalse(self.runner.config.dry_run)
        # Schedule-only edits preserve an already explicitly enabled mode.
        self.assertEqual(self.save(service={'interval_seconds': 90})[0], 200)
        self.assertFalse(self.runner.config.dry_run)
        self.assertEqual(self.save(targets=[{**CF, 'name': 'other.example.com'}])[0], 200)
        self.assertTrue(self.runner.config.dry_run)
        self.assertEqual(self.save(service={'dry_run': False}, confirm_apply=True)[0], 200)
        self.assertEqual(self.save(targets=[])[0], 200)
        self.assertTrue(self.runner.config.dry_run)
        self.assertEqual(self.http.calls, 0)

    def test_force_preview_cli_overlay_cannot_be_disabled(self):
        self.login()
        self.add_cf(apply=True)
        restart = Runner(replace(self.base, port=8788), State(self.directory), SourceFixture())
        forced = Admin(restart, force_preview=True)
        self.assertTrue(restart.config.dry_run)
        with self.assertRaises(AdminError):
            forced.update({'revision': forced.view()['revision'], 'service': {'dry_run': False}, 'confirm_apply': True})
        self.assertFalse(load_saved_config(replace(self.base, port=8788)).dry_run)
        self.assertTrue(load_saved_config(replace(self.base, port=8788), force_preview=True).dry_run)

    def test_manual_async_duplicate_and_config_conflict(self):
        self.login()
        self.http.release.clear()
        status, body, _ = self.request('POST', '/api/admin/run', {'dry_run': True})
        self.assertEqual((status, body), (202, {'accepted': True}))
        self.assertTrue(self.http.started.wait(2))
        self.assertTrue(self.runner.lock.locked())
        self.assertEqual(self.request('GET', '/api/admin/status')[1]['status'], 'running')
        self.assertFalse(self.request('GET', '/api/admin/status')[1]['ready'])
        self.assertEqual(self.request('POST', '/api/admin/run', {'dry_run': True})[0], 409)
        self.assertEqual(self.save(service={'max_ips': 10})[0], 409)
        self.http.release.set()
        self.runner.worker.join(5)
        self.assertFalse(self.runner.lock.locked())
        self.assertEqual(self.http.calls, 1)
        self.assertEqual(self.runner.state.snapshot()['runs'], 1)

    def test_manual_preview_overrides_apply_and_status_uses_last_actual_mode(self):
        self.login()
        self.add_cf(apply=True)
        self.assertEqual(self.request('POST', '/api/admin/run', {'dry_run': False})[0], 400)
        self.assertEqual(self.request('POST', '/api/admin/run', {'dry_run': True})[0], 202)
        self.runner.worker.join(5)
        status = self.request('GET', '/api/admin/status')[1]
        self.assertFalse(status['dry_run'])
        self.assertFalse(status['configured_dry_run'])
        self.assertEqual(status['history'][-1]['mode'], 'preview')
        self.assertFalse(status['ready'])
        self.assertEqual(self.request('GET', '/readyz')[1]['mode'], 'apply')
        self.assertFalse(self.providers['CF A'].writes)
        self.assertFalse(self.runner.config.dry_run)
        self.assertEqual(self.request('POST', '/api/admin/run', {'dry_run': False, 'confirm_apply': True})[0], 202)
        self.runner.worker.join(5)
        self.assertEqual(self.providers['CF A'].writes, [('1', '1.1.1.1')])
        self.assertFalse(self.request('GET', '/api/admin/status')[1]['dry_run'])
        self.assertEqual(self.save(service={'dry_run': True})[0], 200)
        self.assertTrue(self.request('GET', '/api/admin/status')[1]['dry_run'])
        self.assertTrue(self.request('GET', '/api/admin/status')[1]['configured_dry_run'])
        self.assertEqual(self.request('POST', '/api/admin/run', {'dry_run': False, 'confirm_apply': True})[0], 400)

    def test_scheduler_manual_single_reservation_and_no_stale_due_race(self):
        self.login()
        self.http.release.clear()
        self.assertEqual(self.request('POST', '/api/admin/run', {'dry_run': True})[0], 202)
        self.assertTrue(self.http.started.wait(2))
        scheduler = threading.Thread(target=self.runner.loop)
        scheduler.start()
        try:
            self.http.release.set()
            self.runner.worker.join(5)
            time.sleep(0.3)
            self.assertEqual(self.http.calls, 1)
            # Simulate scheduler decision before the manual run changed next_due.
            self.runner.schedule_context.active = True
            self.assertIsNone(self.runner.run())
            self.runner.schedule_context.active = False
            self.assertEqual(self.http.calls, 1)
        finally:
            self.runner.stop.set()
            scheduler.join(2)
        self.assertFalse(scheduler.is_alive())

    def test_schedule_change_wakes_and_recomputes_next_run(self):
        self.login()
        self.runner.run()
        self.runner.last_completed = time.monotonic() - 31
        self.runner.next_due = self.runner.last_completed + 300
        self.http.started.clear()
        scheduler = threading.Thread(target=self.runner.loop)
        scheduler.start()
        try:
            self.assertEqual(self.save(service={'interval_seconds': 30})[0], 200)
            deadline = time.monotonic() + 2
            while self.runner.state.snapshot()['runs'] < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(self.runner.state.snapshot()['runs'], 2)
            self.assertEqual(self.http.calls, 1)  # DNS deadline does not fetch the source.
        finally:
            self.runner.stop.set()
            scheduler.join(2)
        self.assertFalse(scheduler.is_alive())

    def test_failed_preview_preserves_ips_and_readiness_false(self):
        self.login()
        self.runner.run()
        self.http.error = True
        self.assertEqual(self.request('POST', '/api/admin/run', {'dry_run': True})[0], 202)
        self.runner.worker.join(5)
        status = self.request('GET', '/api/admin/status')[1]
        self.assertEqual(status['ips'], ['1.1.1.1', '8.8.8.8'])
        self.assertFalse(status['ready'])
        self.assertEqual(self.request('GET', '/readyz')[0], 503)

    def test_static_spa_mime_headers_no_secret_or_traversal(self):
        for path in ('/', '/admin/targets', '/login'):
            status, body, headers = self.request('GET', path)
            self.assertEqual(status, 200)
            self.assertIn(b'fixture app', body)
            self.assertEqual(headers['Cache-Control'], 'no-cache')
            self.assertEqual(headers['X-Frame-Options'], 'DENY')
            self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        status, body, headers = self.request('GET', '/assets/app-aBcD1234.js')
        self.assertEqual(status, 200)
        self.assertIn('javascript', headers['Content-Type'])
        self.assertIn('immutable', headers['Cache-Control'])
        self.assertEqual(self.request('GET', '/assets/font-aBcD1234.woff2')[2]['Content-Type'], 'font/woff2')
        for path in ('/../admin.json', '/%2e%2e/admin.json', '/assets/%2e%2e/%2e%2e/admin.json', '/leak.js',
                     '/admin.json', '/initial-admin-password.txt', '/assets/app-aBcD1234.js.map', '/.env', '/api/missing'):
            with self.subTest(path=path):
                self.assertEqual(self.request('GET', path)[0], 404)
        self.assertEqual(self.request('HEAD', '/')[1], b'')
        self.assertEqual(self.request('GET', '/healthz')[0], 200)
        self.assertEqual(self.request('GET', '/api/status')[0], 401)
        self.assertEqual(self.request('GET', '/ipTop.html')[0], 503)
        self.runner.run()
        self.assertEqual(self.request('GET', '/ipTop.html')[1], b'1.1.1.1,8.8.8.8\n')

    def test_request_size_type_duplicate_json_and_login_rate_limits(self):
        self.login()
        for body, headers, expected in [(b'x' * 131073, {}, 413), (b'{}', {'Content-Type': 'text/plain'}, 415),
                                        (b'{"dry_run":true,"dry_run":false}', {}, 400), (b'[]', {}, 400),
                                        (b'{"dry_run":NaN}', {}, 400)]:
            self.assertEqual(self.request('POST', '/api/admin/run', body, headers, raw=True)[0], expected)
        self.admin.login_attempts['127.0.0.1'] = [time.monotonic()] * 10
        self.assertEqual(self.login()[0], 429)
        self.assertLessEqual(len(self.admin.login_attempts['127.0.0.1']), 10)
        self.assertEqual(self.http.calls, 0)

    def test_saved_file_corruption_fails_closed(self):
        self.admin.path.write_text('{bad json')
        with self.assertRaises(AppError):
            load_saved_config(replace(self.base, port=8788))
        restart = Runner(replace(self.base, port=8788), State(self.directory), SourceFixture())
        with self.assertRaises(AppError):
            Admin(restart, bootstrap_password=NEW_PASSWORD)
        self.assertEqual(self.admin.initial_path.read_text().strip(), PASSWORD)

    def test_expired_and_bounded_sessions_and_http_headers(self):
        self.login()
        token = self.cookie.split('=', 1)[1]
        self.admin.session(token)['expires'] = time.monotonic() - 1
        self.assertEqual(self.request('GET', '/api/admin/config')[0], 401)
        self.admin.sessions = {str(index): {'csrf': 'fixture', 'expires': time.monotonic() + 60} for index in range(64)}
        self.login()
        self.assertEqual(len(self.admin.sessions), 64)
        self.assertEqual(self.request('GET', '/api/auth/session', headers={'X-Large': 'x' * 17000})[0], 431)

    def test_missing_preview_credentials_can_be_saved_but_not_applied(self):
        self.login()
        self.assertEqual(self.save(targets=[CF], service={'dry_run': 'false'})[0], 400)
        self.assertEqual(self.save(targets=[CF])[0], 200)
        self.assertEqual(self.admin.view()['credentials'], [{'name': 'TEST_CF_TOKEN', 'configured': False}])
        self.assertEqual(self.save(service={'dry_run': False}, confirm_apply=True)[0], 400)
        self.assertEqual(self.request('POST', '/api/admin/run', {'dry_run': True})[0], 202)
        self.runner.worker.join(5)
        self.assertEqual(self.runner.state.snapshot()['status'], 'error')
        self.assertEqual(self.http.calls, 0)


class BackendIntegrationTests(unittest.TestCase):
    def test_web_root_environment_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'build'
            root.mkdir()
            (root / 'index.html').write_text('<title>environment web root fixture</title>')
            runner = Runner(Config(state_dir=directory), State(directory), SourceFixture())
            Admin(runner, bootstrap_password=PASSWORD)
            runner.config = replace(runner.config, port=0)
            with patch.dict(os.environ, {'CFSPEED_WEB_ROOT': str(root)}):
                server = create_server(runner)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = HTTPConnection('127.0.0.1', server.server_address[1], timeout=5)
                connection.request('GET', '/admin/stats')
                response = connection.getresponse()
                body = response.read()
                connection.close()
                self.assertEqual(response.status, 200)
                self.assertIn(b'environment web root fixture', body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)

    def test_cli_serve_startup_private_bootstrap_restart_and_saved_check(self):
        from cfspeed.__main__ import main
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            config_path = directory / 'config.toml'
            config_path.write_text('[service]\nstate_dir="data"\nport=8788\ndry_run=true\n')
            source = SourceFixture()
            original_create = create_server
            def create(runner):
                runner.config = replace(runner.config, port=0)
                server = original_create(runner, static_dir=directory / 'missing-dist')
                self.assertTrue((directory / 'data' / 'admin.json').exists())
                self.assertEqual(runner.config.targets, ())
                return server
            real_run = Runner.run
            def once(runner):
                result = real_run(runner)
                runner.stop.set()
                return result
            def offline_http(self, method, url, **kwargs):
                return source.request(method, url, **kwargs)
            with patch.dict(os.environ, {'CFSPEED_TEST_ADMIN_PASSWORD': PASSWORD}), \
                    patch('cfspeed.__main__.logging.basicConfig'), \
                    patch('cfspeed.__main__.create_server', side_effect=create), \
                    patch('cfspeed.__main__.signal.signal'), \
                    patch.object(HTTP, 'request', offline_http), \
                    patch.object(HTTP, 'json', side_effect=AssertionError('external API forbidden')), \
                    patch.object(Runner, 'run', once):
                self.assertEqual(main(['serve', '--config', str(config_path)]), 0)
                initial = (directory / 'data' / 'initial-admin-password.txt').read_bytes()
                self.assertEqual(main(['serve', '--config', str(config_path)]), 0)
                self.assertEqual((directory / 'data' / 'initial-admin-password.txt').read_bytes(), initial)
            self.assertEqual(source.calls, 2)
            state = State(directory / 'data').snapshot()
            self.assertEqual(state['runs'], 4)  # Startup records source and DNS separately.
            self.assertEqual(state['status'], 'starting')
            base = load_config(config_path)
            runner = Runner(base, State(base.state_dir), SourceFixture())
            admin = Admin(runner)
            admin.update({'revision': 1, 'service': {'interval_seconds': 99}})
            self.assertEqual(load_saved_config(base).interval_seconds, 99)
            output = io.StringIO()
            with contextlib.redirect_stdout(output), patch('cfspeed.__main__.logging.basicConfig'):
                self.assertEqual(main(['check', '--config', str(config_path)]), 0)
            self.assertEqual(json.loads(output.getvalue()), {'valid': True, 'mode': 'preview', 'targets': 0})

    def test_provider_credentials_explicit_overlay_and_record_bounds(self):
        config = parse_config({'targets': [CF, DP]})
        class CFHTTP:
            credentials = {'TEST_CF_TOKEN': 'fixture-overlay-cf'}
            record_limit = 1
            def json(self, method, url, **kwargs):
                self.header = kwargs['headers']['Authorization']
                return {'success': True, 'result': [], 'result_info': {'total_count': 2, 'total_pages': 1}}
        http = CFHTTP()
        with self.assertRaises(AppError):
            Cloudflare(config.targets[0], http).list_records()
        self.assertEqual(http.header, 'Bearer fixture-overlay-cf')
        class DPHTTP:
            credentials = {'TEST_DP_ID': 'fixture-id', 'TEST_DP_KEY': 'fixture-key'}
            record_limit = 1
            def json(self, method, url, **kwargs):
                self.authorization = kwargs['headers']['Authorization']
                return {'Response': {'RecordList': [], 'RecordCountInfo': {'TotalCount': 2}}}
        http = DPHTTP()
        with self.assertRaises(AppError):
            DNSPod(config.targets[1], http).list_records()
        self.assertIn('Credential=fixture-id/', http.authorization)


if __name__ == '__main__':
    unittest.main()
