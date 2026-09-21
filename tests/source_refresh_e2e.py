"""Isolated browser acceptance: real HTTP/backend/public source; injected failure only."""
from dataclasses import replace
from pathlib import Path
import os
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cfspeed.admin import Admin
from cfspeed.config import Config, AppError
from cfspeed.runtime import Runner, State
from cfspeed.server import create_server
from cfspeed.network import fetch_ips
from playwright.sync_api import sync_playwright, expect

for key in ('CFSPEED_PUBLIC_ORIGIN', 'CFSPEED_TRUSTED_PROXIES', 'CFSPEED_MASTER_KEY_FILE'):
    os.environ.pop(key, None)
with tempfile.TemporaryDirectory(prefix='cfspeed-source-e2e-') as directory:
    runner = Runner(Config(state_dir=directory), State(directory))
    admin = Admin(runner)
    runner.config = replace(runner.config, port=0)
    server = create_server(runner, static_dir=ROOT/'web/dist')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    release = threading.Event()
    errors = []
    def held_fetch(config, http):
        assert release.wait(15)
        return fetch_ips(config, http)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/admin/login')
            page.get_by_label('用户名', exact=True).fill('admin')
            page.get_by_label('密码', exact=True).fill(admin.initial_path.read_text().strip())
            page.get_by_role('button', name='登录', exact=True).click()
            expect(page.get_by_role('heading', name='总览与统计')).to_be_visible()
            button = page.get_by_role('button', name='刷新 IP', exact=True)
            expect(button).to_be_enabled()
            deadline = runner.next_due
            dns_at = runner.state.snapshot()['next_dns_run_at']
            with patch('cfspeed.runtime.fetch_ips', side_effect=held_fetch):
                button.click()
                expect(page.get_by_role('button', name='刷新中…', exact=True)).to_be_disabled()
                expect(page.get_by_role('button', name='立即预览', exact=True)).to_be_disabled()
                release.set()
                expect(page.get_by_text('IP 已刷新。', exact=True)).to_be_visible(timeout=60000)
            expect(page.locator('.ip-row code').first).to_be_visible()
            assert runner.next_due == deadline
            assert runner.state.snapshot()['next_dns_run_at'] == dns_at
            ips = runner.state.snapshot()['ips']
            assert page.locator('.ip-row code').all_text_contents() == ips
            with patch('cfspeed.runtime.fetch_ips', side_effect=AppError('隔离测试：来源暂不可用')):
                button.click()
                expect(page.locator('.notice').filter(has_text='隔离测试：来源暂不可用').first).to_be_visible(timeout=15000)
                expect(button).to_be_enabled()
                assert page.locator('.ip-row code').all_text_contents() == ips
            page.set_viewport_size({'width': 390, 'height': 844})
            button.click()
            expect(page.get_by_text('IP 已刷新。', exact=True)).to_be_visible(timeout=60000)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert runner.next_due == deadline
            assert all(item['kind'] == 'source' for item in runner.state.snapshot()['history'])
            assert not runner.config.targets and not errors
            print('PASS: real public-source refresh; loading/disabled; injected failure/retained IPs; mobile retry; DNS deadline unchanged; zero DNS jobs; no browser errors')
            browser.close()
    finally:
        release.set()
        runner.stop.set()
        if runner.worker:
            runner.worker.join(30)
        server.shutdown()
        server.server_close()
        thread.join(5)
