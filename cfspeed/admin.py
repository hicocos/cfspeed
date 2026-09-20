"""Private management configuration and bounded, in-memory administrator sessions."""
from dataclasses import replace
from pathlib import Path
import copy
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time

from .config import AppError, BUILTIN_SOURCE_URLS, env_name, integer, keys, parse_config
from .crypto import CryptoError, SecretStore, read_regular_file, strict_json

SERVICE_FIELDS = ('source_url', 'interval_seconds', 'timeout_seconds', 'attempts',
                  'dry_run', 'max_ips', 'pushplus_token_env')
COOKIE = 'cfspeed_session'
MAX_CONFIG_BYTES = 524288  # Includes base64/tag overhead for the encrypted map.
MAX_PLAINTEXT_CONFIG_BYTES = 262144
PASSWORD_ROUNDS = 600000
SESSION_SECONDS = 43200


class AdminError(AppError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def atomic_private(path, data):
    """Replace and fsync a private file; never leave credentials in a public temp file."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.admin-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def target_view(target):
    fields = ['label', 'provider', 'name']
    fields += (['zone_id', 'token_env'] if target.provider == 'cloudflare' else
               ['domain', 'line_id', 'secret_id_env', 'secret_key_env'])
    return {name: getattr(target, name) for name in fields}


def password_valid(password):
    if not isinstance(password, str) or not 8 <= len(password) <= 256 or any(ord(c) < 32 or ord(c) == 127 for c in password):
        raise AdminError('密码必须为 8–256 个字符，且不能包含控制字符')


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), PASSWORD_ROUNDS).hex()


def new_auth(password):
    password_valid(password)
    salt = secrets.token_hex(32)
    return {'username': 'admin', 'algorithm': 'pbkdf2-sha256', 'rounds': PASSWORD_ROUNDS,
            'salt': salt, 'hash': password_hash(password, salt)}


def read_admin_data(path, *, store=None):
    """Validate/decrypt saved data; callers must persist v1 migration before use."""
    path = Path(path)
    try:
        raw = read_regular_file(path, MAX_CONFIG_BYTES)
        data = strict_json(raw)
        keys(data, ('version', 'revision', 'service', 'targets', 'secrets', 'auth'), 'admin')
        if type(data['version']) is not int or data['version'] not in (1, 2):
            raise ValueError()
        if data['version'] == 1 and len(raw) > MAX_PLAINTEXT_CONFIG_BYTES:
            raise ValueError()
        integer(data['revision'], 1, 2**53 - 1, 'revision')
        auth = data['auth']
        keys(auth, ('username', 'algorithm', 'rounds', 'salt', 'hash'), 'auth')
        if (auth['username'] != 'admin' or auth['algorithm'] != 'pbkdf2-sha256' or
                type(auth['rounds']) is not int or auth['rounds'] != PASSWORD_ROUNDS or
                len(bytes.fromhex(auth['salt'])) != 32 or
                len(bytes.fromhex(auth['hash'])) != 32):
            raise ValueError()
        keys(data['service'], (*SERVICE_FIELDS, 'history_retention_days'), 'service')
        if data['version'] == 2:
            store = store or SecretStore(path.parent)
            data['secrets'] = store.decrypt(data['secrets'], {k: v for k, v in data.items() if k != 'secrets'})
        Admin._secrets(data['secrets'])
        # Authenticate the original metadata before discarding the retired setting.
        # Read compatibility only: never rewrite credentials/configuration on startup.
        data['service'].pop('history_retention_days', None)
        return data
    except CryptoError:
        raise
    except (ValueError, TypeError, KeyError, OSError, AppError, RecursionError):
        raise AppError('管理配置不可读或损坏，请备份检查；拒绝重置密码或静默覆盖') from None


def persist_admin_data(path, data, store):
    """Encrypt before any file is opened; even temporary files contain ciphertext."""
    context = {k: v for k, v in data.items() if k != 'secrets'}
    context['version'] = 2
    encrypted = {**context, 'secrets': store.encrypt(data['secrets'], context)}
    payload = (json.dumps(encrypted, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
    if len(payload) > MAX_CONFIG_BYTES:
        raise AdminError('管理配置超过大小限制')
    atomic_private(path, payload)


def migrate_admin_data(path, data, store):
    if data['version'] == 1:
        store.unlock(allow_create=True)
        persist_admin_data(path, data, store)
        data['version'] = 2


def effective_config(base, data, force_preview=False):
    service = {name: getattr(base, name) for name in base.__dataclass_fields__
               if name not in ('targets', 'credential_values')}
    service.update(data['service'])
    if force_preview:
        service['dry_run'] = True
    config = parse_config({'service': service, 'targets': data['targets']})
    return replace(config, credential_values=dict(data['secrets']))


def load_saved_config(base, *, force_preview=False):
    """Read without bootstrap/sessions; migrate plaintext atomically before use."""
    path = Path(base.state_dir) / 'admin.json'
    if not os.path.lexists(path):
        return base
    store = SecretStore(path.parent)
    data = read_admin_data(path, store=store)
    config = effective_config(base, data, force_preview or os.path.lexists(path.parent / 'config-update.pending'))
    migrate_admin_data(path, data, store)
    return config


class Admin:
    def __init__(self, runner, *, bootstrap_password=None, force_preview=False):
        self.runner = runner
        self.base = runner.config
        self.force_preview = force_preview
        self.path = Path(self.base.state_dir) / 'admin.json'
        self.initial_path = self.path.parent / 'initial-admin-password.txt'
        self.pending_path = self.path.parent / 'config-update.pending'
        self.secret_store = SecretStore(self.path.parent)
        self.lock = threading.RLock()
        self.sessions = {}
        self.login_attempts = {}
        self.password_attempts = []
        self.login_slots = threading.BoundedSemaphore(2)
        if os.path.lexists(self.path):
            try:
                self.data = read_admin_data(self.path, store=self.secret_store)
                config = self._config(self.data)
                migrate_admin_data(self.path, self.data, self.secret_store)
                if os.path.lexists(self.pending_path):
                    # A failed/ interrupted commit must never arm unacknowledged
                    # live settings after Docker automatically restarts us.
                    self.data['service']['dry_run'] = True
                    self.data['revision'] += 1
                    self._persist(self.data)
                    config = self._config(self.data)
                    self.pending_path.unlink()
                os.chmod(self.path, 0o600)
            except CryptoError:
                raise
            except (ValueError, TypeError, KeyError, OSError, AppError):
                raise AppError('管理配置不可读或损坏，请备份检查；拒绝重置密码或静默覆盖') from None
        else:
            self.secret_store.unlock(allow_create=True)
            # Reuse a private orphan bootstrap file after interruption between the two writes.
            if os.path.lexists(self.initial_path):
                try:
                    password = read_regular_file(self.initial_path, 1024, mode=0o600).decode().strip('\n')
                except (OSError, ValueError):
                    raise AppError('初始密码文件无效，拒绝覆盖') from None
            else:
                password = bootstrap_password if bootstrap_password is not None else secrets.token_urlsafe(24)
                password_valid(password)
                atomic_private(self.initial_path, (password + '\n').encode())
            os.chmod(self.initial_path, 0o600)
            self.data = {'version': 2, 'revision': 1,
                         'service': {name: getattr(self.base, name) for name in SERVICE_FIELDS if name != 'source_url'},
                         'targets': [target_view(t) for t in self.base.targets], 'secrets': {}, 'auth': new_auth(password)}
            self._persist(self.data)
            config = self._config(self.data)
        runner.configure(config)
        runner.admin = self

    @staticmethod
    def _secrets(values):
        if not isinstance(values, dict) or len(values) > 128:
            raise AdminError('凭据项最多为 128 个')
        for name, value in values.items():
            env_name(name, 'credential')
            if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 4096 or
                                      value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                raise AdminError('凭据必须为非空、无首尾空白或控制字符的字符串；null 表示清除')

    def _config(self, data):
        return effective_config(self.base, data, self.force_preview)

    def _persist(self, data):
        persist_admin_data(self.path, data, self.secret_store)

    def view(self):
        with self.lock:
            config = self.runner.config
            names = set(self.data['secrets'])
            for target in config.targets:
                names.update(target.credential_names())
            if config.pushplus_token_env:
                names.add(config.pushplus_token_env)
            return {'revision': self.data['revision'],
                    'service': {name: getattr(config, name) for name in SERVICE_FIELDS},
                    'targets': [target_view(target) for target in config.targets],
                    'credentials': [{'name': name, 'configured': bool(config.credential(name))} for name in sorted(names)]}

    def update(self, payload):
        keys(payload, ('revision', 'service', 'targets', 'secrets', 'confirm_apply'), 'config')
        if not self.runner.lock.acquire(blocking=False):
            raise AdminError('同步任务已经在运行，配置未保存', 409)
        try:
            with self.lock:
                if type(payload.get('revision')) is not int:
                    raise AdminError('必须提供整数 revision')
                if payload['revision'] != self.data['revision']:
                    raise AdminError('配置已被修改，请刷新后重试', 409)
                service = payload.get('service', {})
                keys(service, SERVICE_FIELDS, 'service')
                if 'source_url' in service and service['source_url'] not in (*BUILTIN_SOURCE_URLS, self.base.source_url):
                    raise AdminError('source_url: 请选择内置来源或服务器配置来源')
                if 'confirm_apply' in payload and type(payload['confirm_apply']) is not bool:
                    raise AdminError('confirm_apply 必须为布尔值')
                if 'dry_run' in service and type(service['dry_run']) is not bool:
                    raise AdminError('dry_run 必须为布尔值')
                if service.get('dry_run') is False:
                    if payload.get('confirm_apply') is not True:
                        raise AdminError('关闭预览必须显式确认 confirm_apply=true')
                    if self.force_preview:
                        raise AdminError('命令行 --dry-run 强制预览，不能在 Web 中关闭')
                candidate = copy.deepcopy(self.data)
                candidate['service'].update(service)
                if 'targets' in payload:
                    candidate['targets'] = payload['targets']
                updates = payload.get('secrets', {})
                self._secrets(updates)
                candidate['secrets'].update(updates)
                self._secrets(candidate['secrets'])
                # Editing any scope/credential must not cause unconfirmed scheduled writes.
                scope_changed = (('source_url' in service and service['source_url'] != self.runner.config.source_url)
                                 or ('targets' in payload and candidate['targets'] != self.data['targets']) or bool(updates)
                                 or ('pushplus_token_env' in service and service['pushplus_token_env'] != self.runner.config.pushplus_token_env))
                if scope_changed and payload.get('confirm_apply') is not True:
                    candidate['service']['dry_run'] = True
                config = self._config(candidate)
                used = {name for target in config.targets for name in target.credential_names()}
                if config.pushplus_token_env:
                    used.add(config.pushplus_token_env)
                if any(updates.get(name, '') is None for name in used):
                    raise AdminError('不能清除正在使用的凭据；请先移除相应目标或通知配置')
                # Allow incomplete preview setup, but never arm a schedule with missing credentials.
                if not config.dry_run:
                    config.validate_credentials()
                candidate['revision'] += 1
                # Keep the prior encrypted bytes for rollback: configure() can fail
                # while persisting runtime state, after admin.json was replaced.
                previous = read_regular_file(self.path, MAX_CONFIG_BYTES)
                previous_config = self.runner.config
                atomic_private(self.pending_path, b'configuration transaction pending\n')
                try:
                    self._persist(candidate)
                    self.runner.configure(config)
                    self.pending_path.unlink()
                except Exception:
                    self.runner.config = previous_config
                    try:
                        atomic_private(self.path, previous)
                        self.pending_path.unlink(missing_ok=True)
                    except OSError:
                        # If durability cannot be restored, stop scheduled work
                        # rather than run against an unacknowledged configuration.
                        self.runner.config = replace(previous_config, dry_run=True)
                        self.runner.stop.set()
                        self.sessions.clear()
                        raise AppError('配置保存失败且无法回滚；服务已停止同步，请检查存储后重启') from None
                    raise
                self.data = candidate
                return self.view()
        finally:
            self.runner.lock.release()

    def _prune_sessions(self):
        current = time.monotonic()
        self.sessions = {key: item for key, item in self.sessions.items() if item['expires'] > current}

    def session(self, cookie):
        with self.lock:
            self._prune_sessions()
            if not isinstance(cookie, str) or len(cookie) > 128:
                return None
            return self.sessions.get(hashlib.sha256(cookie.encode()).hexdigest())

    @staticmethod
    def session_view(session):
        return {'authenticated': bool(session), 'username': 'admin' if session else '',
                'csrf': session['csrf'] if session else ''}

    def _check_password(self, password):
        # Bound input before running an expensive KDF, even when called outside HTTP.
        if not isinstance(password, str) or not 1 <= len(password) <= 256:
            return False
        auth = self.data['auth']
        return hmac.compare_digest(password_hash(password, auth['salt']), auth['hash'])

    def login(self, payload, client='local'):
        keys(payload, ('username', 'password'), 'login')
        if not self.login_slots.acquire(blocking=False):
            raise AdminError('登录验证繁忙，请稍后重试', 429)
        try:
            return self._login(payload, client)
        finally:
            self.login_slots.release()

    def _login(self, payload, client):
        with self.lock:
            current = time.monotonic()
            self.login_attempts = {key: [stamp for stamp in stamps if current - stamp < 60]
                                   for key, stamps in self.login_attempts.items() if stamps and current - stamps[-1] < 60}
            if client not in self.login_attempts and len(self.login_attempts) >= 1024:
                raise AdminError('登录验证繁忙，请稍后重试', 429)
            attempts = self.login_attempts.setdefault(client, [])
            if len(attempts) >= 10:
                raise AdminError('登录尝试过于频繁，请稍后重试', 429)
            attempts.append(current)
            valid = self._check_password(payload.get('password'))
            if not valid or payload.get('username') != 'admin':
                raise AdminError('用户名或密码不正确', 401)
            self._prune_sessions()
            if len(self.sessions) >= 64:
                del self.sessions[next(iter(self.sessions))]
            token = secrets.token_urlsafe(32)
            session = {'csrf': secrets.token_urlsafe(32), 'expires': current + SESSION_SECONDS}
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = session
            return token, self.session_view(session)

    def logout(self, token):
        with self.lock:
            self.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)

    def change_password(self, payload):
        keys(payload, ('current_password', 'new_password'), 'password')
        password_valid(payload.get('new_password'))
        with self.lock:
            current = time.monotonic()
            self.password_attempts = [stamp for stamp in self.password_attempts if current - stamp < 60]
            if len(self.password_attempts) >= 10:
                raise AdminError('密码尝试过于频繁，请稍后重试', 429)
            self.password_attempts.append(current)
            if not self._check_password(payload.get('current_password')):
                raise AdminError('当前密码不正确', 401)
            candidate = copy.deepcopy(self.data)
            candidate['auth'] = new_auth(payload['new_password'])
            previous = read_regular_file(self.path, MAX_CONFIG_BYTES)
            try:
                self._persist(candidate)
            except Exception:
                try:
                    atomic_private(self.path, previous)
                except OSError:
                    self.sessions.clear()
                    self.runner.stop.set()
                    raise AppError('密码保存失败且无法回滚，服务已停止；请检查存储') from None
                raise
            self.data = candidate
            self.sessions.clear()
            self.initial_path.unlink(missing_ok=True)
