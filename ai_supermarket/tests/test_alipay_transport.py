"""来源：本项目原创。本机HTTP与内存临时RSA密钥，不调用支付宝。"""
import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from ai_supermarket.alipay_transport import AlipayVerificationClient, RSA2RequestSigner, GATEWAYS
from ai_supermarket.alipay_proof import AlipayProofVerifier, METHOD, request_digest
from ai_supermarket.common import MarketError
from ai_supermarket.tests import test_alipay_proof as fixture


class AlipayTransportTests(unittest.TestCase):
    actor=fixture.AlipayProofTests.actor
    draft=fixture.AlipayProofTests.draft
    publish=fixture.AlipayProofTests.publish
    count=fixture.AlipayProofTests.count
    assert_code=fixture.AlipayProofTests.assert_code
    quote=fixture.AlipayProofTests.quote
    prepare=fixture.AlipayProofTests.prepare
    start=fixture.AlipayProofTests.start
    signed=fixture.AlipayProofTests.signed

    @classmethod
    def setUpClass(cls):
        fixture.AlipayProofTests.setUpClass.__func__(cls)
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        cls.request_key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        cls.request_pem=cls.request_key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,serialization.NoEncryption())

    def setUp(self):
        fixture.AlipayProofTests.setUp(self)
        self.signer=RSA2RequestSigner(self.request_pem)
        self.calls=[];self.server_errors=[];self.mode='valid';self.delay=0
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                try:
                    query=parse_qs(urlsplit(self.path).query,strict_parsing=True)
                    body=self.rfile.read(int(self.headers['Content-Length']))
                    form=parse_qs(body.decode('ascii'),strict_parsing=True)
                    params={**{k:v[0] for k,v in query.items()},**{k:v[0] for k,v in form.items()}}
                    signature=base64.b64decode(params.pop('sign'),validate=True)
                    from cryptography.hazmat.primitives import hashes
                    from cryptography.hazmat.primitives.asymmetric import padding
                    source='&'.join(k+'='+params[k] for k in sorted(params)).encode('utf-8')
                    owner.request_key.public_key().verify(signature,source,padding.PKCS1v15(),hashes.SHA256())
                    # Use independently parsed wire values, not the client builder.
                    owner.calls.append({'method':params['method'],'timestamp':params['timestamp'],
                                        'business':json.loads(params['biz_content']),'params':set(params),
                                        'content_type':self.headers['Content-Type']})
                    if owner.mode=='drop': self.connection.close();return
                    if owner.delay: time.sleep(owner.delay)
                    if owner.mode=='redirect':
                        self.send_response(302);self.send_header('Location',owner.endpoint);self.end_headers();return
                    if owner.mode=='error':self.send_response(500);self.end_headers();return
                    raw=owner.signed()
                    if owner.mode=='tampered':raw=raw.replace(b'1.00',b'2.00')
                    if owner.mode=='malformed':raw=b'not-json-fictional-proof-only'
                    if owner.mode=='oversized':raw=b'x'*65537
                    self.send_response(200)
                    self.send_header('Content-Type','application/json')
                    if owner.mode=='compressed':self.send_header('Content-Encoding','gzip')
                    self.send_header('Content-Length',str(len(raw)+(10 if owner.mode=='truncated' else 0)))
                    self.end_headers();self.wfile.write(raw)
                except ConnectionError:pass  # Client timeout/rejection can abort Windows sockets.
                except Exception as error:
                    owner.server_errors.append(type(error).__name__)
                    self.send_response(500);self.end_headers()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=True
        self.endpoint='http://127.0.0.1:'+str(self.server.server_port)+'/gateway.do'
        self.thread=threading.Thread(target=lambda:self.server.serve_forever(poll_interval=0.01),daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.client=self.make_client()

    def make_client(self,**kwargs):
        options={'network_enabled':True,'loopback_endpoint':self.endpoint,'timeout':1,'clock':lambda:1000}
        options.update(kwargs)
        return AlipayVerificationClient(self.orders,self.verifier,self.signer,**options)

    def run_query(self,attempt='v1',request=None):
        return self.client.verify(self.order_id,attempt,request or self.request)

    def test_real_http_signed_request_raw_response_and_persisted_evidence(self):
        self.request['client_session']='虚构+会话&="value"'
        result=self.run_query()
        self.assertEqual([],self.server_errors)
        self.assertEqual(1,len(self.calls))
        call=self.calls[0]
        self.assertEqual(METHOD,call['method'])
        self.assertEqual('1970-01-01 08:16:40',call['timestamp'])
        self.assertEqual(self.request,call['business'])
        self.assertEqual({'method','app_id','format','charset','sign_type','timestamp','version','biz_content'},call['params'])
        self.assertEqual('application/x-www-form-urlencoded',call['content_type'])
        self.assertEqual('signature_and_order_match',result['transport']['outcome'])
        self.assertFalse(result['transport']['https_response_received'])
        self.assertTrue(result['transport']['loopback_test'])
        self.assertTrue(result['request_sent_this_call'])
        self.assertFalse(result['paid_verified']);self.assertFalse(result['entitlement_created'])
        self.assertEqual(0,self.count('acquisitions'))
        self.assertEqual(request_digest(self.request,self.merchant),result['transport']['request_sha256'])
        self.assertEqual(result['transport']['response_sha256'],self.orders.response_evidence(self.order_id,'v1')['response_sha256'])
        with self.buyer.store.connect() as db:
            stored=''.join(row[0] for row in db.execute("SELECT value FROM meta WHERE key LIKE 'payment_%'"))
        self.assertNotIn('fictional-proof-only',stored);self.assertNotIn('虚构+会话',stored)
        self.assertNotIn('PRIVATE KEY',stored)

    def test_disabled_client_never_connects_or_reserves(self):
        client=self.make_client(network_enabled=False)
        self.assert_code('channel_disabled',client.verify,self.order_id,'v1',self.request)
        self.assertEqual([],self.orders.get(self.order_id)['attempts']);self.assertEqual([],self.calls)

    def test_only_fixed_gateways_or_explicit_loopback_allowed(self):
        for url in ('http://example.com/gateway.do','http://localhost:123/gateway.do',
                    'http://127.0.0.1:123/other','http://127.0.0.1:123/gateway.do?x=1',
                    'http://x@127.0.0.1:123/gateway.do','http://127.0.0.1:65536/gateway.do'):
            self.assert_code('invalid_endpoint',self.make_client,loopback_endpoint=url)
        configured=self.make_client(loopback_endpoint=None,network_enabled=False)
        self.assertEqual(GATEWAYS['sandbox'],configured._endpoint)

    def test_changed_mapping_fails_before_network(self):
        self.orders.merchant['app_id']='changed'
        self.assert_code('order_changed',self.run_query)
        self.assertEqual([],self.calls)

    def test_replay_across_client_restart_never_resends(self):
        original=self.run_query();self.client=self.make_client()
        self.now[0]=2000
        replay=self.run_query()
        self.assertEqual(original['transport'],replay['transport'])
        self.assertTrue(replay['replayed']);self.assertFalse(replay['request_sent_this_call'])
        self.assertEqual(1,len(self.calls))

    def test_concurrent_same_attempt_sends_at_most_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.run_query(),range(2)))
        self.assertEqual(1,len(self.calls))
        self.assertEqual(1,sum(r['replayed'] for r in results))

    def test_different_payload_cannot_reuse_attempt(self):
        self.run_query()
        self.assert_code('idempotency_conflict',self.run_query,request={**self.request,'payment_proof':'different'})
        self.assertEqual(1,len(self.calls))

    def test_crash_before_transport_reservation_does_not_resend(self):
        with patch.object(self.client,'_reserve',side_effect=OSError('fixture interruption')):
            with self.assertRaises(OSError):self.run_query()
        result=self.run_query()
        self.assertTrue(result['replayed']);self.assertIsNone(result['transport'])
        self.assertEqual([],self.calls)
        self.assert_code('reconciliation_required',self.run_query,'new')

    def test_crash_after_send_before_finish_retains_original_evidence(self):
        with patch.object(self.client,'_finish',side_effect=OSError('fixture interruption')):
            with self.assertRaises(OSError):self.run_query()
        result=self.run_query()
        self.assertEqual('reserved',result['transport']['outcome'])
        self.assertEqual('signature_checked_reconciliation_required',result['order_status'])
        self.assertTrue(result['replayed']);self.assertEqual(1,len(self.calls))

    def test_redirect_is_not_followed(self):
        self.mode='redirect';result=self.run_query()
        self.assertEqual('unknown',result['transport']['outcome'])
        self.assertIsNone(result['request_sent_this_call']);self.assertEqual(1,len(self.calls))
        self.assertFalse(self.run_query()['request_sent_this_call']);self.assertEqual(1,len(self.calls))

    def test_timeout_preserves_unknown_and_does_not_retry(self):
        self.delay=0.2;self.client=self.make_client(timeout=0.05)
        result=self.run_query()
        self.assertEqual('unknown',result['transport']['outcome'])
        self.assertEqual('unknown',self.orders.get(self.order_id)['attempts'][0]['outcome'])
        self.run_query();self.assertEqual(1,len(self.calls))
        self.assert_code('reconciliation_required',self.orders.cancel_unstarted,self.order_id)

    def test_invalid_signature_or_json_does_not_grant_or_leak(self):
        for i,mode in enumerate(('tampered','malformed')):
            self.mode=mode;result=self.run_query('v'+str(i))
            self.assertEqual('rejected',result['transport']['outcome'])
            self.assertNotIn('fictional-proof-only',json.dumps(result))
            self.assert_code('verification_not_found',self.orders.response_evidence,self.order_id,'v'+str(i))
        self.assertEqual(0,self.count('acquisitions'))

    def test_http_errors_compression_size_and_truncation_are_unknown(self):
        for i,mode in enumerate(('error','compressed','oversized','truncated')):
            self.mode=mode;result=self.run_query(str(i))
            self.assertEqual('unknown',result['transport']['outcome'])
            self.assertFalse(result['paid_verified'])
        self.assertEqual(4,len(self.calls))
        self.assert_code('attempt_limit',self.run_query,'fifth')

    def test_explicit_new_attempt_after_unknown_keeps_order_and_original_observation(self):
        self.mode='drop';self.run_query()
        self.mode='valid';result=self.run_query('v2')
        self.assertEqual('signature_and_order_match',result['transport']['outcome'])
        self.assertEqual(self.order_id,result['order_id'])
        self.assertEqual('unknown',self.orders.get(self.order_id)['attempts'][0]['outcome'])
        self.assertEqual(2,len(self.calls))

    def test_invalid_clock_key_and_timeout_fail_without_send(self):
        self.assert_code('invalid_signing_key',RSA2RequestSigner,b'invalid')
        for timeout in (True,0,31,float('nan')):
            self.assert_code('invalid_params',self.make_client,timeout=timeout)
        self.client=self.make_client(clock=lambda:float('nan'))
        self.assert_code('invalid_clock',self.run_query)
        self.assertEqual([],self.calls)

    def test_official_https_uses_default_certificate_and_hostname_checks(self):
        self.client=self.make_client(loopback_endpoint=None)
        with patch('ai_supermarket.alipay_transport.http.client.HTTPSConnection') as factory:
            factory.return_value.connect.side_effect=OSError('fixture no network')
            result=self.run_query()
            args,kwargs=factory.call_args
            self.assertEqual(('openapi-sandbox.dl.alipaydev.com',443),args)
            import ssl
            self.assertTrue(kwargs['context'].check_hostname)
            self.assertEqual(ssl.CERT_REQUIRED,kwargs['context'].verify_mode)
            self.assertEqual('unknown',result['transport']['outcome'])
            factory.return_value.close.assert_called_once()
        self.assertEqual([],self.calls)

    def test_rejected_response_releases_its_socket(self):
        with patch('ai_supermarket.alipay_transport.http.client.HTTPConnection') as factory:
            response=factory.return_value.getresponse.return_value
            response.status=302
            self.run_query()
            response.close.assert_called_once()
            factory.return_value.close.assert_called_once()
        self.assertEqual([],self.calls)


if __name__=='__main__':unittest.main()
