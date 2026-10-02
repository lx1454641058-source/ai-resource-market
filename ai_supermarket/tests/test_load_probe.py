"""来源：本项目原创。验证本机负载报告的判定、边界与真实网络；不把时延作脆弱断言。"""

import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch

from tools import market_load as probe


class LoadProbeTests(unittest.TestCase):
    def test_invalid_options_stop_before_creating_data_or_network(self):
        for size, workers, samples, workload in [(0, 1, 1, 'search'), (201, 1, 1, 'search'),
                (True, 1, 1, 'search'), (1, 16, 1, 'search'), (1, 1, 97, 'search'),
                (1, 1, 17, 'journey'), (1, 1, 1, 'other')]:
            with patch.object(probe.tempfile, 'TemporaryDirectory', side_effect=AssertionError('unexpected side effect')):
                with self.assertRaises(ValueError):
                    probe.run(size, workers, samples, workload)

    def test_nearest_rank_percentiles_keep_empty_and_failure_observations_explicit(self):
        self.assertIsNone(probe.percentiles([])['p95_ms'])
        values = list(reversed(range(1, 21)))
        result = probe.percentiles(values)
        self.assertEqual({'count': 20, 'p50_ms': 10, 'p95_ms': 19, 'max_ms': 20}, result)
        self.assertEqual(list(reversed(range(1, 21))), values)

    def test_http_200_alone_and_wrong_body_are_not_success(self):
        for tool, result in [('market_search', {'status': 'ok', 'items': []}),
                ('market_resolve', {'status': 'resolved', 'id': 'vendor:load-0000', 'compatibility': None}),
                ('market_acquire', {'status': 'acquired', 'acquisition_id': '', 'payment': 'not_required'}),
                ('market_read', {'status': 'read', 'usage_id': 'u', 'content': 'wrong'}),
                ('market_feedback', {'status': 'accepted', 'usage_id': ''})]:
            self.assertFalse(probe.valid_result(tool, 200, result, 'original'))
        self.assertFalse(probe.valid_result('market_feedback', 503, {'status': 'accepted', 'usage_id': 'u'}, ''))

    def test_expired_admission_does_not_send_or_retry(self):
        with patch.object(probe.http.client, 'HTTPConnection', side_effect=AssertionError('unexpected network')):
            result, trace = probe.request(1, 'synthetic-not-valid', 'market_search', {}, '', time.monotonic() - 1)
        self.assertIsNone(result)
        self.assertEqual('deadline', trace['business_status'])
        self.assertFalse(trace['valid'])
        self.assertGreaterEqual(trace['latency_ms'], 0)

    def test_rate_limit_and_bad_json_preserve_failure_not_fake_success(self):
        for status, body, expected in [(429, b'{"status":"error","error_code":"rate_limited"}', 'rate_limited'),
                                        (200, b'invalid-json', 'invalid_response')]:
            reply = SimpleNamespace(status=status, read=lambda _: body, getheader=lambda *_: '12')
            connection = SimpleNamespace(request=lambda *_: None, getresponse=lambda: reply, close=lambda: None)
            with patch.object(probe.http.client, 'HTTPConnection', return_value=connection) as factory:
                result, trace = probe.request(1, 'synthetic-not-valid', 'market_search', {}, '', time.monotonic() + 2)
            self.assertEqual(1, factory.call_count)
            self.assertEqual(expected, trace['business_status'])
            self.assertFalse(trace['valid'])
            self.assertEqual(status, trace['http_status'])
            self.assertNotIn('synthetic-not-valid', json.dumps(trace))

    def test_existing_output_refused_before_running_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.json'
            path.write_bytes(b'original')
            output = io.BytesIO()
            with patch.object(probe, 'run', side_effect=AssertionError('must not run')), \
                 patch.object(probe.sys, 'stdout', SimpleNamespace(buffer=output)):
                code = probe.main(['--output', str(path)])
            self.assertEqual(1, code)
            self.assertEqual('destination_exists', json.loads(output.getvalue())['error_code'])
            self.assertEqual(b'original', path.read_bytes())

    def test_timeout_during_body_keeps_unknown_with_phase_even_after_200(self):
        def timeout(_):
            raise TimeoutError('synthetic-sensitive-text')
        reply = SimpleNamespace(status=200, read=timeout)
        connection = SimpleNamespace(request=lambda *_: None, getresponse=lambda: reply, close=lambda: None)
        with patch.object(probe.http.client, 'HTTPConnection', return_value=connection) as factory:
            result, trace = probe.request(1, 'synthetic-not-valid', 'market_acquire', {}, '', time.monotonic() + 2)
        self.assertIsNone(result)
        self.assertEqual(1, factory.call_count)
        self.assertEqual(200, trace['http_status'])
        self.assertEqual('transport_unknown', trace['business_status'])
        self.assertEqual('response_body', trace['phase'])
        self.assertEqual('timeout', trace['failure_kind'])
        self.assertNotIn('synthetic-sensitive-text', json.dumps(trace))


