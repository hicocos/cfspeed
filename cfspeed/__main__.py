"""python -m cfspeed [run|serve|check] --config config.toml"""
import argparse
from dataclasses import replace
import json
import logging
import os
from pathlib import Path
import signal
import sys
import threading
from .config import AppError, load_config
from .admin import Admin, load_saved_config
from .runtime import ProcessLock, Runner, State, safe_error
from .server import create_server


def main(argv=None):
    parser = argparse.ArgumentParser(description='cfspeed 独立 DNS 同步服务（不执行测速）')
    parser.add_argument('command', choices=['run', 'serve', 'check'])
    parser.add_argument('--config', default='config.toml')
    parser.add_argument('--dry-run', action='store_true', help='强制预览模式，不写 DNS、不推送通知')
    parser.add_argument('--state-dir', help='覆盖数据目录（预览可用独立目录避免与服务冲突）')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format='%(asctime)s %(levelname)s %(message)s')
    try:
        config = load_config(args.config)
        if args.dry_run:
            config = replace(config, dry_run=True)
        if args.state_dir:
            config = replace(config, state_dir=args.state_dir)
        if args.command == 'check':
            config = load_saved_config(config, force_preview=args.dry_run)
            config.validate_credentials()
            print(json.dumps({'valid': True, 'mode': 'preview' if config.dry_run else 'apply',
                              'targets': len(config.targets)}, ensure_ascii=False))
            return 0
        with ProcessLock(config.state_dir):
            stop = threading.Event()
            runner = Runner(config, State(config.state_dir), stop=stop)
            if args.command == 'serve' or (Path(config.state_dir) / 'admin.json').exists():
                Admin(runner, bootstrap_password=os.environ.get('CFSPEED_TEST_ADMIN_PASSWORD'), force_preview=args.dry_run)
                config = runner.config
            if args.command == 'run':
                config.validate_credentials()
                result = runner.run()
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if result['status'] == 'ok' else 1
            server = create_server(runner)
            thread = threading.Thread(target=server.serve_forever, name='status-http', daemon=True)
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: stop.set())
            thread.start()
            logging.info('cfspeed 监听 %s:%s，模式=%s，周期=%ss', config.host, config.port,
                         'preview' if config.dry_run else 'apply', config.interval_seconds)
            try:
                runner.loop()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                if runner.worker:
                    runner.worker.join(timeout=65)
        return 0
    except (AppError, OSError, ValueError) as error:
        logging.error('%s', safe_error(error))
        return 1


if __name__ == '__main__':
    sys.exit(main())
