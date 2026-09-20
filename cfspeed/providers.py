"""DNS adapters: exact scope, pagination, preserved settings, read-after-write."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import re
import random
import time
from urllib.parse import urlencode
from .config import AppError


@dataclass(frozen=True)
class Record:
    id: str
    name: str
    value: str
    ttl: int
    options: dict = field(default_factory=dict)


def write_and_verify(write, read, expected, provider):
    """Never resend a write; even an ambiguous response must be followed by a read."""
    write_failed = False
    try:
        write()
    except Exception:
        write_failed = True
    try:
        observed = read()
    except Exception:
        raise AppError(f'{provider} 写后读取失败；实际结果未知，不重发写请求、不自动回滚') from None
    if observed != expected:
        raise AppError(f'{provider} 写后核验不一致；实际状态需复查，不自动回滚')
    return '写请求响应异常，但已读回确认目标状态一致' if write_failed else None


def plan_records(records, ips):
    """Retain already-correct assignments, so source reordering is a no-op."""
    if not records:
        raise AppError('未找到匹配的已有 A 记录；初版不自动创建记录')
    if len({r.id for r in records}) != len(records):
        raise AppError('DNS 列表含重复记录 ID，拒绝更新')
    if not ips:
        raise AppError('没有有效 IP，拒绝更新')
    if len(ips) < len(records):
        records = random.sample(records, len(ips))
    wanted = list(ips[:len(records)])
    assigned = {}
    ordered = sorted(records, key=lambda r: r.id)
    for record in ordered:
        if record.value in wanted:
            assigned[record.id] = record.value
            wanted.remove(record.value)
    return [(record, assigned[record.id] if record.id in assigned else wanted.pop(0)) for record in ordered]


class Cloudflare:
    def __init__(self, target, http):
        self.target, self.http = target, http
        self.base = f'https://api.cloudflare.com/client/v4/zones/{target.zone_id}/dns_records'
        self.headers = {'Authorization': 'Bearer ' + getattr(http, 'credentials', os.environ)[target.token_env]}

    def call(self, method, suffix='', payload=None):
        data = self.http.json(method, self.base + suffix, headers=self.headers, payload=payload, retry=method == 'GET')
        if data.get('success') is not True:
            codes = [str(e.get('code')) for e in data.get('errors', []) if isinstance(e, dict)]
            safe = ','.join(c for c in codes if re.fullmatch(r'[0-9]+', c))
            raise AppError('Cloudflare API 失败' + (': ' + safe if safe else ''))
        return data

    def record(self, raw):
        if raw.get('name') != self.target.name or raw.get('type') != 'A':
            raise AppError('Cloudflare 记录不在配置的精确作用域内')
        if not re.fullmatch(r'[0-9a-f]{32}', str(raw.get('id', ''))):
            raise AppError('Cloudflare 记录 ID 无效')
        if type(raw.get('ttl')) is not int or type(raw.get('proxied')) is not bool:
            raise AppError('Cloudflare 记录缺少 TTL / 代理状态')
        options = {'proxied': raw['proxied']}
        for name in ('comment', 'tags', 'settings'):
            if name in raw:
                options[name] = raw[name]
        return Record(raw['id'], raw['name'], raw['content'], raw['ttl'], options)

    def list_records(self):
        records, page, total = [], 1, None
        while page <= 100:
            query = urlencode({'type': 'A', 'name': self.target.name, 'page': page, 'per_page': 100})
            response = self.call('GET', '?' + query)
            items, info = response.get('result'), response.get('result_info', {})
            if not isinstance(items, list):
                raise AppError('Cloudflare 记录列表缺失')
            count = info.get('total_count')
            pages = info.get('total_pages')
            if type(count) is not int or type(pages) is not int:
                raise AppError('Cloudflare 分页计数缺失')
            if count > getattr(self.http, 'record_limit', 1000) or len(records) + len(items) > getattr(self.http, 'record_limit', 1000):
                raise AppError('Cloudflare 记录数量超过 DNS/内存安全限制，整组跳过')
            if total is not None and total != count:
                raise AppError('Cloudflare 记录总数在分页期间变化，请重试')
            total = count
            records.extend(self.record(item) for item in items)
            if page >= pages:
                if len(records) != total:
                    raise AppError('Cloudflare 分页记录数量与总数不一致')
                return records
            if not items:
                raise AppError('Cloudflare 分页意外为空')
            page += 1
        raise AppError('Cloudflare 记录页数超过安全限制')

    def get(self, record_id):
        record = self.record(self.call('GET', '/' + record_id)['result'])
        if record.id != record_id:
            raise AppError('Cloudflare 返回的记录 ID 不匹配')
        return record

    def update(self, before, ip):
        if self.get(before.id) != before:
            raise AppError('记录已被其他操作修改，停止覆盖；等待下轮重新读取')
        payload = {'type': 'A', 'name': before.name, 'content': ip, 'ttl': before.ttl, **before.options}
        return write_and_verify(lambda: self.call('PATCH', '/' + before.id, payload),
                                lambda: self.get(before.id),
                                Record(before.id, before.name, ip, before.ttl, before.options), 'Cloudflare')


def tc3_headers(secret_id, secret_key, action, body, timestamp=None):
    timestamp = int(time.time()) if timestamp is None else timestamp
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime('%Y-%m-%d')
    host = 'dnspod.tencentcloudapi.com'
    signed = 'content-type;host;x-tc-action'
    canonical_headers = f'content-type:application/json\nhost:{host}\nx-tc-action:{action.lower()}\n'
    digest = lambda value: hashlib.sha256(value).hexdigest()
    canonical = f'POST\n/\n\n{canonical_headers}\n{signed}\n{digest(body)}'
    scope = f'{date}/dnspod/tc3_request'
    to_sign = f'TC3-HMAC-SHA256\n{timestamp}\n{scope}\n{digest(canonical.encode())}'
    key = ('TC3' + secret_key).encode()
    for value in (date, 'dnspod', 'tc3_request'):
        key = hmac.new(key, value.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return {'Authorization': f'TC3-HMAC-SHA256 Credential={secret_id}/{scope}, SignedHeaders={signed}, Signature={signature}',
            'Content-Type': 'application/json', 'Host': host, 'X-TC-Action': action,
            'X-TC-Version': '2021-03-23', 'X-TC-Timestamp': str(timestamp)}


class DNSPod:
    def __init__(self, target, http):
        self.target, self.http = target, http

    def call(self, action, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        credentials = getattr(self.http, 'credentials', os.environ)
        headers = tc3_headers(credentials[self.target.secret_id_env], credentials[self.target.secret_key_env], action, body)
        raw = self.http.json('POST', 'https://dnspod.tencentcloudapi.com/', headers=headers, body=body, retry=action.startswith('Describe'))
        response = raw.get('Response')
        if not isinstance(response, dict):
            raise AppError('DNSPod 缺少 Response')
        if 'Error' in response:
            code = response['Error'].get('Code', '')
            safe = code if isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9.]+', code) else 'Unknown'
            raise AppError(f'DNSPod API 失败: {safe}')
        return response

    def record(self, raw, detail=False):
        name = raw.get('SubDomain' if detail else 'Name')
        kind = raw.get('RecordType' if detail else 'Type')
        line_id = str(raw.get('RecordLineId' if detail else 'LineId', ''))
        if name != self.target.name or kind != 'A' or line_id != self.target.line_id:
            raise AppError('DNSPod 记录不在配置的名称/类型/线路范围内')
        enabled = raw.get('Enabled') == 1 if detail else raw.get('Status') == 'ENABLE'
        if not enabled:
            raise AppError('DNSPod 目标包含暂停记录，拒绝自动启用或修改')
        record_id = raw.get('Id' if detail else 'RecordId')
        line = raw.get('RecordLine' if detail else 'Line')
        if type(record_id) is not int or record_id <= 0 or type(raw.get('TTL')) is not int or not isinstance(line, str):
            raise AppError('DNSPod 记录必需字段无效')
        return Record(str(record_id), name, raw['Value'], raw['TTL'], {
            'line_id': line_id, 'line': line, 'weight': raw.get('Weight'), 'status': 'ENABLE'})

    def list_records(self):
        records, offset, total = [], 0, None
        for _ in range(100):
            response = self.call('DescribeRecordList', {'Domain': self.target.domain, 'Subdomain': self.target.name,
                'RecordType': 'A', 'RecordLineId': self.target.line_id, 'Offset': offset, 'Limit': 100})
            items = response.get('RecordList')
            count = response.get('RecordCountInfo', {}).get('TotalCount')
            if not isinstance(items, list) or type(count) is not int:
                raise AppError('DNSPod 记录列表或分页计数缺失')
            if count > getattr(self.http, 'record_limit', 1000) or len(records) + len(items) > getattr(self.http, 'record_limit', 1000):
                raise AppError('DNSPod 记录数量超过 DNS/内存安全限制，整组跳过')
            if total is not None and count != total:
                raise AppError('DNSPod 记录总数在分页期间变化，请重试')
            total = count
            records.extend(self.record(item) for item in items)
            offset += len(items)
            if offset >= total:
                if len(records) != total:
                    raise AppError('DNSPod 分页记录数量与总数不一致')
                return records
            if not items:
                raise AppError('DNSPod 分页意外为空')
        raise AppError('DNSPod 记录页数超过安全限制')

    def get(self, record_id):
        raw = self.call('DescribeRecord', {'Domain': self.target.domain, 'RecordId': int(record_id)})
        record = self.record(raw['RecordInfo'], detail=True)
        if record.id != record_id:
            raise AppError('DNSPod 返回的记录 ID 不匹配')
        return record

    def update(self, before, ip):
        if self.get(before.id) != before:
            raise AppError('记录已被其他操作修改，停止覆盖；等待下轮重新读取')
        payload = {'Domain': self.target.domain, 'RecordId': int(before.id), 'SubDomain': before.name,
                   'RecordType': 'A', 'RecordLine': before.options['line'], 'RecordLineId': before.options['line_id'],
                   'Value': ip, 'TTL': before.ttl, 'Status': before.options['status']}
        if before.options['weight'] is not None:
            payload['Weight'] = before.options['weight']
        def write():
            response = self.call('ModifyRecord', payload)
            if str(response.get('RecordId')) != before.id:
                raise AppError('DNSPod 更新响应缺少匹配的记录 ID；需复查实际状态')
        return write_and_verify(write, lambda: self.get(before.id),
                                Record(before.id, before.name, ip, before.ttl, before.options), 'DNSPod')


def make_provider(target, http):
    return Cloudflare(target, http) if target.provider == 'cloudflare' else DNSPod(target, http)
