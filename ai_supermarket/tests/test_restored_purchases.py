"""本项目原创。真实SQLite恢复副本与合成授权；不修改生产权限或发起支付。"""
import unittest
from unittest.mock import patch

from ai_supermarket import recovery, purchase_connections
from ai_supermarket.alipay_transport import AlipayVerificationClient
from ai_supermarket.budget import BudgetLedger
from ai_supermarket.exchange import Exchange
from ai_supermarket.identity import Identities
from ai_supermarket.payment_bills import authorize_purchase_task
from ai_supermarket.payment_orders import PaymentOrders
from ai_supermarket.tests import test_purchase_connections as fixture


class RestoredPurchasesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): fixture.PurchaseConnectionsTests.setUpClass()

    def setUp(self):
        self.base = fixture.PurchaseConnectionsTests(); self.base.setUp(); self.addCleanup(self.base.doCleanups)
        self.f = self.base.f
        self.base.plan['channels'][0]['verification_enabled'] = True
        self.base.write(); purchase_connections.prepare(self.base.directory, self.base.config); self.base.app()
        code, self.bill, _ = self.f.fetch(); self.assertEqual(402, code, self.bill)
        self.original = self.f.orders.get(self.bill['order_id'])

    def paid(self):
        body = {**self.f.body, **self.original['prepared']['channel_identifiers']}
        with patch.object(AlipayVerificationClient, '_post', return_value=self.f.signed(body)):
            code, result, _ = self.f.fetch(headers=[(b'payment-proof', self.f.proof())])
        self.assertEqual(200, code, result)
        return result

    def restored(self):
        f = self.f; root = self.base.root
        recovery.snapshot_bundle(f.database, f.identity_path, f.ledger.store.path, root / 'snapshot')
        recovery.restore_bundle(root / 'snapshot', root / 'restored')
        paths = recovery.paths_for(root / 'restored')
        self.old_token = f.token
        recovery.release_bundle(root / 'restored', {key: 'synthetic-only-' + key for key in recovery.REVIEW_FIELDS})
        f.database = paths['market']; f.identity_path = paths['identities']
        f.buyer = Exchange(f.database, 'alice', clock=lambda: f.now[0])
        f.orders = PaymentOrders(f.buyer, f.merchant)
        f.ledger = BudgetLedger(paths['budget'], clock=lambda: f.now[0]); f.ledger.set_state('root', 'active', 2)
        f.budget = f.ledger.scope('root', 'task', 'alice')
        f.identities = Identities(f.identity_path); f.issued = f.identities.issue_key('alice'); f.token = f.issued['token']
        app = f.make_app(purchase_hosts=None)
        app.gateway.bind_key(f.issued['key_id'], 'root', 'task', 'alice', 'synthetic-restored-task')
        sellers = [c['policy']['merchant']['market_seller'] for c in self.base.plan['channels']]
        authorize_purchase_task(f.orders, f.budget, 'new-restored-mandate', consent_reference='synthetic-new-consent',
            allowed_sellers=sellers, allowed_purposes=['personal_use'], max_order_cents=100, expires_at=f.now[0]+3600)
        self.base.plan.update(database=str(f.database), identities=str(f.identity_path), budget=str(f.ledger.store.path))
        for task in self.base.plan['tasks']: task.update(key_id=f.issued['key_id'], mandate_id='new-restored-mandate')
        self.base.directory = root / 'restored-connections'; self.base.write()
        purchase_connections.prepare(self.base.directory, self.base.config); self.base.app()

    def test_unpaid_original_request_after_path_change_does_not_issue_second_bill(self):
        self.restored()
        code, result, headers = self.f.fetch()
        self.assertEqual(202, code, result); self.assertEqual(self.bill['order_id'], result['order_id'])
        self.assertNotIn(b'payment-needed', headers)
        self.assertEqual(100, self.f.budget.summary()['held_cents'])

    def test_paid_original_rights_reused_after_fresh_identity_and_task_binding(self):
        original = self.paid(); self.restored()
        with patch.object(AlipayVerificationClient, '_post', side_effect=AssertionError('no payment query')):
            code, result, headers = self.f.fetch()
        self.assertEqual(200, code, result)
        with self.f.buyer.store.connect() as db:
            acquisition = db.execute('SELECT acquisition_id FROM usage_events WHERE id=?', (result['usage_id'],)).fetchone()[0]
        self.assertEqual(original['acquisition_id'], acquisition)
        self.assertNotIn(b'payment-needed', headers); self.assertEqual(1, self.f.count('acquisitions'))
        self.assertEqual(100, self.f.budget.summary()['spent_cents'])

    def test_status_and_handoff_point_to_original_order_without_reissuing_bill(self):
        self.restored()
        for action in ('status', 'handoff'):
            code, result, headers = self.f.fetch({'request_id': 'buy'}, path='/v1/purchases/' + action)
            self.assertEqual(202, code, result); self.assertEqual(self.bill['order_id'], result['order_id'])
            self.assertEqual('host_reconcile_restored_purchase', result['next_action'])
            self.assertNotIn(b'payment-needed', headers)
        self.assertEqual(100, self.f.budget.summary()['held_cents'])

    def test_proof_does_not_query_channel_or_reactivate_old_mandate(self):
        self.restored()
        with patch.object(AlipayVerificationClient, '_post', side_effect=AssertionError('no old query')):
            _, result, _ = self.f.fetch(headers=[(b'payment-proof', self.f.proof())])
        self.assertEqual('purchase_recovery_required', result['error_code'])
        self.assertEqual([], self.f.orders.get(self.bill['order_id'])['attempts'])

    def test_unknown_payment_remains_original_exposure_after_restore(self):
        with patch.object(AlipayVerificationClient, '_post', side_effect=TimeoutError):
            self.f.fetch(headers=[(b'payment-proof', self.f.proof())])
        self.restored()
        with patch.object(AlipayVerificationClient, '_post', side_effect=AssertionError('no retry')):
            code, result, _ = self.f.fetch()
        self.assertEqual(202, code, result); self.assertEqual(self.bill['order_id'], result['order_id'])
        self.assertFalse(result['paid_verified']); self.assertEqual(100, self.f.budget.summary()['held_cents'])
        self.assertEqual(0, self.f.count('acquisitions'))

    def test_same_original_request_cannot_change_resource(self):
        other = self.f.publish(price=100, slug='other')
        self.restored()
        _, result, _ = self.f.fetch({**self.f.payload, 'id': other})
        self.assertEqual('idempotency_conflict', result['error_code'])
        self.assertEqual(100, self.f.budget.summary()['held_cents'])

    def test_revoked_old_key_and_paused_new_budget_do_not_gain_access(self):
        self.paid(); self.restored(); current = self.f.token
        self.f.token = self.old_token; self.assertEqual(401, self.f.fetch()[0]); self.f.token = current
        self.f.ledger.set_state('root', 'paused', 3)
        self.assertEqual(403, self.f.fetch()[0])
        self.assertEqual(202, self.f.fetch({'request_id': 'buy'}, path='/v1/purchases/status')[0])
        self.assertEqual(100, self.f.budget.summary()['spent_cents'])

    def test_suspended_resource_is_not_reenabled_by_restored_paid_rights(self):
        self.paid(); self.restored()
        reviewer = Exchange(self.f.database, 'operator', 'reviewer')
        reviewer.availability(self.f.resource, '1.0.0', 1, 'suspended', 'synthetic-quality-review', 'suspend')
        _, result, headers = self.f.fetch()
        self.assertNotIn('content', result); self.assertNotIn(b'payment-needed', headers)
        self.assertEqual(100, self.f.budget.summary()['spent_cents'])

    def two_sellers(self, paid=False):
        from ai_supermarket.tests import test_purchase_routing as multi
        multi.PurchaseRoutingTests.setUpClass()
        case = multi.PurchaseRoutingTests(); case.setUp(); self.addCleanup(case.doCleanups)
        for channel in case.plan['channels']: channel['verification_enabled'] = True
        case.start(); bills = case.bills()
        self.base = case.base; self.f = case.f; self.f.payload = case.payloads[1]
        self.bill = bills[1][0]; self.original = case.other_orders.get(self.bill['order_id'])
        if paid: self.paid()
        self.restored()

    def test_second_seller_original_state_found_after_restore(self):
        self.two_sellers()
        code, result, headers = self.f.fetch({'request_id': 'second'}, path='/v1/purchases/status')
        self.assertEqual(202, code, result); self.assertEqual(self.bill['order_id'], result['order_id'])
        self.assertNotIn(b'payment-needed', headers); self.assertEqual(200, self.f.budget.summary()['held_cents'])
        self.assertEqual({'status': 'ready'}, recovery.readiness(self.f.database, self.f.identity_path, self.f.ledger.store.path))

    def test_second_seller_paid_rights_reused_without_rebuying_first_seller(self):
        self.two_sellers(paid=True)
        with patch.object(AlipayVerificationClient, '_post', side_effect=AssertionError('no query')):
            code, result, headers = self.f.fetch()
        self.assertEqual(200, code, result); self.assertIn('content', result)
        self.assertNotIn(b'payment-needed', headers); self.assertEqual(1, self.f.count('acquisitions'))
        self.assertEqual(100, self.f.budget.summary()['held_cents'])
        self.assertEqual(100, self.f.budget.summary()['spent_cents'])


if __name__ == '__main__': unittest.main()
