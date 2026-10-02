"""来源：本项目原创。共享预算、并发、未知结果与宿主接入验收，无真实扣款。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import threading
import unittest

from ai_supermarket.budget import BudgetLedger
from ai_supermarket.common import MarketError
from ai_supermarket.exchange import Exchange
from ai_supermarket.exchange_tools import ExchangeInterface
from ai_supermarket.host import TaskResources
from ai_supermarket.identity import Identities
from ai_supermarket.tests.test_exchange import CONTENT, CHECKS, listing


class BudgetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / 'budget.sqlite'
        self.now = [1000.0]
        self.ledger = BudgetLedger(self.path, clock=lambda: self.now[0])
        self.ledger.create('root', 'owner', 100, 'test-approved-limit')
        self.ledger.grant_task('root', 'main', 'alice')
        self.ledger.grant_task('root', 'child', 'bob', 'main')
        self.alice = self.ledger.scope('root', 'main', 'alice')
        self.bob = self.ledger.scope('root', 'child', 'bob')

    def error(self, code, method, *args):
        with self.assertRaises(MarketError) as cm:
            method(*args)
        self.assertEqual(code, cm.exception.code)

    def reserve(self, amount=60):
        return self.alice.reserve('operation', 'model', 'provider:quote-v1', amount)['reservation_id']

    def test_parent_and_child_share_exposure_and_actual_cost_releases_unused_amount(self):
        first = self.reserve(60)
        second = self.bob.reserve('second', 'dependency', 'tool:quote-v1', 40)['reservation_id']
        self.error('budget_exceeded', self.bob.reserve, 'third', 'resource', 'item:1', 1)
        self.alice.begin(first)
        self.alice.settle(first, 25, 'provider:receipt-1')
        summary = self.bob.summary()
        self.assertEqual((25, 40, 35), (summary['spent_cents'], summary['held_cents'], summary['available_cents']))
        self.bob.cancel(second)
        self.assertEqual(75, self.alice.summary()['available_cents'])
        self.assertFalse(summary['payment_authorized'])

    def test_concurrent_children_cannot_overreserve_root(self):
        for index in range(10):
            self.ledger.grant_task('root', 'worker-' + str(index), 'agent-' + str(index), 'main')
        def reserve(index):
            try:
                local = BudgetLedger(self.path).scope('root', 'worker-' + str(index), 'agent-' + str(index))
                local.reserve('request', 'model', 'provider:quote', 30)
                return True
            except MarketError as exc:
                self.assertEqual('budget_exceeded', exc.code)
                return False
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(reserve, range(10)))
        self.assertEqual(3, sum(results))
        self.assertEqual(90, self.alice.summary()['held_cents'])

    def test_same_operation_concurrently_claims_exactly_once(self):
        called, lock = [], threading.Lock()
        def operation(reservation_id):
            with lock:
                called.append(reservation_id)
            return 'checked-output'
        def run(_):
            scope = BudgetLedger(self.path).scope('root', 'main', 'alice')
            return scope.run('request', 'model', 'provider:quote', 60, operation, lambda _: (45, 'provider:receipt'))
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(run, range(8)))
        self.assertEqual(1, len(called))
        self.assertEqual(1, sum(result['status'] == 'completed' for result in results))
        self.assertEqual((45, 0), (self.alice.summary()['spent_cents'], self.alice.summary()['held_cents']))

    def test_independent_processes_share_limit_and_lost_worker_keeps_exposure(self):
        for index in range(4):
            self.ledger.grant_task('root', f'process-{index}', f'agent-{index}', 'main')
        script = """import json,sys
from ai_supermarket.budget import BudgetLedger
from ai_supermarket.common import MarketError
scope = BudgetLedger(sys.argv[1]).scope('root', 'process-' + sys.argv[2], 'agent-' + sys.argv[2])
try:
    record = scope.reserve('one-call', 'model', 'provider:quote', 40)
    scope.begin(record['reservation_id'])
    print(json.dumps(record))
except MarketError as exc:
    print(json.dumps({'error': exc.code}))
