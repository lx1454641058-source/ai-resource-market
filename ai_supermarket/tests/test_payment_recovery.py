"""本项目原创。合成签名支付历史及退款限制恢复，无真实渠道请求。"""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_supermarket.alipay_transport import AlipayVerificationClient, RSA2RequestSigner
from ai_supermarket.alipay_refund_query import AlipayRefundQueryClient
from ai_supermarket.budget import BudgetLedger
from ai_supermarket.common import MarketError, compact
from ai_supermarket.exchange import Exchange
from ai_supermarket.identity import Identities
from ai_supermarket.payment_orders import _hash
from ai_supermarket.payment_fulfillment import PaymentFulfillment
from ai_supermarket.payment_refunds import PaymentRefunds
from ai_supermarket import recovery
from ai_supermarket.tests import test_payment_fulfillment as fixture
from ai_supermarket.tests import test_alipay_refund_query as refund_fixture


class PaymentRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): fixture.PaymentFulfillmentTests.setUpClass()

    def setUp(self):
        self.f = fixture.PaymentFulfillmentTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.root = self.f.database.parent
        self.paths = {'market': self.f.database, 'identities': self.root / 'identities.sqlite', 'budget': self.root / 'budget.sqlite'}
        identities = Identities(self.paths['identities']); identities.create_account('alice', 'buyer')
        self.key = identities.issue_key('alice')
        budget = BudgetLedger(self.paths['budget']); budget.create('root', 'owner', 100, 'test-only')
        budget.grant_task('root', 'task', 'alice')
        from cryptography.hazmat.primitives import serialization
        signer = RSA2RequestSigner(self.f.key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        self.f.policy['signing_public_key_sha256'] = signer.public_key_sha256
        self.f.fulfillment = PaymentFulfillment(self.f.orders, self.f.policy, enabled=True)
        self.transport = AlipayVerificationClient(self.f.orders, self.f.verifier, signer, clock=lambda: self.f.now[0])
        self.query = AlipayRefundQueryClient(self.f.fulfillment, self.transport, capability_reference='synthetic-only', network_enabled=True)
        self.refunds = PaymentRefunds(self.query, enabled=True)

    def seed(self, deliver=True):
        self.f.seed_evidence(); self.f.confirm()
        if deliver:
            acquired = self.f.grant(); self.acquisition = acquired['acquisition_id']
            self.f.buyer.read(self.acquisition, 'task', 'original-read')
            self.f.fulfillment.record_delivery(self.f.order_id, 'task', 'original-read')

    def refund(self):
        raw = refund_fixture.RefundQueryTests.signed(SimpleNamespace(f=self.f))
        with patch.object(self.transport, '_post', return_value=raw) as post:
            self.assertTrue(self.query.query(self.f.order_id, 'refund-1', 30)['refund_confirmed'])
            self.assertEqual(1, post.call_count)
        return self.refunds.reconcile(self.f.order_id, 'refund-1', 30,
            rights_action='block_future_access', decision_reference='synthetic-review')

    def meta(self, path=None):
        with closing(recovery.connect_existing(path or self.paths['market'])) as db:
            return dict(db.execute('SELECT key,value FROM meta'))

    def change(self, key, value):
        self.f.update_meta(key, value)

    def valid(self):
        with closing(recovery.connect_existing(self.paths['market'])) as db: recovery.validate_database(db, 'market')
        self.assertEqual({'status': 'ready'}, recovery.readiness(*self.paths.values()))

    def invalid(self):
        self.assertEqual({'status': 'not_ready'}, recovery.readiness(*self.paths.values()))
        with closing(recovery.connect_existing(self.paths['market'])) as db:
            with self.assertRaises(MarketError): recovery.validate_database(db, 'market')

    def test_signed_unconfirmed_and_paid_pending_and_delivered_are_readable(self):
        self.f.seed_evidence(); self.valid()
        self.f.confirm(); self.valid()
        acquired = self.f.grant()['acquisition_id']; self.valid()
        self.f.buyer.read(acquired, 'task', 'read')
        self.f.fulfillment.record_delivery(self.f.order_id, 'task', 'read'); self.valid()
        before = recovery.digest_file(self.paths['market']); self.f.now[0] += 10000000
        with patch.object(self.transport, '_post', side_effect=AssertionError('no network')): self.valid()
        self.assertEqual(before, recovery.digest_file(self.paths['market']))

    def test_refunded_snapshot_restores_lineage_and_keeps_future_access_blocked(self):
        self.seed(); self.refund(); self.valid(); original = self.meta()
        snapshot = self.root / 'snapshot'; restored = self.root / 'restored'
        recovery.snapshot_bundle(*self.paths.values(), snapshot)
        recovery.restore_bundle(snapshot, restored); paths = recovery.paths_for(restored)
        self.assertEqual({'status': 'not_ready'}, recovery.readiness(*paths.values()))
        self.assertTrue(all(self.meta(paths['market'])[k] == v for k, v in original.items()))
        with self.assertRaises(MarketError): Identities(paths['identities']).authenticate(self.key['token'])
        recovery.release_bundle(restored, {k: 'synthetic-review-' + k for k in recovery.REVIEW_FIELDS})
        self.assertEqual({'status': 'ready'}, recovery.readiness(*paths.values()))
        market = Exchange(paths['market'], 'alice', clock=lambda: self.f.now[0])
        with self.assertRaises(MarketError) as caught: market.read(self.acquisition, 'task', 'new-read')
        self.assertEqual('refunded_access_blocked', caught.exception.code)
        with closing(recovery.connect_existing(paths['budget'])) as db:
            self.assertEqual('paused', db.execute('SELECT state FROM budgets').fetchone()[0])
        self.assertEqual(1, self.f.count('acquisitions'))

    def test_source_evidence_tamper_is_detected_even_when_confirmation_hash_changes(self):
        self.seed(); key = 'payment_order:' + self.f.order_id; order = json.loads(self.meta()[key])
        order['confirmation']['payload']['evidence']['amount_cents'] = 999
        order['confirmation']['sha256'] = _hash(order['confirmation']['payload'])
        self.change(key, order); self.invalid()

    def test_missing_response_transport_or_trade_binding_blocks_history(self):
        self.seed(); original = self.meta()
        for key in [k for k in original if k.startswith(('payment_response:', 'payment_transport:', 'payment_signed_trade:'))]:
            with self.f.buyer.store.connect(write=True) as db: db.execute('DELETE FROM meta WHERE key=?', (key,))
            self.invalid(); self.change(key, json.loads(original[key]))

    def test_unknown_transport_preserves_pending_order_without_claiming_payment(self):
        self.f.orders.reserve_handoff(self.f.order_id, 'a' * 64)
        # Real adapter creates its own bounded historical metadata; no network escapes.
        self.transport._enabled = True
        with patch.object(self.transport, '_post', side_effect=TimeoutError):
            self.transport.verify(self.f.order_id, 'test:attempt', self.f.request)
        self.valid(); self.assertEqual(0, self.f.count('acquisitions'))

    def test_loopback_or_old_freshness_cannot_be_rewritten_into_confirmed_payment(self):
        self.seed(); key = 'payment_order:' + self.f.order_id; original = json.loads(self.meta()[key])
        for field, value in (('loopback_test', True), ('https_response_received', False), ('issued_at', 0)):
            order = deepcopy(original); order['confirmation']['payload']['transport'][field] = value
            order['confirmation']['sha256'] = _hash(order['confirmation']['payload'])
            self.change(key, order); self.invalid()
        self.change(key, original)

    def test_rejected_channel_response_remains_recoverable_and_unpaid(self):
        self.f.orders.reserve_handoff(self.f.order_id, 'a' * 64)
        self.transport._enabled = True
        with patch.object(self.transport, '_post', return_value=b'not-a-signed-response') as post:
            self.transport.verify(self.f.order_id, 'rejected:attempt', self.f.request)
            self.assertEqual(1, post.call_count)
        self.valid(); self.assertEqual(0, self.f.count('acquisitions'))
        original = self.meta()
        key = next(k for k in original if k.startswith('payment_transport:'))
        record = json.loads(original[key])
        self.assertEqual('rejected', record['outcome'])
        record['error'] = 'channel_result_unknown'; self.change(key, record); self.invalid()

    def test_paid_entitlement_audit_and_delivery_links_are_required(self):
        self.seed(); original = self.meta()
        for key in [k for k in original if k.startswith(('paid_entitlement:', 'payment_delivery:'))]:
            with self.f.buyer.store.connect(write=True) as db: db.execute('DELETE FROM meta WHERE key=?', (key,))
            self.invalid(); self.change(key, json.loads(original[key]))
        with self.f.buyer.store.connect(write=True) as db:
            db.execute('UPDATE exchange_audit SET actor=? WHERE id=?', ('other', 'e_' + self.acquisition))
        self.invalid()

    def test_repeated_acquire_retains_original_paid_lineage(self):
        self.seed()
        result = self.f.buyer.acquire(self.f.resource, '1.0.0', 'reuse', purpose='personal_use', expected_release_digest=self.f.digest)
        self.assertEqual(self.acquisition, result['acquisition_id']); self.valid()

    def test_refund_anchor_missing_query_or_amount_change_is_rejected(self):
        self.seed(); self.refund(); original = self.meta()
        key = next(k for k in original if k.startswith('refund_query:'))
        with self.f.buyer.store.connect(write=True) as db: db.execute('DELETE FROM meta WHERE key=?', (key,))
        self.invalid(); self.change(key, json.loads(original[key]))
        query = json.loads(original[key]); query['binding']['refund_cents'] = 31
        query['binding_sha256'] = _hash(query['binding']); self.change(key, query); self.invalid()

    def test_unrefunded_query_unknown_does_not_change_access(self):
        self.seed()
        with patch.object(self.transport, '_post', side_effect=TimeoutError): self.query.query(self.f.order_id, 'refund-1', 30)
        self.valid(); self.assertEqual('acquired', self.f.buyer.status(self.acquisition)['status'])
        self.assertEqual(0, self.refunds.status(self.f.order_id)['recorded_refund_count'])

    def test_empty_bill_without_original_budget_binding_is_rejected(self):
        self.seed(); self.change('payment_bill:' + self.f.order_id, {})
        self.invalid()


if __name__ == '__main__': unittest.main()
