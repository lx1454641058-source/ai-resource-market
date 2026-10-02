"""来源：本项目原创。远程任务预算、失败保留、恢复和真实本机交付验收；无真实费用。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from ai_supermarket.budget import BudgetLedger, TaskBudget
from ai_supermarket.common import MarketError, compact
from ai_supermarket.exchange import Exchange
from ai_supermarket.http_app import MarketHTTP
from ai_supermarket.identity import Identities
from ai_supermarket import recovery
from ai_supermarket.task_budget_gateway import BINDING_PREFIX, TaskBudgetGateway
from ai_supermarket.tests.test_exchange import listing, CONTENT, CHECKS
from ai_supermarket.tests import test_http_live as live_fixture


class TaskBudgetGatewayTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.paths = recovery.paths_for(self.root)
        self.identities = Identities(self.paths['identities'])
        self.policy = {'capabilities': ['read_markdown'], 'permissions': [],
                       'dependencies': ['test-tool:1'], 'extra_budget_cents': 100}
        self.keys = {}
        for principal, role in [('alice', 'buyer'), ('bob', 'buyer'), ('vendor', 'seller'), ('operator', 'reviewer')]:
            self.identities.create_account(principal, role, self.policy if role == 'buyer' else None)
            self.keys[principal] = self.identities.issue_key(principal)
        self.ledger = BudgetLedger(self.paths['budget'])
        self.ledger.create('root', 'owner', 100, 'synthetic-budget-authorization')
        self.ledger.grant_task('root', 'main', 'alice')
        self.ledger.grant_task('root', 'child', 'bob', 'main')
        self.gateway = TaskBudgetGateway(*self.paths.values())
        self.publish()

    def publish(self, cost=0, slug='brief'):
        meta = listing()
        if cost:
            meta['usage_contract']['dependencies'] = [
                {'id': 'test-tool:1', 'license_name': 'LicenseRef-Test-Original', 'max_cost_cents': cost}]
        seller = Exchange(self.paths['market'], 'vendor', 'seller')
        draft = seller.draft(slug, '1.0.0', meta, CONTENT, 'draft-' + slug)
        seller.submit(draft['id'], '1.0.0', 1, 'submit-' + slug)
        Exchange(self.paths['market'], 'operator', 'reviewer').review(
            draft['id'], '1.0.0', 1, 'approve', CHECKS, '原创本地预算演练', 'review-' + slug)
        return draft['id']

    def bind(self, principal='alice', task=None):
        return self.gateway.bind_key(self.keys[principal]['key_id'], 'root',
                                     task or ('main' if principal == 'alice' else 'child'), principal, 'fixture-approval')

    def acquire(self, principal='alice', resource='vendor:brief', request='get'):
        result = self.gateway.call(self.keys[principal]['token'], 'market_acquire',
                                   {'id': resource, 'version': '1.0.0', 'request_id': request})
        self.assertEqual('acquired', result['status'], result)
        return result['acquisition_id']

    def read(self, acquisition, principal='alice', task=None, request='read', token=None):
        return self.gateway.call(token or self.keys[principal]['token'], 'market_read',
            {'acquisition_id': acquisition, 'task_id': task or ('main' if principal == 'alice' else 'child'),
             'request_id': request})

    def denied(self, result, code):
        self.assertEqual(code, result.get('error_code'), result)
        self.assertNotIn('content', result)

    def error(self, code, callback, *args):
        with self.assertRaises(MarketError) as caught:
            callback(*args)
        self.assertEqual(code, caught.exception.code)

    def usage_count(self):
        with closing(recovery.connect_existing(self.paths['market'])) as db:
            return db.execute('SELECT COUNT(*) FROM usage_events').fetchone()[0]

    def test_unbound_discovery_and_acquisition_do_not_authorize_body(self):
        tools = self.gateway.tool_list(self.keys['alice']['token'])
        self.assertTrue(tools['task_budget_required'])
        self.assertEqual(7, len(tools['tools']))
        acquisition = self.acquire()
        self.denied(self.read(acquisition), 'task_budget_required')
        self.assertEqual(0, self.usage_count())
        self.bind()
        tools = self.gateway.tool_list(self.keys['alice']['token'])
        self.assertEqual('main', tools['bound_task_id'])
        read = next(t for t in tools['tools'] if t['name'] == 'market_read')
        self.assertEqual(['main'], read['inputSchema']['properties']['task_id']['enum'])
        delivered = self.read(acquisition)
        self.assertEqual(CONTENT, delivered['content'])
        self.assertFalse(delivered['task_budget']['execution_authorized'])
        self.assertFalse(delivered['task_budget']['payment_authorized'])

    def test_binding_is_immutable_revocable_and_key_specific(self):
        self.bind()
        self.assertEqual('bound', self.bind()['status'])
        self.ledger.grant_task('root', 'another', 'alice')
        self.error('task_binding_conflict', self.bind, 'alice', 'another')
        acquisition = self.acquire()
        second = self.identities.issue_key('alice')
        self.denied(self.read(acquisition, token=second['token']), 'task_budget_required')
        self.gateway.revoke_binding(self.keys['alice']['key_id'])
        self.gateway.revoke_binding(self.keys['alice']['key_id'])
        self.denied(self.read(acquisition), 'task_binding_denied')
        self.error('task_binding_conflict', self.bind)
        self.assertEqual('alice', self.identities.authenticate(self.keys['alice']['token'])['principal'])

    def test_binding_rejects_wrong_owner_role_and_revoked_key(self):
        self.error('task_binding_denied', self.gateway.bind_key,
                   self.keys['bob']['key_id'], 'root', 'main', 'alice', 'approval')
        self.ledger.grant_task('root', 'seller-task', 'vendor')
        self.error('task_binding_denied', self.gateway.bind_key,
                   self.keys['vendor']['key_id'], 'root', 'seller-task', 'vendor', 'approval')
        self.identities.revoke_key(self.keys['alice']['key_id'])
        self.error('task_binding_denied', self.bind)

    def test_wrong_task_and_cross_principal_never_deliver(self):
        self.bind()
        self.bind('bob')
        acquisition = self.acquire()
        self.denied(self.read(acquisition, task='child'), 'budget_scope_mismatch')
        result = self.read(acquisition, principal='bob')
        self.assertEqual('error', result['status'])
        self.assertNotIn('content', result)
        self.assertEqual(0, self.usage_count())

    def test_pause_and_parent_close_block_even_free_previously_read_rights(self):
        self.bind()
        self.bind('bob')
        acquisition = self.acquire()
        child = self.acquire('bob')
        self.assertEqual('read', self.read(acquisition)['status'])
        self.ledger.set_state('root', 'paused', 1)
        self.denied(self.read(acquisition), 'budget_inactive')
        self.ledger.set_state('root', 'active', 2)
        self.ledger.close_task('root', 'main')
        self.denied(self.read(child, 'bob'), 'budget_inactive')

    def test_repeat_read_reserves_once_and_preserves_usage_idempotency(self):
        resource = self.publish(60, 'dependency')
        self.bind()
        acquisition = self.acquire(resource=resource)
        first, repeated = self.read(acquisition), self.read(acquisition)
        self.assertEqual(first['usage_id'], repeated['usage_id'])
        self.assertEqual(first['task_budget'], repeated['task_budget'])
        self.assertEqual('read', self.read(acquisition, request='read-again')['status'])
        scope = self.ledger.scope('root', 'main', 'alice')
        self.assertEqual(60, scope.summary()['held_cents'])
        self.assertEqual(2, self.usage_count())

    def test_concurrent_parent_and_child_share_limit_before_delivery(self):
        resource = self.publish(60, 'dependency')
        for principal in ('alice', 'bob'):
            self.bind(principal)
        acquisitions = {p: self.acquire(p, resource) for p in ('alice', 'bob')}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda p: self.read(acquisitions[p], p), ('alice', 'bob')))
        self.assertEqual(1, sum(r['status'] == 'read' for r in results))
        failure = next(r for r in results if r['status'] == 'error')
        self.denied(failure, 'budget_exceeded')
        self.assertEqual(1, self.usage_count())
        self.assertEqual(60, self.ledger.scope('root', 'main', 'alice').summary()['held_cents'])

    def test_cancelled_inflight_unknown_block_and_settled_can_reuse(self):
        self.bind()
        scope = self.ledger.scope('root', 'main', 'alice')
        for state in ('cancelled', 'in_flight', 'unknown', 'settled'):
            with self.subTest(state=state):
                resource = self.publish(10, state.replace('_', '-'))
                acquisition = self.acquire(resource=resource, request='get-' + state)
                read = self.read(acquisition, request=state)
                reservation = read['task_budget']['dependency_reservation']['reservation_id']
                if state == 'cancelled':
                    scope.cancel(reservation)
                else:
                    scope.begin(reservation)
                    if state == 'unknown':
                        scope.mark_unknown(reservation)
                    elif state == 'settled':
                        scope.settle(reservation, 5, 'fixture-receipt')
                result = self.read(acquisition, request=state)
                if state == 'settled':
                    self.assertEqual('read', result['status'])
                else:
                    self.denied(result, 'budget_operation_' + state)

    def test_failed_delivery_keeps_reservation_and_retry_does_not_duplicate(self):
        resource = self.publish(60, 'dependency')
        self.bind()
        acquisition = self.acquire(resource=resource)
        with patch.object(Exchange, 'read', side_effect=sqlite3.OperationalError('fixture failure')):
            self.denied(self.read(acquisition), 'unavailable')
        self.assertEqual(0, self.usage_count())
        self.assertEqual(60, self.ledger.scope('root', 'main', 'alice').summary()['held_cents'])
        inspection = self.gateway.inspect_binding(self.keys['alice']['key_id'])
        self.assertEqual('reserved', inspection['reservations'][0]['state'])
        self.assertEqual(60, inspection['budget']['held_cents'])
        self.assertFalse(inspection['credential_validity_checked'])
        self.assertEqual('read', self.read(acquisition)['status'])
        self.assertEqual(60, self.ledger.scope('root', 'main', 'alice').summary()['held_cents'])

    def test_revocation_between_preflight_and_final_gate_stops_delivery(self):
        original = Exchange.check_compatibility
        self.bind()
        acquisition = self.acquire()
        def revoke(market, *args, **kwargs):
            result = original(market, *args, **kwargs)
            self.identities.revoke_key(self.keys['alice']['key_id'])
            return result
        with patch.object(Exchange, 'check_compatibility', revoke):
            self.denied(self.read(acquisition), 'unauthenticated')
        self.assertEqual(0, self.usage_count())

    def test_binding_revocation_after_reservation_stops_delivery_and_keeps_exposure(self):
        original = TaskBudget.reserve
        resource = self.publish(60, 'dependency')
        self.bind()
        acquisition = self.acquire(resource=resource)
        def revoke(scope, *args, **kwargs):
            result = original(scope, *args, **kwargs)
            self.gateway.revoke_binding(self.keys['alice']['key_id'])
            return result
        with patch.object(TaskBudget, 'reserve', revoke):
            self.denied(self.read(acquisition), 'task_binding_denied')
        self.assertEqual(0, self.usage_count())
        self.assertEqual(60, self.ledger.scope('root', 'main', 'alice').summary()['held_cents'])

    def test_pause_waits_for_admitted_delivery_then_blocks_following_reads(self):
        self.bind()
        acquisition = self.acquire()
        entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
        original = Exchange.read
        def held_read(market, **kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('fixture timeout')
            return original(market, **kwargs)
        def pause():
            attempted.set()
            return self.ledger.set_state('root', 'paused', 1)
        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(Exchange, 'read', held_read):
            reading = pool.submit(self.read, acquisition)
            try:
                self.assertTrue(entered.wait(2))
                pausing = pool.submit(pause)
                self.assertTrue(attempted.wait(2))
                self.assertFalse(pausing.done())
            finally:
                release.set()
            self.assertEqual('read', reading.result(timeout=4)['status'])
            pausing.result(timeout=4)
        self.denied(self.read(acquisition), 'budget_inactive')

    def test_corrupt_binding_is_rejected_by_snapshot_and_delivery(self):
        self.bind()
        acquisition = self.acquire()
        key = BINDING_PREFIX + self.keys['alice']['key_id']
        with self.ledger.store.connect(write=True) as db:
            original = json.loads(db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()[0])
            db.execute('UPDATE meta SET value=? WHERE key=?', (compact({**original, 'identity_binding': 42}), key))
        self.denied(self.read(acquisition), 'invalid_database')
        self.error('invalid_database', recovery.snapshot_bundle, *self.paths.values(), self.root / 'bad-snapshot')

    def test_restore_preserves_binding_history_but_requires_new_key_and_release(self):
        self.bind()
        acquisition = self.acquire()
        snapshot, restored = self.root / 'snapshot', self.root / 'restored'
        recovery.snapshot_bundle(*self.paths.values(), snapshot)
        recovery.restore_bundle(snapshot, restored)
        paths = recovery.paths_for(restored)
        gateway = TaskBudgetGateway(*paths.values())
        new_key = gateway.identities.issue_key('alice')
        self.error('budget_inactive', gateway.bind_key,
                   new_key['key_id'], 'root', 'main', 'alice', 'new-approval')
        recovery.release_bundle(restored, {key: 'fixture:' + key for key in recovery.REVIEW_FIELDS})
        ledger = BudgetLedger(paths['budget'])
        revision = ledger.scope('root', 'main', 'alice').summary()['revision']
        ledger.set_state('root', 'active', revision)
        self.denied(gateway.call(self.keys['alice']['token'], 'market_read',
            {'acquisition_id': acquisition, 'task_id': 'main', 'request_id': 'old'}), 'unauthenticated')
        gateway.bind_key(new_key['key_id'], 'root', 'main', 'alice', 'new-approval')
        delivered = gateway.call(new_key['token'], 'market_read',
            {'acquisition_id': acquisition, 'task_id': 'main', 'request_id': 'restored-read'})
        self.assertEqual(CONTENT, delivered['content'])
        with ledger.store.connect() as db:
            self.assertEqual(2, db.execute('SELECT COUNT(*) FROM meta WHERE key LIKE ?', (BINDING_PREFIX + '%',)).fetchone()[0])

    def test_missing_budget_configuration_never_initializes_budget(self):
        missing = self.root / 'missing.sqlite'
        invitations = self.root / 'never-created-enrollments.sqlite'
        with self.assertRaises(ValueError):
            MarketHTTP(self.paths['market'], self.paths['identities'], allowed_hosts=['localhost'],
                       enforce_task_budget=True, enrollment_database=invitations)
        with self.assertRaises(MarketError):
            TaskBudgetGateway(self.paths['market'], self.paths['identities'], missing)
        with self.assertRaises(ValueError):
            MarketHTTP(self.paths['market'], self.paths['identities'], allowed_hosts=['localhost'],
                       enforce_task_budget=True, budget_database=missing, enrollment_database=invitations)
        self.assertFalse(missing.exists())
        self.assertFalse(invitations.exists())

    def test_expired_key_and_disabled_account_cannot_read_cached_rights(self):
        self.bind()
        acquisition = self.acquire()
        self.assertEqual('read', self.read(acquisition)['status'])
        future = self.keys['alice']['expires_at'] + 1
        expired = TaskBudgetGateway(*self.paths.values(), clock=lambda: future)
        self.denied(expired.call(self.keys['alice']['token'], 'market_read',
            {'acquisition_id': acquisition, 'task_id': 'main', 'request_id': 'expired'}), 'unauthenticated')
        self.error('task_binding_denied', expired.bind_key,
                   self.keys['alice']['key_id'], 'root', 'main', 'alice', 'fixture-approval')
        self.identities.disable_account('alice')
        self.denied(self.read(acquisition), 'unauthenticated')

    def test_operator_cli_binds_and_revokes_without_token_arguments_or_output(self):
        command = [sys.executable, '-m', 'ai_supermarket.task_budget_admin', '--database', str(self.paths['market']),
                   '--identities', str(self.paths['identities']), '--budget', str(self.paths['budget'])]
        actions = [['bind', '--key-id', self.keys['alice']['key_id'], '--root-id', 'root', '--task-id', 'main',
                    '--principal', 'alice', '--authorization-ref', 'fixture-approval'],
                   ['inspect', '--key-id', self.keys['alice']['key_id']],
                   ['revoke', '--key-id', self.keys['alice']['key_id']]]
        for action, expected in zip(actions, ('bound', 'inspected', 'revoked')):
            result = subprocess.run(command + action, capture_output=True, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr.decode('utf-8', errors='replace'))
            self.assertEqual(expected, json.loads(result.stdout)['status'])
            for issued in self.keys.values():
                self.assertNotIn(issued['token'].encode(), result.stdout + result.stderr)

    def test_operator_inspection_pages_and_excludes_other_tasks(self):
        self.bind()
        scope = self.ledger.scope('root', 'main', 'alice')
        expected = {scope.reserve('inspect-' + str(i), 'model', 'fixture', 0)['reservation_id'] for i in range(101)}
        foreign = self.ledger.scope('root', 'child', 'bob').reserve('foreign', 'model', 'fixture', 0)['reservation_id']
        self.gateway.revoke_binding(self.keys['alice']['key_id'])
        first = self.gateway.inspect_binding(self.keys['alice']['key_id'])
        second = self.gateway.inspect_binding(self.keys['alice']['key_id'], first['next_cursor'])
        actual = [row['reservation_id'] for page in (first, second) for row in page['reservations']]
        self.assertEqual((100, 1), (len(first['reservations']), len(second['reservations'])))
        self.assertEqual(expected, set(actual))
        self.assertEqual(len(expected), len(actual))
        self.assertNotIn(foreign, actual)
        self.assertIsNone(second['next_cursor'])
        self.assertEqual('revoked', first['binding']['state'])


@unittest.skipUnless(importlib.util.find_spec('uvicorn'), '网络实测使用既有HTTP依赖')
class TaskBudgetLiveTests(unittest.TestCase):
    stop = live_fixture.LiveHTTPTests.stop
    client = live_fixture.LiveHTTPTests.client
    publish = live_fixture.LiveHTTPTests.publish

    def setUp(self):
        def application(market, identities, **kwargs):
            self.ledger = BudgetLedger(self.root / 'budget.sqlite')
            self.ledger.create('root', 'owner', 100, 'fixture-approval')
            self.ledger.grant_task('root', 'main', 'alice')
            return MarketHTTP(market, identities, budget_database=self.root / 'budget.sqlite',
                              enforce_task_budget=True, **kwargs)
        with patch.object(live_fixture, 'MarketHTTP', application):
            live_fixture.LiveHTTPTests.setUp(self)

    def test_real_http_requires_binding_and_rechecks_pause(self):
        ref = self.publish()
        buyer = self.client('alice')
        acquired = buyer.call('market_acquire', {**ref, 'request_id': 'get'})
        args = {'acquisition_id': acquired['acquisition_id'], 'task_id': 'main', 'request_id': 'read'}
        denied = buyer.call('market_read', args)
        self.assertEqual('task_budget_required', denied['error_code'])
        self.app.gateway.bind_key(self.issued['alice']['key_id'], 'root', 'main', 'alice', 'fixture-approval')
        self.assertEqual(CONTENT, buyer.call('market_read', args)['content'])
        self.ledger.set_state('root', 'paused', 1)
        self.assertEqual('budget_inactive', buyer.call('market_read', args)['error_code'])

    def test_real_http_rejects_wrong_task_and_revoked_binding(self):
        ref = self.publish()
        buyer = self.client('alice')
        acquired = buyer.call('market_acquire', {**ref, 'request_id': 'get'})
        self.app.gateway.bind_key(self.issued['alice']['key_id'], 'root', 'main', 'alice', 'fixture-approval')
        args = {'acquisition_id': acquired['acquisition_id'], 'task_id': 'other', 'request_id': 'read'}
        self.assertEqual('budget_scope_mismatch', buyer.call('market_read', args)['error_code'])
        self.app.gateway.revoke_binding(self.issued['alice']['key_id'])
        args['task_id'] = 'main'
        self.assertEqual('task_binding_denied', buyer.call('market_read', args)['error_code'])

    def test_real_network_concurrent_tasks_cannot_overreserve_shared_budget(self):
        policy = {'capabilities': ['read_markdown'], 'permissions': [],
                  'dependencies': ['test-tool:1'], 'extra_budget_cents': 100}
        # Change only synthetic fixture accounts before issuing any task requests.
        with self.identities.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET policy=? WHERE principal IN ('alice','bob')", (compact(policy),))
        meta = listing()
        meta['usage_contract']['dependencies'] = [
            {'id': 'test-tool:1', 'license_name': 'LicenseRef-Test-Original', 'max_cost_cents': 60}]
        with patch.object(live_fixture, 'listing', return_value=meta):
            ref = self.publish()
        self.ledger.grant_task('root', 'child', 'bob', 'main')
        requests = {}
        for principal, task in [('alice', 'main'), ('bob', 'child')]:
            self.app.gateway.bind_key(self.issued[principal]['key_id'], 'root', task, principal, 'fixture-approval')
            acquired = self.client(principal).call('market_acquire', {**ref, 'request_id': 'get'})
            requests[principal] = {'acquisition_id': acquired['acquisition_id'], 'task_id': task, 'request_id': 'read'}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda p: self.client(p).call('market_read', requests[p]), ('alice', 'bob')))
        self.assertEqual(1, sum(r['status'] == 'read' for r in results))
        denied = next(r for r in results if r['status'] == 'error')
        self.assertEqual('budget_exceeded', denied['error_code'])
        self.assertNotIn('content', denied)
        self.assertEqual(60, self.ledger.scope('root', 'main', 'alice').summary()['held_cents'])


if __name__ == '__main__':
    unittest.main()
