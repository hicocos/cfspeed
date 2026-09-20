"""Offline shortage policy: deterministic sampling and no real DNS writes."""
from dataclasses import replace
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from cfspeed.config import AppError, Config
from cfspeed.providers import Cloudflare, DNSPod, Record, plan_records
from cfspeed.runtime import Runner, State
from test_cfspeed import CF, POD, ENV, IP, OLD, FakeHTTP, cfraw, podraw


class ShortageTests(unittest.TestCase):
    def test_sample_exact_count_and_stable_selected_assignment(self):
        records = [Record(str(n), 'x', OLD if n == 3 else '8.8.8.8', 600) for n in range(1, 5)]
        with patch('cfspeed.providers.random.sample', return_value=[records[2], records[0]]) as sample:
            plan = plan_records(records, [IP, OLD])
        sample.assert_called_once_with(records, 2)
        self.assertEqual([(r.id, ip) for r, ip in plan], [('1', IP), ('3', OLD)])
        self.assertEqual(len({r.id for r, _ in plan}), 2)
        self.assertEqual(len(records), 4)

    def test_equal_and_more_never_sample(self):
        records = [Record('2', 'x', OLD, 600), Record('1', 'x', IP, 600)]
        with patch('cfspeed.providers.random.sample') as sample:
            for ips in ([OLD, IP], [OLD, IP, '8.8.8.8']):
                self.assertTrue(all(r.value == ip for r, ip in plan_records(records, ips)))
        sample.assert_not_called()

    def test_invalid_inputs_reject_before_sampling(self):
        r = Record('1', 'x', OLD, 600)
        with patch('cfspeed.providers.random.sample') as sample:
            for records, ips in (([], [IP]), ([r], []), ([r, r], [IP]), ([r, r], [IP, OLD])):
                with self.subTest(records=records, ips=ips), self.assertRaises(AppError):
                    plan_records(records, ips)
        sample.assert_not_called()

    def test_apply_only_selected_preview_zero_writes_and_independent_selection(self):
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run), TemporaryDirectory() as directory:
                records = [Record(str(n), CF.name, '8.8.8.8', 600) for n in (1, 2, 3)]
                stored = {r.id: r for r in records}
                writes = []
                class Provider:
                    def list_records(self):
                        return list(stored.values())
                    def update(self, before, ip):
                        writes.append(before.id)
                        stored[before.id] = replace(before, value=ip)
                config = replace(Config(), state_dir=directory, max_ips=2, targets=(CF,),
                                 dry_run=dry_run, credential_values=ENV)
                runner = Runner(config, State(directory), FakeHTTP(source=f'{IP},{OLD}'.encode()),
                                provider_factory=lambda target, http: Provider())
                with patch('cfspeed.providers.random.sample', return_value=[records[2], records[0]]) as sample:
                    result = runner.run()
                assert result is not None
                sample.assert_called_once_with(records, 2)
                self.assertEqual(result['status'], 'ok')
                self.assertEqual(result['planned'], 2)
                self.assertEqual(result['changed'], 0 if dry_run else 2)
                self.assertEqual(writes, [] if dry_run else ['1', '3'])
                self.assertEqual(stored['2'], records[1])
                self.assertEqual(len(stored), 3)
                skipped = [r for r in result['targets'][0]['records'] if r['status'] == 'unselected']
                self.assertEqual([r['id'] for r in skipped], ['2'])
                self.assertEqual(skipped[0]['current'], skipped[0]['target'])
                self.assertIn('继续参与解析', skipped[0]['warning'])
                self.assertEqual(runner.http.record_limit, 1000)
                self.assertEqual(runner.state.snapshot()['pending_operations'], [])
                if dry_run:
                    with patch('cfspeed.providers.random.sample', return_value=[records[1], records[2]]) as again:
                        second = runner.run()
                    assert second is not None
                    again.assert_called_once()
                    self.assertEqual([r['id'] for r in second['targets'][0]['records'] if r['status'] == 'unselected'], ['1'])
                    self.assertEqual(writes, [])

    def test_both_provider_listing_independent_of_max_ips_and_bounded(self):
        for target, adapter in ((CF, Cloudflare), (POD, DNSPod)):
            for count in (3, 1001):
                with self.subTest(provider=target.provider, count=count), TemporaryDirectory() as directory:
                    if adapter is Cloudflare:
                        response = {'success': True, 'result': [cfraw(id=str(n) * 32) for n in (1, 2, 3)],
                                    'result_info': {'total_count': count, 'total_pages': 1}}
                    else:
                        response = {'Response': {'RecordList': [podraw(id=n) for n in (1, 2, 3)],
                                                 'RecordCountInfo': {'TotalCount': count}}}
                    http = FakeHTTP([response], source=f'{IP},{OLD}'.encode())
                    config = replace(Config(), state_dir=directory, max_ips=2, targets=(target,), credential_values=ENV)
                    runner = Runner(config, State(directory), http)
                    with patch('cfspeed.providers.random.sample', side_effect=lambda records, k: records[:k]):
                        result = runner.run()
                    assert result is not None
                    self.assertEqual(getattr(http, 'record_limit'), 1000)
                    self.assertEqual(result['status'], 'ok' if count == 3 else 'error')
                    if count == 3:
                        self.assertEqual(len(result['targets'][0]['records']), 3)
                    else:
                        self.assertIn('安全限制', result['targets'][0]['error'])
                    self.assertEqual(result['changed'], 0)
                    self.assertEqual(len(http.calls), 2)


if __name__ == '__main__':
    unittest.main()
