"""来源：本项目原创。验证宿主只读等待、真实并发确认和中止边界。"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from ai_wallet.agent_interface import TaskWallet
from ai_wallet.agent_planner import TaskPlanner
from ai_wallet.allocation import AllocationWallet
from ai_wallet.approval_wait import wait_for_budget


class ApprovalWaitTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / 'wait.sqlite'
        self.wallet = AllocationWallet(self.database)
        self.wallet.configure_pool({'total_cents': 100000, 'expected_revision': 1})
        self.planner = TaskPlanner(self.wallet, 'creative')
        self.task = self.planner.propose('宣传图片', 30000, '配图', 'original-task')

    def decide(self, task=None, decision='approve'):
        task = task or self.task
        payload = {'decision': decision, 'expected_revision': task['revision']}
        if decision == 'approve':
            payload['budget_cents'] = task['proposed_budget_cents']
        return self.wallet.decide_budget(task['task_id'], payload)

    def wait(self, planner=None, **kwargs):
        return wait_for_budget(planner or self.planner, self.task['task_id'], **kwargs)

    def test_pending_timeout_does_not_change_any_ledger_bytes(self):
        before = hashlib.sha256(self.database.read_bytes()).digest()
        result = self.wait(timeout_seconds=0.3, poll_seconds=0.25)
        self.assertEqual(result['event'], 'timeout')
        self.assertEqual(result['result']['next_action'], 'wait_for_user')
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).digest(), before)
        self.assertEqual(self.wallet.state()['orders'], [])

    def test_actual_concurrent_owner_decision_wakes_waiter_without_buying(self):
        read_pending = threading.Event()
        original = self.planner.status
        def observed(task_id):
            state = original(task_id)
            if state['status'] == 'pending_budget':
                read_pending.set()
            return state
        with patch.object(self.planner, 'status', side_effect=observed), ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.wait, timeout_seconds=3, poll_seconds=0.25)
            self.assertTrue(read_pending.wait(1), 'waiter never observed pending task')
            self.decide()
            result = future.result(timeout=2)
        self.assertEqual(result['event'], 'ready')
        self.assertEqual(result['result']['budget_cents'], 30000)
        self.assertEqual(self.wallet.state()['orders'], [])

    def test_restart_reads_current_authorization_and_idempotent_purchase_survives(self):
        self.decide()
        resumed = TaskPlanner(AllocationWallet(self.database), 'creative')
        self.assertEqual(self.wait(resumed, timeout_seconds=0)['event'], 'ready')
        purchase = TaskWallet(self.wallet, self.task['task_id'])
        first = purchase.purchase('image-12', '宣传配图', 'same-purchase')
        self.assertEqual(first['status'], 'paid')
        self.assertEqual(self.wait(resumed, timeout_seconds=0)['event'], 'ready')
        self.assertEqual(first, purchase.purchase('image-12', '宣传配图', 'same-purchase'))
        self.assertEqual(self.wallet.state()['allocation']['spent_cents'], 1200)

    def test_reject_and_close_stop_and_foreign_task_is_hidden(self):
        self.decide(decision='reject')
        self.assertEqual(self.wait(timeout_seconds=0)['event'], 'stopped')
        other = TaskPlanner(self.wallet, 'other')
        foreign = self.wait(other, timeout_seconds=0)
        missing = wait_for_budget(other, 'missing', timeout_seconds=0)
        self.assertEqual(foreign, missing)
        self.assertNotIn('task_id', foreign['result'])
        new = self.planner.propose('任务2', 1000, '配图', 'task-2')
        self.wallet.close_task(new['task_id'], {'expected_revision': new['revision']})
        self.assertEqual(wait_for_budget(self.planner, new['task_id'], timeout_seconds=0)['event'], 'stopped')

    def test_rejected_increase_keeps_old_budget_and_pause_does_not_resume(self):
        self.decide()
        active = self.planner.status(self.task['task_id'])
        proposal = self.planner.request_increase(active['task_id'], 40000, '多一张配图', active['revision'])
        self.assertEqual(self.wait(timeout_seconds=0)['event'], 'timeout')
        self.decide(proposal, decision='reject')
        state = self.wait(timeout_seconds=0)
        self.assertEqual(state['event'], 'ready')
        self.assertEqual(state['result']['budget_cents'], 30000)
        self.wallet.update_task(active['task_id'], {'status': 'paused', 'expected_revision': state['result']['revision']})
        self.assertEqual(self.wait(timeout_seconds=0)['event'], 'stopped')

    def test_wallet_disabled_or_revoked_after_notice_cannot_purchase(self):
        self.decide()
        state = self.wait(timeout_seconds=0)
        self.assertEqual(state['event'], 'ready')
        self.wallet.update_policy({'enabled': False})
        self.assertEqual(self.wait(timeout_seconds=0)['event'], 'stopped')
        result = TaskWallet(self.wallet, self.task['task_id']).purchase('image-12', '配图', 'after-revoke')
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(self.wallet.state()['allocation']['spent_cents'], 0)

    def test_read_does_not_take_write_lock_and_missing_database_is_not_created(self):
        # 一个未提交的写事务存在时，观察者仍能读到之前已提交的预算状态。
        with self.wallet._connection():
            result = self.wait(timeout_seconds=0)
            self.assertEqual(result['event'], 'timeout')
        self.wallet.database = str(self.database.parent / 'missing.sqlite')
        self.assertEqual(self.wait(timeout_seconds=0)['event'], 'unavailable')
        self.assertFalse(Path(self.wallet.database).exists())

    def test_cancellation_and_read_failure_return_without_authorizing(self):
        cancel = threading.Event()
        read_pending = threading.Event()
        original = self.planner.status
        def observed(task_id):
            state = original(task_id)
            read_pending.set()
            return state
        with patch.object(self.planner, 'status', side_effect=observed), ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.wait, timeout_seconds=5, poll_seconds=5, cancel_event=cancel)
            self.assertTrue(read_pending.wait(1))
            cancel.set()
            self.assertEqual(future.result(timeout=1)['event'], 'cancelled')
        with patch.object(self.wallet, '_read_connection', side_effect=sqlite3.OperationalError('private path')):
            result = self.wait(timeout_seconds=0)
        self.assertEqual(result['event'], 'unavailable')
        self.assertNotIn('private path', str(result))
        self.assertEqual(self.wallet.state()['orders'], [])

    def test_invalid_durations_are_rejected_before_reading(self):
        for params in ({'timeout_seconds': True}, {'timeout_seconds': float('nan')},
                       {'timeout_seconds': float('inf')}, {'timeout_seconds': 61},
                       {'timeout_seconds': -1}, {'poll_seconds': 0}, {'cancel_event': True}):
            with self.subTest(params=params), patch.object(self.planner, 'status') as status:
                with self.assertRaises(ValueError):
                    self.wait(**params)
                status.assert_not_called()

    def test_cancellation_during_read_wins_over_ready_notice(self):
        self.decide()
        cancel = threading.Event()
        original = self.planner.status
        def cancel_during_read(task_id):
            state = original(task_id)
            cancel.set()
            return state
        with patch.object(self.planner, 'status', side_effect=cancel_during_read):
            self.assertEqual(self.wait(timeout_seconds=0, cancel_event=cancel)['event'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
