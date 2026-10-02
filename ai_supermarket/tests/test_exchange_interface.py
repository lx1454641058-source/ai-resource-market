"""来源：本项目原创。真实CLI进程、MCP角色隔离和输入边界验证。"""

from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ai_supermarket.common import compact
from ai_supermarket.exchange import Exchange
from ai_supermarket.exchange_tools import ExchangeInterface
from ai_supermarket.protocol import MCPServer, serve
from ai_supermarket.tests.test_exchange import CONTENT, CHECKS, listing


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.database = self.root / 'exchange.sqlite'
        self.seller = Exchange(self.database, 'vendor', 'seller')
        self.interface = ExchangeInterface(self.seller)

    def args(self):
        return {'slug': 'brief', 'version': '1.0.0', 'metadata': listing(), 'content': CONTENT, 'request_id': 'draft'}

    def test_each_role_only_sees_own_tools(self):
        expected = {'buyer': 7, 'seller': 9, 'reviewer': 4}
        for role, count in expected.items():
            interface = ExchangeInterface(Exchange(self.database, role, role))
            tools = interface.tool_list()
            self.assertEqual(count, len(tools))
            self.assertTrue(all(t['name'].startswith({'buyer': 'market_', 'seller': 'seller_', 'reviewer': 'review_'}[role]) for t in tools))
            for tool in tools:
                self.assertFalse(tool['inputSchema']['additionalProperties'])
        self.assertEqual('unknown_tool', self.interface.call('review_decide', {})['error_code'])

    def test_unknown_identity_fields_and_nested_fields_rejected(self):
        for key in ['role', 'principal', 'seller', 'approved', 'payment_status']:
            with self.subTest(key=key):
                reply = self.interface.call('seller_draft', {**self.args(), key: 'reviewer'})
                self.assertEqual('invalid_params', reply['error_code'])
        args = self.args()
        args['metadata'] = {**listing(), 'permissions': ['shell']}
        self.assertEqual('invalid_params', self.interface.call('seller_draft', args)['error_code'])

    def test_nested_schema_types_and_unknown_tool_never_execute(self):
        for bad in [None, True, 1, [], {}, 'bad']:
            args = self.args()
            args['metadata'] = bad
            self.assertEqual('error', self.interface.call('seller_draft', args)['status'])
        for tags in [[{}], [True], 'brief', [], ['x'] * 31]:
            args = self.args()
            args['metadata']['keywords'] = tags
            self.assertEqual('error', self.interface.call('seller_draft', args)['status'])
        self.assertEqual('unknown_tool', self.interface.call('__class__', {})['error_code'])

    def test_newline_content_supported_and_inspection_preserves_it(self):
        reply = self.interface.call('seller_draft', self.args())
        self.assertEqual('draft', reply['status'])
        inspected = self.interface.call('seller_status', {'id': reply['id'], 'version': '1.0.0'})
        self.assertEqual(CONTENT, inspected['content'])

    def test_mcp_lifecycle_role_list_and_notifications_cannot_mutate(self):
        server = MCPServer(self.seller, self.interface)
        init = server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}})
        self.assertEqual('ai-supermarket-seller', init['result']['serverInfo']['name'])
        server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        names = [t['name'] for t in server.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})['result']['tools']]
        self.assertIn('seller_draft', names)
        self.assertNotIn('review_decide', names)
        server.handle({'jsonrpc': '2.0', 'method': 'tools/call', 'params': {'name': 'seller_draft', 'arguments': self.args()}})
        self.assertEqual([], self.seller.list_submissions()['items'])

    def test_oversized_frame_drained_and_next_request_processed(self):
        request = compact({'tool': 'seller_draft', 'arguments': self.args()}).encode()
        source = BytesIO(b'x' * 9000 + b'\n' + request + b'\n')
        output = BytesIO()
        serve(self.seller, source, output, interface=self.interface, max_request_bytes=8192)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual('invalid_size', replies[0]['error_code'])
        self.assertEqual('draft', replies[1]['status'])

    def cli(self, role, tool, arguments, principal=None):
        entry = Path(__file__).resolve().parents[1] / 'exchange_cli.py'
        command = [sys.executable, str(entry), '--database', str(self.database), '--principal', principal or role,
                   '--role', role, 'call', tool]
        process = subprocess.run(command, cwd=self.root, input=compact(arguments).encode('utf-8'), capture_output=True, timeout=10)
        self.assertEqual(b'', process.stderr)
        return process.returncode, json.loads(process.stdout)

    def test_processes_can_submit_review_search_acquire_and_read_from_other_cwd(self):
        code, draft = self.cli('seller', 'seller_draft', self.args())
        self.assertEqual(0, code)
        ref = {'id': draft['id'], 'version': '1.0.0'}
        code, submitted = self.cli('seller', 'seller_submit', {**ref, 'revision': 1, 'request_id': 'submit'})
        self.assertEqual('pending_review', submitted['status'])
        code, reviewed = self.cli('reviewer', 'review_decide', {**ref, 'revision': 1, 'decision': 'approve', 'checks': CHECKS,
                                                               'note': '仅验证原创样例流程，不是生产审核', 'request_id': 'review'})
        self.assertEqual('approved', reviewed['status'])
        _, search = self.cli('buyer', 'market_search', {'q': '产品简报'})
        self.assertEqual(ref['id'], search['items'][0]['id'])
        _, acquired = self.cli('buyer', 'market_acquire', {**ref, 'request_id': 'acquire'})
        _, content = self.cli('buyer', 'market_read', {'acquisition_id': acquired['acquisition_id'], 'task_id': 'task', 'request_id': 'read'})
        self.assertEqual(CONTENT, content['content'])
        code, forbidden = self.cli('buyer', 'review_decide', {})
        self.assertEqual(1, code)
        self.assertEqual('unknown_tool', forbidden['error_code'])

    def test_paid_acquire_is_error_for_cli_and_never_creates_rights(self):
        args = self.args()
        args['metadata']['price_cents'] = 1000
        _, draft = self.cli('seller', 'seller_draft', args)
        ref = {'id': draft['id'], 'version': '1.0.0'}
        self.cli('seller', 'seller_submit', {**ref, 'revision': 1, 'request_id': 'submit'})
        self.cli('reviewer', 'review_decide', {**ref, 'revision': 1, 'decision': 'approve', 'checks': CHECKS,
                                              'note': '本地费用演练', 'request_id': 'review'})
        code, result = self.cli('buyer', 'market_acquire', {**ref, 'request_id': 'paid'})
        self.assertEqual(1, code)
        self.assertEqual('payment_unavailable', result['status'])
        with self.seller.store.connect() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM acquisitions').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
