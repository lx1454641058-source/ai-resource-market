"""本项目原创。文件/回执历史隔离测试；原站仅本机、渠道仅合成响应。"""
from contextlib import closing
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_supermarket import recovery
from ai_supermarket.alipay_fulfillment import AlipayFulfillmentClient
from ai_supermarket.alipay_transport import AlipayVerificationClient
from ai_supermarket.alipay_refund_query import AlipayRefundQueryClient
from ai_supermarket.client_receipts import ClientReceipts
from ai_supermarket.common import MarketError
from ai_supermarket.exchange import Exchange
from ai_supermarket.identity import Identities
from ai_supermarket.payment_orders import _hash
from ai_supermarket.payment_refunds import PaymentRefunds
from ai_supermarket.tests import test_client_receipts as fixture
from ai_supermarket.tests import test_alipay_fulfillment as channel_fixture
from ai_supermarket.tests import test_alipay_refund_query as refund_fixture


class DeliveryRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): fixture.ClientReceiptTests.setUpClass()

    def setUp(self):
        self.c = fixture.ClientReceiptTests(); self.c.setUp(); self.addCleanup(self.c.doCleanups)
        self.f = self.c.f

    def start(self, paid=False, acknowledge=False):
        self.c.download(paid=paid)
        self.root = self.f.identity_path.parent
        self.paths = {'market': self.f.database, 'identities': self.f.identity_path, 'budget': self.f.ledger.store.path}
        if acknowledge: self.assertEqual(200, self.c.call()[0])
        if paid: self.channel = AlipayFulfillmentClient(self.f.fulfillment, self.f.client, network_enabled=True)

    def meta(self, path=None):
        with closing(recovery.connect_existing(path or self.paths['market'])) as db:
            return dict(db.execute('SELECT key,value FROM meta'))

    def valid(self):
        with closing(recovery.connect_existing(self.paths['market'])) as db: recovery.validate_database(db, 'market')
        self.assertEqual({'status': 'ready'}, recovery.readiness(*self.paths.values()))

    def invalid(self):
        self.assertEqual({'status': 'not_ready'}, recovery.readiness(*self.paths.values()))
        with self.assertRaises(MarketError): recovery.snapshot_bundle(*self.paths.values(), self.root / 'rejected-snapshot')

    def send(self, body=None):
        raw = channel_fixture.AlipayFulfillmentTests.receipt_bytes(self.f, body)
        with patch.object(self.channel.transport, '_post', return_value=raw) as post:
            result = self.channel.send(self.f.order_id)
            self.assertEqual(1, post.call_count)
        return result

    def test_free_prepared_and_acknowledged_files_survive_revocation(self):
        self.start(); self.valid(); self.assertFalse(self.c.call('receipt_status')[1]['server_acknowledged'])
        self.c.call(); self.valid()
        self.f.identities.revoke_key(self.f.issued['key_id']); self.f.ledger.set_state('root', 'paused', 1)
        self.valid()

    def test_paid_file_before_ack_then_pending_channel_keep_distinct_states(self):
        self.start(paid=True); self.valid()
        with self.assertRaises(MarketError): self.channel.prepare(self.f.order_id)
        self.c.call(); self.assertEqual('pending', self.channel.prepare(self.f.order_id)['status']); self.valid()
        self.assertFalse(self.channel.history(self.f.order_id)['channel_fulfillment_confirmed'])

    def test_confirmed_channel_and_refund_restore_keep_original_rights_decision(self):
        self.start(paid=True, acknowledge=True); self.assertEqual('confirmed', self.send()['status'])
        query = AlipayRefundQueryClient(self.f.fulfillment, self.f.client, capability_reference='synthetic-only', network_enabled=True)
        raw = refund_fixture.RefundQueryTests.signed(SimpleNamespace(f=self.f))
        with patch.object(self.f.client, '_post', return_value=raw): query.query(self.f.order_id, 'refund-1', 30)
        PaymentRefunds(query, enabled=True).reconcile(self.f.order_id, 'refund-1', 30,
            rights_action='block_future_access', decision_reference='synthetic-refund-review')
        self.assertEqual('confirmed', self.channel.history(self.f.order_id)['status'])
        with self.assertRaises(MarketError): self.c.receipts.acknowledge('buy', self.c.digest)
        self.valid(); original = self.meta()
        recovery.snapshot_bundle(*self.paths.values(), self.root / 'snapshot')
        recovery.restore_bundle(self.root / 'snapshot', self.root / 'restored')
        paths = recovery.paths_for(self.root / 'restored')
        self.assertTrue(all(self.meta(paths['market'])[k] == v for k, v in original.items()))
        with self.assertRaises(MarketError): Identities(paths['identities']).authenticate(self.f.token)
        recovery.release_bundle(self.root / 'restored', {k: 'synthetic-review-' + k for k in recovery.REVIEW_FIELDS})
        self.assertEqual({'status': 'ready'}, recovery.readiness(*paths.values()))
        order = json.loads(original['payment_order:' + self.f.order_id])
        buyer = Exchange(paths['market'], 'buyer', clock=lambda: self.f.now[0])
        with self.assertRaises(MarketError) as caught: buyer.status(order['entitlement_id'])
        self.assertEqual('refunded_access_blocked', caught.exception.code)

    def test_original_resource_license_and_manifest_hashes_are_checked(self):
        self.start(); original = self.meta(); key = next(k for k in original if k.startswith('client_delivery:'))
        for name in ('resource_sha256', 'license_sha256', 'manifest_sha256'):
            record = json.loads(original[key]); record['offer'][name] = 'a' * 64
            record['offer_sha256'] = _hash(record['offer']); self.f.update_meta(key, record); self.invalid()
        self.f.update_meta(key, json.loads(original[key])); self.valid()

    def test_paid_receipt_pointer_cannot_be_removed_or_substituted(self):
        self.start(paid=True, acknowledge=True); key = 'order_client_receipt:' + self.f.order_id
        original = json.loads(self.meta()[key])
        with self.f.buyer.store.connect(write=True) as db: db.execute('DELETE FROM meta WHERE key=?', (key,))
        self.invalid(); self.f.update_meta(key, original)
        self.f.update_meta('remote_file_delivery:' + self.f.order_id, {}); self.invalid()

    def test_unknown_channel_send_is_retained_and_never_retried_by_recovery(self):
        self.start(paid=True, acknowledge=True)
        with patch.object(self.f.client, '_post', side_effect=TimeoutError) as post:
            self.assertEqual('unknown', self.channel.send(self.f.order_id)['status'])
            self.valid(); recovery.snapshot_bundle(*self.paths.values(), self.root / 'snapshot')
            self.assertFalse(self.channel.send(self.f.order_id)['request_sent_this_call'])
            self.assertEqual(1, post.call_count)

    def test_retryable_history_and_confirmed_latest_attempt_are_both_validated(self):
        self.start(paid=True, acknowledge=True)
        self.assertEqual('retryable', self.send({'code': '40004', 'sub_code': 'SYSTEM_ERROR'})['status']); self.valid()
        raw = channel_fixture.AlipayFulfillmentTests.receipt_bytes(self.f)
        with patch.object(self.f.client, '_post', return_value=raw): self.channel.retry(self.f.order_id)
        self.valid()
        key = 'channel_fulfillment:' + self.f.order_id; record = json.loads(self.meta()[key])
        record['attempts'][0]['response']['public_key_sha256'] = 'a' * 64
        self.f.update_meta(key, record); self.invalid()

    def test_loopback_response_cannot_be_relabelled_real_confirmation(self):
        self.start(paid=True, acknowledge=True)
        transport = AlipayVerificationClient(self.f.orders, self.f.verifier, self.f.signer,
            loopback_endpoint='http://127.0.0.1:19999/gateway.do', clock=lambda: self.f.now[0])
        self.channel = AlipayFulfillmentClient(self.f.fulfillment, transport, network_enabled=True)
        self.assertEqual('test_response', self.send()['status']); self.valid()
        key = 'channel_fulfillment:' + self.f.order_id; record = json.loads(self.meta()[key])
        record['state'] = record['attempts'][-1]['state'] = 'confirmed'
        self.f.update_meta(key, record); self.invalid()

    def test_rejected_signature_response_is_historical_failure(self):
        self.start(paid=True, acknowledge=True)
        with patch.object(self.f.client, '_post', return_value=b'not-signed'):
            self.assertEqual('rejected', self.channel.send(self.f.order_id)['status'])
        self.valid()

    def test_other_identity_cannot_replace_original_receipt_key_even_with_new_hash(self):
        self.start(); key = next(k for k in self.meta() if k.startswith('client_delivery:'))
        record = json.loads(self.meta()[key]); record['scope']['key_id'] = 'ak_' + 'a' * 32
        new_key = 'client_delivery:' + _hash([record['scope'], record['request_id']])
        with self.f.buyer.store.connect(write=True) as db: db.execute('DELETE FROM meta WHERE key=?', (key,))
        self.f.update_meta(new_key, record)
        with closing(recovery.connect_existing(self.paths['market'])) as db: recovery.validate_database(db, 'market')
        self.invalid()

    def test_receipt_timestamp_and_original_usage_task_are_checked(self):
        self.start(acknowledge=True); key = next(k for k in self.meta() if k.startswith('client_delivery:'))
        original = json.loads(self.meta()[key]); record = deepcopy(original)
        record['acknowledgement']['acknowledged_at'] = True; self.f.update_meta(key, record); self.invalid()
        self.f.update_meta(key, original)
        with self.f.buyer.store.connect(write=True) as db: db.execute("UPDATE usage_events SET task_id='other'")
        self.invalid()

    def test_started_local_channel_before_later_http_download_remains_valid(self):
        self.c.case.origin(); self.f.bill(); self.f.seed_evidence(); self.f.host.resume('buy')
        self.channel = AlipayFulfillmentClient(self.f.fulfillment, self.f.client, network_enabled=True)
        with patch.object(self.f.client, '_post', side_effect=TimeoutError): self.channel.send(self.f.order_id)
        self.assertEqual(200, self.c.case.fetch()[0])
        self.root = self.f.identity_path.parent
        self.paths = {'market': self.f.database, 'identities': self.f.identity_path, 'budget': self.f.ledger.store.path}
        self.assertNotIn('remote_file_delivery:' + self.f.order_id, self.meta()); self.valid()

    def test_explicit_retry_interrupted_before_send_keeps_prior_signed_error(self):
        self.start(paid=True, acknowledge=True)
        self.send({'code': '40004', 'sub_code': 'SYSTEM_ERROR'})
        with patch.object(self.channel, '_request', side_effect=OSError('synthetic preparation interruption')):
            with self.assertRaises(OSError): self.channel.retry(self.f.order_id)
        self.assertEqual('pending', self.channel.history(self.f.order_id)['status']); self.valid()
        self.assertEqual(1, self.channel.history(self.f.order_id)['attempt_count'])
        self.assertEqual('confirmed', self.send()['status']); self.valid()


if __name__ == '__main__': unittest.main()
