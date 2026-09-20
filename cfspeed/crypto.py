"""Authenticated saved-secret storage; master keys never enter configuration JSON.

Keys are exactly 32 random bytes in a separate 0600 regular file. An explicitly
configured key is provisioned out of band; only the native default may be created
on bootstrap or migration. Envelopes use independent 96-bit nonces and authenticate
both their format and the surrounding non-secret administrator configuration.
"""
from contextlib import contextmanager
from pathlib import Path
import base64
import binascii
import hmac
import json
import os
import stat

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import AppError

KEY_ENV = 'CFSPEED_MASTER_KEY_FILE'
DEFAULT_KEY_FILENAME = 'master.key'
KEY_BYTES = 32
NONCE_BYTES = 12
MAX_SECRET_BYTES = 262144
ENVELOPE_VERSION = 1
ALGORITHM = 'AES-256-GCM'
AAD_DOMAIN = b'cfspeed/admin/secrets\x00v1\x00AES-256-GCM\x00'


class CryptoError(AppError):
    """Safe operational errors: never include paths, key bytes or ciphertext."""


@contextmanager
def private_directory(path):
    """Open the directory without following any symlink path components."""
    path = Path(os.path.abspath(path))
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def read_regular_file(path, limit, *, mode=None):
    """Bound the actual read, reject non-regular files, and avoid stat/open races."""
    path = Path(path)
    with private_directory(path.parent) as directory:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=directory)
        with os.fdopen(fd, 'rb', buffering=0) as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_size > limit or
                    (mode is not None and stat.S_IMODE(info.st_mode) != mode)):
                raise ValueError('unsafe file')
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise ValueError('file exceeds limit')
            return data


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('duplicate JSON field')
        result[name] = value
    return result


def _invalid_constant(_value):
    raise ValueError('invalid JSON constant')


def strict_json(data):
    return json.loads(data, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def _aad(context):
    return AAD_DOMAIN + _json_bytes(context)


def _decode(value, maximum):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError('invalid envelope')
    decoded = base64.b64decode(value, validate=True)
    if base64.b64encode(decoded).decode('ascii') != value:
        raise ValueError('noncanonical base64')
    return decoded


class SecretStore:
    def __init__(self, state_dir):
        configured = os.environ.get(KEY_ENV)
        if configured is not None and not configured:
            raise CryptoError('CFSPEED_MASTER_KEY_FILE 不能为空')
        self.path = Path(configured) if configured is not None else Path(state_dir) / DEFAULT_KEY_FILENAME
        self.default = configured is None
        self._key = None

    def _create_key(self):
        """Publish a fully fsynced key without ever overwriting an existing key."""
        # Open the parent first so symlink components are rejected before writing.
        with private_directory(self.path.parent) as directory:
            # Use a relative, exclusive name through the checked directory descriptor.
            name = '.master-key-' + os.urandom(16).hex()
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(os.urandom(KEY_BYTES))
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(name, self.path.name, src_dir_fd=directory, dst_dir_fd=directory,
                            follow_symlinks=False)
                except FileExistsError:
                    # A concurrent initializer won; validate and use only its complete key.
                    pass
            finally:
                os.unlink(name, dir_fd=directory)
                os.fsync(directory)

    def unlock(self, *, allow_create=False):
        """Read/validate the key each time; a running process must not rotate it silently."""
        try:
            try:
                key = read_regular_file(self.path, KEY_BYTES, mode=0o600)
            except FileNotFoundError:
                if not (allow_create and self.default and self._key is None):
                    raise CryptoError('加密主密钥文件缺失；拒绝生成替代密钥或重置已保存凭据') from None
                self._create_key()
                key = read_regular_file(self.path, KEY_BYTES, mode=0o600)
            if len(key) != KEY_BYTES:
                raise ValueError('invalid key length')
            if self._key is not None and not hmac.compare_digest(key, self._key):
                raise CryptoError('加密主密钥已改变；拒绝覆盖已保存凭据')
            self._key = key
        except (OSError, ValueError):
            raise CryptoError('加密主密钥文件不可读或不安全；必须为 0600 普通文件、无符号链接且恰为 32 字节') from None

    def encrypt(self, values, context):
        self.unlock()
        assert self._key is not None
        try:
            plaintext = _json_bytes(values)
            if not isinstance(values, dict) or len(plaintext) > MAX_SECRET_BYTES:
                raise ValueError('invalid secrets')
            nonce = os.urandom(NONCE_BYTES)
            ciphertext = AESGCM(self._key).encrypt(nonce, plaintext, _aad(context))
            return {'version': ENVELOPE_VERSION, 'algorithm': ALGORITHM,
                    'nonce': base64.b64encode(nonce).decode('ascii'),
                    'ciphertext': base64.b64encode(ciphertext).decode('ascii')}
        except (ValueError, TypeError, RecursionError):
            raise CryptoError('管理凭据无法加密或超过大小限制；配置未保存') from None

    def decrypt(self, envelope, context):
        # Never create a key here, including when the encrypted map is empty.
        self.unlock()
        assert self._key is not None
        try:
            if (not isinstance(envelope, dict) or
                    set(envelope) != {'version', 'algorithm', 'nonce', 'ciphertext'} or
                    type(envelope['version']) is not int or envelope['version'] != ENVELOPE_VERSION or
                    envelope['algorithm'] != ALGORITHM):
                raise ValueError('invalid envelope')
            nonce = _decode(envelope['nonce'], 16)
            ciphertext = _decode(envelope['ciphertext'], ((MAX_SECRET_BYTES + 16 + 2) // 3) * 4)
            if len(nonce) != NONCE_BYTES or not 16 <= len(ciphertext) <= MAX_SECRET_BYTES + 16:
                raise ValueError('invalid envelope size')
            plaintext = AESGCM(self._key).decrypt(nonce, ciphertext, _aad(context))
            values = strict_json(plaintext)
            if not isinstance(values, dict):
                raise ValueError('invalid secrets')
            return values
        except (InvalidTag, ValueError, TypeError, KeyError, RecursionError, binascii.Error):
            raise CryptoError('管理凭据解密失败（主密钥不匹配或数据损坏）；拒绝重置或静默覆盖') from None
