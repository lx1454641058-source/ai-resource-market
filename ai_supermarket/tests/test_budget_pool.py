"""来源：本项目原创。同账本跨根任务总额、并发及恢复测试；仅合成费用。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_supermarket.budget import BudgetLedger, POOL_KEY, POOL_AUDIT, validate_pool_metadata
from ai_supermarket.common import MarketError, compact
from ai_supermarket import recovery
from ai_supermarket.tests import test_recovery, test_task_budget_gateway, test_codex_meter, test_http_live
from ai_supermarket.tests.test_exchange import listing, CONTENT
from ai_supermarket.codex_meter import MeasuredCodexCalls
from ai_supermarket.measurement import TaskMeter


class BudgetPoolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / 'budget.sqlite'
        self.ledger = BudgetLedger(self.path)
        self.first = self.make_scope('first', 'org-a')
        self.second = self.make_scope('second', 'org-a')
        self.other = self.make_scope('other', 'org-b')

    def make_scope(self, root, owner):
        self.ledger.create(root, owner, 100, 'synthetic:root-limit')
        self.ledger.grant_task(root, 'task', 'agent-' + root)
        return self.ledger.scope(root, 'task', 'agent-' + root)

    def configure(self, limit=10000, a=10000, b=10000, state='active', revision=0):
        return self.ledger.configure_pool(limit, {'org-a': a, 'org-b': b}, state,
                                          'synthetic:policy-not-payment', revision)

    def reserve(self, scope, amount, request='request'):
        return scope.reserve_micro(request, 'model', 'synthetic:quote', amount)['reservation_id']

    def error(self, expected, function, *args):
        with self.assertRaises(MarketError) as caught:
            function(*args)
        self.assertEqual(expected, caught.exception.code)

    def audit(self):
        with self.ledger.store.connect() as db:
            validate_pool_metadata(db, dict(db.execute('SELECT key,value FROM meta')))

    def test_legacy_ledgers_remain_opt_in_and_table_schema_is_unchanged(self):
        with self.ledger.store.connect() as db:
            before = list(db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name"))
        self.assertEqual('not_configured', self.ledger.pool_summary()['status'])
        self.reserve(self.first, 600000)
        self.reserve(self.second, 600000)
        self.error('budget_exceeded', self.configure)
        self.assertEqual('not_configured', self.ledger.pool_summary()['status'])
        self.configure(limit=2000000, a=2000000, b=2000000)
        with self.ledger.store.connect() as db:
            after = list(db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name"))
            self.assertEqual('2', db.execute("SELECT value FROM meta WHERE key='budget_schema_version'").fetchone()[0])
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])
        self.audit()

    def test_owner_limit_and_ledger_limit_both_apply_across_roots(self):
        self.configure(a=6000)
        self.reserve(self.first, 4000)
        self.reserve(self.second, 2000)
        self.error('budget_exceeded', self.reserve, self.second, 1, 'excess-owner')
        self.reserve(self.other, 4000)
        self.error('budget_exceeded', self.reserve, self.other, 1, 'excess-global')
        report = self.ledger.pool_summary()
        self.assertEqual((10000, 0), (report['global']['held_micro_cny'], report['global']['available_micro_cny']))
        self.assertEqual(6000, report['owners']['org-a']['held_micro_cny'])
        self.assertFalse(report['payment_authorized'])
        # Buyer-facing root summaries must not reveal another owner's accounting.
        self.assertNotIn('owners', self.first.summary())

    def test_mixed_cent_and_micro_amounts_are_not_rounded_per_reservation(self):
        self.configure(limit=20001, a=20001)
        self.first.reserve('cent', 'resource', 'item', 1)
        self.reserve(self.second, 10000)
        self.reserve(self.other, 1)
        self.error('budget_exceeded', self.reserve, self.other, 1, 'last')
        self.assertEqual(20001, self.ledger.pool_summary()['global']['held_micro_cny'])

    def test_unknown_keeps_capacity_until_receipt_or_no_charge_evidence(self):
        self.configure()
        rid = self.reserve(self.first, 6000)
        self.first.begin(rid)
        self.first.mark_unknown(rid)
        self.reserve(self.other, 4000)
        restarted = BudgetLedger(self.path)
        self.assertEqual(10000, restarted.pool_summary()['global']['held_micro_cny'])
        self.error('invalid_state', self.first.cancel, rid)
        self.error('budget_exceeded', self.reserve, self.second, 1)
        self.first.confirm_no_charge(rid, 'synthetic:no-charge')
        self.first.confirm_no_charge(rid, 'synthetic:no-charge')
        next_id = self.reserve(self.second, 6000)
        self.second.begin(next_id)
        self.second.settle_micro(next_id, 2501, 'synthetic:receipt')
        self.assertEqual((2501, 4000, 3499), tuple(restarted.pool_summary()['global'][field]
                         for field in ('spent_micro_cny', 'held_micro_cny', 'available_micro_cny')))

    def test_closed_root_and_new_root_cannot_reset_owner_spending(self):
        self.configure(a=5000)
        rid = self.reserve(self.first, 5000)
        self.first.begin(rid)
        self.first.settle_micro(rid, 5000, 'synthetic:receipt')
        self.ledger.set_state('first', 'closed', 1)
        fresh = self.make_scope('fresh', 'org-a')
        self.error('budget_exceeded', self.reserve, fresh, 1)
        self.error('budget_scope_mismatch', self.ledger.create, 'bypass', 'new-owner', 100, 'synthetic:new')
        self.error('budget_scope_mismatch', self.ledger.configure_pool, 10000,
                   {'org-b': 10000}, 'active', 'synthetic:remove-owner', 1)
        self.assertEqual(5000, self.ledger.pool_summary()['owners']['org-a']['spent_micro_cny'])

    def test_pause_blocks_pending_execution_and_new_tasks_but_not_state_lookup(self):
        self.configure()
        rid = self.reserve(self.first, 1000)
        self.configure(state='paused', revision=1)
        self.error('budget_inactive', self.first.begin, rid)
        self.error('budget_inactive', self.reserve, self.second, 0)
        self.error('budget_inactive', self.ledger.grant_task, 'second', 'new', 'agent')
        self.error('budget_inactive', self.ledger.create, 'new', 'org-a', 100, 'synthetic:new')
        self.assertEqual(rid, self.reserve(self.first, 1000))
        self.assertEqual('reserved', self.first.status(rid)['state'])
        self.first.cancel(rid)
        self.assertEqual(0, self.ledger.pool_summary()['global']['held_micro_cny'])
        self.configure(revision=2)
        self.reserve(self.second, 1)
        self.audit()

    def test_revision_conflict_and_closed_policy_cannot_be_reopened(self):
        self.configure()
        self.error('revision_conflict', self.configure)
        self.configure(state='closed', revision=1)
        self.error('budget_inactive', self.configure, 10000, 10000, 10000, 'active', 2)
        self.assertEqual(('closed', 2), tuple(self.ledger.pool_summary()[k] for k in ('state', 'revision')))
        self.audit()

    def test_existing_exposure_must_fit_before_activation_and_cannot_be_erased(self):
        self.reserve(self.first, 6000)
        self.error('budget_exceeded', self.configure, 5000)
        self.assertEqual('not_configured', self.ledger.pool_summary()['status'])
        self.configure(limit=5000, state='paused')
        self.assertEqual(1000, self.ledger.pool_summary()['global']['over_budget_micro_cny'])
        self.error('budget_exceeded', self.configure, 5000, 10000, 10000, 'active', 1)
        self.assertEqual(1, self.ledger.pool_summary()['revision'])
        self.configure(limit=6000, revision=1)
        self.assertEqual(6000, self.ledger.pool_summary()['global']['held_micro_cny'])

    def test_late_global_overrun_records_actual_and_stops_other_roots(self):
        self.configure()
        rid = self.reserve(self.first, 4000)
        pending = self.reserve(self.other, 6000)
        self.first.begin(rid)
        self.first.mark_unknown(rid)
        self.first.settle_micro(rid, 4001, 'synthetic:overrun')
        self.assertEqual(4001, self.first.status(rid)['actual_micro_cny'])
        report = self.ledger.pool_summary()
        self.assertEqual(('paused', 2, 1), (report['state'], report['revision'], report['global']['over_budget_micro_cny']))
        self.error('budget_inactive', self.other.begin, pending)
        self.error('budget_inactive', self.reserve, self.second, 0)
        self.first.settle_micro(rid, 4001, 'synthetic:overrun')
        self.assertEqual(2, self.ledger.pool_summary()['revision'])
        self.audit()

    def test_owner_overrun_blocks_same_owner_even_if_global_has_room(self):
        self.configure(a=4000)
        rid = self.reserve(self.first, 4000)
        self.first.begin(rid)
        self.first.settle_micro(rid, 4001, 'synthetic:overrun')
        self.assertEqual('active', self.ledger.pool_summary()['state'])
        self.error('budget_exceeded', self.reserve, self.second, 0)
        self.reserve(self.other, 5999)
        self.assertEqual(1, self.ledger.pool_summary()['owners']['org-a']['over_budget_micro_cny'])

    def test_closed_policy_still_records_late_actual_without_reopening(self):
        self.configure()
        rid = self.reserve(self.first, 100)
        self.first.begin(rid)
        self.configure(state='closed', revision=1)
        self.first.settle_micro(rid, 111, 'synthetic:late')
        report = self.ledger.pool_summary()
        self.assertEqual(('closed', 111, 0), (report['state'], report['global']['spent_micro_cny'],
                         report['global']['available_micro_cny']))
        self.audit()

    def test_policy_and_overrun_audit_are_atomic_on_storage_failure(self):
        with patch('ai_supermarket.budget.save_pool', side_effect=OSError('synthetic storage failure')):
            with self.assertRaises(OSError):
                self.configure()
        self.assertEqual('not_configured', self.ledger.pool_summary()['status'])
        self.configure(limit=100)
        rid = self.reserve(self.first, 100)
        self.first.begin(rid)
        with patch.object(self.ledger, '_audit', side_effect=OSError('synthetic storage failure')):
            with self.assertRaises(OSError):
                self.first.settle_micro(rid, 101, 'synthetic:late')
        self.assertEqual(('active', 1), tuple(self.ledger.pool_summary()[k] for k in ('state', 'revision')))
        self.assertEqual(('in_flight', None), tuple(self.first.status(rid)[k] for k in ('state', 'actual_micro_cny')))
        self.first.settle_micro(rid, 101, 'synthetic:late')
        self.audit()

    def test_concurrent_root_reservations_respect_shared_owner_limit(self):
        scopes = [self.make_scope('worker-' + str(i), 'org-a') for i in range(8)]
        self.configure(a=6001)
        def take(scope):
            current = BudgetLedger(self.path).scope(scope.root_id, scope.task_id, scope.principal)
            try:
                return current.reserve_micro('request', 'model', 'synthetic:quote', 2001)['state']
            except MarketError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(take, scopes))
        self.assertEqual(2, results.count('reserved'))
        self.assertEqual(6, results.count('budget_exceeded'))
        self.assertEqual(4002, self.ledger.pool_summary()['global']['held_micro_cny'])

    def test_independent_processes_cannot_overclaim_global_and_restart_keeps_holds(self):
        for i in range(6):
            self.make_scope('process-' + str(i), 'org-a' if i % 2 else 'org-b')
        self.configure()
        script = '''import json,sys
from ai_supermarket.budget import BudgetLedger
from ai_supermarket.common import MarketError
root='process-'+sys.argv[2]
scope=BudgetLedger(sys.argv[1]).scope(root,'task','agent-'+root)
try:
    row=scope.reserve_micro('request','model','synthetic:quote',3001)
    scope.begin(row['reservation_id'])
    print(json.dumps({'state':'in_flight'}))
except MarketError as exc:
    print(json.dumps({'state':exc.code}))
'''
        def take(index):
            result = subprocess.run([sys.executable, '-c', script, str(self.path), str(index)],
                                    capture_output=True, timeout=20)
            self.assertEqual((0, b''), (result.returncode, result.stderr))
            return json.loads(result.stdout)['state']
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(take, range(6)))
        self.assertEqual(3, results.count('in_flight'))
        self.assertEqual(3, results.count('budget_exceeded'))
        report = BudgetLedger(self.path).pool_summary()['global']
        self.assertEqual((9003, 997), (report['held_micro_cny'], report['available_micro_cny']))

    def test_invalid_or_uncovered_policies_do_not_activate_feature(self):
        for amount in (None, True, -1, '1', 1.5, 10**15 + 1):
            self.error('invalid_params', self.configure, amount)
        self.error('budget_scope_mismatch', self.ledger.configure_pool, 10000,
                   {'org-a': 10000}, 'active', 'synthetic:policy', 0)
        self.error('invalid_params', self.ledger.configure_pool, 10000,
                   {'org-a': True}, 'active', 'synthetic:policy', 0)
        self.assertEqual('not_configured', self.ledger.pool_summary()['status'])

    def test_corrupt_config_or_capability_marker_fails_closed_in_existing_instance(self):
        self.configure()
        with self.ledger.store.connect(write=True) as db:
            db.execute('UPDATE meta SET value=? WHERE key=?', ('{}', POOL_KEY))
        self.error('invalid_database', self.reserve, self.first, 1)
        self.error('invalid_database', BudgetLedger, self.path)
        with self.ledger.store.connect(write=True) as db:
            db.execute('DELETE FROM meta WHERE key=?', (POOL_KEY,))
        self.error('invalid_database', self.ledger.pool_summary)

    def test_cli_requires_existing_ledger_and_exact_bounded_json(self):
        def cli(path, *args):
            process = subprocess.run([sys.executable, '-m', 'ai_supermarket.budget',
                                      '--database', str(path), *args], capture_output=True, timeout=15)
            self.assertEqual(b'', process.stderr)
            return process.returncode, json.loads(process.stdout)
        missing = self.directory / 'missing' / 'budget.sqlite'
        code, result = cli(missing, 'pool-inspect')
        self.assertEqual((1, 'not_found', False), (code, result['error_code'], missing.parent.exists()))
        policy = self.directory / 'policy.json'
        policy.write_text(compact({'limit_micro_cny': 10000, 'owners': {'org-a': 6000, 'org-b': 10000},
                          'state': 'active', 'authorization_ref': 'synthetic:cli', 'expected_revision': 0}))
        code, result = cli(self.path, 'pool-configure', '--input', str(policy))
        self.assertEqual((0, 1), (code, result['revision']))
        self.assertEqual(result, cli(self.path, 'pool-inspect')[1])
        policy.write_text('{"state":"active","state":"paused"}')
        self.assertEqual('invalid_json', cli(self.path, 'pool-configure', '--input', str(policy))[1]['error_code'])
        policy.write_bytes(b' ' * 65537)
        self.assertEqual('invalid_params', cli(self.path, 'pool-configure', '--input', str(policy))[1]['error_code'])
        self.assertEqual(1, self.ledger.pool_summary()['revision'])

    def test_restored_bundle_keeps_totals_and_requires_pool_and_root_reactivation(self):
        fixture = test_recovery.RecoveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.ledger.configure_pool(1000000, {'owner': 1000000}, 'active', 'synthetic:pool', 0)
        before = fixture.ledger.pool_summary()
        fixture.backup()
        recovery.restore_bundle(fixture.snapshot, fixture.restored)
        restored = BudgetLedger(fixture.restored / 'budget.sqlite')
        report = restored.pool_summary()
        self.assertEqual(('paused', 2), (report['state'], report['revision']))
        self.assertEqual((200000, 600000), (report['global']['spent_micro_cny'], report['global']['held_micro_cny']))
        self.error('recovery_required', restored.configure_pool, 1000000,
                   {'owner': 1000000}, 'active', 'synthetic:release', 2)
        recovery.release_bundle(fixture.restored, fixture.evidence)
        self.error('budget_inactive', restored.set_state, 'root', 'active', 2)
        restored.configure_pool(1000000, {'owner': 1000000}, 'active', 'synthetic:release', 2)
        restored.set_state('root', 'active', 2)
        scope = restored.scope('root', 'task', 'alice')
        scope.settle(fixture.unknown, 59, 'synthetic:late')
        scope.reserve_micro('new', 'model', 'synthetic:quote', 210000)
        self.assertEqual(0, restored.pool_summary()['global']['available_micro_cny'])
        self.assertEqual(before, fixture.ledger.pool_summary())
        with closing(recovery.connect_existing(fixture.restored / 'budget.sqlite')) as db:
            recovery.validate_database(db, 'budget')
        with fixture.ledger.store.connect(write=True) as db:
            db.execute('DELETE FROM meta WHERE key=?', (POOL_AUDIT + '1',))
        self.error('invalid_database', recovery.snapshot_bundle, *fixture.paths.values(), fixture.root / 'bad-snapshot')
        self.assertFalse((fixture.root / 'bad-snapshot').exists())

    def test_model_adapter_checks_cross_root_total_before_second_provider_call(self):
        fixture = test_codex_meter.CodexMeterTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.ledger.create('second', 'owner', 1, 'synthetic:second')
        fixture.ledger.grant_task('second', 'task', 'bob')
        fixture.ledger.configure_pool(1500, {'owner': 1500}, 'active', 'synthetic:pool', 0)
        meter = TaskMeter(fixture.root / 'meter.sqlite', 'second-run', 'bob', experiment='experiment',
                          case='case', arm='market', configuration_hash='a'*64, evidence_mode='synthetic')
        model = test_codex_meter.ModelDouble()
        bridge = MeasuredCodexCalls(model, meter, fixture.ledger.scope('second', 'task', 'bob'))
        first = fixture.run_call(maximum=1000)
        self.error('budget_exceeded', bridge.run, 'request', 'chat', {'message': 'fictional'}, 1000)
        self.assertEqual((0, 0), (model.calls, len(meter.call_records())))
        fixture.bridge.record_cost(fixture.reservation(), first['call_id'], 500, 'synthetic:receipt')
        result = bridge.run('request', 'chat', {'message': 'fictional'}, 1000)
        self.assertEqual('completed_cost_unknown', result['status'])
        self.assertEqual((1, 1), (model.calls, fixture.model.calls))
        self.assertEqual((500, 1000), tuple(fixture.ledger.pool_summary()['global'][k]
                         for k in ('spent_micro_cny', 'held_micro_cny')))
        fixture.ledger.configure_pool(1500, {'owner': 1500}, 'paused', 'synthetic:pause', 1)
        self.assertEqual('not_executed', bridge.run('request', 'chat', {'message': 'fictional'}, 1000)['status'])
        self.assertEqual(1, model.calls)

    def test_concurrent_policy_changes_do_not_overwrite_each_other(self):
        self.configure()
        def change(state):
            try:
                return BudgetLedger(self.path).configure_pool(10000, {'org-a': 10000, 'org-b': 10000},
                            state, 'synthetic:change', 1)['revision']
            except MarketError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(change, ('active', 'paused')))
        self.assertCountEqual([2, 'revision_conflict'], results)
        self.audit()

    def test_policy_snapshot_does_not_mix_concurrent_revision_and_totals(self):
        self.configure()
        # WAL lets a writer commit while an earlier reader retains its snapshot.
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual('wal', db.execute('PRAGMA journal_mode=WAL').fetchone()[0])
        original = self.ledger._pool_totals
        def change_during_read(db, config):
            other = BudgetLedger(self.path)
            other.configure_pool(12000, {'org-a': 12000, 'org-b': 12000}, 'active', 'synthetic:change', 1)
            other.scope('other', 'task', 'agent-other').reserve_micro('new', 'model', 'synthetic:quote', 11000)
            return original(db, config)
        with patch.object(self.ledger, '_pool_totals', side_effect=change_during_read):
            report = self.ledger.pool_summary()
        self.assertEqual((1, 10000, 0), (report['revision'], report['global']['limit_micro_cny'],
                         report['global']['held_micro_cny']))
        self.assertEqual(11000, self.ledger.pool_summary()['global']['held_micro_cny'])

    def test_recovery_retains_closed_policy_without_reopening(self):
        fixture = test_recovery.RecoveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.ledger.configure_pool(1000000, {'owner': 1000000}, 'closed', 'synthetic:closed', 0)
        fixture.backup()
        recovery.restore_bundle(fixture.snapshot, fixture.restored)
        recovery.release_bundle(fixture.restored, fixture.evidence)
        restored = BudgetLedger(fixture.restored / 'budget.sqlite')
        self.assertEqual(('closed', 2), tuple(restored.pool_summary()[k] for k in ('state', 'revision')))
        self.error('budget_inactive', restored.configure_pool, 1000000,
                   {'owner': 1000000}, 'active', 'synthetic:reopen', 2)
        restored.scope('root', 'task', 'alice').settle(fixture.unknown, 70, 'synthetic:late')
        self.assertEqual('closed', restored.pool_summary()['state'])
        self.assertEqual(900000, restored.pool_summary()['global']['spent_micro_cny'])


@unittest.skipUnless(importlib.util.find_spec('uvicorn'), '网络实测使用既有HTTP依赖')
class BudgetPoolNetworkTests(unittest.TestCase):
    setUp = test_task_budget_gateway.TaskBudgetLiveTests.setUp
    stop = test_task_budget_gateway.TaskBudgetLiveTests.stop
    client = test_task_budget_gateway.TaskBudgetLiveTests.client
    publish = test_task_budget_gateway.TaskBudgetLiveTests.publish

    def test_real_http_roots_compete_and_replay_respects_global_pause(self):
        policy = {'capabilities': ['read_markdown'], 'permissions': [],
                  'dependencies': ['test-tool:1'], 'extra_budget_cents': 100}
        with self.identities.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET policy=? WHERE principal IN ('alice','bob')", (compact(policy),))
        meta = listing()
        meta['usage_contract']['dependencies'] = [
            {'id': 'test-tool:1', 'license_name': 'LicenseRef-Test-Original', 'max_cost_cents': 60}]
        with patch.object(test_http_live, 'listing', return_value=meta):
            ref = self.publish()
        self.ledger.create('second', 'other-owner', 100, 'synthetic:second')
        self.ledger.grant_task('second', 'task', 'bob')
        self.ledger.configure_pool(1000000, {'owner': 1000000, 'other-owner': 1000000},
                                   'active', 'synthetic:pool', 0)
        requests = {}
        for principal, root, task in [('alice', 'root', 'main'), ('bob', 'second', 'task')]:
            self.app.gateway.bind_key(self.issued[principal]['key_id'], root, task, principal, 'synthetic:binding')
            acquired = self.client(principal).call('market_acquire', {**ref, 'request_id': 'get'})
            requests[principal] = {'acquisition_id': acquired['acquisition_id'], 'task_id': task, 'request_id': 'read'}
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda p: (p, self.client(p).call('market_read', requests[p])), ('alice', 'bob')))
        winner, success = next((p, r) for p, r in outcomes if r['status'] == 'read')
        loser, denied = next((p, r) for p, r in outcomes if r['status'] == 'error')
        self.assertEqual('budget_exceeded', denied['error_code'])
        self.assertNotIn('content', denied)
        self.assertEqual(CONTENT, success['content'])
        self.assertNotIn('owners', compact(success))
        self.assertEqual(600000, self.ledger.pool_summary()['global']['held_micro_cny'])
        self.ledger.configure_pool(1000000, {'owner': 1000000, 'other-owner': 1000000},
                                   'paused', 'synthetic:pause', 1)
        replay = self.client(winner).call('market_read', requests[winner])
        self.assertEqual('budget_inactive', replay['error_code'])
        self.assertNotIn('content', replay)
        scopes = {'alice': ('root', 'main'), 'bob': ('second', 'task')}
        scope = self.ledger.scope(*scopes[winner], winner)
        scope.cancel(success['task_budget']['dependency_reservation']['reservation_id'])
        self.ledger.configure_pool(1000000, {'owner': 1000000, 'other-owner': 1000000},
                                   'active', 'synthetic:resume', 2)
        retry = self.client(loser).call('market_read', requests[loser])
        self.assertEqual('read', retry['status'])
        self.assertEqual(600000, self.ledger.pool_summary()['global']['held_micro_cny'])


if __name__ == '__main__':
    unittest.main()
