"""Isolated at-rest crypto tests: temporary keys, synthetic tokens, no network."""
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import base64
import copy
import io
import json
import logging
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

from cfspeed.admin import Admin, MAX_CONFIG_BYTES, atomic_private, load_saved_config, new_auth
from cfspeed.config import AppError, Config
from cfspeed.crypto import (ALGORITHM, DEFAULT_KEY_FILENAME, KEY_BYTES, KEY_ENV,
                            MAX_SECRET_BYTES, CryptoError, SecretStore)
from cfspeed.runtime import Runner, State

PASSWORD = 'crypto-isolated-password-only'
TOKEN = 'fixture-secret-not-a-live-provider-token-73e968'
VALUES = {'TEST_TOKEN': TOKEN, 'TEST_CLEARED': None, 'TEST_UNICODE': '测试凭据-é'}


class IsolatedCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        environment = {k: v for k, v in os.environ.items() if k != KEY_ENV}
        self.environment = patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.key_path = self.directory / DEFAULT_KEY_FILENAME

    def write_key(self, path=None, value=None, mode=0o600):
        path = self.key_path if path is None else path
        path.write_bytes(os.urandom(KEY_BYTES) if value is None else value)
        path.chmod(mode)
        return path


class KeyTests(IsolatedCase):
    def test_default_creation_private_random_and_restart(self):
        with patch('os.umask', wraps=os.umask) as umask:
            first = SecretStore(self.directory)
            first.unlock(allow_create=True)
            umask.assert_not_called()
        key = self.key_path.read_bytes()
        self.assertEqual(len(key), KEY_BYTES)
        self.assertEqual(stat.S_IMODE(self.key_path.stat().st_mode), 0o600)
        self.assertEqual(list(self.directory.iterdir()), [self.key_path])
        SecretStore(self.directory).unlock(allow_create=True)
        self.assertEqual(self.key_path.read_bytes(), key)
        other = self.directory / 'other'
        other.mkdir()
        SecretStore(other).unlock(allow_create=True)
        self.assertNotEqual((other / DEFAULT_KEY_FILENAME).read_bytes(), key)

    def test_existing_default_key_is_never_replaced(self):
        self.write_key()
        before = self.key_path.read_bytes()
        with patch('cfspeed.crypto.os.urandom', side_effect=AssertionError('no regeneration')):
            SecretStore(self.directory).unlock(allow_create=True)
        self.assertEqual(self.key_path.read_bytes(), before)

    def test_explicit_external_key_and_no_default_file(self):
        external = self.write_key(self.directory / 'mounted-master-key')
        with patch.dict(os.environ, {KEY_ENV: str(external)}):
            store = SecretStore(self.directory / 'state')
            store.unlock(allow_create=True)
            envelope = store.encrypt(VALUES, {'version': 2})
            self.assertEqual(store.decrypt(envelope, {'version': 2}), VALUES)
        self.assertFalse(self.key_path.exists())
        self.assertFalse((self.directory / 'state').exists())

    def test_explicit_missing_or_empty_key_never_auto_created(self):
        for value in (str(self.directory / 'missing.key'), ''):
            with self.subTest(value=value), patch.dict(os.environ, {KEY_ENV: value}):
                with self.assertRaises(CryptoError):
                    SecretStore(self.directory).unlock(allow_create=True)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_missing_key_without_creation_permission_fails(self):
        with self.assertRaises(CryptoError):
            SecretStore(self.directory).unlock()
        self.assertFalse(self.key_path.exists())

    def test_permissive_or_non_0600_modes_refused_without_chmod(self):
        for mode in (0o644, 0o640, 0o666, 0o700, 0o400, 0o000, 0o4600):
            with self.subTest(mode=oct(mode)):
                self.write_key(mode=mode)
                with self.assertRaises(CryptoError):
                    SecretStore(self.directory).unlock(allow_create=True)
                self.assertEqual(stat.S_IMODE(self.key_path.stat().st_mode), mode)

    def test_truncated_oversized_or_encoded_key_refused_unchanged(self):
        for value in (b'', b'x' * 31, b'x' * 33, b'x' * 4096, b'ab' * 32, b'x' * 32 + b'\n'):
            with self.subTest(length=len(value)):
                self.write_key(value=value)
                with self.assertRaises(CryptoError):
                    SecretStore(self.directory).unlock(allow_create=True)
                self.assertEqual(self.key_path.read_bytes(), value)

    def test_key_symlink_and_dangling_symlink_refused(self):
        target = self.write_key(self.directory / 'real-key')
        for destination in (target, self.directory / 'missing-target'):
            with self.subTest(destination=destination.name):
                self.key_path.symlink_to(destination)
                with self.assertRaises(CryptoError):
                    SecretStore(self.directory).unlock(allow_create=True)
                self.assertTrue(self.key_path.is_symlink())
                self.key_path.unlink()
        self.assertFalse((self.directory / 'missing-target').exists())

    def test_symlink_directory_component_refused_before_creation(self):
        actual = self.directory / 'actual'
        actual.mkdir()
        alias = self.directory / 'alias'
        alias.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(CryptoError):
            SecretStore(alias).unlock(allow_create=True)
        self.assertEqual(list(actual.iterdir()), [])

    def test_key_directory_and_fifo_refused_without_blocking(self):
        self.key_path.mkdir()
        with self.assertRaises(CryptoError):
            SecretStore(self.directory).unlock(allow_create=True)
        self.key_path.rmdir()
        os.mkfifo(self.key_path, 0o600)
        with self.assertRaises(CryptoError):
            SecretStore(self.directory).unlock(allow_create=True)

    def test_read_bound_enforced_even_if_stat_size_is_stale(self):
        self.write_key(value=b'x' * 4096)
        original = os.fstat
        def stale(fd):
            info = original(fd)
            return SimpleNamespace(st_mode=info.st_mode, st_size=0)
        with patch('cfspeed.crypto.os.fstat', side_effect=stale), self.assertRaises(CryptoError):
            SecretStore(self.directory).unlock(allow_create=True)

    def test_key_creation_is_atomic_and_race_winner_is_preserved(self):
        winner = os.urandom(KEY_BYTES)
        original_link = os.link
        observed = []
        def race(source, target, **kwargs):
            temporary = self.directory / source
            observed.append(temporary.read_bytes())
            self.assertEqual(stat.S_IMODE(temporary.stat().st_mode), 0o600)
            self.write_key(value=winner)
            return original_link(source, target, **kwargs)
        with patch('cfspeed.crypto.os.link', side_effect=race):
            store = SecretStore(self.directory)
            store.unlock(allow_create=True)
        self.assertEqual(len(observed), 1)
        self.assertEqual(len(observed[0]), KEY_BYTES)
        self.assertEqual(self.key_path.read_bytes(), winner)
        self.assertEqual(list(self.directory.iterdir()), [self.key_path])

    def test_running_store_detects_removed_replaced_or_chmod_key(self):
        store = SecretStore(self.directory)
        store.unlock(allow_create=True)
        key = self.key_path.read_bytes()
        for failure in ('removed', 'replaced', 'permissions'):
            with self.subTest(failure=failure):
                self.write_key(value=key)
                if failure == 'removed':
                    self.key_path.unlink()
                elif failure == 'replaced':
                    self.write_key()
                else:
                    self.key_path.chmod(0o644)
                with self.assertRaises(CryptoError):
                    store.encrypt(VALUES, {})
                with self.assertRaises(CryptoError):
                    store.unlock(allow_create=True)
                if failure == 'removed':
                    self.assertFalse(self.key_path.exists())


class EnvelopeTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.store = SecretStore(self.directory)
        self.store.unlock(allow_create=True)
        self.context = {'version': 2, 'revision': 5, 'service': {'dry_run': True}, 'targets': [],
                        'auth': {'hash': 'fixture-hash'}}
        self.envelope = self.store.encrypt(VALUES, self.context)

    def test_whole_map_roundtrip_random_nonces_and_restart(self):
        self.assertEqual(self.envelope['version'], 1)
        self.assertEqual(self.envelope['algorithm'], ALGORITHM)
        encoded = json.dumps(self.envelope, ensure_ascii=False)
        for name, value in VALUES.items():
            self.assertNotIn(name, encoded)
            if value:
                self.assertNotIn(value, encoded)
        self.assertEqual(self.store.decrypt(self.envelope, self.context), VALUES)
        self.assertEqual(SecretStore(self.directory).decrypt(self.envelope, self.context), VALUES)
        another = self.store.encrypt(VALUES, self.context)
        self.assertNotEqual(another['nonce'], self.envelope['nonce'])
        self.assertNotEqual(another['ciphertext'], self.envelope['ciphertext'])
        self.assertEqual(len(base64.b64decode(another['nonce'])), 12)
        empty = self.store.encrypt({}, self.context)
        self.assertEqual(self.store.decrypt(empty, self.context), {})

    def test_ciphertext_tag_and_nonce_tampering_fail_authentication(self):
        for field, position in (('ciphertext', 0), ('ciphertext', -1), ('nonce', 0)):
            with self.subTest(field=field, position=position):
                changed = copy.deepcopy(self.envelope)
                value = bytearray(base64.b64decode(changed[field]))
                value[position] ^= 1
                changed[field] = base64.b64encode(value).decode()
                with self.assertRaises(CryptoError):
                    self.store.decrypt(changed, self.context)

    def test_outer_metadata_authenticated_and_key_order_irrelevant(self):
        for field, value in (('revision', 6), ('version', 1), ('service', {'dry_run': False}),
                             ('targets', ['changed']), ('auth', {'hash': 'replaced'})):
            with self.subTest(field=field), self.assertRaises(CryptoError):
                self.store.decrypt(self.envelope, {**self.context, field: value})
        reordered = dict(reversed(list(self.context.items())))
        self.assertEqual(self.store.decrypt(self.envelope, reordered), VALUES)

    def test_unknown_and_malformed_envelopes_fail_closed(self):
        invalid = [None, {}, [], VALUES, {**self.envelope, 'version': 2},
                   {**self.envelope, 'version': True}, {**self.envelope, 'algorithm': 'AES-128-GCM'},
                   {**self.envelope, 'key': 'no-inline-key'}, {**self.envelope, 'nonce': '!invalid'},
                   {**self.envelope, 'nonce': 'AAAA'}, {**self.envelope, 'nonce': 42},
                   {**self.envelope, 'ciphertext': ''}, {**self.envelope, 'ciphertext': None},
                   {**self.envelope, 'ciphertext': 'x' * (MAX_SECRET_BYTES * 2)}]
        for value in invalid:
            with self.subTest(value_type=type(value).__name__), self.assertRaises(CryptoError):
                self.store.decrypt(value, self.context)

    def test_wrong_key_never_falls_back_or_resets(self):
        self.write_key()
        before = self.key_path.read_bytes()
        with self.assertRaises(CryptoError):
            SecretStore(self.directory).decrypt(self.envelope, self.context)
        self.assertEqual(self.key_path.read_bytes(), before)

    def test_missing_key_never_regenerated_for_encrypted_empty_map(self):
        envelope = self.store.encrypt({}, self.context)
        self.key_path.unlink()
        with self.assertRaises(CryptoError):
            SecretStore(self.directory).decrypt(envelope, self.context)
        self.assertFalse(self.key_path.exists())

    def test_oversized_plaintext_rejected(self):
        with self.assertRaises(CryptoError):
            self.store.encrypt({'TEST_TOKEN': 'x' * MAX_SECRET_BYTES}, self.context)


