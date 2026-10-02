"""来源：本项目原创。计量口径与实际本地宿主联动；所有模型数值均为合成测试。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_supermarket.common import MarketError
from ai_supermarket.host import TaskResources
from ai_supermarket.measurement import TaskMeter, compare_runs, summarize_runs
from ai_supermarket.service import Market


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / 'measurement.sqlite'
        self.meter = self.new()

    def new(self, run='run', arm='market', **changes):
        settings = dict(experiment='experiment', case='case', arm=arm,
                        configuration_hash='a' * 64, evidence_mode='synthetic')
        settings.update(changes)
        return TaskMeter(self.db, run, 'alice', **settings)

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(MarketError) as caught:
            function(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)

    def test_missing_usage_and_incomplete_coverage_never_become_zero_cost(self):
        call = self.meter.begin('model', 'provider-model-v1', 'task')
        self.meter.end(call, 'succeeded', 12)
        self.meter.finish('pass', 'shape-check', 'artifact-check', coverage_complete=True)
        report = self.meter.report()
        self.assertIsNone(report['input_tokens'])
        self.assertIsNone(report['total_cost_micro_cny'])
        self.assertFalse(report['savings_proven'])
        self.assertEqual(0, report['known_cost_micro_cny'])
        other = self.new('other')
        other.finish('pass', 'shape', 'test')
        self.assertIsNone(other.report()['total_cost_micro_cny'])

    def test_input_and_output_coverage_are_independent(self):
        for index, (input_tokens, output_tokens) in enumerate(((100, None), (None, 20), (0, None))):
            with self.subTest(input_tokens=input_tokens, output_tokens=output_tokens):
                meter = self.new('partial-' + str(index))
                call = meter.begin('model', 'model', 'task')
                meter.end(call, 'succeeded', 10)
                meter.receipt(call, 'provider:partial-' + str(index),
                              input_tokens=input_tokens, output_tokens=output_tokens)
                self.assertIsNone(meter.report()['input_tokens'])
                self.assertIsNone(meter.report()['output_tokens'])
                meter.finish('unknown', 'check', 'ref', coverage_complete=True)
                report = meter.report()
                self.assertEqual(input_tokens, report['input_tokens'])
                self.assertEqual(output_tokens, report['output_tokens'])
                self.assertIsNone(report['total_cost_micro_cny'])
                self.assertFalse(report['savings_proven'])

    def test_missing_usage_in_one_call_does_not_become_partial_total(self):
        for index, inputs in enumerate((100, None)):
            call = self.meter.begin('model', 'model', 'task')
            self.meter.end(call, 'succeeded', 10)
            self.meter.receipt(call, 'provider:mixed-' + str(index),
                               input_tokens=inputs, output_tokens=20)
        self.meter.finish('pass', 'check', 'ref', coverage_complete=True)
        report = self.meter.report()
        self.assertIsNone(report['input_tokens'])
        self.assertEqual(40, report['output_tokens'])
        baseline = {**report, 'arm': 'direct'}
        self.assertEqual('insufficient_evidence', compare_runs(baseline, report)['status'])

    def test_cached_tokens_are_subset_and_late_cost_enriches_original_receipt(self):
        call = self.meter.begin('model', 'model', 'task')
        self.meter.end(call, 'succeeded', 10)
        self.meter.receipt(call, 'provider:request-1', input_tokens=100, output_tokens=20)
        self.meter.finish('pass', 'check', 'ref', coverage_complete=True)
        self.assertIsNone(self.meter.report()['total_cost_micro_cny'])
        self.meter.receipt(call, 'provider:request-1', cached_input_tokens=80, cost_micro_cny=125)
        self.meter.receipt(call, 'provider:request-1', cost_micro_cny=125)
        report = self.meter.report()
        self.assertEqual((100, 20, 125), (report['input_tokens'], report['output_tokens'], report['total_cost_micro_cny']))
        self.error('measurement_conflict', self.meter.receipt, call, 'provider:request-1', input_tokens=101)
        self.error('measurement_conflict', self.meter.receipt, call, 'provider:new', cost_micro_cny=125)

    def test_duplicate_receipt_across_retries_and_runs_cannot_double_count(self):
        first = self.meter.begin('model', 'model', 'task')
        second = self.new('second').begin('model', 'model', 'task')
        self.meter.receipt(first, 'provider:one', cost_micro_cny=10)
        self.error('duplicate_receipt', self.new('second').receipt, second, 'provider:one', cost_micro_cny=10)

    def test_crash_recovery_keeps_call_and_blocks_premature_finish(self):
        call = self.meter.begin('model', 'model', 'child')
        resumed = self.new()
        self.assertEqual(call, resumed.call_records()[0]['id'])
        self.error('measurement_in_flight', resumed.finish, 'pass', 'check', 'ref', coverage_complete=True)
        resumed.end(call, 'unknown', 20)
        resumed.finish('unknown', 'check', 'ref', coverage_complete=True)
        self.assertEqual(1, resumed.report()['unknown_calls'])
        self.assertIsNone(resumed.report()['total_cost_micro_cny'])

    def test_observed_exception_is_recorded_once_without_retry_or_body(self):
        called = []
        def failing():
            called.append(1)
            raise TimeoutError('private response secret')
        with self.assertRaises(TimeoutError):
            self.meter.observe('model', 'provider-model', 'task', failing)
        self.assertEqual([1], called)
        records = self.meter.call_records()
        self.assertEqual('unknown', records[0]['state'])
        self.assertNotIn('private response secret', str(records))

    def test_children_and_failed_attempts_all_contribute_to_cost(self):
        for task, state, price in [('main', 'failed', 30), ('child', 'succeeded', 40)]:
            call = self.meter.begin('model', 'model', task)
            self.meter.end(call, state, 10)
            self.meter.receipt(call, 'provider:' + task, input_tokens=10, output_tokens=2, cost_micro_cny=price)
        self.meter.finish('pass', 'check', 'ref', coverage_complete=True)
        self.assertEqual(70, self.meter.report()['total_cost_micro_cny'])
        self.assertEqual(2, self.meter.report()['model_calls'])

    def test_business_failure_and_meter_failure_do_not_hide_original_error(self):
        result, call = self.meter.observe('resource', 'resource', 'task', lambda: {'status': 'payment_unavailable'})
        self.assertEqual('failed', self.meter.call_records()[0]['state'])
        self.error('invalid_params', self.meter.receipt, call, 'provider:bad-kind', input_tokens=10)
        with patch.object(self.meter, 'end', side_effect=OSError('meter disk failure')):
            with self.assertRaisesRegex(TimeoutError, 'operation timeout'):
                self.meter.observe('model', 'model', 'task',
                                   lambda: (_ for _ in ()).throw(TimeoutError('operation timeout')))
        self.assertEqual('in_flight', self.meter.call_records()[1]['state'])

    def test_same_run_conflict_and_other_run_isolation(self):
        self.error('measurement_conflict', self.new, arm='direct')
        call = self.meter.begin('model', 'model', 'task')
        self.error('not_found', self.new('other').receipt, call, 'provider:one', cost_micro_cny=0)
        self.error('measurement_conflict', TaskMeter, self.db, 'run', 'bob', experiment='experiment',
                   case='case', arm='market', configuration_hash='a'*64, evidence_mode='synthetic')

    def test_concurrent_events_and_receipts_preserve_every_call(self):
        def operate(i):
            call = self.meter.begin('model', 'model', 'child-' + str(i))
            self.meter.end(call, 'succeeded', 1)
            self.meter.receipt(call, 'provider:' + str(i), input_tokens=1, output_tokens=1, cost_micro_cny=1)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(operate, range(12)))
        self.meter.finish('pass', 'check', 'ref', coverage_complete=True)
        self.assertEqual((12, 12), (self.meter.report()['calls'], self.meter.report()['total_cost_micro_cny']))

    def test_numeric_validation_finish_binding_and_no_new_calls_after_finish(self):
        call = self.meter.begin('model', 'model', 'task')
        for value in (True, -1, 0.5, float('nan')):
            self.error('invalid_params', self.meter.receipt, call, 'ref', cost_micro_cny=value)
        self.error('invalid_params', self.meter.receipt, call, 'ref', input_tokens=1, cached_input_tokens=2)
        self.meter.end(call, 'succeeded', 1)
        self.meter.finish('fail', 'check', 'ref', coverage_complete=True)
        self.meter.finish('fail', 'check', 'ref', coverage_complete=True)
        self.error('measurement_conflict', self.meter.finish, 'pass', 'check', 'ref', coverage_complete=True)
        self.error('measurement_closed', self.meter.begin, 'model', 'model', 'task')

    def test_local_host_automatically_observes_search_use_and_existing_check(self):
        market = Market(self.root / 'market.sqlite', 'alice')
        task = TaskResources(market, 'task', '产品简报', kind='template', meter=self.meter)
        read = task.use('product-brief', '1.0.0', 'read')
        task.checked(read['usage_id'], 'shape-test', True)
        records = self.meter.call_records()
        self.assertEqual(['search', 'resource', 'feedback'], [r['kind'] for r in records])
        self.assertTrue(all(r['reply_bytes'] > 0 for r in records))
        self.assertFalse(any('content' in r for r in records))
        self.assertEqual(0, self.meter.report()['model_calls'])
        self.assertIsNone(self.meter.report()['total_cost_micro_cny'])
        self.error('measurement_scope_mismatch', TaskResources,
                   Market(self.root / 'market.sqlite', 'bob'), 'other', 'test', meter=self.meter)

    def test_paired_diagnostic_rejects_failed_missing_and_different_configuration(self):
        baseline = self.new('base', 'direct')
        for meter, price, tokens in [(baseline, 10, 100), (self.meter, 8, 80)]:
            call = meter.begin('model', 'model', 'task')
            meter.end(call, 'succeeded', 1)
            meter.receipt(call, 'provider:' + meter.run_id, input_tokens=tokens, output_tokens=10, cost_micro_cny=price)
            meter.finish('pass', 'check', 'ref', coverage_complete=True)
        result = compare_runs(baseline.report(), self.meter.report())
        self.assertEqual((-2, -20, False), (result['cost_delta_micro_cny'], result['token_delta'], result['savings_proven']))
        bad = {**self.meter.report(), 'outcome': 'fail'}
        self.assertEqual('insufficient_evidence', compare_runs(baseline.report(), bad)['status'])
        bad = {**self.meter.report(), 'evidence_mode': 'real'}
        self.error('incomparable_runs', compare_runs, baseline.report(), bad)

    def test_cohort_counts_failed_costs_and_zero_success_is_undefined(self):
        reports = []
        for run, outcome, cost in [('passed', 'pass', 30), ('failed', 'fail', 20)]:
            meter = self.new(run)
            call = meter.begin('other', 'local-cost-allocation', 'task')
            meter.end(call, 'succeeded', 1)
            meter.receipt(call, 'allocation:' + run, cost_micro_cny=cost)
            meter.finish(outcome, 'check', 'ref', coverage_complete=True)
            reports.append(meter.report())
        result = summarize_runs(reports)
        self.assertEqual({'numerator_micro_cny': 50, 'denominator_tasks': 1}, result['cost_per_qualified_task'])
        self.assertIsNone(summarize_runs([reports[1]])['cost_per_qualified_task'])
        self.error('measurement_conflict', summarize_runs, [reports[0], reports[0]])


if __name__ == '__main__':
    unittest.main()
