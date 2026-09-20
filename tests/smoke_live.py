"""Opt-in real-source/native-daemon smoke test; never configures DNS credentials."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen


def main():
    with tempfile.TemporaryDirectory(prefix='cfspeed-live-') as directory:
        root = Path(directory)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        config = root / 'config.toml'
        config.write_text(f'[service]\nstate_dir="{root / "data"}"\nport={port}\ninterval_seconds=30\ndry_run=true\n')
        with (root / 'daemon.log').open('w+') as logs:
            process = subprocess.Popen([sys.executable, '-m', 'cfspeed', 'serve', '--config', str(config)],
                                       stdout=logs, stderr=logs)
            try:
                deadline = time.monotonic() + 55
                state = {}
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError('native daemon exited prematurely')
                    try:
                        with urlopen(f'http://127.0.0.1:{port}/healthz', timeout=2) as response:
                            assert json.load(response)['status'] == 'alive'
                        state = json.loads((root / 'data/state.json').read_text())
                        if state.get('runs', 0) >= 2:
                            break
                    except (URLError, OSError):
                        pass
                    time.sleep(0.25)
                assert state.get('runs', 0) >= 2, 'scheduler did not execute twice'
                assert state['status'] == 'ok' and state['dry_run'] and state['targets'] == []
                with urlopen(f'http://127.0.0.1:{port}/readyz', timeout=2) as response:
                    assert json.load(response)['ready']
                with urlopen(f'http://127.0.0.1:{port}/ipTop.html', timeout=2) as response:
                    assert response.read().decode().strip() == ','.join(state['ips'])
                process.terminate()
                assert process.wait(timeout=10) == 0, 'SIGTERM did not shut down cleanly'
                saved = json.loads((root / 'data/state.json').read_text())
                assert saved['runs'] >= 2 and saved['current_run'] is None
                print(json.dumps({'native_daemon': 'passed', 'real_source': True, 'scheduled_runs': saved['runs'],
                                  'ips': saved['ips'], 'dns_writes': 0, 'sigterm_exit': process.returncode}, ensure_ascii=False))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                if process.returncode:
                    logs.seek(0)
                    print(logs.read(), file=sys.stderr)


if __name__ == '__main__':
    main()
