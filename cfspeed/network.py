"""Bounded HTTPS transport and strict public-IPv4 source parsing."""
import ipaddress
from http.client import HTTPException
import json
import os
import re
import ssl
import time
import threading
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPSHandler
from .config import AppError, V2TOO_SOURCE_URL


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward API credentials or silently change the configured source.


class HTTP:
    def __init__(self, timeout=15, attempts=3):
        self.timeout = timeout
        self.attempts = attempts
        self.stop: threading.Event | None = None
        self.credentials = os.environ
        self.record_limit = 1000
        self.opener = build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))

    def request(self, method, url, *, headers=None, body=None, limit=2_000_000, retry=False):
        count = self.attempts if retry else 1
        for attempt in range(count):
            if self.stop is not None and self.stop.is_set():
                raise AppError('服务正在停止，请求已取消；未核验写入保留待核对')
            try:
                request = Request(url, data=body, method=method, headers={
                    'User-Agent': 'cfspeed-standalone/0.2.0', **(headers or {})})
                with self.opener.open(request, timeout=self.timeout) as response:
                    if response.status != 200:
                        raise AppError(f'HTTP 状态异常: {response.status}')
                    # read1 returns currently available bytes rather than waiting to fill
                    # a large read, so a trickling upstream cannot bypass every timeout.
                    deadline = time.monotonic() + self.timeout
                    chunks, length = [], 0
                    while True:
                        if self.stop is not None and self.stop.is_set():
                            raise AppError('服务正在停止，请求已取消；未核验写入保留待核对')
                        if time.monotonic() >= deadline:
                            raise TimeoutError()
                        chunk = response.read1(min(65536, limit + 1 - length))
                        if not chunk:
                            break
                        length += len(chunk)
                        if length > limit:
                            raise AppError('HTTP 响应超过大小限制')
                        chunks.append(chunk)
                    data = b''.join(chunks)
                    return data
            except HTTPError as error:
                code = error.code
                error.close()
                if code not in (429, 500, 502, 503, 504) or attempt + 1 == count:
                    raise AppError(f'远端 HTTP 错误: {code}') from None
            except (URLError, TimeoutError, OSError, HTTPException):
                if attempt + 1 == count:
                    raise AppError('网络请求失败或超时') from None
            if self.stop is not None:
                if self.stop.wait(min(2 ** attempt, 4)):
                    raise AppError('服务正在停止，请求重试已取消')
            else:
                time.sleep(min(2 ** attempt, 4))
        raise AppError('网络重试耗尽')

    def json(self, method, url, *, headers=None, payload=None, body=None, retry=False):
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        raw = self.request(method, url, headers={'Content-Type': 'application/json', **(headers or {})}, body=body, retry=retry)
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise AppError('远端返回无效 JSON 对象') from None


def parse_ips(raw, max_ips=30):
    try:
        text = raw.decode('utf-8-sig') if isinstance(raw, bytes) else raw
        parts = re.split(r'[,\s]+', text.strip())
        result = []
        for part in parts:
            if not part:
                continue
            ip = ipaddress.IPv4Address(part)
            if not ip.is_global or ip.is_multicast or ip.is_reserved:
                raise ValueError()
            if part not in result:
                result.append(part)
        if not result or len(result) > max_ips:
            raise ValueError()
        return result
    except (ValueError, UnicodeError):
        raise AppError('IP 源无效：需要非空公网 IPv4 列表，拒绝 HTML、私网、IPv6 或超限内容；保留原有解析') from None


def fetch_ips(config, http):
    if config.source_url == V2TOO_SOURCE_URL:
        selected = []
        for carrier in ('ct', 'cm', 'cu'):
            try:
                raw = http.request('GET', f'{V2TOO_SOURCE_URL}?carrier={carrier}', limit=65536, retry=True)
                nodes = json.loads(raw)
                if not isinstance(nodes, list) or not nodes or not isinstance(nodes[0], dict):
                    raise ValueError()
                first = nodes[0]
                ip = first.get('ip')
                if not isinstance(ip, str) or first.get('carrier', carrier) != carrier:
                    raise ValueError()
                # Validate the first entry only: never silently substitute a later node.
                address = ipaddress.IPv4Address(ip)
                if not address.is_global or address.is_multicast or address.is_reserved:
                    raise ValueError()
                selected.append(ip)
            except (ValueError, UnicodeError, AppError):
                raise AppError(f'IP 源 {carrier} 接口失败或首项不是有效公网 IPv4；保留原有解析') from None
        return parse_ips(','.join(selected), config.max_ips)
    raw = http.request('GET', config.source_url, limit=65536, retry=True)
    return parse_ips(raw, config.max_ips)
