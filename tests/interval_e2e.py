"""Isolated real backend acceptance; public source only, no DNS targets/writes."""
from dataclasses import replace
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cfspeed.config import Config
from cfspeed.admin import Admin, load_saved_config
from cfspeed.runtime import Runner, State
from cfspeed.server import create_server

for key in ('CFSPEED_PUBLIC_ORIGIN', 'CFSPEED_TRUSTED_PROXIES', 'CFSPEED_MASTER_KEY_FILE'):
    os.environ.pop(key, None)
with tempfile.TemporaryDirectory(prefix='cfspeed-interval-e2e-') as directory:
    base = Config(state_dir=directory)
    assert base.interval_seconds == 21600
    runner = Runner(base, State(directory))
    admin = Admin(runner)
    runner.config = replace(runner.config, port=0)
    server = create_server(runner, static_dir=ROOT/'web/dist')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert not runner.config.targets and runner.config.dry_run
        for _ in range(2):
            result = runner.run()
            assert result is not None and result['status'] == 'ok'
        runner.next_due = time.monotonic() + base.interval_seconds
        subprocess.run([sys.executable, str(ROOT/'tests/web_e2e.py'), '--url',
                        f'http://127.0.0.1:{server.server_port}', '--credentials',
                        str(admin.initial_path)], cwd=ROOT, check=True)
        saved = load_saved_config(base)
        assert saved.interval_seconds == 21600
        assert not saved.targets and saved.dry_run
        print('ISOLATED_RELOAD: interval_seconds=21600; preview; no DNS targets')
    finally:
        runner.stop.set()
        if runner.worker:
            runner.worker.join(20)
        server.shutdown()
        server.server_close()
        thread.join(5)
