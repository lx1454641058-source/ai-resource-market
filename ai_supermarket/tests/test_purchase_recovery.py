"""本项目原创。实际采购代码生成的隔离历史；合成签名，不访问支付渠道。"""
import base64
from contextlib import closing
import json
from unittest.mock import patch
import unittest

from ai_supermarket import recovery, payment_bills
from ai_supermarket.common import MarketError
from ai_supermarket.identity import Identities
from ai_supermarket.payment_orders import _hash
from ai_supermarket.payment_preparation import _canonical
from ai_supermarket.tests import test_purchase_host as fixture


class PurchaseRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): fixture.PurchaseHostTests.setUpClass()

    def setUp(self):
        self.f = fixture.PurchaseHostTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.root = self.f.database.parent
        self.paths = {'market': self.f.database, 'identities': self.root / 'identities.sqlite',
                      'budget': self.root / 'budget.sqlite'}
        identities = Identities(self.paths['identities']); identities.create_account('alice', 'buyer')
        self.key = identities.issue_key('alice')

    def meta(self, path=None):
        with closing(recovery.connect_existing(path or self.paths['market'])) as db:
            return dict(db.execute('SELECT key,value FROM meta'))

    def valid(self):
        with closing(recovery.connect_existing(self.paths['market'])) as db: recovery.validate_database(db, 'market')
        self.assertEqual({'status': 'ready'}, recovery.readiness(*self.paths.values()))

    def invalid(self):
        self.assertEqual({'status': 'not_ready'}, recovery.readiness(*self.paths.values()))
        with self.assertRaises(MarketError): recovery.snapshot_bundle(*self.paths.values(), self.root / 'rejected-snapshot')
        self.assertFalse((self.root / 'rejected-snapshot').exists())

    def paid(self):
        self.f.use(); self.f.seed_evidence()
        with patch.object(self.f.client, '_post', side_effect=AssertionError('no network')):
            return self.f.host.resume('buy')

    def budget_write(self, query, params=()):
        with self.f.ledger.store.connect(write=True) as db: db.execute(query, params)

    def test_mandate_bill_confirmed_and_delivered_history_and_expiry(self):
        self.valid(); self.f.use(); self.valid(); self.f.seed_evidence(); self.valid()
        self.f.confirm(); self.valid(); self.f.host.resume('buy'); self.valid()
        original = self.meta(); self.f.now[0] += 100000
        self.valid(); self.assertEqual(original, self.meta())
        payment_bills.revoke_purchase_task(self.f.orders, 'mandate'); self.valid()
        self.assertEqual(100, self.f.budget.summary()['spent_cents'])

    def test_paid_snapshot_restore_preserves_expense_and_revokes_old_authority(self):
        self.paid(); original = self.meta()
        recovery.snapshot_bundle(*self.paths.values(), self.root / 'snapshot')
        recovery.restore_bundle(self.root / 'snapshot', self.root / 'restored')
        paths = recovery.paths_for(self.root / 'restored')
        self.assertTrue(all(self.meta(paths['market'])[k] == v for k, v in original.items()))
        self.assertEqual({'status': 'not_ready'}, recovery.readiness(*paths.values()))
        with self.assertRaises(MarketError): Identities(paths['identities']).authenticate(self.key['token'])
        recovery.release_bundle(self.root / 'restored', {k: 'synthetic-review-' + k for k in recovery.REVIEW_FIELDS})
        self.assertEqual({'status': 'ready'}, recovery.readiness(*paths.values()))
        with closing(recovery.connect_existing(paths['budget'])) as db:
            self.assertEqual('paused', db.execute('SELECT state FROM budgets').fetchone()[0])
            self.assertEqual((100, 'settled'), tuple(db.execute('SELECT actual_cents,state FROM reservations').fetchone()))

    def test_forged_bill_amount_is_rejected_even_after_rehash_and_reencoding(self):
        self.f.use(); key = 'payment_bill:' + self.f.order_id; bill = json.loads(self.meta()[key])
        bill['payload']['protocol']['amount'] = '2.00'
        bill['payload_sha256'] = _hash(bill['payload'])
        bill['header'] = base64.urlsafe_b64encode(_canonical(bill['payload']).encode()).decode()
        self.f.update_meta(key, bill)
        order = json.loads(self.meta()['payment_order:' + self.f.order_id])
        order['handoff_sha256'] = bill['payload_sha256']; self.f.update_meta('payment_order:' + self.f.order_id, order)
        self.invalid()

    def test_missing_budget_reservation_rejects_snapshot_despite_valid_market(self):
        self.f.use(); self.budget_write('DELETE FROM reservations')
        with closing(recovery.connect_existing(self.paths['market'])) as db: recovery.validate_database(db, 'market')
        self.invalid()

    def test_mandate_or_bill_history_requires_budget_for_readiness(self):
        self.assertEqual({'status': 'not_ready'}, recovery.readiness(self.paths['market'], self.paths['identities']))
        self.f.use(); self.assertEqual({'status': 'not_ready'}, recovery.readiness(self.paths['market'], self.paths['identities']))

    def test_wrong_settlement_receipt_and_amount_are_rejected(self):
        self.paid()
        self.budget_write("UPDATE reservations SET evidence_ref='another-receipt'"); self.invalid()
        order = json.loads(self.meta()['payment_order:' + self.f.order_id])
        self.budget_write('UPDATE reservations SET evidence_ref=?,actual_cents=99',
                          ('payment-confirmation:' + order['confirmation']['sha256'],))
        self.invalid()

    def test_torn_settlement_is_preserved_without_double_accounting(self):
        self.f.use(); self.f.seed_evidence(); self.f.confirm()
        with patch.object(payment_bills, '_put', side_effect=OSError('synthetic interrupted market commit')):
            with self.assertRaises(OSError): self.f.biller.settle_budget(self.f.order_id)
        self.assertEqual('handed_off', self.f.biller.history(self.f.order_id)['state'])
        self.assertEqual(100, self.f.budget.summary()['spent_cents']); self.valid()
        recovery.snapshot_bundle(*self.paths.values(), self.root / 'snapshot')
        self.f.biller.settle_budget(self.f.order_id); self.valid()
        self.assertEqual(100, self.f.budget.summary()['spent_cents'])

    def test_staged_bill_with_in_flight_budget_survives_interruption(self):
        original = payment_bills._put
        def fail(db, key, value):
            if key.startswith('payment_order:'): raise OSError('synthetic interrupted handoff')
            return original(db, key, value)
        with patch.object(payment_bills, '_put', side_effect=fail):
            with self.assertRaises(OSError): self.f.use()
        self.valid()
        bill = next(json.loads(v) for k, v in self.meta().items() if k.startswith('payment_bill:'))
        self.assertEqual('staged', bill['state']); self.assertEqual(100, self.f.budget.summary()['held_cents'])
        self.f.host.handoff('buy'); self.valid()
        self.assertEqual(100, self.f.budget.summary()['held_cents'])

    def test_pre_stage_orphan_hold_is_not_reset(self):
        with patch.object(self.f.biller, '_build', side_effect=OSError('synthetic interrupted bill build')):
            with self.assertRaises(OSError): self.f.use()
        self.valid(); self.assertEqual(100, self.f.budget.summary()['held_cents'])
        recovery.snapshot_bundle(*self.paths.values(), self.root / 'snapshot')
        recovery.restore_bundle(self.root / 'snapshot', self.root / 'restored')
        with closing(recovery.connect_existing(self.root / 'restored/budget.sqlite')) as db:
            self.assertEqual((100, 'reserved'), tuple(db.execute('SELECT max_cents,state FROM reservations').fetchone()))

    def test_revoked_failed_purchase_flow_remains_history_without_bill(self):
        payment_bills.revoke_purchase_task(self.f.orders, 'mandate')
        with self.assertRaises(MarketError): self.f.use()
        self.valid(); self.assertFalse(any(k.startswith('payment_bill:') for k in self.meta()))
        self.assertEqual(0, self.f.budget.summary()['held_cents'])

    def test_host_reverse_pointer_and_mandate_binding_cannot_be_lost(self):
        self.f.use(); key = 'purchase_host_order:' + self.f.order_id; old = json.loads(self.meta()[key])
        with self.f.buyer.store.connect(write=True) as db: db.execute('DELETE FROM meta WHERE key=?', (key,))
        self.invalid(); self.f.update_meta(key, old)
        key = next(k for k in self.meta() if k.startswith('purchase_mandate:'))
        with self.f.buyer.store.connect(write=True) as db: db.execute('DELETE FROM meta WHERE key=?', (key,))
        self.invalid()

    def test_restore_release_rechecks_cross_ledger_before_opening_gate(self):
        self.paid(); recovery.snapshot_bundle(*self.paths.values(), self.root / 'snapshot')
        recovery.restore_bundle(self.root / 'snapshot', self.root / 'restored')
        paths = recovery.paths_for(self.root / 'restored')
        with closing(recovery.connect_existing(paths['budget'], writable=True)) as db, db:
            db.execute("UPDATE reservations SET evidence_ref='wrong-proof'")
        with self.assertRaises(MarketError):
            recovery.release_bundle(self.root / 'restored', {k: 'synthetic-review-' + k for k in recovery.REVIEW_FIELDS})
        self.assertEqual('pending', self.meta(paths['market'])['recovery_state'])

    def test_unknown_payment_keeps_original_exposure_and_no_entitlement(self):
        self.f.use(); self.f.client._enabled = True
        with patch.object(self.f.client, '_post', side_effect=TimeoutError): self.f.host.verify_payment('buy', 'v1', self.f.request)
        self.valid(); self.assertEqual(100, self.f.budget.summary()['held_cents'])
        self.assertEqual(0, self.f.count('acquisitions'))

    def test_unregistered_client_or_channel_extension_remains_rejected(self):
        self.paid(); self.f.update_meta('purchase_client:synthetic', {})
        self.invalid()

    def test_removing_both_bill_and_link_does_not_hide_started_purchase(self):
        self.paid()
        with self.f.buyer.store.connect(write=True) as db:
            for prefix in ('payment_bill:', 'payment_bill_budget:'):
                db.execute('DELETE FROM meta WHERE key=?', (prefix + self.f.order_id,))
        self.invalid()
        # Also check billing without a purchase-host pointer; the budget itself
        # retains the original expense request, which cannot become an orphan.
        with self.f.buyer.store.connect(write=True) as db:
            for key in list(self.meta()):
                if key.startswith(('purchase_host:', 'purchase_host_order:')):
                    db.execute('DELETE FROM meta WHERE key=?', (key,))
        with closing(recovery.connect_existing(self.paths['market'])) as db: recovery.validate_database(db, 'market')
        self.invalid()


if __name__ == '__main__': unittest.main()
