"""来源：本项目原创。MCP真实子进程协议及原钱包规则复用检查。"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ai_wallet.agent_stdio import AgentTools, MAX_REQUEST_BYTES
from ai_wallet.allocation import AllocationWallet

ENTRY = Path(__file__).resolve().parents[1] / 'mcp_entry.py'


def request(method, params=None, request_id=1):
    return dict(jsonrpc='2.0', id=request_id, method=method, params=params or {})


def handshake(version='2025-11-25'):
    return [request('initialize', {'protocolVersion': version, 'capabilities': {},
            'clientInfo': {'name': 'wallet-test-client', 'version': '1'}}),
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'}]


class MCPTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.db = self.folder / 'wallet.sqlite'
        self.wallet = AllocationWallet(self.db)
        self.wallet.configure_pool({'total_cents': 10000, 'expected_revision': 1})

    def run_cli(self, messages=(), raw=None, agent='creative', database=None, extra=()):
        data = raw if raw is not None else b''.join(json.dumps(m, ensure_ascii=False).encode('utf-8') + b'\n' for m in messages)
        process = subprocess.run([sys.executable, str(ENTRY), '--database', str(database or self.db),
            '--agent-id', agent, *extra], cwd=self.folder, input=data, capture_output=True, timeout=10)
        return process, [json.loads(line) for line in process.stdout.splitlines()] if '--print-config' not in extra else []

    def proposal(self):
        return request('tools/call', {'name': 'wallet_task_propose', 'arguments': {
            'title': '配图', 'budget_cents': 3000, 'reason': '测试创作', 'request_id': 'one-task'}}, 2)

    def test_handshake_lists_only_six_tools_and_notifications_have_no_response(self):
        process, replies = self.run_cli([*handshake(), request('tools/list', request_id='list'), request('ping', request_id=3)],
                                        extra=('--enable-demo-purchases',))
        self.assertEqual(process.returncode, 0)
        self.assertEqual(process.stderr, b'')
        self.assertEqual(len(replies), 3)
        self.assertEqual(replies[0]['result']['protocolVersion'], '2025-11-25')
        tools = replies[1]['result']['tools']
        self.assertEqual(len(tools), 6)
        self.assertTrue(all(t['inputSchema']['additionalProperties'] is False for t in tools))
        self.assertFalse(any('approve' in t['name'] for t in tools))
        self.assertEqual(replies[2], {'jsonrpc': '2.0', 'id': 3, 'result': {}})

    def test_before_initialized_no_tools_and_duplicate_initialize_does_not_reset(self):
        init, notice = handshake()
        _, replies = self.run_cli([request('tools/list'), init, self.proposal(), notice,
                                  init, self.proposal()])
        self.assertEqual(replies[0]['error']['code'], -32000)
        self.assertEqual(replies[2]['error']['code'], -32000)
        self.assertEqual(replies[3]['error']['code'], -32600)
        self.assertEqual(replies[-1]['result']['structuredContent']['status'], 'pending_budget')

    def test_negotiation_offers_supported_version_and_response_matches_request_id(self):
        _, replies = self.run_cli(handshake('2099-01-01'))
        self.assertEqual(replies[0]['result']['protocolVersion'], '2025-11-25')
        self.assertEqual(replies[0]['id'], 1)

    def test_owner_confirmation_required_and_reconnection_reuses_purchase(self):
        _, replies = self.run_cli([*handshake(), self.proposal()])
        tool_result = replies[-1]['result']
        task = tool_result['structuredContent']
        self.assertEqual(json.loads(tool_result['content'][0]['text']), task)
        self.assertEqual(task['status'], 'pending_budget')
        self.assertEqual(self.wallet.state()['allocation']['allocated_cents'], 0)
        # Only the isolated test owner can approve; MCP has no owner method.
        self.wallet.decide_budget(task['task_id'], {'decision': 'approve', 'budget_cents': 3000,
                                                  'expected_revision': task['revision']})
        buy = request('tools/call', {'name': 'wallet_purchase_submit', 'arguments': {'task_id': task['task_id'],
            'product_id': 'image-12', 'reason': '配图测试', 'request_id': 'one-purchase'}}, 3)
        _, first = self.run_cli([*handshake(), buy], extra=('--enable-demo-purchases',))
        _, again = self.run_cli([*handshake(), buy], extra=('--enable-demo-purchases',))
        self.assertEqual(first[-1], again[-1])
        self.assertEqual(first[-1]['result']['structuredContent']['status'], 'paid')
        self.assertEqual(self.wallet.state()['allocation']['spent_cents'], 1200)

    def test_other_agent_and_extra_owner_arguments_are_rejected(self):
        task = AgentTools(self.wallet, 'other').planner.propose('其他任务', 100, '测试', 'other')
        _, replies = self.run_cli([*handshake(), request('tools/call', {'name': 'wallet_task_status',
            'arguments': {'task_id': task['task_id']}}),
            request('tools/call', {'name': 'approve', 'arguments': {}}),
            request('tools/call', {'name': 'wallet_task_propose', 'arguments': {'owner': True}})])
        self.assertTrue(replies[1]['result']['isError'])
        self.assertEqual(replies[1]['result']['structuredContent']['error_code'], 'not_found')
        self.assertEqual(replies[2]['error']['code'], -32602)
        self.assertTrue(replies[3]['result']['isError'])
        self.assertEqual(len(self.wallet.state()['tasks']), 1)

    def test_tool_call_notification_never_executes(self):
        message = self.proposal()
        del message['id']
        _, replies = self.run_cli([*handshake(), message, request('ping')])
        self.assertEqual(len(replies), 2)
        self.assertEqual(self.wallet.state()['tasks'], [])

    def test_bad_json_batches_and_oversized_messages_do_not_execute_or_kill_session(self):
        prefix = b''.join(json.dumps(m).encode() + b'\n' for m in handshake())
        invalid = [b'{', b'[]', b'{"jsonrpc":"2.0","jsonrpc":"2.0"}',
                   b'{"jsonrpc":"2.0","id":true,"method":"ping"}',
                   b'{"jsonrpc":"2.0","id":"\\ud800","method":"ping"}', b'x' * (MAX_REQUEST_BYTES * 2)]
        process, replies = self.run_cli(raw=prefix + b'\n'.join(invalid) + b'\n' + json.dumps(self.proposal()).encode() + b'\n')
        self.assertEqual(process.returncode, 0)
        self.assertTrue(all('error' in reply for reply in replies[1:-1]))
        self.assertEqual(replies[-1]['result']['structuredContent']['status'], 'pending_budget')
        self.assertEqual(len(self.wallet.state()['tasks']), 1)

    def test_missing_database_only_reports_startup_error_on_stderr(self):
        missing = self.folder / 'not-created.sqlite'
        process, replies = self.run_cli(database=missing)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(replies, [])
        self.assertTrue(process.stderr)
        self.assertFalse(missing.exists())

    def test_config_is_read_only_and_uses_absolute_entry_from_any_cwd(self):
        before = self.db.read_bytes()
        process, _ = self.run_cli(extra=('--print-config',))
        self.assertEqual(process.returncode, 0)
        config = json.loads(process.stdout)['mcpServers']['yuxing-wallet-demo']
        self.assertEqual(config['command'], sys.executable)
        self.assertEqual(Path(config['args'][0]), ENTRY)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertNotIn('--enable-demo-purchases', config['args'])

    def test_default_exposes_budget_only_and_rejects_hidden_purchase_without_mutation(self):
        task = AgentTools(self.wallet, 'creative').planner.propose('预算任务', 3000, '管理权限', 'task')
        self.wallet.decide_budget(task['task_id'], {'decision': 'approve', 'budget_cents': 3000,
                                                  'expected_revision': task['revision']})
        before = self.wallet.state()
        commands = [*handshake(), request('tools/list')]
        for name in ('wallet_purchase_context', 'wallet_purchase_submit', 'wallet_purchase_status'):
            commands.append(request('tools/call', {'name': name, 'arguments': {'task_id': task['task_id'],
                'product_id': 'image-12', 'reason': '不能采购', 'request_id': 'no-purchase'}}))
        _, replies = self.run_cli(commands)
        self.assertEqual({t['name'] for t in replies[1]['result']['tools']},
                         {'wallet_task_propose', 'wallet_task_status', 'wallet_task_request_increase'})
        self.assertTrue(all(r['error']['code'] == -32602 for r in replies[2:]))
        self.assertEqual(self.wallet.state(), before)

    def test_default_cannot_enable_demo_tools_through_request_parameters(self):
        _, replies = self.run_cli([*handshake(), request('tools/list', {'enable_demo_purchases': True}),
            request('tools/call', {'name': 'wallet_purchase_submit', 'arguments': {},
                                 '_meta': {'enable_demo_purchases': True}})])
        self.assertTrue(all(r['error']['code'] == -32602 for r in replies[1:]))
        self.assertEqual(self.wallet.state()['orders'], [])

    def test_demo_configuration_preserves_explicit_switch(self):
        process, _ = self.run_cli(extra=('--print-config', '--enable-demo-purchases'))
        self.assertEqual(process.returncode, 0)
        config = json.loads(process.stdout)['mcpServers']['yuxing-wallet-demo']
        self.assertEqual(config['args'][-1], '--enable-demo-purchases')


if __name__ == '__main__':
    unittest.main()
