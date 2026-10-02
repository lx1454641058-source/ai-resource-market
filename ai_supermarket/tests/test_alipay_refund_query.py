"""本项目原创。真实本机HTTP与合成签名退款查询，无渠道资金操作。"""
import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from ai_supermarket.alipay_refund_query import AlipayRefundQueryClient, METHOD, RESPONSE_KEY
from ai_supermarket.common import MarketError
from ai_supermarket.payment_orders import _get, _put
from ai_supermarket.tests import test_alipay_fulfillment as fixture


class RefundQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AlipayFulfillmentTests.setUpClass()

    def setUp(self):
        self.f = fixture.AlipayFulfillmentTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.client = self.make()
        self.before_order = deepcopy(self.f.orders.get(self.f.order_id))
        self.before_budget = deepcopy(self.f.budget.summary())

    def make(self, **kwargs):
        options = {'capability_reference': 'synthetic-contract-not-qualification', 'network_enabled': True}
        options.update(kwargs)
        return AlipayRefundQueryClient(self.f.fulfillment, self.f.client, **options)

    def signed(self, changes=None, missing=()):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        body = {'code': '10000', 'trade_no': 'fixture-trade', 'out_trade_no': self.f.order_id,
                'out_request_no': 'refund-1', 'total_amount': '1.00', 'refund_amount': '0.30',
                'refund_status': 'REFUND_SUCCESS', 'sub_msg': 'private-upstream-detail'}
        body.update(changes or {})
        for name in missing:
            body.pop(name, None)
        raw = json.dumps(body, ensure_ascii=True, indent=2).encode()
        signature = base64.b64encode(self.f.key.sign(raw, padding.PKCS1v15(), hashes.SHA256())).decode()
        return ('{"' + RESPONSE_KEY + '":').encode() + raw + b',"sign":' + json.dumps(signature).encode() + b'}'

    def query(self, client=None, amount=30):
        return (client or self.client).query(self.f.order_id, 'refund-1', amount)

    def assert_code(self, code, call, *args):
        with self.assertRaises(MarketError) as raised:
            call(*args)
        self.assertEqual(code, raised.exception.code)

    def assert_original_unchanged(self):
        self.assertEqual(self.before_order, self.f.orders.get(self.f.order_id))
        self.assertEqual(self.before_budget, self.f.budget.summary())
        self.assertEqual(1, self.f.count('acquisitions'))

    def test_real_loopback_checks_signature_and_never_claims_channel_confirmation(self):
        self.f.receipt_bytes = self.signed
        client = AlipayRefundQueryClient(self.f.fulfillment, self.f.transport,
                                        capability_reference='local-fixture-only', network_enabled=True)
        result = self.query(client)
        self.assertEqual('test_response', result['status'])
        self.assertFalse(result['refund_confirmed'])
        self.assertEqual([], self.f.server_errors)
        self.assertEqual([{'method': METHOD, 'request': {'trade_no': 'fixture-trade',
                         'out_trade_no': self.f.order_id, 'out_request_no': 'refund-1'}}], self.f.calls)
        self.assert_original_unchanged()

    def test_synthetic_https_confirmation_reopens_and_does_not_repeat_query_or_settle(self):
        with patch.object(self.f.client, '_post', return_value=self.signed()) as post:
            first = self.query()
            reopened = self.make()
            self.assertEqual(first['response_sha256'], self.query(reopened)['response_sha256'])
            self.assertEqual(1, post.call_count)
        self.assertTrue(first['refund_confirmed'])
        self.assertFalse(first['refund_executed_by_this_client'])
        self.assertFalse(first['entitlements_changed']); self.assertFalse(first['commission_settled'])
        self.assertIsNone(first['cumulative_refund_cents'])
        self.assertEqual('host_reconcile_rights_and_settlement', first['next_action'])
        self.assert_original_unchanged()
        with self.f.buyer.store.connect() as db:
            raw = ' '.join(row[0] for row in db.execute("SELECT value FROM meta WHERE key LIKE 'refund_query:%'"))
        self.assertNotIn('private-upstream-detail', raw)
        self.assertNotIn('fictional-proof-only', raw)

    def test_10000_without_success_status_and_signed_errors_remain_unconfirmed(self):
        samples = [self.signed(missing=['refund_status']), self.signed({'refund_status': 'PROCESSING'}),
                   self.signed({'code': '40004', 'sub_code': 'ACQ.TRADE_NOT_EXIST'})]
        for raw in samples:
            with patch.object(self.f.client, '_post', return_value=raw):
                result = self.query()
            self.assertEqual('unconfirmed', result['status']); self.assertFalse(result['refund_confirmed'])
            self.f.now[0] += 10

    def test_success_requires_original_identifiers_amounts_and_currency(self):
        for changes, missing in [({'trade_no': 'other'}, ()), ({'out_trade_no': 'other'}, ()),
                                 ({'out_request_no': 'other'}, ()), ({'total_amount': '2.00'}, ()),
                                 ({'refund_amount': '0.31'}, ()), ({'refund_amount': 0.30}, ()),
                                 ({'refund_amount': '3e-1'}, ()), ({'currency': 'USD'}, ()),
                                 ({}, ('out_request_no',)), ({}, ('total_amount',))]:
            with self.subTest(changes=changes, missing=missing):
                with self.f.buyer.store.connect() as db:
                    _, value = self.client._load(db, self.f.order_id, 'refund-1', 30)
                self.assert_code('refund_binding_mismatch' if not any(k in missing for k in ('total_amount',))
                                 and not (isinstance(changes.get('refund_amount'), (float,)) or changes.get('refund_amount') == '3e-1')
                                 else 'refund_amount_invalid', self.client._response,
                                 self.signed(changes, missing), value['binding'])

    def test_tampered_response_is_retained_only_as_invalid_digest(self):
        with patch.object(self.f.client, '_post', return_value=self.signed().replace(b'0.30', b'0.40')) as post:
            result = self.query()
        self.assertEqual('invalid_response', result['status']); self.assertEqual(1, post.call_count)
        self.assert_original_unchanged()

    def test_unknown_explicit_requeries_preserve_original_request_and_bound_attempts(self):
        with patch.object(self.f.client, '_post', side_effect=TimeoutError) as post:
            self.assertEqual('unknown', self.query()['status'])
            self.assert_code('query_too_soon', self.query)
            for _ in range(2):
                self.f.now[0] += 10
                self.assertEqual('unknown', self.query(self.make())['status'])
            self.f.now[0] += 10
            self.assert_code('attempt_limit', self.query)
            self.assertEqual(3, post.call_count)
            bodies = [args.args[1] for args in post.call_args_list]
            self.assertEqual([bodies[0]] * 3, bodies)
        self.assert_original_unchanged()

    def test_disabled_unpaid_and_excess_amount_never_dispatch(self):
        with patch.object(self.f.client, '_post', side_effect=AssertionError('must_not_send')):
            self.assert_code('channel_disabled', self.query, self.make(network_enabled=False))
            self.assert_code('invalid_params', self.query, None, 101)
            self.assertEqual('not_queried', self.client.history(self.f.order_id, 'refund-1', 30)['status'])
            with self.f.buyer.store.connect() as db:
                unpaid = db.execute("SELECT json_extract(value, '$.order_id') FROM meta WHERE key LIKE 'payment_order:%' AND json_extract(value, '$.status')='cancelled_before_verification'").fetchone()[0]
            self.assert_code('payment_not_confirmed', self.client.query, unpaid, 'refund-1', 30)
        self.assert_original_unchanged()

    def test_another_buyer_cannot_query_original_private_order(self):
        from ai_supermarket.payment_orders import PaymentOrders
        from ai_supermarket.payment_fulfillment import PaymentFulfillment
        from ai_supermarket.alipay_transport import AlipayVerificationClient
        orders = PaymentOrders(self.f.actor('another-buyer', 'buyer'), self.f.merchant)
        paid = PaymentFulfillment(orders, self.f.policy, enabled=True)
        transport = AlipayVerificationClient(orders, self.f.verifier, self.f.signer)
        other = AlipayRefundQueryClient(paid, transport, capability_reference='same-operator', network_enabled=True)
        with patch.object(transport, '_post', side_effect=AssertionError('must_not_send')):
            self.assert_code('order_not_found', self.query, other)

    def test_original_amount_contract_and_endpoint_cannot_be_rebound(self):
        with patch.object(self.f.client, '_post', side_effect=TimeoutError):
            self.query()
        with patch.object(self.f.client, '_post', side_effect=AssertionError('must_not_send')):
            self.assert_code('refund_binding_mismatch', self.query, None, 31)
            self.assert_code('refund_binding_mismatch', self.query,
                             self.make(capability_reference='other-reference'))
            self.f.client._endpoint = 'https://example.invalid/'
            self.assert_code('payment_policy_mismatch', self.query)

    def test_interrupted_query_reopens_as_reserved_then_only_requeries_status(self):
        with patch.object(self.f.client, '_post', side_effect=RuntimeError('synthetic-process-exit')):
            with self.assertRaises(RuntimeError):
                self.query()
        self.assertEqual('reserved', self.make().history(self.f.order_id, 'refund-1', 30)['status'])
        self.f.now[0] += 10
        with patch.object(self.f.client, '_post', return_value=self.signed()):
            result = self.query(self.make())
        self.assertEqual(('confirmed', 2), (result['status'], result['attempt_count']))
        self.assert_original_unchanged()

    def test_concurrent_query_reservation_prevents_immediate_duplicate_dispatch(self):
        with patch.object(self.f.client, '_post', side_effect=TimeoutError) as post:
            def run(_):
                try: return self.query()['status']
                except MarketError as exc: return exc.code
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(run, range(2)))
            self.assertEqual(['query_too_soon', 'unknown'], sorted(results))
            self.assertEqual(1, post.call_count)

    def test_corrupt_record_cannot_promote_unconfirmed_or_reset_history(self):
        with patch.object(self.f.client, '_post', return_value=self.signed(missing=['refund_status'])):
            self.query()
        key = self.client._key(self.f.order_id, 'refund-1')
        with self.f.buyer.store.connect(write=True) as db:
            value = _get(db, key); value['attempts'][0]['state'] = 'confirmed'; _put(db, key, value)
        self.assert_code('refund_record_corrupt', self.query)