@unittest.skipUnless(importlib.util.find_spec('uvicorn'), '需要既有HTTP依赖；不自动安装')
class RealLoadProbeTests(unittest.TestCase):
    def test_real_search_reports_counts_and_no_business_writes(self):
        report = probe.run(catalog_size=3, workers=4, samples=4, workload='search')
        self.assertEqual('observed', report['status'])
        self.assertEqual(4, report['completed_jobs'])
        self.assertEqual({'200': 4}, report['http_status_counts'])
        self.assertEqual({'acquisitions': 0, 'usage_events': 0, 'feedback': 0}, report['ledger_counts'])
        self.assertTrue(report['ledger_valid'])
        self.assertTrue(report['database_integrity_valid'])
        self.assertTrue(report['foreign_keys_valid'])
        self.assertTrue(report['request_records_consistent'])
        self.assertEqual(0, report['unknown_feedback_count'])
        self.assertFalse(report['production_approved'])
        self.assertFalse(report['server_stderr_observed'])
        self.assertEqual(3, len(report['warmup_requests']))
        self.assertNotIn('ak_', json.dumps(report))

    def test_real_journeys_preserve_exact_usage_and_unknown_feedback(self):
        report = probe.run(catalog_size=3, workers=4, samples=4, workload='journey')
        self.assertEqual('observed', report['status'])
        self.assertEqual(4, report['completed_jobs'])
        self.assertEqual({'200': 20}, report['http_status_counts'])
        self.assertEqual({'acquisitions': 4, 'usage_events': 4, 'feedback': 4}, report['ledger_counts'])
        self.assertEqual(4, report['unknown_feedback_count'])
        self.assertEqual(5, len(report['per_tool_latency']))
        self.assertEqual(0, report['real_model_calls'])
        self.assertEqual(0, report['real_payments'])
        self.assertTrue(report['server_alive_after_load'])
        self.assertFalse(report['server_stderr_observed'])
        for job in report['jobs']:
            self.assertTrue(job['valid'])
            self.assertEqual(5, len(job['requests']))
        self.assertTrue(report['request_records_consistent'])
        for record in report['request_records']:
            self.assertEqual({'response_and_record'}, set(record['writes'].values()))

    def test_lost_write_receipts_reconcile_without_retry_or_fake_completion(self):
        original_request = probe.request
        for lost_tool in ('market_acquire', 'market_read', 'market_feedback'):
            with self.subTest(tool=lost_tool):
                calls = []
                def drop_receipt(*args):
                    result, trace = original_request(*args)
                    calls.append(args[2])
                    if args[2] == lost_tool:
                        self.assertTrue(trace['valid'])
                        return None, {**trace, 'valid': False, 'business_status': 'transport_unknown',
                                      'failure_kind': 'injected_receipt_loss'}
                    return result, trace
                with patch.object(probe, 'request', side_effect=drop_receipt):
                    report = probe.run(3, 1, 1, 'journey')
                self.assertEqual('failed', report['status'])
                self.assertEqual(1, report['failed_jobs'])
                self.assertEqual(0, report['completed_jobs'])
                self.assertEqual(1, calls.count(lost_tool))
                self.assertTrue(report['database_integrity_valid'])
                self.assertTrue(report['foreign_keys_valid'])
                self.assertTrue(report['request_records_consistent'])
                self.assertEqual('record_without_valid_response', report['request_records'][0]['writes'][lost_tool])

    def test_reconciliation_rejects_unattempted_writes_and_wrong_resource(self):
        with tempfile.TemporaryDirectory() as directory:
            database, _, body, _ = probe.seed(Path(directory), 1, 1, time.monotonic() + 30)
            buyer = probe.Exchange(database, 'load-buyer-0', 'buyer')
            buyer.acquire('vendor:load-0000', '1.0.0', 'acquire-0', 'personal_use')
            jobs = [{'index': 0, 'valid': False, 'requests': []}]
            report = probe.reconcile(database, jobs, 'journey', body)
            self.assertFalse(report['request_records_consistent'])
            self.assertEqual('unexpected_record', report['request_records'][0]['writes']['market_acquire'])
            jobs[0]['requests'] = [{'tool': 'market_acquire', 'valid': True}]
            report = probe.reconcile(database, jobs, 'journey', body)
            self.assertFalse(report['request_records_consistent'])
            self.assertEqual('response_record_mismatch', report['request_records'][0]['writes']['market_acquire'])
            report = probe.reconcile(database, jobs, 'journey', 'different body')
            self.assertTrue(report['database_integrity_valid'])
            self.assertFalse(report['request_records_consistent'])

    def test_failed_warmup_still_stops_its_own_server(self):
        stop = probe.stop_server
        stopped = []
        def observed_stop(process):
            result = stop(process)
            stopped.append(process.poll())
            return result
        with patch.object(probe, 'request', return_value=(None, {'valid': False})), \
             patch.object(probe, 'stop_server', side_effect=observed_stop):
            with self.assertRaisesRegex(RuntimeError, 'warmup_failed'):
                probe.run(3, 1, 1, 'search')
        self.assertEqual(1, len(stopped))
        self.assertIsNotNone(stopped[0])


if __name__ == '__main__':
    unittest.main()
