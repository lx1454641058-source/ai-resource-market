"""本项目原创。佣金、报价、准备订单的历史校验及恢复，不调用渠道。"""
from contextlib import closing
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from ai_supermarket.budget import BudgetLedger
from ai_supermarket.commission_agreements import record_agreement, _prefix, _hash as fee_hash
from ai_supermarket.common import MarketError, compact
from ai_supermarket.identity import Identities
from ai_supermarket.payment_orders import PaymentOrders, _hash
from ai_supermarket.payment_fulfillment import bind_payment_environment
from ai_supermarket import recovery
from ai_supermarket.tests import test_payment_orders as fixture


class CommerceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.PaymentOrderTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.f.quote(); self.root = self.f.database.parent
        self.paths = {'market': self.f.database, 'identities': self.root / 'identities.sqlite',
                      'budget': self.root / 'budget.sqlite'}
        identities = Identities(self.paths['identities']); identities.create_account('alice', 'buyer')
        self.key = identities.issue_key('alice')
        budget = BudgetLedger(self.paths['budget']); budget.create('root', 'owner', 100, 'isolated-test')
        budget.grant_task('root', 'task', 'alice')
        self.orders = PaymentOrders(self.f.buyer, self.f.merchant)

    def start(self, agreement=True):
        if agreement:
            record_agreement(self.f.buyer.store, 'vendor', 'intro', 'introductory', 'isolated-source', starts_on='1970-01-01')
            self.f.quoted = self.f.buyer.quote(self.f.resource, '1.0.0', 'personal_use', 'discounted')
        bind_payment_environment(self.f.buyer.store, 'sandbox')
        return self.orders.prepare(self.f.quoted['quote_id'], self.f.digest, 'original-prepare')

    def meta(self, path=None):
        with closing(recovery.connect_existing(path or self.paths['market'])) as db:
            return dict(db.execute('SELECT key,value FROM meta'))

    def set_record(self, key, value):
        with self.f.buyer.store.connect(write=True) as db:
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, compact(value)))

    def remove(self, key):
        with self.f.buyer.store.connect(write=True) as db: db.execute('DELETE FROM meta WHERE key=?', (key,))

    def ready(self):
        self.assertEqual({'status': 'ready'}, recovery.readiness(*self.paths.values()))
        with closing(recovery.connect_existing(self.paths['market'])) as db: recovery.validate_database(db, 'market')

    def invalid(self):
        self.assertEqual({'status': 'not_ready'}, recovery.readiness(*self.paths.values()))
        with closing(recovery.connect_existing(self.paths['market'])) as db:
            with self.assertRaises(MarketError): recovery.validate_database(db, 'market')

    def test_prepare_and_agreement_history_survive_repricing_expiry_and_revision(self):
        order = self.start(); before = recovery.digest_file(self.paths['market']); self.ready()
        self.assertEqual(before, recovery.digest_file(self.paths['market']))
        record_agreement(self.f.buyer.store, 'vendor', 'standard', 'standard', 'isolated-update', expected_revision=1)
        with self.f.buyer.store.connect(write=True) as db:
            db.execute("UPDATE offers SET price_cents=321,availability='withdrawn',revision=revision+1")
        self.f.now[0] = 10**8; self.ready()
        self.assertEqual(order, self.orders.get(order['order_id']))
        self.assertEqual(0, self.f.count('acquisitions'))

    def test_legacy_prepared_order_survives_new_default_policy_recovery(self):
        from ai_supermarket.commerce import fee_breakdown, LEGACY_POLICY_VERSION
        with patch('ai_supermarket.commission_agreements.fee_breakdown',
                   side_effect=lambda paid, **kw: fee_breakdown(paid, policy_version=LEGACY_POLICY_VERSION)):
            self.f.quoted = self.f.buyer.quote(self.f.resource, '1.0.0', 'personal_use', 'legacy')
            order = self.start(False)
        self.ready()
        self.assertEqual(LEGACY_POLICY_VERSION, order['prepared']['binding']['fee_policy_version'])
        self.assertEqual(order, self.orders.get(order['order_id']))

    def test_unknown_terms_version_is_not_silently_treated_as_current(self):
        self.start()
        key = _prefix('vendor') + '1'
        record = json.loads(self.meta()[key]); record['terms_version'] = 3
        self.set_record(key, record)
        self.set_record(_prefix('vendor') + 'current', {'revision': 1, 'sha256': fee_hash(record)})
        self.invalid()

    def test_unknown_verification_survives_snapshot_restore_without_reopening_order(self):
        order = self.start(); oid = order['order_id']
        self.orders.begin_verification(oid, 'attempt-one', '1' * 64)
        self.orders.observe(oid, 'attempt-one', 'unknown')
        self.ready(); original = self.meta(); snapshot = self.root / 'snapshot'; restored = self.root / 'restored'
        self.assertEqual('snapshotted', recovery.snapshot_bundle(*self.paths.values(), snapshot)['status'])
        self.assertEqual('restored_quarantined', recovery.restore_bundle(snapshot, restored)['status'])
        paths = recovery.paths_for(restored)
        after = self.meta(paths['market'])
        self.assertTrue(all(after[key] == raw for key, raw in original.items()))
        self.assertEqual('reconciliation_required', json.loads(after['payment_order:' + oid])['status'])
        self.assertEqual({'status': 'not_ready'}, recovery.readiness(*paths.values()))
        with self.assertRaises(MarketError): Identities(paths['identities']).authenticate(self.key['token'])
        with closing(recovery.connect_existing(paths['budget'])) as db:
            self.assertEqual('paused', db.execute('SELECT state FROM budgets').fetchone()[0])
        recovery.release_bundle(restored, {k: 'isolated-review-' + k for k in recovery.REVIEW_FIELDS})
        self.assertEqual({'status': 'ready'}, recovery.readiness(*paths.values()))

    def test_cancelled_order_can_be_followed_by_new_quote_without_erasing_old(self):
        old = self.start(False); self.orders.cancel_unstarted(old['order_id'])
        quote = self.f.buyer.quote(self.f.resource, '1.0.0', 'personal_use', 'replacement-quote')
        new = self.orders.prepare(quote['quote_id'], self.f.digest, 'replacement-prepare')
        self.assertNotEqual(old['order_id'], new['order_id']); self.ready()

    def test_missing_or_rolled_back_agreement_pointer_is_rejected(self):
        self.start(); prefix = _prefix('vendor')
        old = json.loads(self.meta()[prefix + '1'])
        record_agreement(self.f.buyer.store, 'vendor', 'standard', 'standard', 'next', expected_revision=1)
        current = json.loads(self.meta()[prefix + 'current'])
        self.set_record(prefix + 'current', {'revision': 1, 'sha256': fee_hash(old)}); self.invalid()
        self.set_record(prefix + 'current', current); self.remove(prefix + '1'); self.invalid()

    def test_quote_fee_amount_or_missing_snapshot_is_rejected(self):
        self.start(); key = 'quote_fee:' + self.f.quoted['quote_id']; fee = json.loads(self.meta()[key])
        self.set_record(key, {**fee, 'platform_cents': fee['platform_cents'] + 1}); self.invalid()
        self.set_record(key, fee); self.remove(key); self.invalid()

    def test_order_cannot_change_price_even_when_its_hashes_are_recomputed(self):
        oid = self.start()['order_id']; key = 'payment_order:' + oid
        order = json.loads(self.meta()[key]); order['prepared']['binding']['price_cents'] += 1
        order['prepared']['binding_sha256'] = _hash(order['prepared']['binding'])
        order['prepared_sha256'] = _hash(order['prepared'])
        self.set_record(key, order); self.invalid()

    def test_missing_reverse_indexes_or_orphan_request_are_rejected(self):
        self.start(); original = self.meta()
        for key in [k for k in original if k.startswith(('payment_channel_id:', 'payment_request:', 'payment_intent:'))]:
            self.remove(key); self.invalid(); self.set_record(key, json.loads(original[key]))
        self.set_record('payment_request:' + '0' * 64, {'fingerprint': '0' * 64, 'order_id': 'missing'})
        self.invalid()

    def test_status_cannot_discard_attempt_or_claim_confirmation(self):
        oid = self.start()['order_id']; self.orders.begin_verification(oid, 'attempt', '0' * 64)
        key = 'payment_order:' + oid; original = json.loads(self.meta()[key])
        self.set_record(key, {**original, 'status': 'prepared_not_payable'}); self.invalid()
        self.set_record(key, {**original, 'status': 'paid_entitlement_pending'}); self.invalid()

    def test_duplicate_attempt_and_incorrect_environment_are_rejected(self):
        oid = self.start()['order_id']; self.orders.begin_verification(oid, 'attempt', '0' * 64)
        key = 'payment_order:' + oid; original = json.loads(self.meta()[key])
        self.set_record(key, {**original, 'attempts': original['attempts'] * 2}); self.invalid()
        self.set_record(key, original); self.set_record('payment_environment', 'production'); self.invalid()

    def test_unrecognized_financial_record_remains_blocked(self):
        self.start(); self.set_record('payment_future_extension:unsafe', {})
        self.invalid()
        with self.assertRaises(MarketError):
            recovery.snapshot_bundle(*self.paths.values(), self.root / 'rejected')
        self.assertFalse((self.root / 'rejected').exists())


if __name__ == '__main__': unittest.main()
