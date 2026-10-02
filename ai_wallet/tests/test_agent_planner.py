"""来源：本项目原创。任务提案工具隔离、重放和审批前禁止消费检查。"""

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ai_wallet.agent_planner import TaskPlanner
from ai_wallet.agent_interface import TaskWallet
from ai_wallet.allocation import AllocationWallet


class PlannerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.wallet = AllocationWallet(Path(directory.name) / 'planner.sqlite')
        self.wallet.configure_pool({'total_cents': 100000, 'expected_revision': 1})
        self.planner = TaskPlanner(self.wallet, 'creative-agent')

    def propose(self, request_id='task-1'):
        return self.planner.propose('创作宣传图', 30000, '需要配图与素材', request_id)

    def approve(self, task):
        return self.wallet.decide_budget(task['task_id'], {
            'decision': 'approve', 'budget_cents': task['proposed_budget_cents'],
            'expected_revision': task['revision'],
        })

    def test_propose_is_pending_and_cannot_consume_before_user_confirmation(self):
        task = self.propose()
        self.assertEqual((task['status'], task['budget_cents'], task['available_cents']), ('pending_budget', 0, 0))
        self.assertEqual(task['next_action'], 'wait_for_user')
        self.assertEqual(self.wallet.state()['allocation']['unallocated_cents'], 100000)
        purchase = TaskWallet(self.wallet, task['task_id']).purchase('image-12', '配图', 'asset-1')
        self.assertEqual(purchase['status'], 'rejected')
        self.approve(task)
        observed = self.planner.status(task['task_id'])
        self.assertEqual((observed['budget_cents'], observed['next_action']), (30000, 'purchase_within_task'))
        paid = TaskWallet(self.wallet, task['task_id']).purchase('image-12', '配图', 'asset-2')
        self.assertEqual(paid['status'], 'paid')
        self.assertEqual(self.planner.status(task['task_id'])['available_cents'], 28800)

    def test_replay_keeps_task_and_changed_request_conflicts(self):
        first = self.propose()
        self.assertEqual(first, self.propose())
        changed = self.planner.propose('其他任务', 30000, '其他用途', 'task-1')
        self.assertEqual(changed['error_code'], 'idempotency_conflict')
        self.assertEqual(len(self.wallet.state()['tasks']), 1)

    def test_other_agents_cannot_read_or_increase_task(self):
        task = self.propose()
        other = TaskPlanner(self.wallet, 'other-agent')
        missing = other.status('missing')
        self.assertEqual(other.status(task['task_id']), missing)
        self.assertEqual(other.request_increase(task['task_id'], 40000, '追加', task['revision']), missing)
        other_task = other.propose('创作宣传图', 30000, '需要配图与素材', 'task-1')
        self.assertNotEqual(other_task['task_id'], task['task_id'])

    def test_increase_waits_for_owner_and_cannot_approve_or_expand_pool(self):
        task = self.propose()
        self.approve(task)
        active = self.planner.status(task['task_id'])
        proposed = self.planner.request_increase(task['task_id'], 40000, '增加视频素材', active['revision'])
        self.assertEqual((proposed['budget_cents'], proposed['proposed_budget_cents']), (30000, 40000))
        self.assertEqual(proposed['next_action'], 'wait_for_user')
        self.assertEqual(self.wallet.state()['allocation']['unallocated_cents'], 70000)
        for name in ('approve', 'decide_budget', 'configure_pool', 'close_task', 'update_task'):
            self.assertFalse(hasattr(self.planner, name))
        self.approve(proposed)
        self.assertEqual(self.wallet.state()['allocation']['unallocated_cents'], 60000)

    def test_lost_create_response_preserves_original_request_for_recovery(self):
        original = self.wallet.propose_task
        def save_then_disconnect(payload):
            original(payload)
            raise sqlite3.OperationalError('private path must not escape')
        with patch.object(self.wallet, 'propose_task', side_effect=save_then_disconnect):
            lost = self.propose()
        self.assertEqual(lost['next_action'], 'wait_for_host')
        self.assertNotIn('private path', str(lost))
        recovered = self.propose()
        self.assertEqual(recovered['status'], 'pending_budget')
        self.assertEqual(len(self.wallet.state()['tasks']), 1)

    def test_model_cannot_choose_owner_or_pass_approval_fields(self):
        for extra in ({'agent_id': 'owner'}, {'approved': True}, {'status': 'active'}):
            with self.subTest(extra=extra), self.assertRaises(TypeError):
                self.planner.propose('任务', 1000, '配图', 'one', **extra)
        self.assertEqual(self.wallet.state()['tasks'], [])
