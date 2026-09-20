"""Settings copy/default regressions; offline only."""
from pathlib import Path
import unittest

from cfspeed.config import AppError, Config, load_config, parse_config
from cfspeed.network import parse_ips

ROOT = Path(__file__).resolve().parents[1]


class SettingsDefaultsTests(unittest.TestCase):
    def test_default_interval_and_seconds_compatibility(self):
        self.assertEqual(Config().interval_seconds, 21600)
        self.assertEqual(parse_config({}).interval_seconds, 21600)
        for name in ('config.example.toml', 'config.docker.example.toml'):
            self.assertEqual(load_config(ROOT / name).interval_seconds, 21600)
        for seconds in (30, 123, 300, 21600, 604800):
            self.assertEqual(parse_config({'service': {'interval_seconds': seconds}}).interval_seconds, seconds)
        for seconds in (29, 604801, 30.5, True):
            with self.assertRaises(AppError):
                parse_config({'service': {'interval_seconds': seconds}})

    def test_default_limit_and_examples(self):
        self.assertEqual(Config().max_ips, 30)
        self.assertEqual(parse_config({}).max_ips, 30)
        for name in ('config.example.toml', 'config.docker.example.toml'):
            with self.subTest(name=name):
                self.assertEqual(load_config(ROOT / name).max_ips, 30)
        self.assertEqual(parse_config({'service': {'max_ips': 100}}).max_ips, 100)

    def test_parser_default_boundary_and_explicit_override(self):
        ips = [f'104.18.41.{i}' for i in range(1, 32)]
        self.assertEqual(parse_ips(','.join(ips[:30])), ips[:30])
        with self.assertRaises(AppError):
            parse_ips(','.join(ips))
        self.assertEqual(parse_ips(','.join(ips), 100), ips)

    def test_source_help_removed_without_behavior_change(self):
        text = (ROOT / 'web/src/pages/Settings.vue').read_text()
        for removed in ('分别请求电信', '读取来源提供的公网 IPv4 列表。',
                        '切换来源自动回到预览模式，保存后生效。', '并停用旧 GitHub 定时任务'):
            self.assertNotIn(removed, text)
        self.assertIn('@change="form.dry_run=true"', text)
        self.assertIn('https://ip.v2too.top/api/nodes', text)
        self.assertIn('正式模式会修改已有 A 记录；启用前请核对目标。', text)
        self.assertIn('请先完成一次预览。', text)
        self.assertNotIn('并停用原 GitHub DNS 工作流', text)
        self.assertIn('多条修改不是事务，不会自动回滚。', text)


if __name__ == '__main__':
    unittest.main()
