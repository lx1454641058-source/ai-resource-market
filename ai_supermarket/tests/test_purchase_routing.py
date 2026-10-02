"""本项目原创。多卖家同任务的隔离采购验收；仅合成支付与本地HTTP。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import http.client
import json
import socket
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from ai_supermarket import purchase_connections as module
from ai_supermarket.alipay_transport import AlipayVerificationClient
from ai_supermarket.common import compact
from ai_supermarket.payment_bills import authorize_purchase_task, revoke_purchase_task
from ai_supermarket.payment_orders import PaymentOrders
from ai_supermarket.recovery import readiness
from ai_supermarket.tests import test_purchase_connections as fixture
from ai_supermarket.tests import test_exchange as goods


class PurchaseRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): fixture.PurchaseConnectionsTests.setUpClass()

    def setUp(self):
        self.base = fixture.PurchaseConnectionsTests(); self.base.setUp(); self.addCleanup(self.base.doCleanups)
        self.f = self.base.f; self.plan = self.base.plan
        self.plan['format'] = module.MULTI_FORMAT
        self.other = self.publish('other', 'second')
        merchant = {**self.f.merchant, 'market_seller': 'other', 'seller_id': 'seller-two',
                    'app_id': 'app-two', 'service_id': 'service-two'}
        self.other_orders = PaymentOrders(self.f.buyer, merchant)
        authorize_purchase_task(self.other_orders, self.f.budget, 'configured-other',
            consent_reference='synthetic-only', allowed_sellers=['other'], allowed_purposes=['personal_use'],
            max_order_cents=100, expires_at=self.f.now[0] + 3600)
        channel = deepcopy(self.plan['channels'][0]); channel.update(id='second', seller_name='第二原创测试卖家')
        channel['policy']['merchant'] = merchant; self.plan['channels'].append(channel)
        self.plan['tasks'].append({**self.plan['tasks'][0], 'channel_id': 'second', 'mandate_id': 'configured-other'})
        self.payloads = [self.f.payload, {'id': self.other, 'version': '1.0.0', 'request_id': 'second'}]

    def publish(self, seller, slug, price=100):
        actor = self.f.actor(seller, 'seller')
        row = actor.draft(slug, '1.0.0', goods.listing(price), goods.CONTENT, 'draft-' + slug)
        actor.submit(row['id'], '1.0.0', 1, 'submit-' + slug)
        self.f.reviewer.review(row['id'], '1.0.0', 1, 'approve', goods.CHECKS, '原创合成测试，非真实许可', 'review-' + slug)
        return row['id']

    def start(self):
        self.base.write(); result = module.prepare(self.base.directory, self.base.config)
        self.assertEqual(1, result['tasks']); self.assertEqual(2, result['channels'])
        return self.base.app()

    def bills(self):
        result = []
        for payload in self.payloads:
            code, body, headers = self.f.fetch(payload); self.assertEqual(402, code, body)
            self.assertIn(b'payment-needed', headers); result.append((body, headers))
        self.assertEqual(200, self.f.budget.summary()['held_cents'])
        self.assertNotEqual(result[0][0]['order_id'], result[1][0]['order_id'])
        return result

    def test_two_sellers_select_original_merchants_share_budget_and_restart(self):
        self.start(); bills = self.bills(); self.base.app()
        for payload, (bill, headers), orders in zip(self.payloads, bills, (self.f.orders, self.other_orders)):
            code, state, _ = self.f.fetch({'request_id': payload['request_id']}, path='/v1/purchases/status')
            self.assertEqual(202, code, state); self.assertEqual(bill['order_id'], state['order_id'])
            self.assertEqual(orders.merchant, orders.get(bill['order_id'])['prepared']['binding']['merchant'])
            code, _, recovered = self.f.fetch({'request_id': payload['request_id']}, path='/v1/purchases/handoff')
            self.assertEqual(402, code); self.assertEqual(headers[b'payment-needed'], recovered[b'payment-needed'])
        self.assertEqual(200, self.f.budget.summary()['held_cents'])
        self.assertEqual({'status': 'ready'}, readiness(self.f.database, self.f.identity_path, self.f.ledger.store.path))

    def test_both_synthetic_payments_deliver_once_and_keep_original_requests(self):
        for channel in self.plan['channels']: channel['verification_enabled'] = True
        self.start(); bills = self.bills(); delivered = []
        for i, (payload, (bill, _), orders) in enumerate(zip(self.payloads, bills, (self.f.orders, self.other_orders))):
            trade = 'synthetic-trade-' + str(i)
            body = {**self.f.body, 'trade_no': trade, **orders.get(bill['order_id'])['prepared']['channel_identifiers']}
            proof = self.f.proof({'protocol': {'trade_no': trade, 'payment_proof': 'fictional-proof-only'}})
            with patch.object(AlipayVerificationClient, '_post', return_value=self.f.signed(body)) as post:
                code, result, _ = self.f.fetch(payload, headers=[(b'payment-proof', proof)])
            self.assertEqual(200, code, result); self.assertEqual('read', result['status'])
            self.assertEqual(1, post.call_count); delivered.append(result)
        self.base.app()
        with patch.object(AlipayVerificationClient, '_post', side_effect=AssertionError('no retry')):
            for payload, original in zip(self.payloads, delivered):
                self.assertEqual(original['usage_id'], self.f.fetch(payload)[1]['usage_id'])
        self.assertEqual(200, self.f.budget.summary()['spent_cents'])
        self.assertEqual(0, self.f.budget.summary()['held_cents']); self.assertEqual(2, self.f.count('acquisitions'))
        self.assertEqual({'status': 'ready'}, readiness(self.f.database, self.f.identity_path, self.f.ledger.store.path))

    def test_request_cannot_change_seller_even_with_payment_proof(self):
        self.start(); self.f.fetch()
        with patch.object(AlipayVerificationClient, '_post', side_effect=AssertionError('no network')):
            for headers in ([], [(b'payment-proof', self.f.proof())]):
                result = self.f.fetch({**self.payloads[1], 'request_id': 'buy'}, headers=headers)[1]
                self.assertEqual('idempotency_conflict', result['error_code'])
        self.assertEqual(100, self.f.budget.summary()['held_cents'])

    def test_concurrent_different_sellers_one_request_keeps_one_bill(self):
        self.start(); payloads = [self.payloads[0], {**self.payloads[1], 'request_id': 'buy'}]
        with ThreadPoolExecutor(max_workers=2) as executor:
            replies = list(executor.map(self.f.fetch, payloads))
        self.assertEqual(1, sum(code == 402 for code, _, _ in replies), replies)
        self.assertTrue(any(body.get('error_code') in ('idempotency_conflict', 'purchase_scope_mismatch') for _, body, _ in replies), replies)
        self.assertEqual(100, self.f.budget.summary()['held_cents'])

    def test_total_budget_is_not_multiplied_by_seller_count(self):
        self.start(); self.bills()
        third = self.publish('other', 'third'); fourth = self.publish('vendor', 'fourth')
        self.assertEqual(402, self.f.fetch({'id': third, 'version': '1.0.0', 'request_id': 'third'})[0])
        code, body, _ = self.f.fetch({'id': fourth, 'version': '1.0.0', 'request_id': 'fourth'})
        self.assertNotEqual(402, code, body); self.assertEqual('budget_exceeded', body['error_code'])
        self.assertEqual(300, self.f.budget.summary()['held_cents'])
        self.assertEqual(0, self.f.count('acquisitions'))

    def test_unconfigured_paid_seller_blocked_free_goods_remain_available(self):
        paid = self.publish('outside', 'paid'); free = self.publish('outside', 'free', 0)
        self.start()
        self.assertEqual('purchase_not_authorized', self.f.fetch({'id': paid, 'version': '1.0.0', 'request_id': 'outside'})[1]['error_code'])
        code, body, _ = self.f.fetch({'id': free, 'version': '1.0.0', 'request_id': 'free'})
        self.assertEqual(200, code, body); self.assertEqual('read', body['status'])
        self.assertEqual(0, self.f.budget.summary()['held_cents'])

    def test_removed_original_route_does_not_buy_from_remaining_seller(self):
        app = self.start(); self.bills()
        # Trusted local reconfiguration still cannot hide the durable request.
        app.purchases.hosts[self.f.issued['key_id']] = app.purchases.hosts[self.f.issued['key_id']]['vendor']
        code, body, _ = self.f.fetch({**self.payloads[0], 'request_id': 'second'})
        self.assertEqual('purchase_scope_mismatch', body['error_code']); self.assertEqual(200, self.f.budget.summary()['held_cents'])

    def test_revoked_second_seller_authority_never_falls_back(self):
        self.start(); revoke_purchase_task(self.other_orders, 'configured-other')
        self.assertEqual('purchase_authorization_revoked', self.f.fetch(self.payloads[1])[1]['error_code'])
        self.assertEqual(0, self.f.budget.summary()['held_cents'])
        self.assertEqual(402, self.f.fetch(self.payloads[0])[0])

    def test_other_seller_signed_response_cannot_grant_original_resource(self):
        for channel in self.plan['channels']: channel['verification_enabled'] = True
        self.start(); bills = self.bills()
        wrong_body = {**self.f.body, **self.other_orders.get(bills[1][0]['order_id'])['prepared']['channel_identifiers']}
        with patch.object(AlipayVerificationClient, '_post', return_value=self.f.signed(wrong_body)) as post:
            _, result, _ = self.f.fetch(self.payloads[0], headers=[(b'payment-proof', self.f.proof())])
        self.assertEqual(1, post.call_count); self.assertNotIn('content', result)
        self.assertFalse(self.f.orders.get(bills[0][0]['order_id'])['paid_verified'])
        self.assertFalse(self.other_orders.get(bills[1][0]['order_id'])['paid_verified'])
        self.assertEqual(0, self.f.count('acquisitions')); self.assertEqual(0, self.f.budget.summary()['spent_cents'])
        self.assertEqual(200, self.f.budget.summary()['held_cents'])

    def test_invalid_multi_configuration_rejected_before_output(self):
        original = deepcopy(self.plan)
        mutations = [lambda p: p.update(format=module.FORMAT),
                     lambda p: p['tasks'].append(deepcopy(p['tasks'][1])),
                     lambda p: p['tasks'][1].update(goal='另一目标'),
                     lambda p: p['tasks'][1].update(task_id='another'),
                     lambda p: p['channels'][1]['policy']['merchant'].update(market_id='other-market')]
        for mutate in mutations:
            self.base.plan = deepcopy(original); mutate(self.base.plan); self.base.write()
            self.base.error('invalid_configuration', module.prepare, self.base.directory, self.base.config)
            self.assertFalse(self.base.directory.exists())

    def file_receipt(self, price):
        from ai_supermarket.tests import test_resource_bundle_http as files
        files.ResourceBundleHTTPTests.setUpClass()
        case = files.ResourceBundleHTTPTests(); case.setUp(); self.addCleanup(case.doCleanups)
        source = case.asset(price=price); f = case.f; f.now[0] = time.time()
        f.authorize('configured', allowed_sellers=[f.merchant['market_seller']], expires_at=f.now[0] + 3600)
        merchant = {**f.merchant, 'market_seller': 'other', 'seller_id': 'seller-two', 'app_id': 'app-two'}
        authorize_purchase_task(PaymentOrders(f.buyer, merchant), f.budget, 'configured-other',
            consent_reference='synthetic-only', allowed_sellers=['other'], allowed_purposes=['personal_use'],
            max_order_cents=100, expires_at=f.now[0] + 3600)
        self.base.signing.write_bytes(f.sign_pem); self.base.verification.write_bytes(f.pem)
        self.plan.update(database=str(f.database), identities=str(f.identity_path), budget=str(f.ledger.store.path))
        for i, channel in enumerate(self.plan['channels']):
            channel['policy'] = deepcopy(f.policy)
            if i: channel['policy']['merchant'] = merchant
            channel['verification_enabled'] = True
        for task in self.plan['tasks']: task.update(key_id=f.issued['key_id'], principal='buyer')
        self.plan['tasks'].reverse()  # Resource seller is deliberately NOT the first host.
        self.base.f = f; self.base.write(); module.prepare(self.base.directory, self.base.config); self.base.app()
        self.assertEqual(['other', f.merchant['market_seller']], list(f.app.purchases.hosts[f.issued['key_id']]))
        if price:
            code, bill, _ = f.fetch(); self.assertEqual(402, code, bill)
            body = {**f.body, **f.orders.get(bill['order_id'])['prepared']['channel_identifiers']}
            with patch.object(AlipayVerificationClient, '_post', return_value=f.signed(body)) as post:
                result = case.fetch(proof=f.proof())
            self.assertEqual(1, post.call_count)
        else:
            with patch.object(AlipayVerificationClient, '_post', side_effect=AssertionError('free')): result = case.fetch()
        resource, license_raw, manifest = case.unpack(result)
        self.assertTrue(resource); self.assertTrue(license_raw); self.assertEqual('personal_use', manifest['purpose'])
        digest = hashlib.sha256(result[1]).hexdigest()
        code, receipt, _ = f.fetch({'request_id': 'buy', 'bundle_sha256': digest}, path='/v1/purchases/receipt')
        self.assertEqual(200, code, receipt); self.assertTrue(receipt['server_acknowledged'])
        self.base.app()
        self.assertEqual(result[1], case.fetch()[1])
        code, status, _ = f.fetch({'request_id': 'buy'}, path='/v1/purchases/receipt_status')
        self.assertEqual(200, code, status); self.assertTrue(status['server_acknowledged'])
        self.assertEqual(price, f.budget.summary()['spent_cents']); self.assertEqual(1, f.count('acquisitions'))
        self.assertEqual({'status': 'ready'}, readiness(f.database, f.identity_path, f.ledger.store.path))

    def test_second_seller_paid_file_license_and_receipt_survive_restart(self): self.file_receipt(100)

    def test_second_seller_free_file_receipt_routes_without_purchase_order(self): self.file_receipt(0)

    def test_real_cli_two_sellers_restart_without_new_bills(self):
        self.base.write(); module.prepare(self.base.directory, self.base.config)
        with socket.socket() as sock: sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        command = [sys.executable, '-X', 'utf8', '-B', '-m', 'ai_supermarket.http_server',
            '--database', self.plan['database'], '--identities', self.plan['identities'], '--budget', self.plan['budget'],
            '--enforce-task-budget', '--purchase-connections', str(self.base.directory), '--port', str(port)]
        def request(method, path, body=None):
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
            try:
                conn.request(method, path, None if body is None else compact(body).encode(),
                    {'Authorization': 'Bearer ' + self.f.token, 'Content-Type': 'application/json'})
                response = conn.getresponse(); raw = response.read()
                return response.status, json.loads(raw), dict(response.getheaders())
            finally: conn.close()
        def stop(process):
            if process.poll() is None: process.terminate(); process.wait(timeout=5)
            if process.stderr is not None: process.stderr.close()
        def start():
            process = subprocess.Popen(command, cwd=fixture.ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self.addCleanup(stop, process); deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None: self.fail('configured server exited before readiness')
                try:
                    if request('GET', '/health/ready')[0] == 200: return process
                except OSError: pass
                time.sleep(0.05)
            self.fail('configured server readiness timeout')
        process = start(); bills = []
        self.assertFalse(request('GET', '/v1/tools')[1]['purchase_api']['wallet_execution_available'])
        for payload in self.payloads:
            code, bill, headers = request('POST', '/v1/purchases/resource', payload)
            self.assertEqual(402, code, bill); bills.append((bill['order_id'], headers['payment-needed']))
        stop(process); start()
        for payload, (order_id, encoded) in zip(self.payloads, bills):
            code, state, headers = request('POST', '/v1/purchases/resource', payload)
            self.assertEqual(202, code, state); self.assertEqual(order_id, state['order_id'])
            self.assertNotIn('payment-needed', headers)
            code, _, headers = request('POST', '/v1/purchases/handoff', {'request_id': payload['request_id']})
            self.assertEqual(402, code); self.assertEqual(encoded, headers['payment-needed'])
        self.assertEqual(200, self.f.budget.summary()['held_cents'])


if __name__ == '__main__': unittest.main()
