"""Public origin and persistent-state failure regression tests; no provider traffic."""
from dataclasses import replace
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from cfspeed.admin import Admin
from cfspeed.config import Config
from cfspeed.runtime import Runner, State
from cfspeed.server import create_server

PASSWORD = 'test-only-Security-pass123'

class PublicBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runner = Runner(replace(Config(), state_dir=self.tmp.name), State(self.tmp.name))
        self.admin = Admin(self.runner, bootstrap_password=PASSWORD)
        self.runner.config = replace(self.runner.config, port=0)
        with patch.dict(os.environ, {'CFSPEED_PUBLIC_ORIGIN': 'https://cfspeed.example.com'}):
            self.server = create_server(self.runner)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def request(self, method='GET', path='/api/auth/session', body=None, headers=None):
        con = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        all_headers = {'Host': 'cfspeed.example.com', **(headers or {})}
        if body is not None:
            all_headers['Content-Type'] = 'application/json'
            body = json.dumps(body)
        con.request(method, path, body=body, headers=all_headers)
        res = con.getresponse(); value = res.read()
        result = res.status, dict(res.getheaders()), json.loads(value)
        con.close()
        return result

    def test_host_and_origin_pinned_ignore_forwarded_spoof(self):
        self.assertEqual(self.request(headers={'Host': 'evil.example'})[0], 421)
        self.assertEqual(self.request(headers={'Host': 'evil.example', 'X-Forwarded-Host': 'cfspeed.example.com'})[0], 421)
        payload = {'username': 'admin', 'password': PASSWORD}
        for origin in ('http://cfspeed.example.com', 'https://evil.example', 'null'):
            self.assertEqual(self.request('POST', '/api/auth/login', payload, {'Origin': origin})[0], 403)
        self.assertEqual(self.request('POST', '/api/auth/login', payload, {'X-Forwarded-Proto': 'https'})[0], 403)
        code, headers, body = self.request('POST', '/api/auth/login', payload, {'Origin': 'https://cfspeed.example.com'})
        self.assertEqual(code, 200)
        self.assertTrue(body['authenticated'])
        for flag in ('Secure', 'HttpOnly', 'SameSite=Strict'):
            self.assertIn(flag, headers['Set-Cookie'])
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_static_private_routes_do_not_leak(self):
        for path in ('/api/status', '/api/admin/config', '/api/admin/status'):
            self.assertEqual(self.request(path=path)[0], 401)
        for path in ('/admin.json', '/master.key', '/initial-admin-password.txt', '/.env', '/data/admin.json', '/assets/../../admin.json'):
            self.assertEqual(self.request(path=path)[0], 404)

class NetworkDeadlineTests(unittest.TestCase):
    def test_slow_drip_response_stops_without_retrying_write(self):
        from unittest.mock import MagicMock
        from cfspeed.network import HTTP
        from cfspeed.config import AppError
        http = HTTP(timeout=1, attempts=3)
        response = MagicMock()
        response.status = 200
        response.read1.return_value = b'x'
        http.opener = MagicMock()
        http.opener.open.return_value.__enter__.return_value = response
        with patch('cfspeed.network.time.monotonic', side_effect=[0, 0.1, 0.6, 1.2]):
            with self.assertRaises(AppError):
                http.request('POST', 'https://example.invalid', body=b'{}')
        self.assertEqual(response.read1.call_count, 2)
        self.assertEqual(http.opener.open.call_count, 1)

class DiskFailureTests(unittest.TestCase):
    def test_configure_disk_failure_keeps_previous_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            original = replace(Config(), state_dir=directory)
            runner = Runner(original, State(directory))
            with patch.object(runner.state, 'save', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    runner.configure(replace(original, dry_run=False))
            self.assertTrue(runner.config.dry_run)

    def test_atomic_state_write_failure_retains_previous_file_and_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(directory); state.save(ips=['1.1.1.1'])
            before = state.path.read_bytes()
            with patch('cfspeed.runtime.os.replace', side_effect=OSError('disk unavailable')):
                with self.assertRaises(OSError):
                    state.save(ips=['8.8.8.8'])
            self.assertEqual(state.path.read_bytes(), before)
            self.assertEqual(state.snapshot()['ips'], ['1.1.1.1'])
            self.assertEqual(list(Path(directory).glob('.state-*')), [])

class AdditionalIncidentTests(unittest.TestCase):
    def test_login_abuse_does_not_lock_other_client_or_password_rotation(self):
        from cfspeed.admin import AdminError
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(replace(Config(), state_dir=directory), State(directory))
            admin = Admin(runner, bootstrap_password=PASSWORD)
            for _ in range(10):
                with self.assertRaises(AdminError):
                    admin.login({'username': 'admin', 'password': ''}, client='attacker')
            with self.assertRaises(AdminError) as error:
                admin.login({'username': 'admin', 'password': PASSWORD}, client='attacker')
            self.assertEqual(error.exception.status, 429)
            self.assertTrue(admin.login({'username': 'admin', 'password': PASSWORD}, client='owner')[1]['authenticated'])
            admin.change_password({'current_password': PASSWORD, 'new_password': PASSWORD + '-changed'})
            self.assertEqual(admin.sessions, {})

    def test_config_interrupted_after_rename_restarts_in_preview(self):
        from cfspeed.admin import load_saved_config
        with tempfile.TemporaryDirectory() as directory:
            base = replace(Config(), state_dir=directory)
            runner = Runner(base, State(directory)); admin = Admin(runner, bootstrap_password=PASSWORD)
            # Simulate process loss after encrypted candidate reached disk but before ack.
            data = json.loads(json.dumps(admin.data))
            data['targets'] = [{'label': 'fixture', 'provider': 'cloudflare', 'name': 'cf.example.com', 'zone_id': 'a'*32}]
            data['secrets'] = {'CF_API_TOKEN': 'fixture-not-a-real-token'}
            data['service']['dry_run'] = False
            admin._persist(data)
            admin.pending_path.write_text('incomplete transaction')
            self.assertTrue(load_saved_config(base).dry_run)
            restarted = Runner(base, State(directory)); recovered = Admin(restarted)
            self.assertTrue(restarted.config.dry_run)
            self.assertFalse(recovered.pending_path.exists())
            self.assertTrue(load_saved_config(base).dry_run)

    def test_network_stop_prevents_any_request(self):
        from unittest.mock import Mock
        from cfspeed.network import HTTP
        from cfspeed.config import AppError
        http = HTTP(); http.stop = threading.Event(); http.stop.set(); http.opener = Mock()
        with self.assertRaises(AppError):
            http.request('GET', 'https://example.invalid', retry=True)
        http.opener.open.assert_not_called()

    def test_trickle_headers_hit_absolute_deadline(self):
        import socket, time
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(replace(Config(), state_dir=directory, port=0), State(directory))
            server = create_server(runner)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            sockets = []
            try:
                for _ in range(8):
                    sock = socket.create_connection(('127.0.0.1', server.server_port), timeout=1)
                    sock.sendall(b'GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Drip: ')
                    sockets.append(sock)
                start = time.monotonic()
                while time.monotonic() - start < 11:
                    for sock in sockets:
                        try: sock.sendall(b'a')
                        except OSError: pass
                    time.sleep(0.5)
                con = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
                con.request('GET', '/healthz'); response = con.getresponse()
                self.assertEqual(response.status, 200); response.read(); con.close()
            finally:
                for sock in sockets: sock.close()
                server.shutdown(); server.server_close(); thread.join()

if __name__ == '__main__':
    unittest.main()
