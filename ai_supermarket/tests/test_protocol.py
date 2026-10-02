"""来源：本项目原创。真实子进程MCP及逐行JSON契约与故障测试。"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ai_supermarket.common import MAX_REQUEST_BYTES
from ai_supermarket.service import Market
from ai_supermarket.tools import tool_list


ENTRY = Path(__file__).resolve().parents[1] / 'entry.py'


def rpc(method, params=None, request_id=1):
    return {'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params or {}}


def handshake(version='2025-11-25'):
    return [rpc('initialize', {'protocolVersion': version, 'capabilities': {},
                              'clientInfo': {'name': 'test-client', 'version': '1'}}),
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'}]


def call(name, arguments, request_id=2):
    return rpc('tools/call', {'name': name, 'arguments': arguments}, request_id)


def wire(messages):
    return b''.join(json.dumps(m, ensure_ascii=False).encode('utf-8') + b'\n' for m in messages)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.database = self.root / 'market.sqlite'

    def run_process(self, messages=(), raw=None, command='mcp'):
        process = subprocess.run([sys.executable, str(ENTRY), '--database', str(self.database),
                                  '--principal', 'test-agent', command], cwd=self.root,
                                 input=raw if raw is not None else wire(messages), capture_output=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr.decode('utf-8', errors='replace'))
        self.assertEqual(process.stderr, b'')
        return [json.loads(line) for line in process.stdout.splitlines()]

    def test_handshake_and_six_tools_from_unrelated_directory(self):
        replies = self.run_process([*handshake(), rpc('tools/list', request_id='list'), rpc('ping')])
        self.assertEqual(len(replies), 3)
        self.assertEqual(replies[0]['result']['protocolVersion'], '2025-11-25')
        self.assertEqual(replies[1]['result']['tools'], tool_list())
        self.assertEqual(len(tool_list()), 6)
        self.assertTrue(all(t['inputSchema']['additionalProperties'] is False for t in tool_list()))

    def test_before_initialize_and_reinitialize_do_not_execute(self):
        replies = self.run_process([rpc('tools/list'), *handshake(), *handshake(), rpc('tools/list')])
        self.assertEqual(replies[0]['error']['code'], -32000)
        self.assertEqual(replies[2]['error']['code'], -32600)
        self.assertIn('tools', replies[-1]['result'])

    def test_unknown_protocol_offers_supported_version(self):
        replies = self.run_process(handshake('2099-01-01'))
        self.assertEqual(replies[0]['result']['protocolVersion'], '2025-11-25')

    def test_search_acquire_read_feedback_over_independent_processes(self):
        reply = self.run_process([*handshake(), call('market_search', {'q': '产品简报', 'kind': 'template'})])[-1]['result']
        self.assertEqual(json.loads(reply['content'][0]['text']), reply['structuredContent'])
        item = reply['structuredContent']['items'][0]
        self.assertEqual(item['id'], 'product-brief')
        acquire = call('market_acquire', {'id': item['id'], 'version': item['version'], 'request_id': 'buy'})
        acquired = self.run_process([*handshake(), acquire])[-1]['result']['structuredContent']
        self.assertEqual(acquired, self.run_process([*handshake(), acquire])[-1]['result']['structuredContent'])
        read = call('market_read', {'acquisition_id': acquired['acquisition_id'], 'task_id': 'brief', 'request_id': 'read'})
        content = self.run_process([*handshake(), read])[-1]['result']['structuredContent']
        self.assertIn('{{产品名称}}', content['content'])
        feedback = call('market_feedback', {'usage_id': content['usage_id'], 'useful': 'unknown', 'check': 'not_run'})
        self.assertEqual(self.run_process([*handshake(), feedback])[-1]['result']['structuredContent']['status'], 'accepted')
        self.assertEqual(self.run_process([*handshake(), feedback])[-1]['result']['structuredContent']['status'], 'duplicate')

    def test_notification_cannot_create_acquisition(self):
        notification = call('market_acquire', {'id': 'product-brief', 'version': '1.0.0', 'request_id': 'bad'})
        del notification['id']
        replies = self.run_process([*handshake(), notification, rpc('ping')])
        self.assertEqual(len(replies), 2)
        with Market(self.database, 'test-agent').store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM acquisitions').fetchone()[0], 0)

    def test_malformed_lines_are_rejected_without_killing_session(self):
        raw = wire(handshake()) + b'{\n[]\n{"jsonrpc":"2.0","jsonrpc":"2.0"}\n' + wire([rpc('ping')])
        replies = self.run_process(raw=raw)
        self.assertEqual(len(replies), 5)
        self.assertTrue(all('error' in reply for reply in replies[1:4]))
        self.assertEqual(replies[-1]['result'], {})

    def test_oversized_line_is_drained_once_then_ping_succeeds(self):
        raw = wire(handshake()) + b'x' * (MAX_REQUEST_BYTES * 2) + b'\n' + wire([rpc('ping')])
        replies = self.run_process(raw=raw)
        self.assertEqual(len(replies), 3)
        self.assertIn('error', replies[1])
        self.assertEqual(replies[2]['result'], {})

    def test_invalid_unicode_nonfinite_and_boolean_request_id(self):
        raw = wire(handshake()) + b'{"x":"\\ud800"}\n{"x":NaN}\n' + wire([rpc('ping', request_id=True), rpc('ping')])
        replies = self.run_process(raw=raw)
        self.assertTrue(all('error' in reply for reply in replies[1:-1]))
        self.assertEqual(replies[-1]['result'], {})

    def test_parameter_validation_does_not_grant_authority(self):
        replies = self.run_process([*handshake(), call('approve_payment', {}),
            call('market_acquire', {'id': 'product-brief', 'version': '1.0.0', 'request_id': 'req', 'principal': 'other'}),
            call('market_search', {'q': '产品', 'limit': True})])
        self.assertTrue(all(x['result']['isError'] for x in replies[1:]))

    def test_json_lines_and_cli_config_do_not_require_client_installation(self):
        replies = self.run_process([{'tool': 'market_search', 'arguments': {'q': 'product analysis'}}], command='stdio')
        self.assertEqual(replies[0]['items'][0]['id'], 'product-analysis')
        fresh = self.root / 'unused.sqlite'
        process = subprocess.run([sys.executable, str(ENTRY), '--database', str(fresh), 'config'],
                                 cwd=self.root, capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0)
        self.assertFalse(fresh.exists())
        config = json.loads(process.stdout)
        args = config['mcpServers']['ai-supermarket-local']['args']
        self.assertEqual(args[0], str(ENTRY))
        self.assertEqual(args[-1], 'mcp')


if __name__ == '__main__':
    unittest.main()
