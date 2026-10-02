"""来源：本项目原创。真实本机HTTP与跨进程MCP适配验收，测试凭据不输出。"""

import http.client
import importlib.util
import json
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ai_supermarket.common import MarketError, compact
from ai_supermarket.http_app import MarketHTTP
from ai_supermarket.http_client import RemoteMarket
from ai_supermarket.identity import Identities
from ai_supermarket.tests.test_exchange import listing, CONTENT, CHECKS


@unittest.skipUnless(importlib.util.find_spec('uvicorn'), '网络实测需requirements-http.txt；基础环境不静默安装')
class LiveHTTPTests(unittest.TestCase):
    def setUp(self):
        import uvicorn
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / 'market.sqlite'
        self.identities = Identities(self.root / 'identities.sqlite')
        self.credentials, self.issued = {}, {}
        for name, role in [('vendor', 'seller'), ('operator', 'reviewer'), ('alice', 'buyer'), ('bob', 'buyer')]:
            self.identities.create_account(name, role)
            issued = self.identities.issue_key(name)
            path = self.root / (name + '.json')
            path.write_text(compact({'token': issued['token']}), encoding='utf-8')
            self.credentials[name], self.issued[name] = path, issued
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.port = self.sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{self.port}'
        self.app = MarketHTTP(self.db, self.root / 'identities.sqlite', allowed_hosts=[f'127.0.0.1:{self.port}'])
        config = uvicorn.Config(self.app, host='127.0.0.1', port=self.port, http='h11', ws='none',
                                proxy_headers=False, access_log=False, log_level='critical', lifespan='on',
                                timeout_graceful_shutdown=5)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=lambda: self.server.run(sockets=[self.sock]), daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        deadline = time.monotonic() + 5
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.server.started)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=8)
        self.sock.close()
        self.assertFalse(self.thread.is_alive(), '测试服务未停止')

    def client(self, principal):
        return RemoteMarket(self.url, self.credentials[principal])

    def publish(self, price=0):
        seller, reviewer = self.client('vendor'), self.client('operator')
        meta = listing()
        meta['price_cents'] = price
        created = seller.call('seller_draft', {'slug': 'brief', 'version': '1.0.0', 'metadata': meta,
                                               'content': CONTENT, 'request_id': 'draft'})
        self.assertEqual('draft', created['status'])
        ref = {'id': created['id'], 'version': '1.0.0'}
        submitted = seller.call('seller_submit', {**ref, 'revision': 1, 'request_id': 'submit'})
        self.assertEqual('pending_review', submitted['status'])
        approved = reviewer.call('review_decide', {**ref, 'revision': 1, 'decision': 'approve', 'checks': CHECKS,
                            'note': '原创本地测试资料及契约演练，不是生产审核', 'request_id': 'review'})
        self.assertEqual('approved', approved['status'])
        return ref

    def test_real_network_seller_review_buyer_feedback_and_cross_owner_denial(self):
        ref = self.publish()
        buyer = self.client('alice')
        results = buyer.call('market_search', {'q': '产品简报'})
        self.assertEqual(ref['id'], results['items'][0]['id'])
        self.assertEqual('ready', buyer.call('market_resolve', ref)['compatibility']['status'])
        acquired = buyer.call('market_acquire', {**ref, 'request_id': 'acquire'})
        replay = buyer.call('market_acquire', {**ref, 'request_id': 'acquire'})
        self.assertEqual(acquired['acquisition_id'], replay['acquisition_id'])
        request = {'acquisition_id': acquired['acquisition_id'], 'task_id': 'task', 'request_id': 'read'}
        read = buyer.call('market_read', request)
        self.assertEqual(CONTENT, read['content'])
        feedback = buyer.call('market_feedback', {'usage_id': read['usage_id'], 'useful': 'unknown'})
        self.assertEqual('accepted', feedback['status'])
        denied = self.client('bob').call('market_read', request)
        self.assertEqual('error', denied['status'])
        self.assertFalse('content' in denied)

    def test_supplier_batch_uses_existing_authenticated_remote_tools(self):
        from ai_supermarket.examples.supplier_bridge import submit_batch
        from tools.market_seller import sample_request
        row = sample_request()
        result = submit_batch([row], 'vendor', self.client('vendor'))
        self.assertEqual('submitted', result['status'])
        self.assertEqual('pending_review', result['items'][0]['state'])
        ref = {'id': 'vendor:' + row['slug'], 'version': row['version']}
        buyer = self.client('alice')
        self.assertEqual('no_match', buyer.call('market_search',
            {'q': '事实清单', 'purpose': 'personal_use'})['status'])
        approved = self.client('operator').call('review_decide', {**ref, 'revision': 1,
            'decision': 'approve', 'checks': CHECKS, 'note': '原创样例工程审核，无真实卖家入驻。',
            'request_id': 'supplier-review'})
        self.assertEqual('approved', approved['status'])
        resolved = buyer.call('market_resolve', {**ref, 'purpose': 'personal_use'})
        acquired = buyer.call('market_acquire', {**ref, 'purpose': 'personal_use',
            'expected_release_digest': resolved['release_digest'], 'request_id': 'supplier-acquire'})
        self.assertEqual('acquired', acquired['status'])
        read = buyer.call('market_read', {'acquisition_id': acquired['acquisition_id'],
            'task_id': 'supplier-task', 'request_id': 'supplier-read'})
        self.assertEqual(row['content'], read['content'])
        replay = submit_batch([row], 'vendor', self.client('vendor'))
        self.assertEqual('approved', replay['items'][0]['state'])

    def test_real_readiness_and_recovery_gate_without_credential_leak(self):
        def probe(path):
            conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
            try:
                conn.request('GET', path)
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()
        self.assertEqual((503, {'status': 'not_ready'}), probe('/health/ready'))
        self.publish()
        self.assertEqual((200, {'status': 'ready'}), probe('/health/ready'))
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO meta VALUES ('recovery_state','pending')")
        self.assertEqual((503, {'status': 'not_ready'}), probe('/health/ready'))
        self.assertEqual('recovery_required', self.client('alice').call('market_search', {'q': '简报'})['error_code'])
        self.assertEqual('alive', probe('/health/live')[1]['status'])

    def test_paid_delivery_is_refused_and_revocation_takes_effect_over_network(self):
        ref = self.publish(price=100)
        buyer = self.client('alice')
        result = buyer.call('market_acquire', {**ref, 'request_id': 'paid'})
        self.assertEqual('payment_unavailable', result['status'])
        self.identities.revoke_key(self.issued['alice']['key_id'])
        self.assertEqual('unauthenticated', buyer.tool_list()['error_code'])

    def test_offline_seller_request_cli_rejection_revision_and_actual_delivery(self):
        from tools.market_seller import check_request, sample_request
        request = sample_request()
        self.assertEqual('checked', check_request(request, 'vendor')['status'])
        request_path = self.root / 'prepared-draft.json'
        request_path.write_text(compact(request), encoding='utf-8')
        result = subprocess.run([sys.executable, '-B', '-m', 'ai_supermarket.remote_cli',
                                '--url', self.url, '--credential-file', str(self.credentials['vendor']),
                                'call', 'seller_draft', '--input', str(request_path)],
                                cwd=Path(__file__).resolve().parents[2], capture_output=True, timeout=10)
        self.assertEqual(0, result.returncode, '预检请求不能通过实际远程入口提交')
        self.assertFalse(result.stderr)
        draft = json.loads(result.stdout)
        self.assertEqual('draft', draft['status'])
        ref = {'id': draft['id'], 'version': draft['version']}
        seller, reviewer, buyer = self.client('vendor'), self.client('operator'), self.client('alice')
        self.assertEqual('pending_review', seller.call('seller_submit',
                         {**ref, 'revision': 1, 'request_id': 'submit-prepared'})['status'])
        self.assertEqual('rejected', reviewer.call('review_decide', {
            **ref, 'revision': 1, 'decision': 'reject', 'checks': {**CHECKS, 'sample_checked': False},
            'note': '本地合成演练：增加来源核对提示后重新提交', 'request_id': 'reject-prepared'})['status'])
        revision = {key: value for key, value in request.items() if key != 'slug'}
        revision.update(**ref, revision=1, request_id='revise-prepared',
                        content=request['content'] + '\n检查每条事实的来源。\n')
        self.assertEqual('checked', check_request(revision, 'vendor', 'seller_revise')['status'])
        updated = seller.call('seller_revise', revision)
        self.assertEqual(2, updated['revision'])
        self.assertTrue(seller.call('seller_revise', revision)['replayed'])
        self.assertEqual('pending_review', seller.call('seller_submit',
                         {**ref, 'revision': 2, 'request_id': 'resubmit-prepared'})['status'])
        self.assertEqual('approved', reviewer.call('review_decide', {
            **ref, 'revision': 2, 'decision': 'approve', 'checks': CHECKS,
            'note': '仅核原创合成样例的内部演练；非生产权利认证', 'request_id': 'approve-prepared'})['status'])
        found = buyer.call('market_search', {'q': '事实清单', 'purpose': 'personal_use'})
        self.assertEqual(ref['id'], found['items'][0]['id'])
        acquired = buyer.call('market_acquire', {**ref, 'purpose': 'personal_use', 'request_id': 'get-prepared'})
        read = buyer.call('market_read', {'acquisition_id': acquired['acquisition_id'],
                                        'task_id': 'prepared-task', 'request_id': 'read-prepared'})
        self.assertEqual(revision['content'], read['content'])

    def test_self_enrolled_seller_to_self_enrolled_buyer_delivery(self):
        from ai_supermarket.enrollment import Enrollments
        from ai_supermarket.enrollment_admin import issue_to_file
        from ai_supermarket.enrollment_client import RemoteEnrollment, prepare_credential
        from ai_supermarket.exchange_store import ExchangeStore
        ExchangeStore(self.db)
        service = Enrollments(self.root / 'enroll.sqlite', self.root / 'identities.sqlite', initialize=True)
        self.app.enrollments = service
        clients = {}
        for role in ('seller', 'buyer'):
            invite, key = self.root / (role + '-invite.json'), self.root / (role + '-key.json')
            issue_to_file(service, role, invite)
            prepare_credential(key)
            remote = RemoteEnrollment(self.url, key)
            first = remote.enroll(invite)
            self.assertEqual('enrolled', first['status'])
            self.assertEqual(role, first['role'])
            self.assertEqual(first, remote.enroll(invite))
            clients[role] = RemoteMarket(self.url, key)
            self.assertEqual(role, clients[role].tool_list()['role'])
            self.assertNotIn('review_decide', [t['name'] for t in clients[role].tool_list()['tools']])
        seller, buyer = clients['seller'], clients['buyer']
        draft = seller.call('seller_draft', {'slug': 'new-seller-brief', 'version': '1.0.0',
                            'metadata': listing(), 'content': CONTENT, 'request_id': 'first-draft'})
        self.assertEqual('draft', draft['status'])
        ref = {'id': draft['id'], 'version': '1.0.0'}
        self.assertEqual('unknown_tool', seller.call('review_decide', {})['error_code'])
        self.assertEqual('pending_review', seller.call('seller_submit', {**ref, 'revision': 1, 'request_id': 'submit'})['status'])
        self.assertEqual('no_match', buyer.call('market_search', {'q': '产品简报'})['status'])
        approved = self.client('operator').call('review_decide', {**ref, 'revision': 1, 'decision': 'approve',
                    'checks': CHECKS, 'note': '原创本地入驻闭环测试，非外部审核', 'request_id': 'approve-new'})
        self.assertEqual('approved', approved['status'])
        found = buyer.call('market_search', {'q': '产品简报'})
        self.assertEqual(ref['id'], found['items'][0]['id'])
        acquisition = buyer.call('market_acquire', {**ref, 'request_id': 'first-acquire'})
        received = buyer.call('market_read', {'acquisition_id': acquisition['acquisition_id'],
                                             'task_id': 'first-task', 'request_id': 'first-read'})
        self.assertEqual(CONTENT, received['content'])

    def test_self_enrollment_lost_response_replays_existing_identity_over_network(self):
        from ai_supermarket.enrollment import Enrollments
        from ai_supermarket.enrollment_admin import issue_to_file
        from ai_supermarket.enrollment_client import RemoteEnrollment, prepare_credential
        from ai_supermarket.exchange_store import ExchangeStore
        ExchangeStore(self.db)
        service = Enrollments(self.root / 'enroll.sqlite', self.root / 'identities.sqlite', initialize=True)
        self.app.enrollments = service
        invite, key = self.root / 'lost-invite.json', self.root / 'lost-key.json'
        issue_to_file(service, 'buyer', invite)
        prepare_credential(key)
        client = RemoteEnrollment(self.url, key)
        async def suppress_response(scope, receive, send):
            async def discard(message):
                pass
            await self.app(scope, receive, discard)
            # Fault injection after the real HTTP request created its account.
            await send({'type': 'http.response.start', 'status': 503, 'headers': [(b'content-type', b'application/json')]})
            await send({'type': 'http.response.body', 'body': b'{"status":"error","error_code":"unavailable"}'})
        loaded = self.server.config.loaded_app
        self.server.config.loaded_app = suppress_response
        try:
            self.assertEqual('unavailable', client.enroll(invite)['error_code'])
        finally:
            self.server.config.loaded_app = loaded
        with self.identities.store.connect() as db:
            before = db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]
        reply = client.enroll(invite)
        self.assertEqual('enrolled', reply['status'])
        with self.identities.store.connect() as db:
            self.assertEqual(before, db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0])
        self.assertEqual('buyer', RemoteMarket(self.url, key).tool_list()['role'])
        self.identities.revoke_key(reply['key_id'])
        self.assertEqual('enrollment_inactive', client.enroll(invite)['error_code'])

    def test_http_status_headers_and_query_credentials_are_not_echoed(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        self.addCleanup(connection.close)
        connection.request('GET', '/v1/tools')
        response = connection.getresponse()
        self.assertEqual(401, response.status)
        self.assertEqual('no-store', response.getheader('Cache-Control'))
        self.assertIsNotNone(response.getheader('WWW-Authenticate'))
        response.read()
        connection.close()
        connection.request('GET', '/v1/tools?token=do-not-echo-this')
        response = connection.getresponse()
        self.assertEqual(400, response.status)
        self.assertFalse(b'do-not-echo-this' in response.read())

    def test_mcp_bridge_from_other_cwd_uses_credential_file_and_role_tools(self):
        ref = self.publish()
        entry = Path(__file__).resolve().parents[1] / 'remote_cli.py'
        commands = [sys.executable, str(entry), '--url', self.url, '--credential-file', str(self.credentials['alice'])]
        config = subprocess.run(commands + ['config'], cwd=self.root, capture_output=True, timeout=10)
        self.assertEqual(0, config.returncode)
        self.assertFalse(self.issued['alice']['token'].encode('ascii') in config.stdout)
        config_args = json.loads(config.stdout)['mcpServers']['ai-supermarket-remote']['args']
        self.assertTrue(Path(config_args[0]).is_absolute())
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25',
                'capabilities': {}, 'clientInfo': {'name': 'local-test', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'market_resolve', 'arguments': ref}},
            {'jsonrpc': '2.0', 'method': 'tools/call', 'params': {'name': 'market_acquire',
                'arguments': {**ref, 'request_id': 'notification-must-not-buy'}}}]
        process = subprocess.run(commands + ['mcp'], cwd=self.root, capture_output=True,
                                 input=('\n'.join(compact(x) for x in requests) + '\n').encode('utf-8'), timeout=10)
        self.assertEqual(0, process.returncode)
        self.assertEqual(b'', process.stderr)
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(3, len(responses))
        self.assertEqual(7, len(responses[1]['result']['tools']))
        self.assertEqual('ready', responses[2]['result']['structuredContent']['compatibility']['status'])
        import sqlite3
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM acquisitions').fetchone()[0])

    def test_redirect_is_not_followed_or_forwarded(self):
        count = []
        async def redirect(scope, receive, send):
            count.append(scope['path'])
            await send({'type': 'http.response.start', 'status': 302,
                        'headers': [(b'location', (self.url + '/stolen').encode('ascii'))]})
            await send({'type': 'http.response.body', 'body': b''})
        # Replace the ASGI callable seen by Uvicorn after startup, not the business gateway.
        loaded = self.server.config.loaded_app
        self.server.config.loaded_app = redirect
        try:
            with self.assertRaises(MarketError) as cm:
                self.client('alice').tool_list()
            self.assertEqual('redirect_denied', cm.exception.code)
            self.assertEqual(['/v1/tools'], count)
        finally:
            self.server.config.loaded_app = loaded

    def test_client_preserves_rate_limit_wait_seconds(self):
        self.app.limits = replace(self.app.limits, principal_per_minute=1)
        client = self.client('alice')
        self.assertEqual('buyer', client.tool_list()['role'])
        reply = client.tool_list()
        self.assertEqual('rate_limited', reply['error_code'])
        self.assertTrue(1 <= reply['retry_after_seconds'] <= 60)

    def test_positive_server_cli_starts_with_explicit_temporary_paths(self):
        from ai_supermarket.enrollment import Enrollments
        from ai_supermarket.enrollment_admin import issue_to_file
        from ai_supermarket.exchange_store import ExchangeStore
        ExchangeStore(self.root / 'child.sqlite')
        enrollments = Enrollments(self.root / 'child-enroll.sqlite', self.root / 'identities.sqlite', initialize=True)
        invite_file, key_file = self.root / 'cli-invite.json', self.root / 'cli-key.json'
        issue_to_file(enrollments, 'seller', invite_file)
        with closing(socket.socket()) as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        command = [sys.executable, '-m', 'ai_supermarket.http_server', '--database', str(self.root / 'child.sqlite'),
                   '--identities', str(self.root / 'identities.sqlite'), '--port', str(port),
                   '--enrollments', str(self.root / 'child-enroll.sqlite')]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        client = RemoteMarket(f'http://127.0.0.1:{port}', self.credentials['alice'], timeout=1)
        result = None
        try:
            deadline = time.monotonic() + 5
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    result = client.tool_list()
                    break
                except MarketError as exc:
                    if exc.code != 'transport_unknown':
                        raise
                    time.sleep(0.02)
            self.assertIsNotNone(result, 'CLI服务未在期限内启动')
            self.assertEqual('buyer', result['role'])
            for args in (['prepare', '--credential-file', str(key_file)],
                         ['enroll', '--url', f'http://127.0.0.1:{port}', '--credential-file', str(key_file),
                          '--invitation-file', str(invite_file)]):
                run = subprocess.run([sys.executable, '-m', 'ai_supermarket.enrollment_client', *args],
                                     capture_output=True, timeout=10)
                self.assertEqual(0, run.returncode)
                self.assertEqual(b'', run.stderr)
                self.assertNotIn(json.loads(key_file.read_text())['token'].encode(), run.stdout)
            self.assertEqual('seller', json.loads(run.stdout)['role'])
        finally:
            if process.poll() is None:
                process.terminate()
            stdout, stderr = process.communicate(timeout=8)
        self.assertEqual(b'', stdout)
        self.assertEqual(b'', stderr)


