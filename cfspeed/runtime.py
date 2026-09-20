"""Single-writer runtime, durable state and DNS synchronization."""
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import copy
import fcntl
import json
import logging
import os
import tempfile
import threading
import time
from .config import AppError, BusyError
from .network import HTTP, fetch_ips
from .providers import make_provider, plan_records

LOG = logging.getLogger('cfspeed')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def safe_error(error):
    return str(error) if isinstance(error, AppError) else f'内部错误 ({type(error).__name__})，请检查配置或 API 响应结构'


def target_scope(target):
    scope = [target.provider, target.zone_id or target.domain, target.name, target.line_id]
    # Cloudflare's zone ID is globally scoped; DNSPod domain names are not account IDs.
    return scope + target.credential_names() if target.provider == 'dnspod' else scope


class ProcessLock(AbstractContextManager):
    def __init__(self, directory):
        self.path = Path(directory)
        self.stream = None

    def __enter__(self):
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.stream = (self.path / 'service.lock').open('a')
        try:
            fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.stream.close()
            raise AppError('此数据目录已有服务运行，拒绝重复执行') from None
        return self

    def __exit__(self, *args):
        if self.stream:
            self.stream.close()


class State:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'state.json'
        self.lock = threading.RLock()
        self.data = {'version': '0.2.0', 'status': 'starting', 'ips': [], 'history': [], 'last_source_success': None,
                     'last_success': None, 'last_error': None, 'targets': [], 'runs': 0, 'pending_operations': []}
        if self.path.exists():
            try:
                if self.path.is_symlink() or self.path.stat().st_size > 8 * 1024 * 1024:
                    raise ValueError()
                loaded = json.loads(self.path.read_text())
                if not isinstance(loaded, dict) or not isinstance(loaded.get('ips'), list) or not isinstance(loaded.get('history'), list):
                    raise ValueError()
                self.data.update(loaded)
                self.data['version'] = '0.2.0'
                self.data['status'] = 'starting'
                interrupted = loaded.get('current_run')
                if interrupted:
                    interrupted = copy.deepcopy(interrupted)
                    interrupted.update(status='interrupted', finished_at=now(),
                                       error='上次进程中断；applying 记录实际状态未知，请核对')
                    for target in interrupted.get('targets', []):
                        for record in target.get('records', []):
                            if record.get('status') == 'applying':
                                record['status'] = 'interrupted_unverified'
                    self.save(current_run=None, history=(self.data['history'] + [interrupted])[-30:],
                              runs=self.data['runs'] + 1)
            except (ValueError, OSError):
                raise AppError('状态文件不可读或损坏，请先备份后检查，拒绝静默覆盖') from None

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def save(self, **changes):
        with self.lock:
            data = {**self.data, **changes}
            if 'history' in changes:
                history = data['history'][-30:]
                while len(history) > 1 and len(json.dumps(history, ensure_ascii=False).encode()) > 2 * 1024 * 1024:
                    history = history[1:]
                data['history'] = history
            fd, temporary = tempfile.mkstemp(prefix='.state-', dir=self.directory)
            try:
                with os.fdopen(fd, 'w') as stream:
                    json.dump(data, stream, ensure_ascii=False, indent=2)
                    stream.write('\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                directory_fd = os.open(self.directory, os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                self.data = data
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)


def notify(config, http, result):
    if config.dry_run or not config.pushplus_token_env:
        return 'disabled'
    if result['status'] == 'ok' and not result['changed']:
        return 'not_needed'
    data = http.json('POST', 'https://www.pushplus.plus/send', payload={
        'token': config.credential(config.pushplus_token_env), 'title': 'cfspeed DNS 同步结果',
        'content': json.dumps(result, ensure_ascii=False), 'template': 'txt', 'channel': 'wechat'})
    if data.get('code') != 200:
        raise AppError('PushPlus 拒绝通知请求')
    return 'accepted'  # API acceptance is not proof of delivery.


