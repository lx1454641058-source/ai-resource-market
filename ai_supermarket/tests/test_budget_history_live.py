"""来源：本项目原创。历史费用、并发静态交付及四库恢复的组合验收；仅合成数据。"""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import importlib.util
import time
import unittest

from ai_supermarket import recovery
from ai_supermarket.budget import BudgetLedger
from ai_supermarket.identity import Identities
from ai_supermarket.measurement import TaskMeter
from ai_supermarket.task_budget_gateway import TaskBudgetGateway
from ai_supermarket.tests import test_http_process_recovery as process_fixture


@unittest.skipUnless(importlib.util.find_spec('uvicorn'), '真实本机组合验收需要既有HTTP依赖')
class BudgetHistoryLiveTests(unittest.TestCase):
    # Reuse the existing test-owned process lifecycle, without inheriting its tests.
    setUp = process_fixture.HTTPProcessRecoveryTests.setUp
    key_file = process_fixture.HTTPProcessRecoveryTests.key_file
    bind = process_fixture.HTTPProcessRecoveryTests.bind
    launch = process_fixture.HTTPProcessRecoveryTests.launch
    stop = process_fixture.HTTPProcessRecoveryTests.stop
    probe = process_fixture.HTTPProcessRecoveryTests.probe
    client = process_fixture.HTTPProcessRecoveryTests.client
    publish = process_fixture.HTTPProcessRecoveryTests.publish
    rows = process_fixture.HTTPProcessRecoveryTests.rows
    history_count = 32

    def prepare(self):
        self.assertIn(self.history_count, (32, 512))
        began = time.monotonic()
        historical = self.ledger.scope('root', 'task', 'alice')
        self.history = {}
        for index in range(self.history_count):
            reservation = historical.reserve_micro(f'history-{index}', 'model', 'synthetic-only', 500)
            rid = reservation['reservation_id']
            if index % 4 in (0, 1):
                historical.begin(rid)
            if index % 4 == 0:
                historical.settle_micro(rid, 125, f'synthetic-receipt-{index}')
            elif index % 4 == 1:
                historical.mark_unknown(rid)
            elif index % 4 == 2:
                historical.cancel(rid)
            self.history[rid] = historical.status(rid)
        self.spent = self.history_count // 4 * 125
        self.held = self.history_count // 4 * 1000
        self.limit = self.spent + self.held + 600_000
        self.bound = []
        gateway = TaskBudgetGateway(self.paths['market'], self.paths['identities'], self.paths['budget'])
        for index in range(8):
            root, task = f'competing-root-{index}', f'competing-task-{index}'
            self.ledger.create(root, 'owner', 100, 'synthetic-concurrency-budget')
            self.ledger.grant_task(root, task, 'alice')
            credential, key_id = self.key_file(self.identities, 'alice', f'competing-{index}')
            gateway.bind_key(key_id, root, task, 'alice', 'synthetic-binding')
            self.bound.append({'root': root, 'task': task, 'credential': credential, 'key_id': key_id})
        self.ledger.configure_pool(self.limit, {'owner': self.limit}, 'active', 'synthetic-pool', 0)
        self.before_pool = self.ledger.pool_summary()
        self.assertEqual(self.spent, self.before_pool['global']['spent_micro_cny'])
        self.assertEqual(self.held, self.before_pool['global']['held_micro_cny'])
        self.server = self.launch()
        ref = self.publish(self.server, dependency=True)
        self.clients, self.requests = [], []
        for index, entry in enumerate(self.bound):
            client = self.client(self.server, credential=entry['credential'])
            got = client.call('market_acquire', {**ref, 'request_id': f'competing-acquire-{index}'})
            self.assertEqual('acquired', got['status'])
            self.clients.append(client)
            self.requests.append({'acquisition_id': got['acquisition_id'], 'task_id': entry['task'],
                                  'request_id': 'same-original-read'})
        self.observations = {'history_count': self.history_count, 'history_states': dict(Counter(r['state'] for r in self.history.values())),
                             'historical_spent_micro_cny': self.spent, 'historical_held_micro_cny': self.held,
                             'pool_limit_micro_cny': self.limit, 'setup_seconds': time.monotonic() - began,
                             'waves': [], 'real_model_calls': 0, 'real_payments': 0, 'production_approved': False}

    def wave(self, clients=None, label='initial'):
        clients = clients or self.clients
        def read(index):
            began = time.perf_counter()
            result = clients[index].call('market_read', self.requests[index])
            return result, (time.perf_counter() - began) * 1000
        with ThreadPoolExecutor(max_workers=8) as pool:
            received = list(pool.map(read, range(8)))
        self.observations['waves'].append({'label': label,
            'results': [r.get('error_code', r['status']) for r, _ in received],
            'elapsed_ms': [elapsed for _, elapsed in received]})
        return [result for result, _ in received]

    def assert_one_delivery(self, results):
        winners = [index for index, result in enumerate(results) if result['status'] == 'read']
        self.assertEqual(1, len(winners), [r.get('error_code', r['status']) for r in results])
        for index, result in enumerate(results):
            if index not in winners:
                self.assertEqual('budget_exceeded', result['error_code'])
                self.assertNotIn('content', result)
            else:
                self.assertEqual(process_fixture.CONTENT, result['content'])
                self.assertFalse(result['task_budget']['execution_authorized'])
                self.assertFalse(result['task_budget']['payment_authorized'])
        return winners[0]

    def verify_ledger(self, paths, ledger):
        summary = ledger.pool_summary()
        self.assertEqual(self.spent, summary['global']['spent_micro_cny'])
        self.assertEqual(self.held + 600_000, summary['global']['held_micro_cny'])
        self.assertEqual(0, summary['global']['available_micro_cny'])
        self.assertEqual(1, len(self.rows(paths, 'usage_events')))
        scope = ledger.scope('root', 'task', 'alice')
        self.assertEqual(self.history, {rid: scope.status(rid) for rid in self.history})
        for path in paths.values():
            with closing(recovery.connect_existing(path)) as db:
                self.assertEqual([('ok',)], [tuple(r) for r in db.execute('PRAGMA quick_check')])
                self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())

    def test_history_and_eight_roots_share_exact_pool_and_repeat_original_read(self):
        self.prepare()
        first = self.wave()
        winner = self.assert_one_delivery(first)
        repeated = self.wave(label='same-request-replay')
        self.assertEqual(winner, self.assert_one_delivery(repeated))
        self.assertEqual(first[winner]['usage_id'], repeated[winner]['usage_id'])
        self.verify_ledger(self.paths, self.ledger)

    def test_paused_pool_blocks_post_pause_wave_without_erasing_history(self):
        self.prepare()
        self.assert_one_delivery(self.wave())
        summary = self.ledger.pool_summary()
        self.ledger.configure_pool(self.limit, {'owner': self.limit}, 'paused', 'synthetic-pause', summary['revision'])
        for result in self.wave(label='after-confirmed-pause'):
            self.assertEqual('budget_inactive', result['error_code'])
            self.assertNotIn('content', result)
        self.verify_ledger(self.paths, self.ledger)

    def test_four_ledger_restore_keeps_history_and_requires_pool_root_and_new_binding(self):
        self.prepare()
        first = self.wave()
        winner = self.assert_one_delivery(first)
        self.verify_ledger(self.paths, self.ledger)
        original_rows = {name: self.rows(self.paths, name) for name in ('acquisitions', 'usage_events', 'feedback')}
        self.stop(self.server)
        snapshot, target = self.root / 'snapshot', self.root / 'restored'
        recovery.snapshot_bundle(self.paths['market'], self.paths['identities'], self.paths['budget'], snapshot,
                                 measurement=self.paths['measurement'])
        recovery.restore_bundle(snapshot, target, require_measurement=True)
        restored_paths = recovery.paths_for(target, include_measurement=True)
        restored = self.launch(restored_paths)
        self.assertEqual((503, {'status': 'not_ready'}), self.probe(restored, '/health/ready'))
        old_client = self.client(restored, credential=self.bound[winner]['credential'])
        self.assertEqual('unauthenticated', old_client.call('market_read', self.requests[winner])['error_code'])
        identities = Identities(restored_paths['identities'])
        fresh = [self.key_file(identities, 'alice', f'restored-{index}') for index in range(8)]
        clients = [self.client(restored, credential=key[0]) for key in fresh]
        self.assertEqual('recovery_required', clients[0].call('market_search', {'q': '产品简报'})['error_code'])
        recovery.release_bundle(target, {key: 'synthetic-local-only:' + key
            for key in (*recovery.REVIEW_FIELDS, 'measurement_reconciliation')})
        self.assertEqual((200, {'status': 'ready'}), self.probe(restored, '/health/ready'))
        ledger = BudgetLedger(restored_paths['budget'])
        pool = ledger.pool_summary()
        self.assertEqual('paused', pool['state'])
        gateway = TaskBudgetGateway(restored_paths['market'], restored_paths['identities'], restored_paths['budget'])
        for result in self.wave(clients, 'released-but-unbound'):
            self.assertEqual('task_budget_required', result['error_code'])
            self.assertNotIn('content', result)
        ledger.configure_pool(self.limit, {'owner': self.limit}, 'active', 'synthetic-reenable-pool', pool['revision'])
        for entry, (_, key_id) in zip(self.bound, fresh):
            root = ledger.scope(entry['root'], entry['task'], 'alice').summary()
            self.assertEqual('paused', root['state'])
            ledger.set_state(entry['root'], 'active', root['revision'])
            gateway.bind_key(key_id, entry['root'], entry['task'], 'alice', 'synthetic-new-binding')
        repeated = self.wave(clients, 'restored-original-requests')
        self.assertEqual(winner, self.assert_one_delivery(repeated))
        self.assertEqual(first[winner]['usage_id'], repeated[winner]['usage_id'])
        self.verify_ledger(restored_paths, ledger)
        for name, rows in original_rows.items():
            self.assertEqual(rows, self.rows(restored_paths, name))
            self.assertEqual(rows, self.rows(self.paths, name))
        report = TaskMeter.existing(restored_paths['measurement'], 'historical', 'alice').report()
        self.assertEqual(1, report['unknown_calls'])
        self.assertTrue(report['recovery_review_required'])
        self.assertIsNone(report['total_cost_micro_cny'])
        self.assertEqual('unauthenticated', old_client.call('market_read', self.requests[winner])['error_code'])
        self.observations['restored_history_and_usage_match'] = True
        self.observations['old_key_rejected'] = True
        self.observations['measurement_unknown_retained'] = True


if __name__ == '__main__':
    unittest.main()
