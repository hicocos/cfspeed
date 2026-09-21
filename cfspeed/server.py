"""Bounded stdlib HTTP server: private management APIs and Vite static assets."""
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
import hmac
import ipaddress
import json
import os
import re
import threading
import socket

from .admin import AdminError, COOKIE, SESSION_SECONDS
from .config import AppError, keys
from .runtime import safe_error

MAX_BODY = 131072
MAX_STATIC = 8 * 1024 * 1024
MIME = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
        '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png',
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.ico': 'image/x-icon',
        '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf'}


class StatusServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(*args, **kwargs)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def create_server(runner, *, static_dir=None):
    admin = getattr(runner, 'admin', None)
    default_root = Path(__file__).resolve().parent.parent / 'web' / 'dist'
    root = Path(static_dir if static_dir is not None else os.environ.get('CFSPEED_WEB_ROOT', default_root)).resolve()
    public_origin = os.environ.get('CFSPEED_PUBLIC_ORIGIN', '')
    try:
        trusted_proxies = [ipaddress.ip_network(item.strip()) for item in
                           os.environ.get('CFSPEED_TRUSTED_PROXIES', '').split(',') if item.strip()]
    except ValueError:
        raise AppError('CFSPEED_TRUSTED_PROXIES 必须为明确的 IP/CIDR 列表') from None
    if public_origin:
        origin_url = urlsplit(public_origin)
        if (origin_url.scheme != 'https' or not origin_url.hostname or origin_url.username
                or origin_url.password or origin_url.path or origin_url.query or origin_url.fragment):
            raise AppError('CFSPEED_PUBLIC_ORIGIN 必须为无路径的 HTTPS 来源')

    class Handler(BaseHTTPRequestHandler):
        server_version = 'cfspeed'
        sys_version = ''

        def setup(self):
            super().setup()
            # Absolute budget, not only an idle socket timeout: trickling a byte
            # cannot hold every backend worker indefinitely.
            def expire():
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self.deadline_timer = threading.Timer(10, expire)
            self.deadline_timer.daemon = True
            self.deadline_timer.start()

        def finish(self):
            self.deadline_timer.cancel()
            super().finish()

        def client_identity(self):
            peer = ipaddress.ip_address(self.client_address[0])
            values = self.headers.get_all('X-Real-IP', [])
            if any(peer in network for network in trusted_proxies) and len(values) == 1:
                try:
                    return str(ipaddress.ip_address(values[0]))
                except ValueError:
                    pass
            return str(peer)

        def log_message(self, format, *args):
            pass  # No request paths, cookies, passwords, or raw request bodies in logs.

        def send_error(self, code, message=None, explain=None):
            self.respond(code, {'error': 'HTTP 请求无效'})

        def parse_request(self):
            if not super().parse_request():
                return False
            if sum(len(key) + len(value) for key, value in self.headers.items()) > 16384:
                self.respond(431, {'error': '请求头过大'})
                return False
            hosts = self.headers.get_all('Host', [])
            if len(hosts) != 1 or not re.fullmatch(r'(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])(?::[0-9]{1,5})?', hosts[0]):
                self.respond(400, {'error': 'Host 无效'})
                return False
            if public_origin and hosts[0] != urlsplit(public_origin).netloc:
                # Docker healthcheck remains loopback-only with its literal local Host.
                if not (self.command in ('GET', 'HEAD') and self.path == '/healthz'
                        and self.client_address[0] in ('127.0.0.1', '::1')
                        and hosts[0] == f'127.0.0.1:{runner.config.port}'):
                    self.respond(421, {'error': '请求来源不匹配'})
                    return False
            return True

        def respond(self, status, value, *, content_type='application/json; charset=utf-8', cookie=None,
                    cache='no-store', ready=None):
            data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', cache)
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")
            self.send_header('Connection', 'close')
            self.close_connection = True
            if cookie:
                self.send_header('Set-Cookie', cookie)
            if ready is not None:
                self.send_header('X-CFSPEED-Ready', str(ready).lower())
            try:
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

        def path_only(self):
            if len(self.path) > 2048 or not self.path.startswith('/') or self.path.startswith('//'):
                raise AdminError('请求路径无效')
            return urlsplit(self.path).path

        def token(self):
            raw = self.headers.get('Cookie', '')
            if len(raw) > 4096 or len(self.headers.get_all('Cookie', [])) > 1:
                return ''
            try:
                cookie = SimpleCookie()
                cookie.load(raw)
                return cookie[COOKIE].value if COOKIE in cookie else ''
            except CookieError:
                return ''

        def auth(self, csrf=False):
            session = admin.session(self.token()) if admin else None
            if not session:
                raise AdminError('请先登录', 401)
            if csrf:
                values = self.headers.get_all('X-CSRF-Token', [])
                if len(values) != 1 or not hmac.compare_digest(values[0].encode(), session['csrf'].encode()):
                    raise AdminError('CSRF 校验失败', 403)
            return session

        def origin(self):
            origins, hosts = self.headers.get_all('Origin', []), self.headers.get_all('Host', [])
            if len(origins) != 1 or len(hosts) != 1:
                raise AdminError('缺少同源 Origin', 403)
            host, origin = hosts[0], origins[0]
            if public_origin and origin != public_origin:
                raise AdminError('仅允许配置的 HTTPS 来源', 403)
            # Ignore all forwarded headers. Never trust an attacker-controlled proxy hint.
            if (not re.fullmatch(r'(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])(?::[0-9]{1,5})?', host)
                    or origin not in ('http://' + host, 'https://' + host)
                    or self.headers.get('Sec-Fetch-Site', 'same-origin') not in ('same-origin', 'none')):
                raise AdminError('仅允许同源操作', 403)
            return origin.startswith('https://')

        def cookie(self, token, secure=False):
            return (f'{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS if token else 0}'
                    + ('; Secure' if secure or public_origin else ''))

        def body(self):
            if self.headers.get_all('Transfer-Encoding', []):
                raise AdminError('不支持分块请求')
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,8}', lengths[0]):
                raise AdminError('必须提供 Content-Length', 411)
            size = int(lengths[0])
            if size > MAX_BODY:
                raise AdminError('请求内容过大', 413)
            if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                raise AdminError('只接受 application/json', 415)
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise AdminError('请求内容不完整')
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result
            try:
                value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if not isinstance(value, dict):
                    raise ValueError()
                return value
            except (ValueError, UnicodeError, RecursionError):
                raise AdminError('需要有效 JSON 对象，不能包含重复字段') from None

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            try:
                path = self.path_only()
                if path == '/api/auth/session' and admin:
                    return self.respond(200, admin.session_view(admin.session(self.token())))
                if path.startswith('/api/admin/') or (path == '/api/status' and admin):
                    self.auth()
                    if path == '/api/admin/config':
                        assert admin is not None
                        return self.respond(200, admin.view())
                    if path not in ('/api/admin/status', '/api/status'):
                        raise AdminError('not_found', 404)
                ready, state = runner.readiness()
                dry_run = runner.config.dry_run
                if path == '/healthz':
                    value = {'status': 'alive', 'version': '0.2.0'}
                elif path == '/readyz':
                    return self.respond(200 if ready else 503, {'ready': ready, 'mode': 'preview' if dry_run else 'apply',
                                                              'status': state['status']}, ready=ready)
                elif path in ('/api/status', '/api/admin/status'):
                    value = {**state, 'ready': ready, 'dry_run': dry_run,
                             'configured_dry_run': runner.config.dry_run}
                elif path in ('/ipTop.html', '/ipTop10.html'):
                    return self.respond(200 if state['ips'] else 503, (','.join(state['ips']) + '\n').encode(),
                                        content_type='text/plain; charset=utf-8', ready=ready)
                elif path.startswith('/api/'):
                    raise AdminError('not_found', 404)
                elif admin:
                    return self.static(path)
                elif path == '/':
                    value = {'service': 'cfspeed', 'version': '0.2.0', 'mode': 'preview' if dry_run else 'apply',
                             'endpoints': ['/healthz', '/readyz', '/api/status', '/ipTop.html']}
                else:
                    raise AdminError('not_found', 404)
                return self.respond(200, value, ready=ready)
            except AppError as error:
                self.respond(getattr(error, 'status', 400), {'error': str(error)})
            except (ValueError, OSError, RecursionError):
                self.respond(500, {'error': '请求处理失败'})

        def static(self, path):
            try:
                path = unquote(path, errors='strict')
            except UnicodeError:
                raise AdminError('not_found', 404) from None
            parts = path.split('/')
            if any(p.startswith('.') for p in parts if p) or any(c in path for c in ('\\', '\x00', '%')):
                raise AdminError('not_found', 404)
            candidate = (root / path.lstrip('/')).resolve()
            if not candidate.is_relative_to(root):
                raise AdminError('not_found', 404)
            if path == '/' or (not candidate.suffix and not candidate.is_file()):
                candidate = root / 'index.html'
            # Resolve again for the index fallback, which must not be a symlink escape.
            candidate = candidate.resolve()
            if not candidate.is_relative_to(root) or candidate.suffix.lower() not in MIME or not candidate.is_file():
                raise AdminError('not_found', 404)
            if candidate.stat().st_size > MAX_STATIC:
                raise AdminError('静态文件超过安全限制', 413)
            cache = 'public, max-age=31536000, immutable' if '/assets/' in path and re.search(r'-[A-Za-z0-9_-]{8,}\.', candidate.name) else 'no-cache'
            self.respond(200, candidate.read_bytes(), content_type=MIME[candidate.suffix.lower()], cache=cache)

        def do_POST(self):
            self.mutate()

        def do_PATCH(self):
            self.mutate()

        def mutate(self):
            try:
                if not admin:
                    return self.respond(501, {'error': '管理接口未启用'})
                path = self.path_only()
                secure = self.origin()
                if path == '/api/auth/login' and self.command == 'POST':
                    token, view = admin.login(self.body(), client=self.client_identity())
                    return self.respond(200, view, cookie=self.cookie(token, secure))
                self.auth(csrf=True)
                payload = self.body()
                # Authorization and commit share the revocation lock; a logout
                # cannot complete before a stale authorized mutation commits.
                with admin.lock:
                    self.auth(csrf=True)
                    if path == '/api/auth/logout' and self.command == 'POST':
                        keys(payload, (), 'logout')
                        admin.logout(self.token())
                        return self.respond(200, {'ok': True}, cookie=self.cookie('', secure))
                    if path == '/api/auth/password' and self.command == 'POST':
                        admin.change_password(payload)
                        return self.respond(200, {'ok': True}, cookie=self.cookie('', secure))
                    if path == '/api/admin/config' and self.command == 'PATCH':
                        return self.respond(200, admin.update(payload))
                    if path == '/api/admin/source/refresh' and self.command == 'POST':
                        keys(payload, (), 'source refresh')
                        runner.start_source_async()
                        return self.respond(202, {'accepted': True})
                    if path == '/api/admin/run' and self.command == 'POST':
                        keys(payload, ('dry_run', 'confirm_apply'), 'run')
                        runner.start_async(payload.get('dry_run'), payload.get('confirm_apply', False))
                        return self.respond(202, {'accepted': True})
                    raise AdminError('not_found', 404)
            except AppError as error:
                self.respond(getattr(error, 'status', 400), {'error': str(error)})
            except (ValueError, OSError, RecursionError) as error:
                self.respond(500, {'error': safe_error(error)})

    return StatusServer((runner.config.host, runner.config.port), Handler)