class ClientBoundaryTests(unittest.TestCase):
    def test_no_remote_cleartext_userinfo_query_or_bad_port(self):
        for url in ['http://localhost:8765', 'http://198.51.100.2', 'https://user:secret@example.test',
                    'https://example.test?token=x', 'https://example.test:99999', 'https://example.test/v1',
                    'https://[invalid', 'https://example.test\n', None]:
            with self.subTest(url=url):
                with self.assertRaises(MarketError):
                    RemoteMarket(url, 'unused')

    def test_missing_credentials_and_network_failure_never_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'credential.json'
            client = RemoteMarket('https://example.test', path)
            with self.assertRaises(MarketError) as cm:
                client.tool_list()
            self.assertEqual('credential_unavailable', cm.exception.code)
            # Format-valid synthetic bytes; not a real issued credential.
            path.write_text(compact({'token': 'ak_' + 'a' * 32 + '.' + 'b' * 43}), encoding='utf-8')
            with patch.object(client.opener, 'open', side_effect=TimeoutError) as send:
                with self.assertRaises(MarketError) as cm:
                    client.call('market_acquire', {'request_id': 'keep-this-id'})
                self.assertEqual('transport_unknown', cm.exception.code)
                self.assertEqual(1, send.call_count)

    def test_server_cli_refuses_public_cleartext_before_loading_identity(self):
        command = [sys.executable, '-m', 'ai_supermarket.http_server', '--database', 'unused',
                   '--identities', 'unused', '--host', '0.0.0.0']
        result = subprocess.run(command, capture_output=True, timeout=10)
        self.assertEqual(1, result.returncode)
        self.assertEqual(b'', result.stdout)
        self.assertFalse(Path('unused').exists())

    def test_tool_response_without_business_status_is_not_a_success(self):
        client = RemoteMarket('https://example.test', 'unused')
        with patch.object(client, 'request', return_value={'message': 'looks successful'}):
            with self.assertRaises(MarketError) as cm:
                client.call('market_acquire', {'request_id': 'same-id'})
        self.assertEqual('invalid_response', cm.exception.code)


if __name__ == '__main__':
    unittest.main()
