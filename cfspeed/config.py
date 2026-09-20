"""Strict TOML configuration. Secrets are referenced by environment variable name."""
from dataclasses import dataclass, field
from pathlib import Path
import os
import re
import tomllib
from urllib.parse import urlsplit


DEFAULT_SOURCE_URL = "https://ip.164746.xyz/ipTop.html"
V2TOO_SOURCE_URL = "https://ip.v2too.top/api/nodes"
BUILTIN_SOURCE_URLS = (DEFAULT_SOURCE_URL, V2TOO_SOURCE_URL)


class AppError(Exception):
    """A safe-to-display operational error (never include credentials or raw bodies)."""


class BusyError(AppError):
    """Nonblocking task reservation failed."""


def keys(data, allowed, where):
    if not isinstance(data, dict) or set(data) - set(allowed):
        raise AppError(f"{where}: 配置项不正确或含未知字段")


def integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise AppError(f"{label}: 必须是 {low}–{high} 的整数")
    return value


def text(value, label):
    if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise AppError(f"{label}: 必须是非空字符串")
    return value


def env_name(value, label):
    if not isinstance(value, str) or len(value) > 128 or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
        raise AppError(f"{label}: 必须填写环境变量名称，而非密钥本身")
    return value


def domain(value, label, sub=False):
    text(value, label)
    if sub and value == "@":
        return value
    if len(value) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x) for x in value.split('.')):
        raise AppError(f"{label}: 使用小写 ASCII 域名（国际化域名请用 punycode），不要结尾点号")
    return value


@dataclass(frozen=True)
class Target:
    label: str
    provider: str
    name: str
    zone_id: str = ""
    domain: str = ""
    line_id: str = "0"
    token_env: str = "CF_API_TOKEN"
    secret_id_env: str = "DNSPOD_SECRET_ID"
    secret_key_env: str = "DNSPOD_SECRET_KEY"

    def credential_names(self):
        return [self.token_env] if self.provider == "cloudflare" else [self.secret_id_env, self.secret_key_env]


@dataclass(frozen=True)
class Config:
    source_url: str = "https://ip.164746.xyz/ipTop.html"
    interval_seconds: int = 21600
    timeout_seconds: int = 15
    attempts: int = 3
    dry_run: bool = True
    max_ips: int = 30
    state_dir: str = "data"
    host: str = "127.0.0.1"
    port: int = 8788
    pushplus_token_env: str = ""
    targets: tuple = field(default_factory=tuple)
    credential_values: dict = field(default_factory=dict, repr=False, compare=False)

    def credential(self, name):
        # A persisted null is an explicit clear, not a fallback to the environment.
        return self.credential_values.get(name, os.environ.get(name, '')) or ''

    def validate_credentials(self):
        for target in self.targets:
            for name in target.credential_names():
                value = self.credential(name)
                if not value or value != value.strip() or any(ord(c) < 32 for c in value):
                    raise AppError(f"缺少或无效的凭据环境变量: {name}")
        if self.pushplus_token_env and not self.credential(self.pushplus_token_env):
            raise AppError(f"缺少通知环境变量: {self.pushplus_token_env}")


def load_config(path):
    try:
        with open(path, 'rb') as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        raise AppError("无法读取 TOML 配置，请检查文件路径与语法") from None
    return parse_config(raw, path)


def parse_config(raw, path='config.toml'):
    """The single strict parser for both TOML and management JSON."""
    keys(raw, ['service', 'targets'], 'root')
    service = raw.get('service', {})
    defaults = Config()
    allowed = set(Config.__dataclass_fields__) - {'targets', 'credential_values'}
    keys(service, allowed, 'service')
    values = {key: service.get(key, getattr(defaults, key)) for key in allowed}
    try:
        url = urlsplit(text(values['source_url'], 'source_url'))
    except ValueError:
        raise AppError('source_url: URL 无效') from None
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.fragment:
        raise AppError('source_url: 只允许不带凭据与片段的 HTTPS URL')
    for name, low, high in [('interval_seconds', 30, 604800), ('timeout_seconds', 1, 60), ('attempts', 1, 5), ('max_ips', 1, 1000), ('port', 1, 65535)]:
        integer(values[name], low, high, name)
    if type(values['dry_run']) is not bool:
        raise AppError('dry_run: 必须是 true 或 false')
    text(values['host'], 'host')
    state = Path(text(values['state_dir'], 'state_dir'))
    if not state.is_absolute():
        state = Path(path).resolve().parent / state
    values['state_dir'] = str(state)
    if values['pushplus_token_env'] != '':
        env_name(values['pushplus_token_env'], 'pushplus_token_env')
    if not isinstance(raw.get('targets', []), list) or len(raw.get('targets', [])) > 32:
        raise AppError('targets: 使用数组格式，最多 32 个目标')
    targets, labels, scopes = [], set(), set()
    for entry in raw.get('targets', []):
        provider = entry.get('provider') if isinstance(entry, dict) else None
        common = ['label', 'provider', 'name']
        if provider == 'cloudflare':
            keys(entry, common + ['zone_id', 'token_env'], 'targets')
            if not isinstance(entry.get('zone_id'), str) or not re.fullmatch(r'[0-9a-f]{32}', entry['zone_id']):
                raise AppError('Cloudflare zone_id: 必须是 32 位小写十六进制 ID')
        elif provider == 'dnspod':
            keys(entry, common + ['domain', 'line_id', 'secret_id_env', 'secret_key_env'], 'targets')
            domain(entry.get('domain'), 'domain')
            if not isinstance(entry.get('line_id', '0'), str) or len(entry.get('line_id', '0')) > 128 or not re.fullmatch(r'[0-9A-Za-z_=.-]+', entry.get('line_id', '0')):
                raise AppError('line_id: 无效线路 ID')
        else:
            raise AppError('provider: 仅支持 cloudflare / dnspod')
        label = text(entry.get('label'), 'label')
        if len(label) > 80 or label in labels:
            raise AppError('label: 必须唯一且不超过 80 字符')
        labels.add(label)
        domain(entry.get('name'), 'name', sub=provider == 'dnspod')
        target = Target(**entry)
        for credential in target.credential_names():
            env_name(credential, 'credential')
        scope = (provider, target.zone_id or target.domain, target.name, target.line_id)
        if provider == 'dnspod':
            scope += (target.secret_id_env, target.secret_key_env)
        if scope in scopes:
            raise AppError('同一 DNS 目标不能重复配置')
        scopes.add(scope)
        targets.append(target)
    if not values['dry_run'] and not targets:
        raise AppError('正式模式至少需要一个 DNS 目标')
    return Config(**values, targets=tuple(targets))
