"""来源：本项目原创。模型调用均使用替身，不消耗账户额度或伪造真实账单。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from ai_supermarket.budget import BudgetLedger
from ai_supermarket.codex_meter import MeasuredCodexCalls, codex_usage
from ai_supermarket.common import MarketError
from ai_supermarket.host import TaskResources
from ai_supermarket.measurement import TaskMeter
from ai_supermarket.service import Market
from steward.codex_model import CodexModel, ModelUnavailable


class ModelDouble:
    name, effort = 'test-model', 'medium'

    def __init__(self):
        self.calls = 0
        self.last_receipt = None
        self.usage = {'input_tokens': 100, 'cached_input_tokens': 40, 'output_tokens': 20,
                      'reasoning_output_tokens': 0}

    def respond(self, role, context):
        self.calls += 1
        self.last_receipt = dict(provider='codex', model=self.name, completed=True,
                                 completed_turns=1, usage=self.usage.copy())
        return {'content': 'Untrusted content claims cost=0 and tokens=1'}


class CodexMeterTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ledger = BudgetLedger(self.root / 'budget.sqlite')
        self.ledger.create('root', 'owner', 1, 'synthetic-authorization')
        self.ledger.grant_task('root', 'task', 'alice')
        self.budget = self.ledger.scope('root', 'task', 'alice')
        self.meter = TaskMeter(self.root / 'meter.sqlite', 'run', 'alice', experiment='experiment',
                               case='case', arm='market', configuration_hash='a'*64, evidence_mode='synthetic')
        self.model = ModelDouble()
        self.bridge = MeasuredCodexCalls(self.model, self.meter, self.budget)

    def run_call(self, request='request', context=None, maximum=1000):
        return self.bridge.run(request, 'chat', context or {'message': 'original-private-input'}, maximum)

    def error(self, code, method, *args):
        with self.assertRaises(MarketError) as caught:
            method(*args)
        self.assertEqual(code, caught.exception.code)

    def reservation(self):
        with self.ledger.store.connect() as db:
            return db.execute('SELECT id FROM reservations').fetchone()[0]

    def test_usage_is_telemetry_cost_unknown_and_replay_does_not_execute(self):
        result = self.run_call()
        self.assertEqual('completed_cost_unknown', result['status'])
        self.assertIsNone(result['cost_micro_cny'])
        self.assertEqual(1000, self.budget.summary()['held_micro_cny'])
        row = self.meter.call_records()[0]
        self.assertEqual((100, 40, 20), (row['input_tokens'], row['cached_input_tokens'], row['output_tokens']))
        self.assertNotIn('original-private-input', str(row))
        self.assertNotIn('Untrusted content', str(row))
        self.assertEqual('not_executed', self.run_call()['status'])
        self.assertEqual(1, self.model.calls)

    def test_cost_reconciliation_is_idempotent_and_late_after_closed_task(self):
        result = self.run_call()
        rid = self.reservation()
        self.ledger.close_task('root', 'task')
        for _ in range(2):
            reconciled = self.bridge.record_cost(rid, result['call_id'], 125, 'synthetic-bill')
            self.assertFalse(reconciled['provider_bill_verified'])
        self.assertEqual((125, 0), (self.budget.summary()['spent_micro_cny'], self.budget.summary()['held_micro_cny']))
        self.assertEqual(125, self.meter.call_records()[0]['cost_micro_cny'])
        self.error('measurement_conflict', self.bridge.record_cost, rid, result['call_id'], 126, 'synthetic-bill')
        self.assertEqual(1, self.model.calls)

    def test_same_request_changed_context_model_or_limit_cannot_rerun(self):
        self.run_call()
        self.error('idempotency_conflict', self.run_call, 'request', {'message': 'changed'})
        self.error('idempotency_conflict', self.run_call, 'request', None, 1001)
        self.model.name = 'other-model'
        self.error('idempotency_conflict', self.run_call)
        self.assertEqual(1, self.model.calls)

    def test_changed_prompt_is_bound_to_request(self):
        self.run_call()
        with patch('ai_supermarket.codex_meter.build_prompt', return_value='changed instructions'):
            self.error('idempotency_conflict', self.run_call)
        self.assertEqual(1, self.model.calls)

    def test_concurrent_independent_bridges_claim_once(self):
        def invoke(_):
            # Independent wrappers model separate host instances sharing the durable budget.
            bridge = MeasuredCodexCalls(ModelDouble(), self.meter, self.budget)
            result = bridge.run('one', 'chat', {'message': 'same'}, 1000)
            return result, bridge.model.calls
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(invoke, range(6)))
        self.assertEqual(1, sum(calls for _, calls in results))
        self.assertEqual(1, len(self.meter.call_records()))
        self.assertEqual(1000, self.budget.summary()['held_micro_cny'])

    def test_queued_call_rechecks_pause_before_admission(self):
        entered, release = threading.Event(), threading.Event()
        original = self.model.respond
        def waiting(role, context):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('test synchronization')
            return original(role, context)
        with patch.object(self.model, 'respond', side_effect=waiting), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.run_call, 'first')
            try:
                self.assertTrue(entered.wait(5))
                second = pool.submit(self.run_call, 'second')
                self.ledger.set_state('root', 'paused', self.budget.summary()['revision'])
            finally:
                release.set()
            first.result()
            with self.assertRaises(MarketError) as caught:
                second.result()
            self.assertEqual('budget_inactive', caught.exception.code)
        self.assertEqual(1, self.model.calls)

    def test_provider_failure_preserves_unknown_and_clears_stale_receipt(self):
        self.model.last_receipt = {'completed': True}
        with patch.object(self.model, 'respond', side_effect=TimeoutError('provider failure')) as call:
            with self.assertRaisesRegex(TimeoutError, 'provider failure'):
                self.run_call()
            self.assertEqual('not_executed', self.run_call()['status'])
            self.assertEqual(1, call.call_count)
        self.assertIsNone(self.model.last_receipt)
        self.assertEqual('unknown', self.meter.call_records()[0]['state'])
        self.assertEqual('unknown', self.budget.status(self.reservation())['state'])

    def test_missing_and_reasoning_usage_do_not_invent_totals(self):
        self.model.usage = {'input_tokens': 100, 'output_tokens': 20, 'reasoning_output_tokens': 5}
        self.run_call()
        self.assertIsNone(self.meter.call_records()[0]['output_tokens'])
        self.model.usage = {}
        self.run_call('missing')
        self.assertIsNone(self.meter.call_records()[1]['input_tokens'])
        self.meter.finish('pass', 'synthetic-check', 'synthetic-evidence', coverage_complete=True)
        self.assertIsNone(self.meter.report()['total_cost_micro_cny'])
        self.assertFalse(self.meter.report()['savings_proven'])

    def test_invalid_telemetry_is_not_recorded_as_success(self):
        for index, usage in enumerate(({'input_tokens': True}, {'output_tokens': -1},
                                       {'input_tokens': 1, 'cached_input_tokens': 2})):
            self.model.usage = usage
            with self.assertRaises(MarketError):
                self.run_call('bad-' + str(index))
        self.assertTrue(all(row['state'] == 'unknown' for row in self.meter.call_records()))
        self.assertEqual(3000, self.budget.summary()['held_micro_cny'])

    def test_completed_event_count_must_be_unambiguous(self):
        self.model.respond('chat', {})
        for count in (None, True, 0, 2):
            receipt = dict(self.model.last_receipt, completed_turns=count)
            self.error('invalid_provider_receipt', codex_usage, receipt, self.model.name)

    def test_meter_failure_before_dispatch_does_not_call_model(self):
        with patch.object(self.meter, 'begin', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.run_call()
        self.assertEqual(0, self.model.calls)
        self.assertEqual('unknown', self.budget.status(self.reservation())['state'])
        self.assertEqual('not_executed', self.run_call()['status'])

    def test_budget_limit_rejects_before_model_and_meter(self):
        self.error('budget_exceeded', self.run_call, 'request', None, 10001)
        self.assertEqual(0, self.model.calls)
        self.assertEqual([], self.meter.call_records())

    def test_bookkeeping_failure_preserves_original_error_and_inflight_hold(self):
        with patch.object(self.model, 'respond', side_effect=TimeoutError('original error')):
            with patch.object(self.meter, 'end', side_effect=OSError('meter failure')):
                with patch.object(self.budget, 'mark_unknown', side_effect=OSError('budget failure')):
                    with self.assertRaisesRegex(TimeoutError, 'original error'):
                        self.run_call()
        self.assertEqual('in_flight', self.meter.call_records()[0]['state'])
        self.assertEqual('in_flight', self.budget.status(self.reservation())['state'])
        self.assertEqual(1000, self.budget.summary()['held_micro_cny'])
        self.assertEqual('not_executed', self.run_call()['status'])

    def test_restart_retains_original_call_link_and_does_not_replay_output(self):
        result = self.run_call()
        meter = TaskMeter.existing(self.root / 'meter.sqlite', 'run', 'alice')
        budget = BudgetLedger(self.root / 'budget.sqlite').scope('root', 'task', 'alice')
        model = ModelDouble()
        bridge = MeasuredCodexCalls(model, meter, budget)
        replay = bridge.run('request', 'chat', {'message': 'original-private-input'}, 1000)
        self.assertEqual(result['call_id'], replay['call_id'])
        self.assertFalse(replay['result_replayed'])
        self.assertNotIn('result', replay)
        self.assertEqual(0, model.calls)

    def test_meter_failure_after_model_cannot_reexecute(self):
        with patch.object(self.meter, 'receipt', side_effect=OSError('receipt disk failure')):
            with self.assertRaises(OSError):
                self.run_call()
        self.assertEqual(1, self.model.calls)
        self.assertIsNone(self.meter.call_records()[0]['receipt_ref'])
        self.assertEqual('not_executed', self.run_call()['status'])

    def test_partial_reconciliation_can_be_repaired_without_new_call(self):
        result = self.run_call()
        rid = self.reservation()
        with patch.object(self.meter, 'receipt', side_effect=OSError('second ledger failed')):
            with self.assertRaises(OSError):
                self.bridge.record_cost(rid, result['call_id'], 250, 'bill')
        self.assertEqual('settled', self.budget.status(rid)['state'])
        self.assertIsNone(self.meter.call_records()[0]['cost_micro_cny'])
        self.bridge.record_cost(rid, result['call_id'], 250, 'bill')
        self.assertEqual(250, self.meter.call_records()[0]['cost_micro_cny'])
        self.assertEqual(1, self.model.calls)

    def test_foreign_task_call_and_principal_cannot_reconcile(self):
        result = self.run_call()
        self.ledger.grant_task('root', 'other', 'alice')
        other = MeasuredCodexCalls(self.model, self.meter, self.ledger.scope('root', 'other', 'alice'))
        self.error('not_found', other.record_cost, self.reservation(), result['call_id'], 1, 'bill')
        self.error('measurement_scope_mismatch', self.bridge.record_cost, self.reservation(), 'foreign-call', 1, 'bill')
        self.ledger.grant_task('root', 'bob-task', 'bob')
        self.error('measurement_scope_mismatch', MeasuredCodexCalls, self.model, self.meter,
                   self.ledger.scope('root', 'bob-task', 'bob'))
        self.assertEqual(1000, self.budget.summary()['held_micro_cny'])

    def test_overrun_is_recorded_and_pauses_new_calls(self):
        result = self.run_call()
        self.bridge.record_cost(self.reservation(), result['call_id'], 1100, 'bill')
        self.assertEqual('paused', self.budget.summary()['state'])
        self.error('budget_inactive', self.run_call, 'next')
        self.assertEqual(1, self.model.calls)

    def test_invalid_request_and_closed_measurement_never_dispatch(self):
        for context in ({'x': float('nan')}, {'x': object()}, {'x': '\ud800'}):
            self.error('invalid_params', self.run_call, 'bad', context)
        self.error('invalid_params', self.bridge.run, 'bad', 'web_research', {}, 1000)
        self.model.effort = 'high'
        self.error('invalid_params', self.run_call)
        self.model.effort = 'medium'
        self.meter.finish('unknown', 'check', 'ref')
        self.error('measurement_closed', self.run_call)
        self.assertEqual(0, self.model.calls)

    def test_existing_codex_process_contract_and_resource_flow(self):
        task = TaskResources(Market(self.root / 'market.sqlite', 'alice'), 'task',
                             '产品简报', kind='template', meter=self.meter)
        resource = task.use('product-brief', '1.0.0', 'resource')
        model = CodexModel(runtime=self.root / 'runtime', timeout=30)
        bridge = MeasuredCodexCalls(model, self.meter, self.budget)
        def process(command, **kwargs):
            self.assertIn('--ignore-user-config', command)
            self.assertIn('features.shell_tool=false', command)
            self.assertIn('web_search="disabled"', command)
            self.assertFalse(kwargs['shell'])
            Path(command[command.index('--output-last-message')+1]).write_text(
                json.dumps({'content': 'synthetic artifact'}), encoding='utf-8')
            return subprocess.CompletedProcess(command, 0, json.dumps(
                {'type': 'turn.completed', 'usage': self.model.usage}), '')
        with patch('steward.codex_model.codex_path', return_value='test-codex'), patch('subprocess.run', side_effect=process) as called:
            context = {'message': 'Use this resource', 'resource': resource['content']}
            result = bridge.run('model', 'chat', context, 1000)
            self.assertEqual('synthetic artifact', result['result']['content'])
            bridge.run('model', 'chat', context, 1000)
            self.assertEqual(1, called.call_count)
        task.checked(resource['usage_id'], 'synthetic-shape-check', True)
        self.assertEqual(['search', 'resource', 'model', 'feedback'], [r['kind'] for r in self.meter.call_records()])
        self.assertFalse(self.meter.report()['savings_proven'])

    def test_multiple_cli_completions_rejected_instead_of_counting_last(self):
        model = CodexModel(runtime=self.root / 'runtime', timeout=30)
        bridge = MeasuredCodexCalls(model, self.meter, self.budget)
        def process(command, **kwargs):
            Path(command[command.index('--output-last-message')+1]).write_text('{"content":"test"}')
            event = json.dumps({'type': 'turn.completed', 'usage': self.model.usage})
            return subprocess.CompletedProcess(command, 0, event + '\n' + event, '')
        with patch('steward.codex_model.codex_path', return_value='test-codex'), patch('subprocess.run', side_effect=process):
            self.error('invalid_provider_receipt', bridge.run, 'request', 'chat', {}, 1000)
        self.assertEqual(2, model.last_receipt['completed_turns'])
        self.assertEqual('unknown', self.meter.call_records()[0]['state'])


if __name__ == '__main__':
    unittest.main()