"""
        def run(index):
            process = subprocess.run([sys.executable, '-c', script, str(self.path), str(index)],
                                     capture_output=True, timeout=15)
            self.assertEqual(0, process.returncode)
            self.assertEqual(b'', process.stderr)
            return json.loads(process.stdout)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run, range(4)))
        self.assertEqual(2, sum('reservation_id' in result for result in results))
        restarted = BudgetLedger(self.path).scope('root', 'main', 'alice')
        self.assertEqual(80, restarted.summary()['held_cents'])
        self.error('budget_exceeded', restarted.reserve, 'another', 'model', 'quote:2', 21)

    def test_reservation_replay_binding_and_cancel_does_not_reopen_it(self):
        rid = self.reserve()
        self.assertEqual(rid, self.reserve())
        self.error('idempotency_conflict', self.alice.reserve, 'operation', 'model', 'changed', 60)
        self.alice.cancel(rid)
        self.assertEqual('cancelled', self.alice.reserve('operation', 'model', 'provider:quote-v1', 60)['state'])
        self.assertFalse(self.alice.begin(rid)['execute'])
        self.assertEqual(100, self.alice.summary()['available_cents'])

    def test_unknown_and_in_flight_hold_after_time_restart_pause_and_close(self):
        rid = self.reserve(100)
        self.alice.begin(rid)
        self.error('invalid_state', self.alice.cancel, rid)
        self.alice.mark_unknown(rid)
        self.now[0] += 86_400 * 30
        restarted = BudgetLedger(self.path).scope('root', 'main', 'alice')
        self.assertEqual(100, restarted.summary()['held_cents'])
        self.assertFalse(restarted.begin(rid)['execute'])
        self.ledger.set_state('root', 'closed', 1)
        self.assertEqual(100, self.alice.summary()['held_cents'])
        self.error('invalid_state', self.alice.cancel, rid)
        self.alice.settle(rid, 80, 'provider:late-receipt')
        self.assertEqual((80, 0, 0), (self.alice.summary()['spent_cents'], self.alice.summary()['held_cents'],
                                    self.alice.summary()['available_cents']))
        self.error('budget_inactive', self.ledger.set_state, 'root', 'active', 2)

    def test_unknown_needs_explicit_no_charge_evidence_before_release(self):
        rid = self.reserve()
        self.alice.begin(rid)
        self.alice.mark_unknown(rid)
        self.error('invalid_state', self.alice.cancel, rid)
        self.error('invalid_params', self.alice.confirm_no_charge, rid, '')
        self.alice.confirm_no_charge(rid, 'provider:confirmed-no-charge')
        self.alice.confirm_no_charge(rid, 'provider:confirmed-no-charge')
        self.assertEqual(0, self.alice.summary()['held_cents'])
        self.error('invalid_state', self.alice.confirm_no_charge, rid, 'different-evidence')

    def test_real_cost_overrun_is_recorded_and_freezes_budget_instead_of_being_hidden(self):
        rid = self.reserve(80)
        self.alice.begin(rid)
        self.alice.settle(rid, 110, 'provider:unexpected-overcharge')
        state = self.alice.summary()
        self.assertEqual(('paused', 110, 10, 0), (state['state'], state['spent_cents'], state['over_budget_cents'], state['available_cents']))
        self.error('budget_exceeded', self.ledger.set_state, 'root', 'active', 2)
        self.error('budget_inactive', self.bob.reserve, 'new', 'model', 'quote:2', 1)
        self.alice.settle(rid, 110, 'provider:unexpected-overcharge')
        self.assertEqual(110, self.alice.summary()['spent_cents'])

    def test_same_receipt_cannot_settle_two_reservations_or_replace_previous_amount(self):
        first = self.reserve(40)
        second = self.bob.reserve('second', 'model', 'quote:2', 40)['reservation_id']
        self.alice.begin(first)
        self.bob.begin(second)
        self.alice.settle(first, 25, 'provider:one-receipt')
        self.error('evidence_conflict', self.bob.settle, second, 25, 'provider:one-receipt')
        self.error('idempotency_conflict', self.alice.settle, first, 20, 'provider:one-receipt')
        self.assertEqual(40, self.alice.summary()['held_cents'])

    def test_other_principal_and_other_task_cannot_access_reservation(self):
        rid = self.reserve()
        self.error('not_found', self.ledger.scope, 'root', 'main', 'bob')
        for method in (self.bob.status, self.bob.begin, self.bob.cancel):
            self.error('not_found', method, rid)
        self.ledger.grant_task('root', 'other', 'alice')
        self.error('not_found', self.ledger.scope('root', 'other', 'alice').status, rid)

    def test_parent_close_blocks_descendant_begin_and_new_reserve_but_allows_cancel(self):
        rid = self.bob.reserve('r', 'model', 'quote:1', 40)['reservation_id']
        self.ledger.close_task('root', 'main')
        self.error('budget_inactive', self.bob.begin, rid)
        self.error('budget_inactive', self.bob.reserve, 'new', 'model', 'quote:2', 1)
        self.bob.cancel(rid)
        self.ledger.grant_task('root', 'child', 'bob', 'main')
        self.error('budget_inactive', self.bob.reserve, 'new', 'model', 'quote:2', 1)

    def test_repeated_grant_and_create_cannot_raise_limit_or_rebind_task(self):
        self.error('budget_conflict', self.ledger.create, 'root', 'owner', 1000, 'test-approved-limit')
        self.error('budget_conflict', self.ledger.grant_task, 'root', 'child', 'alice', 'main')
        self.ledger.set_state('root', 'paused', 1)
        self.ledger.create('root', 'owner', 100, 'test-approved-limit')
        self.assertEqual('paused', self.alice.summary()['state'])
        self.error('revision_conflict', self.ledger.set_state, 'root', 'active', 1)

    def test_boolean_negative_and_unknown_money_never_authorize(self):
        for invalid in (True, -1, None, 1.5, '10'):
            self.error('invalid_params', self.alice.reserve, 'bad', 'model', 'quote', invalid)
        self.assertEqual(0, self.alice.summary()['held_cents'])

    def test_host_operation_exception_and_receipt_failure_remain_unknown_without_retry(self):
        calls = []
        def operation(rid):
            calls.append(rid)
            raise TimeoutError('simulated upstream loss')
        with self.assertRaises(TimeoutError):
            self.alice.run('run', 'model', 'provider:quote', 60, operation, lambda _: (0, 'unused'))
        retry = self.alice.run('run', 'model', 'provider:quote', 60, operation, lambda _: (0, 'unused'))
        self.assertEqual('unknown', retry['budget']['state'])
        self.assertEqual(1, len(calls))
        self.assertEqual(60, self.alice.summary()['held_cents'])

    def test_receipt_validation_failure_retains_hold_after_operation_succeeded(self):
        called = []
        def operation(rid):
            called.append(rid)
            return {'output': 'generated'}
        with self.assertRaises(MarketError):
            self.alice.run('bad-receipt', 'model', 'quote:1', 50, operation, lambda _: (None, 'bad'))
        self.assertEqual('unknown', self.alice.status(called[0])['state'])
        self.assertEqual(50, self.alice.summary()['held_cents'])

    def test_closed_parent_preserves_late_child_settlement_and_closed_root_stays_closed_on_overrun(self):
        rid = self.bob.reserve('call', 'model', 'quote:1', 50)['reservation_id']
        self.bob.begin(rid)
        self.ledger.close_task('root', 'main')
        self.ledger.set_state('root', 'closed', 1)
        self.bob.settle(rid, 70, 'provider:late-cost')
        summary = self.bob.summary()
        self.assertEqual(('closed', 70, 0), (summary['state'], summary['spent_cents'], summary['held_cents']))

    def test_wrong_existing_database_refused_without_modification(self):
        path = self.root / 'identity.sqlite'
        Identities(path)
        previous = path.read_bytes()
        self.error('wrong_database', BudgetLedger, path)
        self.assertEqual(previous, path.read_bytes())


class BudgetHostTests(unittest.TestCase):
    setUp = BudgetTests.setUp
    error = BudgetTests.error
    def publish(self, *, price=0):
        path = self.root / 'market.sqlite'
        seller, reviewer = Exchange(path, 'vendor', 'seller'), Exchange(path, 'reviewer', 'reviewer')
        meta = listing()
        meta['price_cents'] = price
        meta['usage_contract']['dependencies'] = [{'id': 'tool:1', 'license_name': 'MIT', 'max_cost_cents': 80}]
        seller.draft('brief', '1.0.0', meta, CONTENT, 'draft')
        seller.submit('vendor:brief', '1.0.0', 1, 'submit')
        reviewer.review('vendor:brief', '1.0.0', 1, 'approve', CHECKS, '本地契约演练', 'review')
        policy = {'capabilities': ['read_markdown'], 'permissions': [], 'dependencies': ['tool:1'], 'extra_budget_cents': 100}
        return Exchange(path, 'alice', host_policy=policy), Exchange(path, 'bob', host_policy=policy)

    def test_host_requires_shared_budget_and_repeated_resource_read_reserves_once(self):
        market, _ = self.publish()
        without = TaskResources(market, 'main', '产品简报')
        self.assertEqual('shared_budget_required', without.use('vendor:brief', '1.0.0', 'one')['error_code'])
        host = TaskResources(market, 'main', '产品简报', budget=self.alice)
        first = host.use('vendor:brief', '1.0.0', 'one')
        second = host.use('vendor:brief', '1.0.0', 'two')
        self.assertEqual('read', first['status'])
        self.assertEqual(first['budget']['reservation_id'], second['budget']['reservation_id'])
        self.assertEqual((0, 80), (self.alice.summary()['spent_cents'], self.alice.summary()['held_cents']))

    def test_actual_resource_flow_shares_budget_across_agents_and_uses_host_receipt(self):
        alice, bob = self.publish()
        first = TaskResources(alice, 'main', '产品简报', budget=self.alice).use('vendor:brief', '1.0.0', 'read')
        child = TaskResources(bob, 'child', '产品简报', budget=self.bob)
        self.error('budget_exceeded', child.use, 'vendor:brief', '1.0.0', 'read')
        executed = self.alice.run_reserved(first['budget']['reservation_id'],
                    lambda rid: {'checked': True}, lambda result: (20, 'test-host:receipt-one'))
        self.assertEqual('completed', executed['status'])
        self.assertEqual('read', child.use('vendor:brief', '1.0.0', 'read')['status'])
        self.assertEqual((20, 80, 0), (self.alice.summary()['spent_cents'], self.alice.summary()['held_cents'],
                                     self.alice.summary()['available_cents']))

    def test_paid_unavailable_does_not_reserve_and_later_free_resource_can_be_used(self):
        market, _ = self.publish(price=100)
        host = TaskResources(market, 'main', '产品简报', budget=self.alice)
        result = host.use('vendor:brief', '1.0.0', 'read')
        self.assertEqual('payment_unavailable', result['status'])
        self.assertEqual(0, self.alice.summary()['held_cents'])
        with self.ledger.store.connect() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM reservations').fetchone()[0])
        Exchange(self.root / 'market.sqlite', 'vendor', 'seller').reprice('vendor:brief', '1.0.0', 1, 0, 'free')
        self.assertEqual('read', host.use('vendor:brief', '1.0.0', 'read')['status'])
        self.assertEqual(80, self.alice.summary()['held_cents'])

    def test_budget_scope_mismatch_and_model_budget_mutations_are_refused(self):
        market, _ = self.publish()
        self.error('budget_scope_mismatch', TaskResources, market, 'main', '产品简报', None, None, self.bob)
        interface = ExchangeInterface(market)
        self.assertEqual(7, len(interface.tool_list()))
        self.assertEqual('unknown_tool', interface.call('budget_settle', {})['error_code'])


if __name__ == '__main__':
    unittest.main()
