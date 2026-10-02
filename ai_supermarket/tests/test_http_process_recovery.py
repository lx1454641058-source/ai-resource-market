"""来源：本项目原创。独立进程崩溃与四库恢复演练，仅临时本机数据，无真实调用或付款。"""

from contextlib import closing
import http.client
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from ai_supermarket import recovery
from ai_supermarket.budget import BudgetLedger
from ai_supermarket.common import MarketError, compact
from ai_supermarket.enrollment import Enrollments
from ai_supermarket.enrollment_admin import issue_to_file
from ai_supermarket.enrollment_client import RemoteEnrollment, prepare_credential
from ai_supermarket.exchange_store import ExchangeStore
from ai_supermarket.http_client import RemoteMarket
from ai_supermarket.identity import Identities
from ai_supermarket.measurement import TaskMeter
from ai_supermarket.task_budget_gateway import TaskBudgetGateway
from ai_supermarket.tests.test_exchange import CHECKS, CONTENT, listing


@unittest.skipUnless(importlib.util.find_spec('uvicorn'), '独立服务进程实测需要既有HTTP依赖')
class HTTPProcessRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.live = self.root / 'live'
        self.live.mkdir()
        self.paths = recovery.paths_for(self.live, include_measurement=True)
        self.identities = Identities(self.paths['identities'])
        self.keys = {}
        for name, role in [('operator', 'reviewer'), ('vendor', 'seller'), ('alice', 'buyer')]:
            policy = {'capabilities': ['read_markdown'], 'permissions': [],
                      'dependencies': ['local-check:1'], 'extra_budget_cents': 100}
            self.identities.create_account(name, role, policy if role == 'buyer' else None)
            self.keys[name] = self.key_file(self.identities, name, name)
        ExchangeStore(self.paths['market'])
        self.ledger = BudgetLedger(self.paths['budget'])
        self.ledger.create('root', 'owner', 100, 'synthetic-only-budget')
        self.ledger.grant_task('root', 'task', 'alice')
        self.bind(self.paths, 'alice', self.keys['alice'][1])
        self.meter = TaskMeter(self.paths['measurement'], 'historical', 'alice',
                               experiment='process-recovery', case='pending', arm='market',
                               configuration_hash='a' * 64, evidence_mode='synthetic')
        self.pending_call = self.meter.begin('model', 'synthetic-never-executed', 'task')
        self.project = Path(__file__).resolve().parents[2]
        self.sequence = 0

    def key_file(self, identities, principal, label):
        issued = identities.issue_key(principal)
        path = self.root / (label + '.credential.json')
        with path.open('x', encoding='utf-8') as handle:
            handle.write(compact({'token': issued['token']}))
        return path, issued['key_id']

    def bind(self, paths, principal, key_id, task='task'):
        gateway = TaskBudgetGateway(paths['market'], paths['identities'], paths['budget'])
        gateway.bind_key(key_id, 'root', task, principal, 'synthetic-only-binding')

    def launch(self, paths=None, fault=None, enrollments=None):
        paths = paths or self.paths
        with closing(socket.socket()) as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        self.sequence += 1
        marker = self.root / f'fault-{self.sequence}.json'
        module = 'ai_supermarket.tests.http_crash_fixture' if fault else 'ai_supermarket.http_server'
        args = [sys.executable, '-m', module, '--database', str(paths['market']),
                '--identities', str(paths['identities']), '--budget', str(paths['budget']),
                '--measurement', str(paths['measurement']), '--port', str(port)]
        args += ['--fault-tool', fault, '--fault-marker', str(marker)] if fault else ['--enforce-task-budget']
        if enrollments:
            args += ['--enrollments', str(enrollments)]
        process = subprocess.Popen(args, cwd=self.project, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        state = {'process': process, 'port': port, 'url': f'http://127.0.0.1:{port}',
                 'marker': marker, 'closed': False}
        self.addCleanup(self.stop, state)
        deadline = time.monotonic() + 8
        while process.poll() is None and time.monotonic() < deadline:
            try:
                # The API version is not an assertion about readiness or business success.
                status, body = self.probe(state, '/health/live')
                if status == 200 and body.get('status') == 'alive':
                    return state
            except (OSError, http.client.HTTPException):
                time.sleep(0.02)
        self.fail('独立测试服务未在期限内启动；不自动改端口重启')

    def stop(self, state):
        if state['closed']:
            return
        process = state['process']
        if process.poll() is None:
            process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            # Only this confirmed live, test-owned child can be force-stopped.
            process.kill()
            stdout, stderr = process.communicate(timeout=8)
        state['closed'] = True
        self.assertIsNotNone(process.poll(), '测试子进程未停止')
        self.assertFalse(stdout or stderr, '测试服务出现意外输出；不将其内容打印到验收报告')

    def probe(self, state, endpoint):
        connection = http.client.HTTPConnection('127.0.0.1', state['port'], timeout=1)
        try:
            connection.request('GET', endpoint)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def client(self, state, principal='alice', credential=None):
        return RemoteMarket(state['url'], credential or self.keys[principal][0], timeout=3)

    def publish(self, state, dependency=False, seller=None):
        seller = seller or self.client(state, 'vendor')
        metadata = listing()
        if dependency:
            metadata['usage_contract']['dependencies'] = [
                {'id': 'local-check:1', 'license_name': 'LicenseRef-Test-Original', 'max_cost_cents': 60}]
        draft = seller.call('seller_draft', {'slug': 'process-brief', 'version': '1.0.0',
                    'metadata': metadata, 'content': CONTENT, 'request_id': 'draft'})
        self.assertEqual('draft', draft['status'])
        ref = {'id': draft['id'], 'version': '1.0.0'}
        self.assertEqual('pending_review', seller.call('seller_submit',
            {**ref, 'revision': 1, 'request_id': 'submit'})['status'])
        self.assertEqual('no_match', self.client(state).call('market_search', {'q': '产品简报'})['status'])
        result = self.client(state, 'operator').call('review_decide', {**ref, 'revision': 1,
                    'decision': 'approve', 'checks': CHECKS, 'note': '原创本机演练，不是生产审核', 'request_id': 'review'})
        self.assertEqual('approved', result['status'])
        return ref

    def rows(self, paths, table):
        self.assertIn(table, ('acquisitions', 'usage_events', 'feedback'))
        with closing(recovery.connect_existing(paths['market'])) as db:
            return [dict(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]

    def crashed_call(self, state, tool, arguments):
        with self.assertRaises(MarketError) as caught:
            self.client(state).call(tool, arguments)
        self.assertEqual('transport_unknown', caught.exception.code)
        self.assertEqual(91, state['process'].wait(timeout=8))
        marker = json.loads(state['marker'].read_text(encoding='utf-8'))
        self.assertEqual(tool, marker['tool'])
        self.stop(state)

    def command(self, module, *arguments):
        run = subprocess.run([sys.executable, '-m', module, *map(str, arguments)], cwd=self.project,
                             capture_output=True, timeout=15)
        self.assertEqual(0, run.returncode, '本地管理命令失败；不输出潜在敏感原文')
        self.assertFalse(run.stderr, '管理命令有意外错误输出')
        return json.loads(run.stdout)

    def test_acquire_committed_before_crash_replays_one_right_after_process_restart(self):
        server = self.launch(fault='market_acquire')
        ref = self.publish(server)
        request = {**ref, 'request_id': 'lost-acquisition'}
        self.crashed_call(server, 'market_acquire', request)
        committed = self.rows(self.paths, 'acquisitions')
        self.assertEqual(1, len(committed))
        restarted = self.launch()
        replay = self.client(restarted).call('market_acquire', request)
        self.assertEqual('acquired', replay['status'])
        self.assertEqual(committed[0]['id'], replay['acquisition_id'])
        self.assertEqual(committed, self.rows(self.paths, 'acquisitions'))
        received = self.client(restarted).call('market_read', {'acquisition_id': replay['acquisition_id'],
                                        'task_id': 'task', 'request_id': 'after-restart'})
        self.assertEqual(CONTENT, received['content'])
        self.assertEqual(1, self.meter.report()['unknown_calls'])

    def test_read_committed_before_crash_replays_once_and_keeps_dependency_reservation(self):
        server = self.launch(fault='market_read')
        ref = self.publish(server, dependency=True)
        acquisition = self.client(server).call('market_acquire', {**ref, 'request_id': 'get'})
        request = {'acquisition_id': acquisition['acquisition_id'], 'task_id': 'task', 'request_id': 'lost-read'}
        self.crashed_call(server, 'market_read', request)
        committed = self.rows(self.paths, 'usage_events')
        self.assertEqual(1, len(committed))
        scope = self.ledger.scope('root', 'task', 'alice')
        self.assertEqual(60, scope.summary()['held_cents'])
        restarted = self.launch()
        reply = self.client(restarted).call('market_read', request)
        self.assertEqual(CONTENT, reply['content'])
        self.assertEqual(committed[0]['id'], reply['usage_id'])
        self.assertEqual(committed, self.rows(self.paths, 'usage_events'))
        self.assertEqual(60, scope.summary()['held_cents'])
        self.assertFalse(reply['task_budget']['execution_authorized'])
        self.assertFalse(reply['task_budget']['payment_authorized'])
        self.assertEqual('reserved', reply['task_budget']['dependency_reservation']['state'])

    def test_enrolled_actors_four_ledger_recovery_and_network_reentry(self):
        invitations = self.live / 'enrollment.sqlite'
        service = Enrollments(invitations, self.paths['identities'], initialize=True)
        server = self.launch(enrollments=invitations)
        enrolled = {}
        for role in ('seller', 'buyer'):
            invite, credential = self.root / (role + '.invite.json'), self.root / (role + '.key.json')
            issue_to_file(service, role, invite)
            prepare_credential(credential)
            reply = RemoteEnrollment(server['url'], credential).enroll(invite)
            self.assertEqual('enrolled', reply['status'])
            enrolled[role] = (reply, credential)
        person, credential = enrolled['buyer']
        self.ledger.grant_task('root', 'enrolled-task', person['principal'])
        self.bind(self.paths, person['principal'], person['key_id'], 'enrolled-task')
        seller = self.client(server, credential=enrolled['seller'][1])
        buyer = self.client(server, credential=credential)
        self.assertEqual('unknown_tool', seller.call('review_decide', {})['error_code'])
        ref = self.publish(server, seller=seller)
        self.assertEqual(ref['id'], buyer.call('market_search', {'q': '产品简报'})['items'][0]['id'])
        acquisition = buyer.call('market_acquire', {**ref, 'request_id': 'enrolled-get'})
        request = {'acquisition_id': acquisition['acquisition_id'], 'task_id': 'enrolled-task', 'request_id': 'enrolled-read'}
        received = buyer.call('market_read', request)
        self.assertEqual(CONTENT, received['content'])
        self.assertEqual('accepted', buyer.call('market_feedback', {'usage_id': received['usage_id'], 'useful': 'unknown'})['status'])
        scope = self.ledger.scope('root', 'task', 'alice')
        pending = scope.reserve_micro('unresolved', 'model', 'synthetic-no-provider-call', 12345)['reservation_id']
        scope.begin(pending)
        scope.mark_unknown(pending)
        self.stop(server)
        before = {table: self.rows(self.paths, table) for table in ('acquisitions', 'usage_events', 'feedback')}
        snapshot, target = self.root / 'snapshot', self.root / 'restored'
        args = []
        for kind, path in self.paths.items():
            args += ['--' + kind, path]
        self.assertEqual('snapshotted', self.command('ai_supermarket.recovery', 'snapshot', *args, '--output', snapshot)['status'])
        self.assertEqual(4, self.command('ai_supermarket.recovery', 'verify', '--bundle', snapshot, '--require-measurement')['files'])
        self.assertEqual('restored_quarantined', self.command('ai_supermarket.recovery', 'restore', '--bundle', snapshot,
                                                            '--output', target, '--require-measurement')['status'])
        paths = recovery.paths_for(target, include_measurement=True)
        restored = self.launch(paths)
        self.assertEqual((503, {'status': 'not_ready'}), self.probe(restored, '/health/ready'))
        self.assertEqual('unauthenticated', self.client(restored, credential=credential).tool_list()['error_code'])
        identities = Identities(paths['identities'])
        fresh, key_id = self.key_file(identities, person['principal'], 'restored-buyer')
        client = self.client(restored, credential=fresh)
        self.assertEqual('recovery_required', client.call('market_search', {'q': '产品简报'})['error_code'])
        with self.assertRaises(MarketError) as error:
            Enrollments(invitations, paths['identities'])
        self.assertEqual('enrollment_recovery_required', error.exception.code)
        evidence = self.root / 'synthetic-release.json'
        evidence.write_text(compact({key: 'synthetic-local-only:' + key
                                     for key in (*recovery.REVIEW_FIELDS, 'measurement_reconciliation')}), encoding='utf-8')
        self.assertEqual('released', self.command('ai_supermarket.recovery', 'release', '--bundle', target,
                                                  '--evidence', evidence)['status'])
        self.assertEqual((200, {'status': 'ready'}), self.probe(restored, '/health/ready'))
        ledger = BudgetLedger(paths['budget'])
        summary = ledger.scope('root', 'task', 'alice').summary()
        self.assertEqual(('paused', 12345), (summary['state'], summary['held_micro_cny']))
        self.assertEqual('task_budget_required', client.call('market_read', request)['error_code'])
        with self.assertRaises(MarketError) as error:
            self.bind(paths, person['principal'], key_id, 'enrolled-task')
        self.assertEqual('budget_inactive', error.exception.code)
        ledger.set_state('root', 'active', summary['revision'])
        self.bind(paths, person['principal'], key_id, 'enrolled-task')
        replay = client.call('market_read', request)
        self.assertEqual(received['usage_id'], replay['usage_id'])
        self.assertEqual(CONTENT, replay['content'])
        self.assertEqual('duplicate', client.call('market_feedback', {'usage_id': replay['usage_id'], 'useful': 'unknown'})['status'])
        self.assertEqual(before, {table: self.rows(paths, table) for table in before})
        report = TaskMeter.existing(paths['measurement'], 'historical', 'alice').report()
        self.assertTrue(report['recovery_review_required'])
        self.assertEqual(1, report['unknown_calls'])
        self.assertIsNone(report['total_cost_micro_cny'])
        self.assertEqual('unknown', ledger.scope('root', 'task', 'alice').status(pending)['state'])
        self.assertEqual('unauthenticated', self.client(restored, credential=credential).tool_list()['error_code'])
        self.assertEqual(before, {table: self.rows(self.paths, table) for table in before})

    def test_revoked_credential_cannot_replay_read_after_crash(self):
        server = self.launch(fault='market_read')
        ref = self.publish(server)
        acquisition = self.client(server).call('market_acquire', {**ref, 'request_id': 'get'})
        request = {'acquisition_id': acquisition['acquisition_id'], 'task_id': 'task', 'request_id': 'lost-read'}
        self.crashed_call(server, 'market_read', request)
        committed = self.rows(self.paths, 'usage_events')
        self.identities.revoke_key(self.keys['alice'][1])
        restarted = self.launch()
        result = self.client(restarted).call('market_read', request)
        self.assertEqual('unauthenticated', result['error_code'])
        self.assertNotIn('content', result)
        self.assertEqual(committed, self.rows(self.paths, 'usage_events'))

    def test_suspended_product_cannot_replay_read_after_crash(self):
        server = self.launch(fault='market_read')
        ref = self.publish(server)
        acquisition = self.client(server).call('market_acquire', {**ref, 'request_id': 'get'})
        request = {'acquisition_id': acquisition['acquisition_id'], 'task_id': 'task', 'request_id': 'lost-read'}
        self.crashed_call(server, 'market_read', request)
        committed = self.rows(self.paths, 'usage_events')
        restarted = self.launch()
        result = self.client(restarted, 'operator').call('review_availability', {**ref,
                    'offer_revision': 1, 'state': 'suspended', 'reason': 'synthetic-risk-isolation',
                    'request_id': 'suspend-after-crash'})
        self.assertEqual('suspended', result['status'])
        denied = self.client(restarted).call('market_read', request)
        self.assertEqual('error', denied['status'])
        self.assertNotIn('content', denied)
        self.assertEqual(committed, self.rows(self.paths, 'usage_events'))


if __name__ == '__main__':
    unittest.main()
