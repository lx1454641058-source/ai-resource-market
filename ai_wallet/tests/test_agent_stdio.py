"""来源：本项目原创。真实子进程的AI工具协议、作用范围与用户确认边界检查。"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ai_wallet.agent_stdio import AgentTools, MAX_REQUEST_BYTES
from ai_wallet.allocation import AllocationWallet


ROOT = Path(__file__).resolve().parents[2]


class AgentStdioTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.database = self.folder / 'tools.sqlite'
        self.wallet = AllocationWallet(self.database)
        self.wallet.configure_pool({'total_cents': 100000, 'expected_revision': 1})
        self.tools = AgentTools(self.wallet, 'creative')

    def run_cli(self, requests=(), *, raw=None, agent='creative', extra=(), database=None):
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        command = [sys.executable, '-m', 'ai_wallet.agent_stdio', '--database', str(database or self.database), '--agent-id', agent, *extra]
        content = raw if raw is not None else b''.join(json.dumps(item, ensure_ascii=False).encode('utf-8') + b'\n' for item in requests)
        result = subprocess.run(command, input=content, capture_output=True, cwd=self.folder, env=env, timeout=10)
        return result, [json.loads(line) for line in result.stdout.splitlines()]

    def propose(self, key='one'):
        return {'method': 'task.propose', 'params': {'title': '宣传配图', 'budget_cents': 30000, 'reason': '完成作品', 'request_id': key}}

    def test_separate_processes_resume_original_task_after_owner_confirmation(self):
        process, lines = self.run_cli([self.propose(), self.propose()])
        self.assertEqual(process.returncode, 0)
        self.assertEqual(process.stderr, b'')
        self.assertEqual(lines[0], lines[1])
        task = lines[0]['result']
        self.assertEqual(task['status'], 'pending_budget')
        self.assertEqual(self.wallet.state()['allocation']['allocated_cents'], 0)
        purchase = {'method': 'purchase.submit', 'params': {'task_id': task['task_id'], 'product_id': 'report-35', 'reason': '参考素材', 'request_id': 'asset-one'}}
        _, denied = self.run_cli([purchase])
        self.assertEqual(denied[0]['result']['status'], 'rejected')
        # 仅在测试中模拟用户批准；子进程工具没有此方法。
        self.wallet.decide_budget(task['task_id'], {'decision': 'approve', 'budget_cents': 30000, 'expected_revision': task['revision']})
        purchase['params']['request_id'] = 'new-authorized-need'
        _, first = self.run_cli([{'method': 'task.status', 'params': {'task_id': task['task_id']}}, purchase])
        self.assertEqual(first[0]['result']['next_action'], 'purchase_within_task')
        self.assertEqual(first[1]['result']['status'], 'paid')
        _, resumed = self.run_cli([purchase])
        self.assertEqual(first[1], resumed[0])
        self.assertEqual(self.wallet.state()['allocation']['spent_cents'], 3500)

    def test_tool_surface_rejects_owner_actions_and_extra_parameters(self):
        requests = [{'method': name, 'params': {}} for name in ('approve', 'configure_pool', '__dict__', 'task.close')]
        proposal = self.propose()
        proposal['params']['agent_id'] = 'owner'
        requests.append(proposal)
        _, outputs = self.run_cli(requests)
        self.assertTrue(all(not result['ok'] for result in outputs))
        self.assertEqual(self.wallet.state()['tasks'], [])

    def test_foreign_agent_cannot_read_purchase_or_context_for_another_task(self):
        _, lines = self.run_cli([self.propose()])
        task_id = lines[0]['result']['task_id']
        requests = [{'method': 'task.status', 'params': {'task_id': task_id}},
                    {'method': 'purchase.context', 'params': {'task_id': task_id}},
                    {'method': 'purchase.submit', 'params': {'task_id': task_id, 'product_id': 'image-12', 'reason': '越界', 'request_id': 'foreign'}},
                    {'method': 'purchase.status', 'params': {'task_id': task_id, 'order_id': 'unknown'}}]
        _, outputs = self.run_cli(requests, agent='other')
        self.assertTrue(all(item['result']['error_code'] == 'not_found' for item in outputs))
        self.assertEqual(self.wallet.state()['orders'], [])

    def test_bad_json_duplicate_fields_and_surrogates_do_not_kill_session(self):
        valid = json.dumps(self.propose()).encode() + b'\n'
        malformed = [b'{', b'{"method":"a","method":"task.propose","params":{}}',
                     b'{"method":"task.propose","params":{"budget_cents":NaN}}',
                     b'{"method":"task.status","params":{"task_id":"\\ud800"}}']
        process, results = self.run_cli(raw=b'\n'.join(malformed) + b'\n' + valid)
        self.assertEqual(process.returncode, 0)
        self.assertTrue(all(not item['ok'] for item in results[:-1]))
        self.assertEqual(results[-1]['result']['status'], 'pending_budget')

    def test_oversized_line_is_discarded_once_then_next_request_works(self):
        raw = b'x' * (MAX_REQUEST_BYTES * 3) + b'\n' + json.dumps(self.propose()).encode() + b'\n'
        _, results = self.run_cli(raw=raw)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]['result']['error_code'], 'invalid_size')
        self.assertEqual(results[1]['result']['status'], 'pending_budget')

    def test_missing_database_is_not_created_and_describe_needs_no_database(self):
        missing = self.folder / 'missing.sqlite'
        process, results = self.run_cli(database=missing)
        self.assertEqual(process.returncode, 2)
        self.assertFalse(missing.exists())
        self.assertEqual(results[0]['result']['error_code'], 'missing_ledger')
        process, result = self.run_cli(database=missing, extra=['--describe'])
        self.assertEqual(process.returncode, 0)
        self.assertEqual(len(result[0]['methods']), 6)
        self.assertFalse(missing.exists())

    def test_wrong_types_cannot_be_interpreted_as_money_or_method(self):
        for payload in ([1], {'method': [], 'params': {}}, {'method': 'task.propose', 'params': dict(self.propose()['params'], budget_cents=True)}):
            self.assertFalse(self.tools.dispatch(payload)['ok'])
        self.assertEqual(self.wallet.state()['tasks'], [])
