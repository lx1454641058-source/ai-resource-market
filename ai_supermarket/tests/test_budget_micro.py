"""来源：本项目原创。微额累计与预算/计量/恢复验收；全部费用为合成数据。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_supermarket.budget import BudgetLedger, MICRO_ACTUAL
from ai_supermarket.common import MarketError
from ai_supermarket.measurement import TaskMeter
from ai_supermarket import recovery
from ai_supermarket.tests import test_recovery


class MicroBudgetTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / 'budget.sqlite'
        self.ledger = BudgetLedger(self.path)
        self.ledger.create('root', 'owner', 1, 'synthetic:limit-not-payment')
        self.ledger.grant_task('root', 'main', 'alice')
        self.ledger.grant_task('root', 'child', 'bob', 'main')
        self.main = self.ledger.scope('root', 'main', 'alice')
        self.child = self.ledger.scope('root', 'child', 'bob')

    def error(self, code, function, *args):
        with self.assertRaises(MarketError) as caught:
            function(*args)
        self.assertEqual(code, caught.exception.code)

    def reserve(self, amount, request='request', scope=None):
        return (scope or self.main).reserve_micro(request, 'model', 'synthetic:quote', amount)['reservation_id']

    def test_small_costs_accumulate_without_per_call_rounding(self):
        for index in range(10):
            rid = self.reserve(999, str(index))
            self.main.begin(rid)
            self.main.settle_micro(rid, 999, 'receipt:' + str(index))
        report = self.child.summary()
        self.assertEqual((9990, 10, 1, 0), (report['spent_micro_cny'], report['available_micro_cny'],
                         report['spent_cents'], report['available_cents']))
        self.error('budget_exceeded', self.child.reserve, 'cent', 'model', 'quote', 1)
        rid = self.reserve(10, 'last', self.child)
        self.child.begin(rid)
        self.child.settle_micro(rid, 10, 'receipt:last')
        self.assertEqual(10000, self.main.summary()['spent_micro_cny'])
        self.error('budget_exceeded', self.main.reserve_micro, 'overflow', 'model', 'quote', 1)

    def test_cent_and_micro_reservations_share_exact_root_limit(self):
        self.ledger.create('mixed', 'owner', 3, 'synthetic:mixed')
        self.ledger.grant_task('mixed', 'task', 'alice')
        scope = self.ledger.scope('mixed', 'task', 'alice')
        cent = scope.reserve('cent', 'resource', 'item', 1)['reservation_id']
        micro = scope.reserve_micro('micro', 'model', 'model', 19999)['reservation_id']
        self.assertEqual(1, scope.summary()['available_micro_cny'])
        self.error('budget_exceeded', scope.reserve, 'more', 'resource', 'item2', 1)
        scope.reserve_micro('last', 'dependency', 'dependency', 1)
        scope.begin(cent)
        scope.settle(cent, 1, 'receipt:cent')
        self.assertEqual((10000, 20000, 0), tuple(scope.summary()[key] for key in
                         ('spent_micro_cny', 'held_micro_cny', 'available_micro_cny')))
        self.error('amount_unit_mismatch', scope.settle_micro, cent, 1, 'receipt:cent')
        self.error('amount_unit_mismatch', scope.settle, micro, 1, 'receipt:micro')

    def test_exact_request_and_receipt_idempotency_not_rounded_value(self):
        rid = self.reserve(300)
        self.assertEqual(rid, self.reserve(300))
        self.error('idempotency_conflict', self.main.reserve_micro, 'request', 'model', 'synthetic:quote', 301)
        self.error('idempotency_conflict', self.main.reserve, 'request', 'model', 'synthetic:quote', 1)
        self.main.begin(rid)
        self.main.settle_micro(rid, 100, 'receipt')
        self.main.settle_micro(rid, 100, 'receipt')
        self.error('idempotency_conflict', self.main.settle_micro, rid, 101, 'receipt')
        self.assertEqual(100, self.main.summary()['spent_micro_cny'])

    def test_invalid_amounts_and_zero_are_distinct(self):
        for invalid in (None, True, -1, 1.5, '1', 10**12 + 1):
            self.error('invalid_params', self.main.reserve_micro, 'bad', 'model', 'quote', invalid)
        rid = self.reserve(0)
        self.main.begin(rid)
        for invalid in (None, True, -1, 1.5, '1', 10**12 + 1):
            self.error('invalid_params', self.main.settle_micro, rid, invalid, 'receipt')
        self.assertIsNone(self.main.status(rid)['actual_micro_cny'])
        self.main.settle_micro(rid, 0, 'receipt:explicit-zero')
        self.assertEqual(0, self.main.status(rid)['actual_micro_cny'])

    def test_concurrent_children_reserve_exact_limit(self):
        for index in range(8):
            self.ledger.grant_task('root', str(index), 'worker', 'main')
        def take(index):
            scope = BudgetLedger(self.path).scope('root', str(index), 'worker')
            try:
                scope.reserve_micro('request', 'model', 'quote', 2001)
                return True
            except MarketError as exc:
                self.assertEqual('budget_exceeded', exc.code)
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(take, range(8)))
        self.assertEqual(4, sum(results))
        self.assertEqual((8004, 1996), (self.main.summary()['held_micro_cny'], self.main.summary()['available_micro_cny']))

    def test_independent_processes_preserve_unknown_exposure_on_exit(self):
        for index in range(5):
            self.ledger.grant_task('root', str(index), 'worker', 'main')
        script = '''import json,sys
from ai_supermarket.budget import BudgetLedger
from ai_supermarket.common import MarketError
scope=BudgetLedger(sys.argv[1]).scope('root',sys.argv[2],'worker')
try:
    row=scope.reserve_micro('request','model','quote',3001)
    scope.begin(row['reservation_id'])
    print(json.dumps(row))
except MarketError as exc:
    print(json.dumps({'error':exc.code}))
'''
        def take(index):
            result = subprocess.run([sys.executable, '-c', script, str(self.path), str(index)],
                                    capture_output=True, timeout=15)
            self.assertEqual((0, b''), (result.returncode, result.stderr))
            return json.loads(result.stdout)
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(take, range(5)))
        self.assertEqual(3, sum('reservation_id' in r for r in results))
        report = BudgetLedger(self.path).scope('root', 'main', 'alice').summary()
        self.assertEqual((9003, 997), (report['held_micro_cny'], report['available_micro_cny']))

    def test_one_micro_overrun_is_not_hidden_by_same_cent_display(self):
        rid = self.reserve(1000)
        self.reserve(9000, 'other', self.child)
        self.main.begin(rid)
        self.main.settle_micro(rid, 1001, 'receipt:overrun')
        report = self.main.summary()
        self.assertEqual(('paused', 1, 1, 0), (report['state'], report['over_budget_micro_cny'],
                         report['over_budget_cents'], report['available_micro_cny']))
        self.error('budget_exceeded', self.ledger.set_state, 'root', 'active', report['revision'])
        self.error('budget_inactive', self.child.reserve_micro, 'new', 'model', 'quote', 0)

    def test_unknown_late_receipt_survives_close_and_does_not_reopen(self):
        rid = self.reserve(9000)
        self.main.begin(rid)
        self.main.mark_unknown(rid)
        self.ledger.set_state('root', 'closed', 1)
        restarted = BudgetLedger(self.path).scope('root', 'main', 'alice')
        self.assertEqual(9000, restarted.summary()['held_micro_cny'])
        self.error('invalid_state', restarted.cancel, rid)
        self.assertFalse(restarted.begin(rid)['execute'])
        restarted.settle_micro(rid, 8001, 'receipt:late')
        self.assertEqual(('closed', 8001, 0), tuple(restarted.summary()[key] for key in
                         ('state', 'spent_micro_cny', 'available_micro_cny')))

    def test_receipt_cannot_cross_units_or_tasks(self):
        first = self.reserve(4000)
        second = self.reserve(4000, 'second', self.child)
        self.main.begin(first)
        self.child.begin(second)
        self.main.settle_micro(first, 3000, 'receipt:one')
        self.error('evidence_conflict', self.child.settle_micro, second, 2000, 'receipt:one')
        self.error('not_found', self.child.settle_micro, first, 3000, 'receipt:one')
        self.assertEqual(4000, self.main.summary()['held_micro_cny'])

    def test_wrong_wrapper_refused_before_callback_or_claim(self):
        rid = self.reserve(100)
        called = []
        self.error('amount_unit_mismatch', self.main.run_reserved, rid, lambda _: called.append(1), lambda _: (1, 'receipt'))
        self.assertEqual([], called)
        self.assertEqual('reserved', self.main.status(rid)['state'])

    def test_micro_callback_executes_once_under_concurrent_replay(self):
        called = []
        def run(_):
            scope = BudgetLedger(self.path).scope('root', 'main', 'alice')
            return scope.run_micro('one', 'model', 'quote', 123,
                                   lambda rid: called.append(rid), lambda _: (101, 'receipt:one'))
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(run, range(5)))
        self.assertEqual(1, len(called))
        self.assertEqual(1, sum(r['status'] == 'completed' for r in results))
        self.assertEqual(101, self.main.summary()['spent_micro_cny'])

    def test_storage_failure_rolls_back_exact_fee_and_preserves_original_error(self):
        rid = self.reserve(500)
        self.main.begin(rid)
        with patch.object(self.ledger, '_audit', side_effect=OSError('synthetic storage outage')):
            with self.assertRaises(OSError):
                self.main.settle_micro(rid, 251, 'receipt:atomic')
        self.assertEqual(('in_flight', None), (self.main.status(rid)['state'], self.main.status(rid)['actual_micro_cny']))
        self.assertEqual(500, self.main.summary()['held_micro_cny'])
        second = self.reserve(200, 'second')
        def failed(_):
            raise RuntimeError('synthetic provider failure')
        with patch.object(self.main, 'mark_unknown', side_effect=OSError('synthetic storage outage')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic provider failure'):
                self.main.run_reserved_micro(second, failed, lambda _: (0, 'not-used'))
        self.assertEqual('in_flight', self.main.status(second)['state'])

    def test_measured_receipt_and_budget_use_same_precision_without_model_call(self):
        meter = TaskMeter(self.root / 'measurement.sqlite', 'run', 'alice', experiment='experiment', case='case',
                         arm='market', configuration_hash='a' * 64, evidence_mode='synthetic')
        def operation(rid):
            call_id = meter.begin('model', 'synthetic-provider', 'main')
            meter.end(call_id, 'succeeded', 1)
            return {'call_id': call_id, 'receipt': 'synthetic:' + rid, 'cost': 125}
        def receipt(result):
            meter.receipt(result['call_id'], result['receipt'], input_tokens=100, output_tokens=10,
                          cost_micro_cny=result['cost'])
            return result['cost'], result['receipt']
        result = self.main.run_micro('measured', 'model', 'synthetic:quote', 150, operation, receipt)
        meter.finish('pass', 'synthetic-validator', 'synthetic-artifact', coverage_complete=True)
        self.assertEqual(125, result['budget']['actual_micro_cny'])
        self.assertEqual(meter.report()['total_cost_micro_cny'], self.main.summary()['spent_micro_cny'])
        self.assertFalse(meter.report()['savings_proven'])

    def test_corrupt_precision_refuses_status_and_new_reservations(self):
        rid = self.reserve(500)
        self.main.begin(rid)
        self.main.settle_micro(rid, 100, 'receipt')
        with self.ledger.store.connect(write=True) as db:
            db.execute('UPDATE meta SET value=? WHERE key=?', ('10001', MICRO_ACTUAL + rid))
        self.error('invalid_database', self.main.status, rid)
        self.error('invalid_database', self.main.reserve_micro, 'next', 'model', 'quote', 1)

    def test_snapshot_restore_preserves_micro_totals_and_blocks_corrupt_metadata(self):
        fixture = test_recovery.RecoveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        ledger = BudgetLedger(fixture.paths['budget'])
        ledger.create('micro-root', 'alice', 1, 'synthetic:authorization')
        ledger.grant_task('micro-root', 'main', 'alice')
        scope = ledger.scope('micro-root', 'main', 'alice')
        rid = scope.reserve_micro('used', 'model', 'quote', 500)['reservation_id']
        scope.begin(rid)
        scope.settle_micro(rid, 125, 'receipt:micro')
        waiting = scope.reserve_micro('pending', 'model', 'quote', 600)['reservation_id']
        scope.begin(waiting)
        scope.mark_unknown(waiting)
        recovery.snapshot_bundle(*fixture.paths.values(), fixture.snapshot)
        recovery.restore_bundle(fixture.snapshot, fixture.restored)
        restored = BudgetLedger(fixture.restored / 'budget.sqlite').scope('micro-root', 'main', 'alice')
        self.assertEqual(('paused', 125, 600), tuple(restored.summary()[key] for key in
                         ('state', 'spent_micro_cny', 'held_micro_cny')))
        restored.settle_micro(waiting, 575, 'receipt:late')
        self.assertEqual(700, restored.summary()['spent_micro_cny'])
        self.error('recovery_required', restored.ledger.set_state, 'micro-root', 'active', restored.summary()['revision'])
        self.error('recovery_required', restored.ledger.create, 'new-root', 'alice', 1, 'synthetic:new')
        self.error('budget_inactive', restored.reserve_micro, 'new', 'model', 'quote', 1)
        # Even if a previous operator left an active root, the pending market gate wins.
        with restored.ledger.store.connect(write=True) as db:
            db.execute("UPDATE budgets SET state='active' WHERE id='micro-root'")
        self.error('recovery_required', restored.reserve_micro, 'new', 'model', 'quote', 1)
        self.error('recovery_required', restored.ledger.grant_task, 'micro-root', 'extra', 'alice')
        with restored.ledger.store.connect(write=True) as db:
            db.execute("UPDATE budgets SET state='paused' WHERE id='micro-root'")
        recovery.release_bundle(fixture.restored, fixture.evidence)
        restored.ledger.set_state('micro-root', 'active', restored.summary()['revision'])
        fresh = restored.reserve_micro('new', 'model', 'quote', 1)
        self.assertTrue(restored.begin(fresh['reservation_id'])['execute'])
        with ledger.store.connect(write=True) as db:
            db.execute('INSERT INTO meta VALUES(?,?)', (MICRO_ACTUAL + 'orphan', '1'))
        self.error('invalid_database', recovery.snapshot_bundle, *fixture.paths.values(), fixture.root / 'bad-snapshot')
        self.assertFalse((fixture.root / 'bad-snapshot').exists())

    def test_partial_restore_cannot_initialize_or_use_unmarked_budget(self):
        (self.root / 'restore-started.json').write_text('{}')
        self.error('recovery_required', BudgetLedger, self.path)
        self.error('recovery_required', self.main.reserve_micro, 'next', 'model', 'quote', 1)

    def test_no_charge_cancellation_and_parent_close_keep_exact_semantics(self):
        rid = self.reserve(99, scope=self.child)
        self.child.begin(rid)
        self.child.mark_unknown(rid)
        self.ledger.close_task('root', 'main')
        self.error('budget_inactive', self.child.reserve_micro, 'next', 'model', 'quote', 1)
        self.child.confirm_no_charge(rid, 'synthetic:no-charge')
        self.child.confirm_no_charge(rid, 'synthetic:no-charge')
        self.assertEqual((0, 0), (self.main.summary()['held_micro_cny'], self.main.summary()['spent_micro_cny']))
        self.assertFalse(self.child.begin(rid)['execute'])
