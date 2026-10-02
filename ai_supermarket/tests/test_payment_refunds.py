"""本项目原创。合成签名、原订单和实际权益门禁；没有真实退款或结算。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from ai_supermarket.alipay_refund_query import AlipayRefundQueryClient
from ai_supermarket.common import MarketError
from ai_supermarket.commission_agreements import record_agreement
from ai_supermarket.payment_orders import _get, _put
from ai_supermarket.payment_refunds import PaymentRefunds
from ai_supermarket.tests import test_purchase_host as fixture
from ai_supermarket.tests import test_alipay_refund_query as refund_fixture


class PaymentRefundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.PurchaseHostTests.setUpClass()

    def setUp(self):
        self.setup_order()

    def setup_order(self, *, introductory=False, api=False):
        self.f = fixture.PurchaseHostTests()
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        if introductory:
            record_agreement(self.f.buyer.store, 'vendor', 'intro', 'introductory',
                             'fixture-only', starts_on='1970-01-01')
        if api:
            from ai_supermarket.exchange import Exchange
            from ai_supermarket.tests.test_api_product import metadata, CONTENT, POLICY, CHECKS
            self.f.seller.draft('api', '1.0.0', metadata(100), CONTENT, 'api-draft')
            self.f.seller.submit('vendor:api', '1.0.0', 1, 'api-submit')
            self.f.reviewer.review('vendor:api', '1.0.0', 1, 'approve', CHECKS, 'fixture-only', 'api-review')
            self.f.buyer = Exchange(self.f.database, 'alice', clock=lambda: self.f.now[0], host_policy=POLICY)
            self.f.orders.market = self.f.buyer
            self.f.resource = 'vendor:api'
            self.f.host = self.f.make_host()
        self.f.use(); self.f.seed_evidence(); self.f.confirm()
        self.client = AlipayRefundQueryClient(self.f.fulfillment, self.f.client,
            capability_reference='synthetic-only', network_enabled=True)
        self.refunds = PaymentRefunds(self.client, enabled=True)

    def error(self, code, call, *args, **kwargs):
        with self.assertRaises(MarketError) as caught:
            call(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)

    def observe(self, refund='refund-1', cents=30, **changes):
        raw = refund_fixture.RefundQueryTests.signed(self, {'out_request_no': refund,
            'refund_amount': f'{cents // 100}.{cents % 100:02d}', **changes})
        with patch.object(self.f.client, '_post', return_value=raw):
            return self.client.query(self.f.order_id, refund, cents)

    def apply(self, refund='refund-1', cents=30, action='preserve_access', reference='fixture-decision'):
        return self.refunds.reconcile(self.f.order_id, refund, cents,
            rights_action=action, decision_reference=reference)

    def test_missing_unconfirmed_unknown_and_disabled_do_not_change_rights(self):
        self.error('refund_not_confirmed', self.apply)
        self.observe(refund_status='PROCESSING')
        self.error('refund_not_confirmed', self.apply)
        with patch.object(self.f.client, '_post', side_effect=TimeoutError):
            self.client.query(self.f.order_id, 'unknown', 30)
        self.error('refund_not_confirmed', self.apply, 'unknown')
        disabled = PaymentRefunds(self.client)
        self.error('refund_reconciliation_disabled', disabled.reconcile, self.f.order_id, 'refund-1', 30,
                   rights_action='block_future_access', decision_reference='fixture')
        self.assertEqual(0, self.refunds.status(self.f.order_id)['recorded_refund_count'])
        self.assertIn('acquisition_id', self.f.fulfillment.grant(self.f.order_id))

    def test_loopback_test_observation_never_changes_access(self):
        from ai_supermarket.alipay_transport import AlipayVerificationClient
        self.f.client = AlipayVerificationClient(self.f.orders, self.f.verifier, self.f.signer,
            clock=lambda: self.f.now[0], loopback_endpoint='http://127.0.0.1:12345/gateway.do')
        self.client = AlipayRefundQueryClient(self.f.fulfillment, self.f.client,
            capability_reference='synthetic-only', network_enabled=True)
        self.refunds = PaymentRefunds(self.client, enabled=True)
        self.assertEqual('test_response', self.observe()['status'])
        self.error('refund_not_confirmed', self.apply)
        self.assertEqual(0, self.refunds.status(self.f.order_id)['recorded_refund_count'])

    def test_preserved_rights_reopen_without_network_or_budget_restore(self):
        delivered = self.f.host.resume('buy')
        before = deepcopy(self.f.orders.get(self.f.order_id))
        budget = deepcopy(self.f.budget.summary())
        self.observe()
        with patch.object(self.f.client, '_post', side_effect=AssertionError('no network')):
            first = self.apply()
            self.refunds = PaymentRefunds(self.client, enabled=True)
            again = self.apply()
            self.assertTrue(again.pop('replayed')); self.assertFalse(first.pop('replayed'))
            self.assertEqual(first, again)
        current = self.f.orders.get(self.f.order_id); current.pop('refund_ledger_sha256')
        self.assertEqual(before, current)
        self.assertEqual(budget, self.f.budget.summary())
        self.assertEqual(delivered['content'], self.f.host.resume('buy')['content'])
        self.assertEqual(30, first['recorded_refund_cents'])
        self.assertFalse(first['future_access_blocked']); self.assertFalse(first['commission_settled'])
        self.assertFalse(first['funds_moved']); self.assertFalse(first['channel_refund_total_known'])
        self.assertEqual(6, first['accounting_proposal']['remaining_platform_cents'])
        self.assertIsNone(first['accounting_proposal']['channel_fee_cents'])

    def test_full_refund_blocks_initial_grant_without_deleting_paid_history(self):
        self.observe(cents=100)
        self.apply(cents=100, action='block_future_access')
        self.error('refunded_access_blocked', self.f.fulfillment.grant, self.f.order_id)
        self.assertEqual(0, self.f.count('acquisitions'))
        self.assertTrue(self.f.orders.get(self.f.order_id)['paid_verified'])
        self.assertTrue(self.client.history(self.f.order_id, 'refund-1', 100)['refund_confirmed'])
        self.assertEqual(0, self.refunds.status(self.f.order_id)['accounting_proposal']['remaining_net_cents'])

    def test_block_rechecks_existing_read_usage_replay_grant_and_purchase_resume(self):
        delivered = self.f.host.resume('buy'); acquired = delivered['acquisition_id']
        self.f.buyer.read(acquired, 'check-task', 'read')
        counts = [self.f.count(t) for t in ('acquisitions', 'usage_events')]
        self.observe(); self.apply(action='block_future_access')
        for call, args in ((self.f.buyer.read, (acquired, 'check-task', 'read')),
                           (self.f.buyer.read, (acquired, 'check-task', 'new-read')),
                           (self.f.fulfillment.grant, (self.f.order_id,)),
                           (self.f.host.resume, ('buy',))):
            self.error('refunded_access_blocked', call, *args)
        self.assertEqual(counts, [self.f.count(t) for t in ('acquisitions', 'usage_events')])
        self.assertTrue(self.refunds.status(self.f.order_id)['future_access_blocked'])
        self.assertTrue(self.f.orders.get(self.f.order_id)['paid_verified'])

    def test_cumulative_rounding_full_refund_and_later_preserve_cannot_unblock(self):
        for number, cents, action, expected in ((1, 15, 'preserve_access', 7),
                                               (2, 15, 'block_future_access', 6),
                                               (3, 70, 'preserve_access', 0)):
            refund = f'r-{number}'
            self.observe(refund, cents)
            result = self.apply(refund, cents, action)
            self.assertEqual(expected, result['accounting_proposal']['remaining_platform_cents'])
            self.assertEqual(number, result['recorded_refund_count'])
        self.assertTrue(result['future_access_blocked'])
        self.assertEqual((8, 92), (result['accounting_proposal']['cumulative_platform_reversal_cents'],
                                   result['accounting_proposal']['cumulative_seller_reversal_before_other_fees_cents']))

    def test_legacy_paid_order_refund_keeps_ten_percent_after_upgrade(self):
        from ai_supermarket.commerce import fee_breakdown, LEGACY_POLICY_VERSION
        with patch('ai_supermarket.commission_agreements.fee_breakdown',
                   side_effect=lambda paid, **kw: fee_breakdown(paid, policy_version=LEGACY_POLICY_VERSION)):
            self.setup_order()
        self.observe(); result = self.apply()
        self.assertEqual((1000, 7), (result['accounting_proposal']['commission_bps'],
                                    result['accounting_proposal']['remaining_platform_cents']))

    def test_original_introductory_rate_survives_current_standard_terms(self):
        self.setup_order(introductory=True)
        record_agreement(self.f.buyer.store, 'vendor', 'standard-now', 'standard', 'fixture-only', expected_revision=1)
        self.observe(); result = self.apply()
        self.assertEqual((500, 4), (result['accounting_proposal']['commission_bps'],
                                   result['accounting_proposal']['remaining_platform_cents']))

    def test_excess_and_changed_decision_cannot_rewrite_ledger(self):
        self.observe(cents=60); self.apply(cents=60)
        before = self.refunds.status(self.f.order_id)
        self.observe('refund-2', 50)
        self.error('refund_exceeds_payment', self.apply, 'refund-2', 50)
        self.error('idempotency_conflict', self.apply, cents=60, action='block_future_access')
        self.error('idempotency_conflict', self.apply, cents=60, reference='new-decision')
        self.error('refund_binding_mismatch', self.apply, cents=61)
        self.assertEqual(before, self.refunds.status(self.f.order_id))

    def test_concurrent_same_refund_is_counted_once(self):
        self.observe()
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: self.apply(), range(2)))
        self.assertEqual(1, sum(r['replayed'] for r in replies))
        self.assertEqual(30, self.refunds.status(self.f.order_id)['recorded_refund_cents'])

    def test_concurrent_different_refunds_cannot_exceed_original_payment(self):
        self.observe('r-a', 60); self.observe('r-b', 60)
        def run(refund):
            try: return self.apply(refund, 60)['recorded_refund_cents']
            except MarketError as exc: return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(run, ('r-a', 'r-b')))
        self.assertCountEqual([60, 'refund_exceeds_payment'], replies)
        self.assertEqual(1, self.refunds.status(self.f.order_id)['recorded_refund_count'])

    def test_late_earlier_query_reply_does_not_replace_the_applied_source(self):
        started, release = threading.Event(), threading.Event()
        raw = refund_fixture.RefundQueryTests.signed(self)
        calls = []
        def post(*args):
            calls.append(True)
            if len(calls) == 1:
                started.set()
                if not release.wait(10): raise RuntimeError('test synchronization timeout')
            return raw
        with patch.object(self.f.client, '_post', side_effect=post):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(self.client.query, self.f.order_id, 'refund-1', 30)
                try:
                    self.assertTrue(started.wait(5))
                    self.f.now[0] += 10
                    self.client.query(self.f.order_id, 'refund-1', 30)
                    self.assertFalse(self.apply()['replayed'])
                finally:
                    release.set()
                self.assertEqual('confirmed', pending.result(timeout=5)['status'])
        self.assertTrue(self.apply()['replayed'])
        self.assertEqual(1, self.refunds.status(self.f.order_id)['recorded_refund_count'])

    def test_refund_does_not_erase_historical_delivery_receipt_audit(self):
        from ai_supermarket.alipay_fulfillment import AlipayFulfillmentClient
        self.f.host.resume('buy')
        receipts = AlipayFulfillmentClient(self.f.fulfillment, self.f.client)
        before = receipts.prepare(self.f.order_id)
        self.observe(); self.apply(action='block_future_access')
        self.assertEqual(before, receipts.history(self.f.order_id))
        self.assertEqual('pending', receipts.history(self.f.order_id)['status'])

    def test_failure_between_ledger_and_anchor_writes_rolls_back(self):
        self.observe()
        def fail_anchor(db, key, value):
            if key.startswith('payment_order:'): raise RuntimeError('synthetic interrupted commit')
            _put(db, key, value)
        with patch('ai_supermarket.payment_refunds._put', side_effect=fail_anchor):
            with self.assertRaises(RuntimeError): self.apply(action='block_future_access')
        self.assertEqual(0, self.refunds.status(self.f.order_id)['recorded_refund_count'])
        self.assertFalse(self.apply(action='block_future_access')['replayed'])

    def test_corrupt_missing_sources_or_anchor_cannot_silently_restore_access(self):
        acquired = self.f.fulfillment.grant(self.f.order_id)['acquisition_id']
        self.observe(); self.apply(action='block_future_access')
        order_key = 'payment_order:' + self.f.order_id
        ledger_key = 'payment_refunds:' + self.f.order_id
        source_key = self.client._key(self.f.order_id, 'refund-1')
        with self.f.buyer.store.connect() as db:
            originals = {key: _get(db, key) for key in (order_key, ledger_key, source_key)}
        for key in (ledger_key, source_key, order_key):
            with self.subTest(key=key):
                with self.f.buyer.store.connect(write=True) as db:
                    for original_key, value in originals.items(): _put(db, original_key, value)
                    if key == order_key:
                        order = deepcopy(originals[key]); order.pop('refund_ledger_sha256'); _put(db, key, order)
                    else: _put(db, key, None)
                self.error('refund_record_corrupt', self.f.buyer.read, acquired, 'task', 'read')
                self.error('refund_record_corrupt', self.refunds.status, self.f.order_id)

    def test_other_buyer_cannot_inspect_or_apply_private_refund(self):
        from ai_supermarket.payment_orders import PaymentOrders
        from ai_supermarket.payment_fulfillment import PaymentFulfillment
        from ai_supermarket.alipay_transport import AlipayVerificationClient
        self.observe(); self.apply()
        orders = PaymentOrders(self.f.actor('another-buyer', 'buyer'), self.f.merchant)
        paid = PaymentFulfillment(orders, self.f.policy, enabled=True)
        transport = AlipayVerificationClient(orders, self.f.verifier, self.f.signer)
        refunds = PaymentRefunds(AlipayRefundQueryClient(paid, transport, capability_reference='synthetic-only'), enabled=True)
        self.error('order_not_found', refunds.status, self.f.order_id)
        self.error('order_not_found', refunds.reconcile, self.f.order_id, 'refund-1', 30,
                   rights_action='preserve_access', decision_reference='fixture-decision')

    def test_api_calls_and_cached_result_reads_check_refund_before_dispatch(self):
        from ai_supermarket.api_delivery import ApiDeliveryHost
        self.setup_order(api=True)
        acquired = self.f.fulfillment.grant(self.f.order_id)['acquisition_id']
        calls = []
        def execute(operation_id, operation, inputs):
            calls.append(operation_id); return {'count': 7}
        host = ApiDeliveryHost(self.f.buyer, 'provider-test', 'task', 'personal_use',
            self.f.database.parent / 'api-quota.sqlite', self.f.database.parent / 'api-outputs',
            'fixed-fixture', lambda *args: True, execute, lambda *args: {'state': 'unknown'})
        first = host.use(acquired, {'warehouse': 'fictional'}, 'one')
        self.assertEqual('api_result_delivered', first['status'])
        self.observe(); self.apply(action='block_future_access')
        for request, options in (('one', {'existing_only': True}), ('two', {}), ('one', {'recover': True})):
            self.error('refunded_access_blocked', host.use, acquired, {'warehouse': 'fictional'}, request, **options)
        self.assertEqual(1, len(calls))
        self.assertTrue(Path(first['path']).is_file())