class RunnerFixture:
    """No HTTP/providers: record only fully persisted configuration applications."""
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.admin = None
        self.calls = []
        self.on_configure: Callable | None = None

    def configure(self, config):
        if self.on_configure:
            self.on_configure(config)
        self.calls.append(config)
        self.config = config


class StorageTests(IsolatedCase):
    @classmethod
    def setUpClass(cls):
        cls.auth = new_auth(PASSWORD)

    def setUp(self):
        super().setUp()
        self.base = Config(state_dir=str(self.directory))
        self.path = self.directory / 'admin.json'
        self.runner = RunnerFixture(self.base)

    def bootstrap(self):
        return Admin(self.runner, bootstrap_password=PASSWORD)

    def legacy(self):
        data = {'version': 1, 'revision': 7, 'service': {'dry_run': True, 'max_ips': 23},
                'targets': [], 'secrets': copy.deepcopy(VALUES), 'auth': copy.deepcopy(self.auth)}
        atomic_private(self.path, (json.dumps(data) + '\n').encode())
        return data

    def assert_encrypted(self):
        raw = self.path.read_bytes()
        self.assertNotIn(TOKEN.encode(), raw)
        self.assertNotIn(b'TEST_TOKEN', raw)
        saved = json.loads(raw)
        self.assertEqual(saved['version'], 2)
        self.assertEqual(saved['secrets']['algorithm'], ALGORITHM)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(saved['auth']['algorithm'], 'pbkdf2-sha256')
        return saved

    def test_fresh_bootstrap_encrypted_empty_map_hash_only_password(self):
        admin = self.bootstrap()
        saved = self.assert_encrypted()
        self.assertNotIn(PASSWORD, self.path.read_text())
        self.assertEqual(admin.data['secrets'], {})
        self.assertNotEqual(saved['secrets'], {})
        self.assertEqual(admin.view()['targets'], [])
        self.assertTrue(self.runner.config.dry_run)
        self.assertEqual(len(self.runner.calls), 1)

    def test_save_restart_no_plaintext_file_temp_view_or_logs(self):
        admin = self.bootstrap()
        seen = []
        original_replace = os.replace
        def inspect(source, destination, **kwargs):
            payload = Path(source).read_bytes()
            seen.append(payload)
            self.assertNotIn(TOKEN.encode(), payload)
            self.assertNotIn(b'TEST_TOKEN', payload)
            self.assertEqual(stat.S_IMODE(Path(source).stat().st_mode), 0o600)
            return original_replace(source, destination, **kwargs)
        logs = io.StringIO()
        handler = logging.StreamHandler(logs)
        logging.getLogger().addHandler(handler)
        try:
            with patch('cfspeed.admin.os.replace', side_effect=inspect):
                view = admin.update({'revision': 1, 'secrets': VALUES})
        finally:
            logging.getLogger().removeHandler(handler)
        self.assertEqual(len(seen), 2)  # transaction marker plus encrypted configuration
        self.assertNotIn(TOKEN, logs.getvalue())
        self.assertNotIn(TOKEN, json.dumps(view, ensure_ascii=False))
        self.assertEqual(view['credentials'], [
            {'name': 'TEST_CLEARED', 'configured': False}, {'name': 'TEST_TOKEN', 'configured': True},
            {'name': 'TEST_UNICODE', 'configured': True}])
        self.assert_encrypted()
        for path in self.directory.iterdir():
            if path.is_file():
                self.assertNotIn(TOKEN.encode(), path.read_bytes(), path.name)
        before = self.path.read_bytes()
        restart = RunnerFixture(self.base)
        again = Admin(restart)
        again.login({'username': 'admin', 'password': PASSWORD})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(restart.config.credential_values, VALUES)
        self.assertEqual(load_saved_config(self.base).credential_values, VALUES)
        self.assertNotIn(TOKEN, repr(restart.config))
        self.assertEqual(list(self.directory.glob('.admin-*')), [])

    def test_legacy_migrates_before_runner_can_use_credentials(self):
        legacy = self.legacy()
        observed = []
        def check(config):
            observed.append(self.assert_encrypted())
            self.assertEqual(config.credential_values, VALUES)
        self.runner.on_configure = check
        admin = Admin(self.runner, bootstrap_password='must-not-replace-password')
        self.assertEqual(len(observed), 1)
        self.assertEqual(admin.data['revision'], legacy['revision'])
        self.assertEqual(observed[0]['auth'], legacy['auth'])
        self.assertEqual(admin.data['version'], 2)
        admin.login({'username': 'admin', 'password': PASSWORD})
        self.assertFalse(admin.initial_path.exists())

    def test_load_saved_config_migrates_and_preserves_preview_overlay(self):
        legacy = self.legacy()
        config = load_saved_config(self.base, force_preview=True)
        saved = self.assert_encrypted()
        self.assertEqual(saved['revision'], legacy['revision'])
        self.assertEqual(saved['auth'], legacy['auth'])
        self.assertEqual(config.credential_values, VALUES)
        self.assertEqual(config.max_ips, 23)
        self.assertTrue(config.dry_run)
        self.assertFalse((self.directory / 'initial-admin-password.txt').exists())

    def test_migration_save_failure_does_not_return_or_configure_plaintext(self):
        for loader in ('admin', 'load'):
            with self.subTest(loader=loader):
                self.legacy()
                before = self.path.read_bytes()
                original_replace = os.replace
                temporary_payloads = []
                def fail(source, destination, **kwargs):
                    if Path(destination) == self.path:
                        temporary_payloads.append(Path(source).read_bytes())
                        raise OSError('fixture atomic replace failure')
                    return original_replace(source, destination, **kwargs)
                with patch('cfspeed.admin.os.replace', side_effect=fail):
                    with self.assertRaises((AppError, OSError)):
                        Admin(self.runner) if loader == 'admin' else load_saved_config(self.base)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.runner.calls, [])
                self.assertEqual(self.runner.config.credential_values, {})
                self.assertEqual(len(temporary_payloads), 1)
                self.assertNotIn(TOKEN.encode(), temporary_payloads[0])
                self.assertEqual(list(self.directory.glob('.admin-*')), [])
                # The complete orphan key is deliberately reusable on retry.
                load_saved_config(self.base)
                self.assert_encrypted()

    def test_existing_key_reused_for_migration(self):
        self.legacy()
        self.write_key()
        before = self.key_path.read_bytes()
        load_saved_config(self.base)
        self.assertEqual(self.key_path.read_bytes(), before)
        self.assert_encrypted()

    def test_cli_check_migrates_and_restart_missing_key_has_no_secret_output(self):
        self.legacy()
        config_file = self.directory / 'config.toml'
        config_file.write_text('[service]\nstate_dir = ' + json.dumps(str(self.directory)) + '\n')
        command = [sys.executable, '-m', 'cfspeed', 'check', '--config', str(config_file)]
        options = {'cwd': Path(__file__).resolve().parents[1], 'capture_output': True,
                   'text': True, 'timeout': 20}
        migrated = subprocess.run(command, **options)
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.assertEqual(json.loads(migrated.stdout), {'valid': True, 'mode': 'preview', 'targets': 0})
        self.assert_encrypted()
        self.assertEqual(subprocess.run(command, **options).returncode, 0)
        saved = self.path.read_bytes()
        self.key_path.unlink()
        missing = subprocess.run(command, **options)
        self.assertEqual(missing.returncode, 1)
        self.assertEqual(missing.stdout, '')
        self.assertIn('主密钥', missing.stderr)
        self.assertNotIn(TOKEN, migrated.stdout + migrated.stderr + missing.stdout + missing.stderr)
        self.assertEqual(self.path.read_bytes(), saved)
        self.assertFalse(self.key_path.exists())

    def test_mounted_key_remains_outside_state_directory_on_bootstrap_and_restart(self):
        state = self.directory / 'state'
        state.mkdir()
        self.base = Config(state_dir=str(state))
        self.path = state / 'admin.json'
        self.runner = RunnerFixture(self.base)
        external = self.write_key(self.directory / 'mounted-master-key')
        original = external.read_bytes()
        with patch.dict(os.environ, {KEY_ENV: str(external)}):
            admin = self.bootstrap()
            admin.update({'revision': 1, 'secrets': VALUES})
            self.assertEqual(Admin(RunnerFixture(self.base)).data['secrets'], VALUES)
            self.assertEqual(load_saved_config(self.base).credential_values, VALUES)
        self.assertEqual(external.read_bytes(), original)
        self.assertFalse((state / DEFAULT_KEY_FILENAME).exists())
        self.assert_encrypted()
        self.assertNotIn(base64.b64encode(original), self.path.read_bytes())
        self.assertNotIn(original.hex().encode(), self.path.read_bytes())

    def test_explicit_missing_key_blocks_migration_and_bootstrap(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                if legacy:
                    self.legacy()
                before = self.path.read_bytes() if legacy else None
                with patch.dict(os.environ, {KEY_ENV: str(self.directory / 'not-provisioned')}):
                    with self.assertRaises(CryptoError):
                        Admin(self.runner)
                self.assertFalse(self.key_path.exists())
                self.assertFalse((self.directory / 'not-provisioned').exists())
                self.assertFalse((self.directory / 'initial-admin-password.txt').exists())
                self.assertEqual(self.runner.calls, [])
                if legacy:
                    self.assertEqual(self.path.read_bytes(), before)
                else:
                    self.assertFalse(self.path.exists())

    def test_missing_wrong_or_unsafe_key_restart_and_cli_load_fail_closed(self):
        admin = self.bootstrap()
        admin.update({'revision': 1, 'secrets': VALUES})
        original_key = self.key_path.read_bytes()
        saved = self.path.read_bytes()
        initial = admin.initial_path.read_bytes()
        for failure in ('missing', 'wrong', 'truncated', 'permissions'):
            with self.subTest(failure=failure):
                self.write_key(value=original_key)
                if failure == 'missing':
                    self.key_path.unlink()
                elif failure == 'wrong':
                    self.write_key()
                elif failure == 'truncated':
                    self.write_key(value=b'broken-key')
                else:
                    self.key_path.chmod(0o644)
                restart = RunnerFixture(self.base)
                with self.assertRaises(CryptoError):
                    Admin(restart, bootstrap_password='ignored-other-password')
                with self.assertRaises(CryptoError):
                    load_saved_config(self.base)
                self.assertEqual(restart.calls, [])
                self.assertEqual(restart.config.credential_values, {})
                self.assertEqual(self.path.read_bytes(), saved)
                self.assertEqual(admin.initial_path.read_bytes(), initial)
                if failure == 'missing':
                    self.assertFalse(self.key_path.exists())

    def test_bad_legacy_data_never_generates_key_or_overwrites(self):
        legacy = self.legacy()
        for changed in ({**legacy, 'version': True}, {**legacy, 'version': 99},
                        {**legacy, 'secrets': {'BAD-NAME': TOKEN}},
                        {**legacy, 'service': {'max_ips': -1}}, {**legacy, 'auth': {}}):
            with self.subTest(field=list(changed)):
                self.path.write_text(json.dumps(changed))
                before = self.path.read_bytes()
                with self.assertRaises(AppError):
                    load_saved_config(self.base)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertFalse(self.key_path.exists())

    def test_envelope_or_outer_fields_tampered_reject_all_load_paths(self):
        admin = self.bootstrap()
        admin.update({'revision': 1, 'secrets': VALUES})
        saved = json.loads(self.path.read_bytes())
        cases = [{**saved, 'secrets': {}}, {**saved, 'revision': 3},
                 {**saved, 'auth': self.auth}, {**saved, 'service': {'dry_run': False}},
                 {**saved, 'version': 1}, {**saved, 'secrets': {'TEST_TOKEN': TOKEN}}]
        for changed in cases:
            with self.subTest(changed_fields=[k for k in saved if saved[k] != changed[k]]):
                self.path.write_text(json.dumps(changed))
                before = self.path.read_bytes()
                with self.assertRaises(AppError):
                    load_saved_config(self.base)
                runner = RunnerFixture(self.base)
                with self.assertRaises(AppError):
                    Admin(runner)
                self.assertEqual(runner.calls, [])
                self.assertEqual(self.path.read_bytes(), before)

    def test_symlink_dangling_fifo_oversize_and_duplicate_admin_json_refused(self):
        self.bootstrap()
        saved = self.path.read_bytes()
        self.path.unlink()
        target = self.directory / 'real-admin.json'
        target.write_bytes(saved)
        for destination in (target, self.directory / 'absent'):
            self.path.symlink_to(destination)
            with self.assertRaises(AppError):
                load_saved_config(self.base)
            with self.assertRaises(AppError):
                Admin(RunnerFixture(self.base))
            self.assertTrue(self.path.is_symlink())
            self.path.unlink()
        os.mkfifo(self.path, 0o600)
        with self.assertRaises(AppError):
            load_saved_config(self.base)
        self.path.unlink()
        for raw in (b'x' * (MAX_CONFIG_BYTES + 1), b'{"version":1,"version":2}',
                    b'{"version":NaN}', b'\xff', b'[' * 2000):
            self.path.write_bytes(raw)
            with self.assertRaises(AppError):
                load_saved_config(self.base)

    def test_no_saved_config_does_not_bootstrap_or_generate_key(self):
        self.assertIs(load_saved_config(self.base), self.base)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_errors_and_exception_strings_do_not_include_key_or_token(self):
        admin = self.bootstrap()
        admin.update({'revision': 1, 'secrets': VALUES})
        key = self.key_path.read_bytes()
        self.write_key()
        with self.assertRaises(CryptoError) as raised:
            load_saved_config(self.base)
        rendered = str(raised.exception) + repr(raised.exception)
        for sensitive in (TOKEN, key.hex(), base64.b64encode(key).decode(), str(self.directory)):
            self.assertNotIn(sensitive, rendered)

    def test_password_change_reencrypts_preserving_secrets_and_restart_hash(self):
        admin = self.bootstrap()
        admin.update({'revision': 1, 'secrets': VALUES})
        before = self.assert_encrypted()
        replacement = 'crypto-new-isolated-password'
        admin.change_password({'current_password': PASSWORD, 'new_password': replacement})
        after = self.assert_encrypted()
        self.assertNotEqual(before['secrets'], after['secrets'])
        self.assertNotEqual(before['auth']['hash'], after['auth']['hash'])
        self.assertNotIn(replacement, self.path.read_text())
        again = Admin(RunnerFixture(self.base))
        again.login({'username': 'admin', 'password': replacement})
        self.assertEqual(again.data['secrets'], VALUES)

    def test_runtime_configure_failure_rolls_back_saved_ciphertext_and_memory(self):
        admin = self.bootstrap()
        admin.update({'revision': 1, 'secrets': VALUES})
        before = self.path.read_bytes()
        prior_config = self.runner.config
        prior_data = copy.deepcopy(admin.data)
        def fail(config):
            # The candidate is encrypted before runtime application is attempted.
            self.assertEqual(json.loads(self.path.read_bytes())['revision'], 3)
            self.assertNotIn(TOKEN.encode(), self.path.read_bytes())
            raise OSError('fixture runtime state failure')
        self.runner.on_configure = fail
        with self.assertRaises(OSError):
            admin.update({'revision': 2, 'service': {'max_ips': 9}})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(admin.data, prior_data)
        self.assertIs(self.runner.config, prior_config)
        self.assertFalse(self.runner.lock.locked())
        self.assertEqual(load_saved_config(self.base).max_ips, prior_config.max_ips)

    def test_rollback_failure_stops_work_and_forces_preview(self):
        admin = self.bootstrap()
        previous = atomic_private
        calls = []
        def fail_rollback(path, data):
            if path == self.path:
                calls.append(path)
            if len(calls) == 2:
                raise OSError('fixture rollback storage failure')
            return previous(path, data)
        self.runner.on_configure = lambda config: (_ for _ in ()).throw(OSError('fixture state failure'))
        with patch('cfspeed.admin.atomic_private', side_effect=fail_rollback), self.assertRaises(AppError):
            admin.update({'revision': 1, 'secrets': VALUES})
        self.assertTrue(self.runner.stop.is_set())
        self.assertTrue(self.runner.config.dry_run)
        self.assertEqual(admin.data['revision'], 1)
        self.assertEqual(admin.sessions, {})

    def test_real_runtime_state_save_failure_rolls_back_admin_and_schedule(self):
        runner = Runner(self.base, State(self.directory))
        admin = Admin(runner, bootstrap_password=PASSWORD)
        prior = admin.path.read_bytes()
        state = runner.state.path.read_bytes()
        config = runner.config
        next_due = runner.next_due
        with patch.object(runner.state, 'save', side_effect=OSError('fixture disk full')):
            with self.assertRaises(OSError):
                admin.update({'revision': 1, 'service': {'interval_seconds': 90}, 'secrets': VALUES})
        self.assertEqual(admin.path.read_bytes(), prior)
        self.assertEqual(runner.state.path.read_bytes(), state)
        self.assertIs(runner.config, config)
        self.assertEqual(runner.next_due, next_due)
        self.assertEqual(admin.data['revision'], 1)
        self.assertEqual(admin.data['secrets'], {})
        self.assertFalse(runner.lock.locked())
        self.assertEqual(load_saved_config(self.base).credential_values, {})

    def test_key_loss_during_update_does_not_commit_new_secrets(self):
        admin = self.bootstrap()
        before = self.path.read_bytes()
        self.key_path.unlink()
        with self.assertRaises(CryptoError):
            admin.update({'revision': 1, 'secrets': VALUES})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(admin.data['secrets'], {})
        self.assertEqual(self.runner.config.credential_values, {})
        self.assertFalse(self.key_path.exists())


if __name__ == '__main__':
    unittest.main()
