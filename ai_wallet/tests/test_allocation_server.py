"""来源：本项目原创。新预算模式的本机 HTTP、导出和启动格式保护检查。"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from ai_wallet.allocation import AllocationWallet
from ai_wallet.engine import Wallet
from ai_wallet.server import LocalServer
from ai_wallet.tests import test_server


class AllocationServerTests(unittest.TestCase):
    call = test_server.ServerTests.call

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / 'allocation.sqlite'
        self.wallet = AllocationWallet(self.database)
        self.server = LocalServer(0, self.wallet)
        self.worker = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.01}, daemon=True)
        self.worker.start()
        self.addCleanup(self.cleanup)
        self.token = self.call('GET', '/api/state')[1]['csrf_token']

    def cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)
        self.temp.cleanup()

    def pool(self, total=100000):
        return self.call('POST', '/api/allocation/pool', {'total_cents': total, 'expected_revision': 1})

    def proposal(self):
        status, data, _ = self.call('POST', '/api/allocation/proposals', {
            'title': '短视频宣传', 'agent_id': 'demo-agent', 'budget_cents': 30000,
            'reason': '图片素材与工具', 'idempotency_key': 'task-one',
        })
        self.assertEqual(status, 200)
        return data['task']

    def test_full_http_flow_allocates_then_spends_then_releases(self):
        self.assertEqual(self.pool()[0], 200)
        task = self.proposal()
        self.assertEqual(task['status'], 'pending_budget')
        self.assertEqual(self.wallet.state()['allocation']['unallocated_cents'], 100000)
        path = f"/api/allocation/tasks/{task['id']}"
        approval = {'decision': 'approve', 'budget_cents': 30000, 'expected_revision': task['revision']}
        code, data, _ = self.call('POST', path + '/decide', approval)
        self.assertEqual(code, 200)
        self.assertEqual(self.call('POST', path + '/decide', approval)[0], 409)
        self.assertEqual(self.wallet.state()['allocation']['unallocated_cents'], 70000)
        task = data['task']
        code, data, _ = self.call('POST', '/api/orders', {
            'task_id': task['id'], 'product_id': 'report-35', 'reason': '宣传参考', 'idempotency_key': 'asset-1',
        })
        self.assertEqual(code, 200)
        self.assertEqual(data['order']['status'], 'approved')
        self.assertEqual(self.call('POST', f"/api/orders/{data['order']['id']}/execute", {'scenario': 'success'})[1]['order']['status'], 'paid')
        allocation = self.wallet.state()['allocation']
        self.assertEqual((allocation['unallocated_cents'], allocation['allocated_cents'], allocation['spent_cents']), (70000, 26500, 3500))
        self.assertEqual(self.call('POST', path + '/close', {'expected_revision': task['revision']})[0], 200)
        allocation = self.wallet.state()['allocation']
        self.assertEqual((allocation['unallocated_cents'], allocation['allocated_cents'], allocation['spent_cents']), (96500, 0, 3500))

    def test_legacy_task_write_cannot_bypass_budget_confirmation(self):
        self.pool()
        old = {'title': '绕过', 'agent_id': 'agent', 'budget_cents': 30000, 'idempotency_key': 'bypass'}
        self.assertNotEqual(self.call('POST', '/api/tasks', old)[0], 200)
        task = self.proposal()
        for body in ({'budget_cents': 30000, 'expected_revision': 1}, {'status': 'active', 'expected_revision': 1}):
            self.assertNotEqual(self.call('POST', f"/api/tasks/{task['id']}", body)[0], 200)
        self.assertEqual(self.wallet.state()['allocation']['allocated_cents'], 0)

    def test_new_routes_keep_origin_and_session_checks(self):
        for path, body in (
            ('/api/allocation/pool', {'total_cents': 100000, 'expected_revision': 1}),
            ('/api/allocation/proposals', {'title': 'test'}),
            ('/api/allocation/tasks/missing/decide', {'decision': 'approve'}),
            ('/api/allocation/tasks/missing/close', {'expected_revision': 1}),
        ):
            for headers in ({'Origin': None}, {'X-Wallet-Token': None}, {'Origin': 'https://foreign.invalid'}):
                with self.subTest(path=path, headers=headers):
                    self.assertEqual(self.call('POST', path, body, headers=headers)[0], 403)
        self.assertEqual(self.wallet.state()['allocation']['total_cents'], 0)
        self.assertEqual(self.wallet.state()['tasks'], [])

    def test_export_contains_pool_and_proposal_without_internal_replay_key(self):
        self.pool()
        task = self.proposal()
        code, exported, _ = self.call('GET', '/api/export')
        self.assertEqual(code, 200)
        self.assertEqual(exported['format'], 'ai-wallet-task-budget-export-v1')
        self.assertEqual(exported['allocation']['total_cents'], 100000)
        self.assertEqual(exported['tasks'][0]['id'], task['id'])
        self.assertNotIn('idempotency_key', json.dumps(exported))
        self.assertNotIn('_creation_key', json.dumps(exported))
        self.assertNotIn('csrf_token', json.dumps(exported))

    def test_bad_fields_and_stale_pool_do_not_change_allocation(self):
        self.pool()
        for body in ({'total_cents': 500000, 'expected_revision': 1},
                     {'total_cents': True, 'expected_revision': 2},
                     {'total_cents': 500000, 'expected_revision': 2, 'owner': True}):
            self.assertNotEqual(self.call('POST', '/api/allocation/pool', body)[0], 200)
        self.assertEqual(self.wallet.state()['allocation']['total_cents'], 100000)
        self.assertEqual(self.call('POST', '/api/allocation/tasks/missing/approve', {})[0], 404)

    def test_support_record_does_not_overwrite_pool_or_task_storage(self):
        self.pool()
        task = self.proposal()
        order = self.wallet.request({'task_id': task['id'], 'product_id': 'image-12',
                                     'reason': '尚未确认的测试', 'idempotency_key': 'rejected-1'})
        before = self.wallet.state()['allocation']
        code, data, _ = self.call('POST', '/api/support/tickets', {
            'order_id': order['id'], 'category': 'other', 'description': '核对预算申请',
            'idempotency_key': 'support-1',
        })
        self.assertEqual(code, 200)
        self.assertEqual(self.call('GET', '/api/support/tickets')[1]['tickets'][0]['id'], data['ticket']['id'])
        self.assertEqual(self.wallet.state()['allocation'], before)
        self.assertEqual(self.wallet.state()['tasks'][0]['id'], task['id'])
        exported = self.call('GET', '/api/export')[1]
        self.assertEqual((exported['allocation'], len(exported['tickets'])), (before, 1))

    def test_legacy_launch_refuses_new_ledger_without_writes(self):
        before = self.database.read_bytes()
        result = subprocess.run([sys.executable, '-m', 'ai_wallet', '--database', str(self.database), '--port', '0'],
                                cwd=Path(__file__).resolve().parents[2], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b'--task-budgets', result.stderr)
        self.assertEqual(self.database.read_bytes(), before)

    def test_old_server_rejects_new_routes_without_changing_legacy_pool(self):
        legacy = Wallet(Path(self.temp.name) / 'old.sqlite')
        original = self.server.wallet
        try:
            self.server.wallet = legacy
            self.assertEqual(self.pool()[0], 409)
            self.assertNotIn('allocation', legacy.state())
        finally:
            self.server.wallet = original