class Runner:
    def __init__(self, config, state, http=None, provider_factory=make_provider, stop=None):
        self.config, self.state = config, state
        self.http = http or HTTP(config.timeout_seconds, config.attempts)
        self.owns_http = http is None
        self.provider_factory = provider_factory
        self.stop = stop or threading.Event()
        if self.owns_http:
            self.http.stop = self.stop
        self.lock = threading.Lock()
        self.session_success = False
        self.admin = None
        self.condition = threading.Condition()
        self.schedule_context = threading.local()
        self.last_completed = None
        self.next_due = time.monotonic()
        self.last_source_completed = None
        self.next_source_due = self.next_due
        self.source_ips = None  # Never authorize DNS from persisted display data.
        self.source_success_time = None
        self.worker = None
        self.state.save(source_valid=False, source_status='starting',
                        source_interval_seconds=config.source_interval_seconds,
                        interval_seconds=config.interval_seconds, dry_run=config.dry_run,
                        **self._deadlines(self.next_due, self.next_source_due))

    @staticmethod
    def _deadlines(dns, source):
        wall, mono = time.time(), time.monotonic()
        def stamp(deadline):
            return datetime.fromtimestamp(wall + max(0, deadline - mono), timezone.utc).isoformat(timespec='seconds')
        return {'next_run_at': stamp(dns), 'next_dns_run_at': stamp(dns), 'next_source_run_at': stamp(source)}

    def configure(self, config):
        """Caller reserves lock before changing settings; no network or automatic run."""
        with self.condition:
            next_due = (self.last_completed + config.interval_seconds
                        if self.last_completed is not None else self.next_due)
            source_changed = (config.source_url, config.max_ips) != (self.config.source_url, self.config.max_ips)
            next_source_due = (time.monotonic() if source_changed else
                               self.last_source_completed + config.source_interval_seconds
                               if self.last_source_completed is not None else self.next_source_due)
            # Do not arm new settings if persistent state cannot be saved.
            self.state.save(status='starting', dry_run=config.dry_run, interval_seconds=config.interval_seconds,
                            source_interval_seconds=config.source_interval_seconds,
                            source_valid=False if source_changed else self.source_ips is not None,
                            **({'source_status': 'starting', 'source_error': None} if source_changed else {}),
                            **self._deadlines(next_due, next_source_due))
            self.config = config
            self.session_success = False
            self.next_due = next_due
            self.next_source_due = next_source_due
            if source_changed:
                self.source_ips = None
                self.source_success_time = None
                self.last_source_completed = None
            if self.owns_http:
                self.http.timeout, self.http.attempts = config.timeout_seconds, config.attempts
            self.condition.notify_all()

    def _completed(self, source=False):
        with self.condition:
            if source:
                self.last_source_completed = time.monotonic()
                self.next_source_due = self.last_source_completed + self.config.source_interval_seconds
            else:
                self.last_completed = time.monotonic()
                self.next_due = self.last_completed + self.config.interval_seconds
            self.state.save(**self._deadlines(self.next_due, self.next_source_due))
            self.condition.notify_all()

    def _fetch(self, config):
        # Revoke first, including on unexpected errors or interrupted persistence.
        self.source_ips = None
        self.source_success_time = None
        self.state.save(source_valid=False, source_status='running', next_source_run_at=None)
        try:
            ips = fetch_ips(config, self.http)
            self.state.save(ips=ips, last_source_success=now(), source_valid=True,
                            source_status='ok', source_error=None)
            self.source_ips = list(ips)
            self.source_success_time = time.monotonic()
            return ips
        except BaseException as error:
            self.session_success = False
            self.state.save(source_valid=False, source_status='error', source_error=safe_error(error))
            raise
        finally:
            try:
                self._completed(source=True)
            except BaseException:
                self.source_ips = None
                self.source_success_time = None
                self.session_success = False
                raise

    def refresh_source(self, scheduled=False):
        """IP-only job, sharing the DNS/config reservation. No provider or notification."""
        if not self.lock.acquire(blocking=False):
            raise BusyError('任务已经在运行')
        try:
            if self.stop.is_set():
                raise AppError('服务正在停止')
            if scheduled and time.monotonic() < self.next_source_due:
                return None
            result = {'kind': 'source', 'mode': 'source', 'started_at': now(), 'status': 'ok',
                      'changed': 0, 'planned': 0, 'targets': [], 'error': None}
            self.state.save(current_run=copy.deepcopy(result))
            try:
                result['ip_count'] = len(self._fetch(self.config))
            except Exception as error:
                result.update(status='error', error=safe_error(error))
            result['finished_at'] = now()
            snapshot = self.state.snapshot()
            self.state.save(current_run=None, history=snapshot['history'] + [result], runs=snapshot['runs'] + 1)
            return result
        finally:
            self.lock.release()

    def start_async(self, dry_run, confirm_apply=False):
        from .admin import AdminError
        if type(dry_run) is not bool or type(confirm_apply) is not bool:
            raise AdminError('dry_run 和 confirm_apply 必须为布尔值')
        if not self.lock.acquire(blocking=False):
            raise AdminError('同步任务已经在运行', 409)
        handed_off = False
        try:
            if self.stop.is_set():
                raise AdminError('服务正在停止', 409)
            if not dry_run and (self.config.dry_run or not confirm_apply):
                raise AdminError('正式执行需要先保存正式模式，并显式确认 confirm_apply=true')
            config = replace(self.config, dry_run=dry_run)
            # Reserve visibly before returning 202; scheduler and configuration use the same lock.
            self.session_success = False
            self.state.save(status='running', next_run_at=None)
            def work():
                try:
                    self._run(config)
                except Exception as error:
                    self.session_success = False
                    LOG.error('%s', safe_error(error))
                finally:
                    try:
                        self._completed()
                    finally:
                        self.lock.release()
            self.worker = threading.Thread(target=work, name='manual-sync', daemon=True)
            self.worker.start()
            handed_off = True
        finally:
            if not handed_off:
                self.lock.release()

    def run(self):
        if not self.lock.acquire(blocking=False):
            raise BusyError('同步任务已经在运行')
        try:
            if self.stop.is_set():
                raise AppError('服务正在停止')
            # A manual run may have completed after the scheduler decided a run was due.
            if getattr(self.schedule_context, 'active', False) and time.monotonic() < self.next_due:
                return None
            try:
                return self._run(self.config, refresh=not getattr(self.schedule_context, 'active', False))
            finally:
                self._completed()
        finally:
            self.lock.release()

    def _run(self, config=None, refresh=True):
        config = config or self.config
        self.session_success = False
        names = {name for target in config.targets for name in target.credential_names()}
        self.http.credentials = {name: config.credential(name) for name in names}
        # DNS listing bounds are independent of the source IP count.
        self.http.record_limit = 1000
        result = {'kind': 'manual' if refresh else 'dns', 'started_at': now(), 'mode': 'preview' if config.dry_run else 'apply',
                  'status': 'ok', 'changed': 0, 'planned': 0, 'targets': [], 'error': None}
        self.state.save(status='running', dry_run=config.dry_run, interval_seconds=config.interval_seconds,
                        last_started=result['started_at'], next_run_at=None, next_dns_run_at=None,
                        targets=[], current_run=copy.deepcopy(result))
        try:
            # Validate all credentials before touching any target; empty-target preview needs none.
            config.validate_credentials()
            if refresh:
                self._fetch(config)
            if self.source_ips is None:
                raise AppError('当前进程尚无有效来源结果；等待 IP 获取成功，DNS 未执行')
            ips = list(self.source_ips)
            result['source_fetched_at'] = self.state.snapshot().get('last_source_success')
            record_count = 0
            for target in config.targets:
                if self.stop.is_set():
                    raise AppError('服务正在停止，剩余目标未执行')
                item = {'label': target.label, 'provider': target.provider, 'name': target.name,
                        'status': 'ok', 'records': [], 'error': None}
                result['targets'].append(item)
                try:
                    provider = self.provider_factory(target, self.http)
                    records = provider.list_records()
                    record_count += len(records)
                    if record_count > 1000:
                        raise AppError('本轮 DNS 记录超过 1000 条内存安全限制，剩余目标跳过')
                    pending = self.state.snapshot()['pending_operations']
                    remaining = []
                    # A fresh exact-scope read resolves uncertainty about current state,
                    # not proof of who performed a previous interrupted write.
                    for operation in pending:
                        observed = next((r for r in records if operation['scope'] == target_scope(target)
                                         and r.id == operation['record_id']), None)
                        if observed is None:
                            remaining.append(operation)
                        else:
                            item.setdefault('reconciled', []).append({**operation, 'observed': observed.value,
                                                                     'observed_at': now()})
                    if remaining != pending:
                        self.state.save(pending_operations=remaining, current_run=copy.deepcopy(result))
                    plan = plan_records(records, ips)
                    selected_ids = {record.id for record, _ in plan}
                    for record in records:
                        if record.id not in selected_ids:
                            item['records'].append({'id': record.id, 'current': record.value,
                                'target': record.value, 'status': 'unselected',
                                'warning': '有效 IP 不足，本轮未随机选中；保留旧值并继续参与解析，不删除'})
                    for record, ip in plan:
                        change = {'id': record.id, 'current': record.value, 'target': ip,
                                  'status': 'unchanged' if record.value == ip else 'planned'}
                        item['records'].append(change)
                        if record.value == ip:
                            continue
                        result['planned'] += 1
                        if not config.dry_run:
                            if self.stop.is_set():
                                raise AppError('服务正在停止，剩余记录未执行')
                            change['status'] = 'applying'
                            operation = {'scope': target_scope(target), 'record_id': record.id,
                                         'before': record.value, 'target': ip, 'started_at': now()}
                            pending = self.state.snapshot()['pending_operations'] + [operation]
                            if len(pending) > 1000:
                                raise AppError('未核验操作超过安全限制，请先处理历史未决操作')
                            # Persist intent before any external write; a kill cannot erase all evidence.
                            self.state.save(targets=copy.deepcopy(result['targets']), current_run=copy.deepcopy(result),
                                            pending_operations=pending)
                            try:
                                warning = provider.update(record, ip)
                                if isinstance(warning, str):
                                    change['warning'] = warning
                            except Exception:
                                change['status'] = 'failed_or_unverified'
                                raise
                            change['status'] = 'verified'
                            result['changed'] += 1
                            self.state.save(targets=copy.deepcopy(result['targets']), current_run=copy.deepcopy(result),
                                            pending_operations=[op for op in pending if op != operation])
                except Exception as error:
                    item['status'], item['error'] = 'error', safe_error(error)
                    result['status'] = 'error'
            if result['status'] == 'error':
                result['error'] = '部分目标失败；已完成且核验的修改不会自动回滚，请查看目标详情'
        except Exception as error:
            result['status'], result['error'] = 'error', safe_error(error)
        result['finished_at'] = now()
        try:
            result['notification'] = notify(config, self.http, result)
        except Exception as error:
            result['notification'] = 'error'
            result['notification_error'] = safe_error(error)
        snapshot = self.state.snapshot()
        updates = {'status': result['status'], 'last_error': result['error'], 'targets': result['targets'],
                   'last_finished': result['finished_at'], 'runs': snapshot['runs'] + 1,
                   'history': (snapshot['history'] + [result])[-30:], 'current_run': None}
        if result['status'] == 'ok':
            updates['last_success'] = result['finished_at']
        self.state.save(**updates)
        self.session_success = result['status'] == 'ok'
        LOG.info('%s', json.dumps(result, ensure_ascii=False))
        return result

    def loop(self):
        while not self.stop.is_set():
            with self.condition:
                delay = min(self.next_due, self.next_source_due) - time.monotonic()
                if delay > 0 or self.lock.locked():
                    self.condition.wait(timeout=min(max(delay, 0.05), 0.25))
                    continue
            self.schedule_context.active = True
            try:
                # When both are due (including startup), refresh before DNS.
                if time.monotonic() >= self.next_source_due:
                    self.refresh_source(scheduled=True)
                if time.monotonic() >= self.next_due:
                    self.run()
            except BusyError:
                pass
            except AppError:
                if self.stop.is_set():
                    break
                raise
            finally:
                self.schedule_context.active = False

    def readiness(self):
        snapshot = self.state.snapshot()
        success = snapshot.get('last_success')
        ready = False
        source_ready = (self.source_ips is not None and self.source_success_time is not None
                        and 0 <= time.monotonic() - self.source_success_time <= max(120, self.config.source_interval_seconds * 2))
        snapshot['source_valid'] = self.source_ips is not None
        snapshot['source_ready'] = source_ready
        if (source_ready and self.session_success and success and not snapshot['pending_operations']
                and snapshot.get('dry_run') == self.config.dry_run and snapshot['status'] in ('ok', 'running')):
            elapsed = time.time() - datetime.fromisoformat(success).timestamp()
            ready = 0 <= elapsed <= max(120, self.config.interval_seconds * 2)
        return ready, snapshot
