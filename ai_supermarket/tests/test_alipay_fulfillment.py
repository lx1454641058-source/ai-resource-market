"""来源：本项目原创。本机HTTP及合成HTTPS响应，不访问真实支付渠道。"""
import base64
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from ai_supermarket.alipay_fulfillment import AlipayFulfillmentClient, METHOD, RESPONSE_KEY
from ai_supermarket.alipay_transport import AlipayVerificationClient
from ai_supermarket.payment_orders import _get, _put
from ai_supermarket.common import MarketError
from ai_supermarket.tests import test_purchase_host as fixture


class AlipayFulfillmentTests(unittest.TestCase):
    actor=fixture.PurchaseHostTests.actor
    draft=fixture.PurchaseHostTests.draft
    publish=fixture.PurchaseHostTests.publish
    count=fixture.PurchaseHostTests.count
    assert_code=fixture.PurchaseHostTests.assert_code
    quote=fixture.PurchaseHostTests.quote
    prepare=fixture.PurchaseHostTests.prepare
    start=fixture.PurchaseHostTests.start
    signed=fixture.PurchaseHostTests.signed
    configure=fixture.PurchaseHostTests.configure
    update_meta=fixture.PurchaseHostTests.update_meta
    seed_evidence=fixture.PurchaseHostTests.seed_evidence
    confirm=fixture.PurchaseHostTests.confirm
    authorize=fixture.PurchaseHostTests.authorize
    make_biller=fixture.PurchaseHostTests.make_biller
    make_host=fixture.PurchaseHostTests.make_host
    use=fixture.PurchaseHostTests.use

    @classmethod
    def setUpClass(cls):fixture.PurchaseHostTests.setUpClass.__func__(cls)

    def setUp(self):
        fixture.PurchaseHostTests.setUp(self)
        self.use();self.seed_evidence();self.delivered=self.host.resume('buy')
        self.calls=[];self.server_errors=[];self.mode='valid'
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                try:
                    values={k:v[0] for k,v in parse_qs(urlsplit(self.path).query,strict_parsing=True).items()}
                    form=self.rfile.read(int(self.headers['Content-Length'])).decode('ascii')
                    values.update({k:v[0] for k,v in parse_qs(form,strict_parsing=True).items()})
                    signature=base64.b64decode(values.pop('sign'),validate=True)
                    from cryptography.hazmat.primitives import hashes
                    from cryptography.hazmat.primitives.asymmetric import padding
                    source='&'.join(k+'='+values[k] for k in sorted(values)).encode('utf-8')
                    owner.bill_key.public_key().verify(signature,source,padding.PKCS1v15(),hashes.SHA256())
                    owner.calls.append({'method':values['method'],'request':json.loads(values['biz_content'])})
                    if owner.mode=='drop':self.connection.close();return
                    if owner.mode=='redirect':
                        self.send_response(302);self.send_header('Location','https://example.invalid/');self.end_headers();return
                    raw=owner.receipt_bytes()
                    if owner.mode=='system_error':
                        raw=owner.receipt_bytes({'code':'40004','sub_code':'SYSTEM_ERROR'})
                    if owner.mode=='tampered':raw=raw.replace(b'fixture-trade',b'changed-trade')
                    self.send_response(200);self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
                except ConnectionError:pass
                except Exception as exc:
                    owner.server_errors.append(type(exc).__name__)
                    self.send_response(500);self.end_headers()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.daemon_threads=True
        thread=threading.Thread(target=lambda:self.server.serve_forever(poll_interval=0.01),daemon=True);thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.transport=AlipayVerificationClient(self.orders,self.verifier,self.signer,
            loopback_endpoint=f'http://127.0.0.1:{self.server.server_port}/gateway.do',clock=lambda:self.now[0],timeout=1)
        self.receipts=AlipayFulfillmentClient(self.fulfillment,self.transport,network_enabled=True)

    def receipt_bytes(self,body=None):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        raw=json.dumps(body or {'code':'10000','msg':'Success','trade_no':'fixture-trade'},indent=2).encode()
        signature=base64.b64encode(self.key.sign(raw,padding.PKCS1v15(),hashes.SHA256())).decode()
        return ('{"'+RESPONSE_KEY+'":').encode()+raw+b',"sign":'+json.dumps(signature).encode()+b',"sign_type":"RSA2"}'

    def test_actual_loopback_request_signature_and_response_are_not_real_confirmation(self):
        result=self.receipts.send(self.order_id)
        self.assertEqual([],self.server_errors)
        self.assertEqual([{'method':METHOD,'request':{'trade_no':'fixture-trade'}}],self.calls)
        self.assertEqual('test_response',result['status'])
        self.assertFalse(result['channel_fulfillment_confirmed'])
        self.assertFalse(result['payment_sent']);self.assertFalse(result['entitlement_created'])
        self.assertEqual(1,self.count('acquisitions'));self.assertEqual(1,self.count('usage_events'))

    def test_queue_through_task_host_is_offline_and_idempotent(self):
        first=self.host.prepare_channel_receipt('buy',self.receipts)
        again=self.host.prepare_channel_receipt('buy',self.receipts)
        self.assertEqual(first,again);self.assertEqual('pending',first['status']);self.assertEqual([],self.calls)

    def test_disabled_send_does_not_create_or_send(self):
        disabled=AlipayFulfillmentClient(self.fulfillment,self.transport)
        self.assert_code('channel_disabled',disabled.send,self.order_id)
        self.assert_code('fulfillment_not_found',disabled.history,self.order_id)
        self.assertEqual([],self.calls)

    def test_valid_fixed_https_observation_uses_synthetic_transport_only(self):
        client=AlipayFulfillmentClient(self.fulfillment,self.client,network_enabled=True)
        with patch.object(self.client,'_post',return_value=self.receipt_bytes()) as send:
            result=client.send(self.order_id);again=client.send(self.order_id)
        self.assertEqual(1,send.call_count)
        self.assertTrue(result['channel_fulfillment_confirmed'])
        self.assertFalse(again['request_sent_this_call'])
        self.assertEqual(result['response_sha256'],again['response_sha256'])
        self.assertEqual('paid_delivered_to_host',self.orders.get(self.order_id)['status'])

    def test_receipt_requires_actual_delivery_event_not_a_paid_assertion(self):
        with self.buyer.store.connect(write=True) as db:
            receipt=_get(db,'payment_delivery:'+self.order_id);receipt['usage_id']='u_invented'
            _put(db,'payment_delivery:'+self.order_id,receipt)
        self.assert_code('delivery_binding_mismatch',self.receipts.prepare,self.order_id)
        self.assertEqual([],self.calls)

    def test_unpaid_order_cannot_prepare(self):
        resource=self.publish(price=100,slug='other')
        detail=self.buyer.resolve(resource,'1.0.0','personal_use')
        quote=self.buyer.quote(resource,'1.0.0','personal_use','q-other')
        order=self.orders.prepare(quote['quote_id'],detail['release_digest'],'p-other')
        self.assert_code('delivery_required',self.receipts.prepare,order['order_id'])

    def test_concurrent_send_transmits_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies=list(pool.map(lambda _:self.receipts.send(self.order_id),range(2)))
        self.assertEqual(1,len(self.calls))
        self.assertEqual(1,sum(r['request_sent_this_call'] is True for r in replies))

    def test_unknown_response_retains_send_intent_and_does_not_retry(self):
        self.mode='drop';first=self.receipts.send(self.order_id);self.mode='valid'
        again=AlipayFulfillmentClient(self.fulfillment,self.transport,network_enabled=True).send(self.order_id)
        self.assertEqual('unknown',first['status']);self.assertIsNone(first['request_sent_this_call'])
        self.assertEqual('unknown',again['status']);self.assertEqual(1,len(self.calls))
        self.assertTrue(self.orders.get(self.order_id)['paid_verified'])
        self.assert_code('reconciliation_required',self.receipts.retry,self.order_id)

    def test_redirect_refused_and_not_followed(self):
        self.mode='redirect';result=self.receipts.send(self.order_id)
        self.assertEqual('unknown',result['status']);self.assertEqual(1,len(self.calls))

    def test_invalid_signature_does_not_confirm_and_cannot_be_overwritten(self):
        self.mode='tampered';result=self.receipts.send(self.order_id);self.mode='valid'
        self.assertEqual('rejected',result['status'])
        self.assertFalse(self.receipts.send(self.order_id)['channel_fulfillment_confirmed'])
        self.assertEqual(1,len(self.calls))

    def test_wrong_trade_failure_code_and_duplicate_fields_are_rejected(self):
        self.receipts.prepare(self.order_id)
        with self.buyer.store.connect() as db:value=_get(db,self.receipts._key(self.order_id))
        binding=value['binding']
        for body,code in [({'code':'10000','trade_no':'other'},'trade_conflict'),
                          ({'code':'40004','trade_no':'fixture-trade'},'fulfillment_rejected'),
                          ({'code':'10000','trade_no':'fixture-trade','seller_id':'other'},'fulfillment_binding_mismatch')]:
            self.assert_code(code,self.receipts._response,self.receipt_bytes(body),binding)
        raw=self.receipt_bytes().replace(b'"code": "10000"',b'"code": "10000", "code":"10000"')
        with self.assertRaises(MarketError):self.receipts._response(raw,binding)

    def test_failure_before_send_commit_leaves_queue_recoverable(self):
        self.receipts.prepare(self.order_id)
        from ai_supermarket import alipay_fulfillment as module
        original=module._put
        def fail(db,key,value):
            if value.get('state')=='reserved':raise OSError('fixture interruption')
            return original(db,key,value)
        with patch.object(module,'_put',side_effect=fail):
            with self.assertRaises(OSError):self.receipts.send(self.order_id)
        self.assertEqual('pending',self.receipts.history(self.order_id)['status']);self.assertEqual([],self.calls)
        self.assertEqual('test_response',self.receipts.send(self.order_id)['status'])

    def test_failure_after_send_preserves_reserved_without_automatic_resend(self):
        with patch.object(self.receipts,'_finish',side_effect=OSError('fixture interruption')):
            with self.assertRaises(OSError):self.receipts.send(self.order_id)
        self.assertEqual('reserved',self.receipts.history(self.order_id)['status'])
        self.assertFalse(self.receipts.send(self.order_id)['request_sent_this_call'])
        self.assertEqual(1,len(self.calls))

    def test_changed_delivery_or_endpoint_cannot_reuse_queue(self):
        self.receipts.prepare(self.order_id)
        with self.buyer.store.connect(write=True) as db:
            receipt=_get(db,'payment_delivery:'+self.order_id);receipt['task_completed']=True
            _put(db,'payment_delivery:'+self.order_id,receipt)
        self.assert_code('fulfillment_binding_mismatch',self.receipts.send,self.order_id)
        self.assertEqual([],self.calls)

    def test_changed_transport_configuration_is_rejected_before_io(self):
        self.transport._endpoint='https://example.invalid/'
        self.assert_code('payment_policy_mismatch',self.receipts.send,self.order_id)
        self.assertEqual([],self.calls)

    def test_valid_signed_payment_envelope_is_not_a_fulfillment_response(self):
        self.receipts.prepare(self.order_id)
        with self.buyer.store.connect() as db:value=_get(db,self.receipts._key(self.order_id))
        from ai_supermarket.common import MarketError
        with self.assertRaises(MarketError):self.receipts._response(self.signed(),value['binding'])

    def test_signed_system_error_allows_explicit_same_trade_retry(self):
        self.mode='system_error';first=self.receipts.send(self.order_id)
        self.assertEqual('retryable',first['status']);self.assertEqual(1,first['attempt_count'])
        self.receipts.send(self.order_id);self.assertEqual(1,len(self.calls))
        self.mode='valid';result=self.receipts.retry(self.order_id)
        self.assertEqual('test_response',result['status']);self.assertEqual(2,result['attempt_count'])
        self.assertEqual(self.calls[0]['request'],self.calls[1]['request'])
        with self.buyer.store.connect() as db:value=_get(db,self.receipts._key(self.order_id))
        self.assertEqual(['retryable','test_response'],[a['state'] for a in value['attempts']])

    def test_retry_limit_is_bounded_and_keeps_original_errors(self):
        self.mode='system_error';self.receipts.send(self.order_id)
        self.receipts.retry(self.order_id);self.receipts.retry(self.order_id)
        self.assert_code('attempt_limit',self.receipts.retry,self.order_id)
        self.assertEqual(3,len(self.calls))
        self.assertEqual('reconcile_original_receipt',self.receipts.history(self.order_id)['next_action'])

    def test_unsigned_system_error_cannot_authorize_retry(self):
        raw=self.receipt_bytes({'code':'40004','sub_code':'OTHER_ERROR'}).replace(b'OTHER_ERROR',b'SYSTEM_ERROR')
        with patch.object(self.transport,'_post',return_value=raw):
            self.assertEqual('rejected',self.receipts.send(self.order_id)['status'])
        self.assert_code('reconciliation_required',self.receipts.retry,self.order_id)


if __name__=='__main__':unittest.main()
