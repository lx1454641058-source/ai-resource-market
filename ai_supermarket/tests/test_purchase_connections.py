"""本项目原创。配置到原采购HTTP的合成演练；CLI进程禁用渠道请求。"""
from copy import deepcopy
import hashlib
import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_supermarket import purchase_connections as module
from ai_supermarket.alipay_transport import AlipayVerificationClient
from ai_supermarket.common import MarketError, compact
from ai_supermarket.payment_bills import revoke_purchase_task
from ai_supermarket.tests import test_machine_pay_http as fixture

ROOT = Path(__file__).resolve().parents[2]


class PurchaseConnectionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): fixture.MachinePayHTTPTests.setUpClass()

    def setUp(self):
        self.f = fixture.MachinePayHTTPTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.root = self.f.database.parent; self.directory = self.root / 'connections'; self.config = self.root / 'config.json'
        self.f.now[0] = time.time(); self.f.authorize('configured', expires_at=self.f.now[0] + 3600)
        self.signing = self.root / 'synthetic-signing.pem'; self.signing.write_bytes(self.f.sign_pem)
        self.verification = self.root / 'synthetic-verification.pem'; self.verification.write_bytes(self.f.pem)
        self.plan = {'format': module.FORMAT, 'database': str(self.f.database), 'identities': str(self.f.identity_path),
            'budget': str(self.f.ledger.store.path), 'channels': [{'id': 'channel', 'policy': self.f.policy,
                'seller_name': '原创测试卖家', 'signing_key_file': str(self.signing), 'verification_key_file': str(self.verification),
                'billing_enabled': True, 'verification_enabled': False}], 'origins': [],
            'tasks': [{'key_id': self.f.issued['key_id'], 'principal': 'alice', 'root_id': 'root', 'task_id': 'task',
                'purpose': 'personal_use', 'mandate_id': 'configured', 'channel_id': 'channel', 'goal': '需要原创模板', 'origins': []}]}
        self.write()

    def write(self): self.config.write_text(compact(self.plan), encoding='utf-8')

    def app(self):
        app = self.f.make_app(purchase_hosts=None); module.attach(app, self.directory); self.f.app = app
        return app

    def error(self, code, action, *args):
        with self.assertRaises(MarketError) as caught: action(*args)
        self.assertEqual(code, caught.exception.code)

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-m', 'ai_supermarket.purchase_connections', *args],
            cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(expected, result.returncode, result.stdout + result.stderr)
        self.assertNotIn('PRIVATE KEY', result.stdout + result.stderr); self.assertNotIn(self.f.token, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_prepare_check_are_read_only_no_authorization_bill_or_network(self):
        paths = [Path(self.plan[n]) for n in ('database', 'identities', 'budget')]
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        with patch.object(AlipayVerificationClient, '_post', side_effect=AssertionError('no network')):
            self.assertEqual('prepared', self.cli('prepare', '--config', str(self.config), '--directory', str(self.directory))['status'])
            self.assertEqual('checked', self.cli('check', '--directory', str(self.directory))['status'])
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})
        self.assertEqual({'connections.json', 'binding.json', 'results'}, {p.name for p in self.directory.iterdir()})

    def test_configuration_to_bill_then_synthetic_payment_delivery_and_restart(self):
        self.plan['channels'][0]['verification_enabled'] = True; self.write()
        module.prepare(self.directory, self.config); app = self.app()
        code, first, headers = self.f.fetch(); self.assertEqual(402, code)
        self.assertIn(b'payment-needed', headers)
        self.f.order_id = first['order_id']; self.f.record = self.f.orders.get(self.f.order_id)
        self.f.body.update(self.f.record['prepared']['channel_identifiers'])
        with patch.object(AlipayVerificationClient, '_post', return_value=self.f.signed()) as post:
            code, delivered, _ = self.f.fetch(headers=[(b'payment-proof', self.f.proof())])
            self.assertEqual(200, code, delivered); self.assertEqual('read', delivered['status'])
            self.assertEqual(1, post.call_count)
        self.app()
        with patch.object(AlipayVerificationClient, '_post', side_effect=AssertionError('no retry')):
            code, again, _ = self.f.fetch()
        self.assertEqual(200, code); self.assertEqual(delivered['usage_id'], again['usage_id'])
        self.assertEqual(100, self.f.budget.summary()['spent_cents']); self.assertEqual(1, self.f.count('acquisitions'))
        self.assertIsNotNone(app.purchases)

    def test_missing_or_revoked_mandate_is_not_created_or_reactivated(self):
        self.plan['tasks'][0]['mandate_id'] = 'missing'; self.write()
        self.error('purchase_not_authorized', module.prepare, self.directory, self.config)
        self.assertFalse(self.directory.exists())
        self.plan['tasks'][0]['mandate_id'] = 'configured'; self.write(); revoke_purchase_task(self.f.orders, 'configured')
        self.error('purchase_not_authorized', module.prepare, self.directory, self.config)
        self.assertFalse(self.directory.exists())

    def test_wrong_task_duplicate_key_and_wrong_key_material_stop_before_output(self):
        original = deepcopy(self.plan)
        self.plan['tasks'][0]['task_id'] = 'other'; self.write()
        self.error('api_host_mismatch', module.prepare, self.directory, self.config)
        self.plan = deepcopy(original); self.plan['tasks'] *= 2; self.write()
        self.error('invalid_configuration', module.prepare, self.directory, self.config)
        self.plan = deepcopy(original); self.plan['channels'][0]['policy']['verification_public_key_sha256'] = 'a' * 64; self.write()
        self.error('payment_policy_mismatch', module.prepare, self.directory, self.config); self.assertFalse(self.directory.exists())

    def test_no_implicit_database_creation_or_reprepare(self):
        original = self.plan['database']; self.plan['database'] = str(self.root / 'missing.sqlite'); self.write()
        self.error('not_found', module.prepare, self.directory, self.config)
        self.assertFalse(Path(self.plan['database']).exists())
        self.plan['database'] = original; self.write(); module.prepare(self.directory, self.config)
        self.error('output_exists', module.prepare, self.directory, self.config)

    def test_changed_key_or_configuration_blocks_already_attached_host(self):
        module.prepare(self.directory, self.config); self.app()
        self.signing.write_bytes(b'changed-synthetic-key')
        self.assertEqual('connection_changed', self.f.fetch()[1]['error_code'])
        self.error('connection_changed', module.check, self.directory)
        self.assertEqual(0, self.f.count('acquisitions'))

    def test_disabled_billing_and_verification_do_not_silently_enable_network(self):
        self.plan['channels'][0]['billing_enabled'] = False; self.write()
        module.prepare(self.directory, self.config); self.app()
        with patch.object(AlipayVerificationClient, '_post', side_effect=AssertionError('no network')):
            self.assertEqual('billing_disabled', self.f.fetch()[1]['error_code'])

    def test_revoked_identity_and_task_policy_are_rechecked_on_request(self):
        module.prepare(self.directory, self.config); self.app()
        self.f.identities.revoke_key(self.f.issued['key_id'])
        self.assertEqual(401, self.f.fetch()[0]); self.assertEqual(0, self.f.count('acquisitions'))

    def test_all_tasks_validate_before_any_host_is_attached(self):
        self.plan['tasks'].append({**self.plan['tasks'][0], 'key_id': 'ak_' + 'a' * 32}); self.write()
        with self.assertRaises(MarketError): module.prepare(self.directory, self.config)
        self.assertFalse(self.directory.exists())

    def test_cli_rejects_purchase_without_budget_enforcement_before_startup(self):
        result = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-m', 'ai_supermarket.http_server',
            '--database', str(self.root / 'absent.sqlite'), '--identities', str(self.f.identity_path),
            '--purchase-connections', str(self.directory)], cwd=ROOT, capture_output=True, timeout=15)
        self.assertEqual(1, result.returncode); self.assertFalse((self.root / 'absent.sqlite').exists())

    def test_replacing_entire_valid_binding_cannot_change_running_configuration(self):
        module.prepare(self.directory, self.config); self.app()
        path = self.directory / 'connections.json'; plan = json.loads(path.read_text(encoding='utf-8'))
        plan['tasks'][0]['goal'] = '替换后的目标'; raw = compact(plan).encode()
        path.write_bytes(raw)
        (self.directory / 'binding.json').write_text(compact({'format': module.FORMAT,
            'files': module._fingerprints(plan, raw)}), encoding='utf-8')
        self.assertEqual('connection_changed', self.f.fetch()[1]['error_code'])

    def test_api_purpose_conflict_rejects_attach_before_enabling_purchase_routes(self):
        module.prepare(self.directory, self.config); app = self.f.make_app(purchase_hosts=None)
        app.apis = SimpleNamespace(hosts={self.f.issued['key_id']: {'provider': SimpleNamespace(purpose='commercial_use')}})
        self.error('purchase_host_mismatch', module.attach, app, self.directory); self.assertIsNone(app.purchases)

    def test_configured_origin_delivers_original_file_license_and_client_receipt(self):
        from ai_supermarket.tests import test_resource_bundle_http as files
        files.ResourceBundleHTTPTests.setUpClass()
        case = files.ResourceBundleHTTPTests(); case.setUp(); self.addCleanup(case.doCleanups)
        source = case.origin(); f = case.f; f.now[0] = time.time()
        f.authorize('configured', allowed_sellers=['vendor'], expires_at=f.now[0] + 3600)
        root = source.root; signing = root / 'synthetic-signing.pem'; signing.write_bytes(f.sign_pem)
        verification = root / 'synthetic-verification.pem'; verification.write_bytes(f.pem)
        origin_key = root / 'synthetic-origin.json'; origin_key.write_text(compact({'key_hex': source.client.key.hex()}))
        plan = deepcopy(self.plan); plan.update(database=str(f.database), identities=str(f.identity_path), budget=str(f.ledger.store.path))
        plan['channels'][0].update(policy=f.policy, signing_key_file=str(signing), verification_key_file=str(verification), verification_enabled=True)
        plan['tasks'][0].update(key_id=f.issued['key_id'], principal='buyer', origins=['provider-demo'])
        plan['origins'] = [{'provider': 'provider-demo', 'endpoint': source.client.endpoint,
                            'binding_id': source.client.binding_id, 'key_file': str(origin_key)}]
        config = root / 'purchase-plan.json'; config.write_text(compact(plan), encoding='utf-8')
        directory = root / 'purchase-connections'; module.prepare(directory, config)
        f.app = f.make_app(purchase_hosts=None); module.attach(f.app, directory)
        f.host = f.app.purchases.hosts[f.issued['key_id']]
        code, body, _ = f.fetch(); self.assertEqual(402, code)
        f.order_id = body['order_id']; f.record = f.orders.get(f.order_id)
        f.body.update(f.record['prepared']['channel_identifiers'])
        with patch.object(AlipayVerificationClient, '_post', return_value=f.signed()) as post:
            result = case.fetch(proof=f.proof()); self.assertEqual(1, post.call_count)
        resource, license_raw, manifest = case.unpack(result)
        self.assertEqual(source.metadata()['origin']['sha256'], hashlib.sha256(resource).hexdigest())
        self.assertTrue(license_raw); self.assertEqual('personal_use', manifest['purpose'])
        code, receipt, _ = f.fetch({'request_id': 'buy', 'bundle_sha256': result[2][b'x-bundle-sha256'].decode()},
                                  path='/v1/purchases/receipt')
        self.assertEqual(200, code); self.assertTrue(receipt['server_acknowledged'])
        calls = len(source.requests); f.app = f.make_app(purchase_hosts=None); module.attach(f.app, directory)
        self.assertEqual(result[1], case.fetch()[1]); self.assertEqual(calls, len(source.requests))

    def test_real_cli_startup_discovery_bill_and_restart_preserve_original_request(self):
        module.prepare(self.directory, self.config)
        with socket.socket() as sock: sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        command = [sys.executable, '-X', 'utf8', '-B', '-m', 'ai_supermarket.http_server',
            '--database', self.plan['database'], '--identities', self.plan['identities'], '--budget', self.plan['budget'],
            '--enforce-task-budget', '--purchase-connections', str(self.directory), '--port', str(port)]
        def request(method, path, body=None):
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
            try:
                headers = {'Authorization': 'Bearer ' + self.f.token, 'Content-Type': 'application/json'}
                conn.request(method, path, None if body is None else compact(body).encode(), headers)
                response = conn.getresponse(); raw = response.read()
                return response.status, json.loads(raw), dict(response.getheaders())
            finally: conn.close()
        def start():
            process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self.addCleanup(stop, process)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None: self.fail('configured server exited before readiness')
                try:
                    if request('GET', '/health/ready')[0] == 200: return process
                except OSError: pass
                time.sleep(0.05)
            self.fail('configured server readiness timeout')
        def stop(process):
            if process.poll() is None: process.terminate(); process.wait(timeout=5)
            if process.stderr is not None: process.stderr.close()
        process = start()
        _, discovery, _ = request('GET', '/v1/tools')
        self.assertFalse(discovery['purchase_api']['wallet_execution_available'])
        status, first, headers = request('POST', '/v1/purchases/resource', self.f.payload)
        self.assertEqual(402, status); bill = headers['payment-needed']; stop(process)
        start()
        status, again, headers = request('POST', '/v1/purchases/resource', self.f.payload)
        self.assertEqual(202, status); self.assertEqual(first['order_id'], again['order_id']); self.assertNotIn('payment-needed', headers)
        status, _, headers = request('POST', '/v1/purchases/handoff', {'request_id': 'buy'})
        self.assertEqual(402, status); self.assertEqual(bill, headers['payment-needed'])
        self.assertEqual(100, self.f.budget.summary()['held_cents'])


if __name__ == '__main__': unittest.main()
