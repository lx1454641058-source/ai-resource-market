"""来源：本项目原创。隔离HTTP交接；虚构支付凭证，不调用支付宝或钱包。"""
import asyncio
import base64
import json
import unittest
from unittest.mock import patch

from ai_supermarket.common import MarketError, compact
from ai_supermarket.http_app import MarketHTTP
from ai_supermarket.identity import Identities
from ai_supermarket.machine_pay import decode_payment_proof
from ai_supermarket.tests import test_purchase_host as fixture
from ai_supermarket.tests import test_http_app as http_fixture


class MachinePayHTTPTests(unittest.TestCase):
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
        self.identity_path=self.database.parent/'identities.sqlite'
        self.identities=Identities(self.identity_path)
        self.identities.create_account('alice','buyer')
        self.issued=self.identities.issue_key('alice');self.token=self.issued['token']
        self.app=self.make_app()
        self.app.gateway.bind_key(self.issued['key_id'],'root','task','alice','fictional-task-binding')
        self.payload={'id':self.resource,'version':'1.0.0','request_id':'buy'}

    def make_app(self,**kwargs):
        options={'allowed_hosts':['market.test:8765'],'budget_database':self.budget.ledger.store.path,
                 'enforce_task_budget':True,'purchase_hosts':{self.issued['key_id']:self.host}}
        options.update(kwargs)
        return MarketHTTP(self.database,self.identity_path,**options)

    def fetch(self,value=None,**kwargs):
        kwargs.setdefault('path','/v1/purchases/resource')
        return asyncio.run(http_fixture.HTTPTests.request(self,self.payload if value is None else value,**kwargs))

    def proof(self,body=None,urlsafe=False,padded=True):
        body=body or {'protocol':dict(self.request)}
        raw=compact(body).encode()
        result=(base64.urlsafe_b64encode(raw) if urlsafe else base64.b64encode(raw))
        return result if padded else result.rstrip(b'=')

    def bill(self):
        status,result,headers=self.fetch()
        self.assertEqual(402,status,result)
        self.order_id=result['order_id'];self.record=self.orders.get(self.order_id)
        self.body.update(self.record['prepared']['channel_identifiers'])
        return result,headers

    def test_bill_on_wire_then_signed_payment_response_returns_resource(self):
        result,headers=self.bill()
        self.assertIn(b'payment-needed',headers);self.assertNotIn('headers',result)
        self.assertEqual(b'no-store',headers[b'cache-control'])
        self.client._enabled=True
        with patch.object(self.client,'_post',return_value=self.signed()) as post:
            code,read,_=self.fetch(headers=[(b'payment-proof',self.proof())])
            again=self.fetch(headers=[(b'payment-proof',self.proof(urlsafe=True,padded=False))])[1]
        self.assertEqual(200,code,read);self.assertEqual('read',read['status'])
        self.assertEqual(read['usage_id'],again['usage_id']);self.assertEqual(1,post.call_count)
        self.assertNotIn('fictional-proof-only',compact(read))
        self.assertEqual(100,self.budget.summary()['spent_cents'])

    def test_repeat_without_proof_is_status_not_another_bill(self):
        first,_=self.bill();code,state,headers=self.fetch()
        self.assertEqual(202,code);self.assertNotIn(b'payment-needed',headers)
        self.assertEqual(first['order_id'],state['order_id'])
        code,_,headers=self.fetch({'request_id':'buy'},path='/v1/purchases/handoff')
        self.assertEqual(402,code);self.assertIn(b'payment-needed',headers)

    def test_validation_header_and_resource_identity_come_from_verified_original_order(self):
        _,headers=self.bill();self.assertNotIn(b'payment-validation',headers)
        self.client._enabled=True
        with patch.object(self.client,'_post',return_value=self.signed()) as post:
            code,read,headers=self.fetch(headers=[(b'payment-proof',self.proof())])
            replay_code,replay,replay_headers=self.fetch()
        self.assertEqual((200,200),(code,replay_code));self.assertEqual(1,post.call_count)
        raw=headers[b'payment-validation']
        validation=json.loads(base64.urlsafe_b64decode(raw+b'='*((-len(raw))%4)))
        expected_resource=self.record['prepared']['channel_identifiers']['resource_id']
        self.assertEqual({'validated':True,'trade_no':self.request['trade_no'],'resource_id':expected_resource},validation)
        self.assertEqual(expected_resource,read['resource_id']);self.assertEqual(expected_resource,replay['resource_id'])
        self.assertEqual(raw,replay_headers[b'payment-validation'])
        self.assertNotIn('payment_proof',compact(validation));self.assertNotIn('client_session',compact(validation))
        self.assertFalse(read['delivery_receipt']['channel_fulfillment_confirmed'])
        self.assertEqual(100,self.budget.summary()['spent_cents'])

    def test_unknown_payment_does_not_emit_validation_header(self):
        self.bill();self.client._enabled=True
        with patch.object(self.client,'_post',side_effect=TimeoutError):
            code,result,headers=self.fetch(headers=[(b'payment-proof',self.proof())])
        self.assertEqual(202,code);self.assertNotIn(b'payment-validation',headers)
        self.assertNotIn('resource_id',result)

    def test_default_app_keeps_new_routes_closed(self):
        self.app=self.make_app(purchase_hosts=None)
        self.assertEqual(404,self.fetch()[0])
        self.assertEqual(0,self.budget.summary()['held_cents'])

    def test_gateway_requires_task_budget_enforcement(self):
        with self.assertRaises(MarketError):self.make_app(enforce_task_budget=False)

    def test_authenticated_tool_discovery_describes_bound_purchase_routes(self):
        code,body,_=self.fetch(path='/v1/tools',method='GET')
        self.assertEqual(200,code)
        self.assertEqual('/v1/purchases/resource',body['purchase_api']['resource'])
        self.assertEqual('task',body['purchase_api']['task_id'])
        self.assertFalse(body['purchase_api']['wallet_execution_available'])
        self.assertNotIn(self.token,compact(body));self.assertNotIn('mandate',compact(body['purchase_api']))

    def test_no_authentication_never_creates_purchase(self):
        self.assertEqual(401,self.fetch(token=False)[0])
        self.assertEqual(0,self.budget.summary()['held_cents'])

    def test_revoked_key_or_binding_blocks_purchase(self):
        self.app.gateway.revoke_binding(self.issued['key_id'])
        self.assertEqual(403,self.fetch()[0])
        self.assertEqual(0,self.budget.summary()['held_cents'])

    def test_wrong_resource_proof_is_rejected_before_channel(self):
        self.bill();other=self.publish(price=100,slug='other')
        self.client._enabled=True
        with patch.object(self.client,'_post',side_effect=AssertionError('must not send')):
            code,body,_=self.fetch({**self.payload,'id':other},headers=[(b'payment-proof',self.proof())])
        self.assertEqual('purchase_binding_mismatch',body['error_code'])
        self.assertEqual(0,len(self.orders.get(self.order_id)['attempts']))

    def test_proof_cannot_create_new_order_without_original_flow(self):
        code,body,_=self.fetch(headers=[(b'payment-proof',self.proof())])
        self.assertEqual('purchase_not_found',body['error_code'])
        self.assertEqual(0,self.budget.summary()['held_cents'])

    def test_duplicate_malformed_and_oversize_proof_are_not_echoed(self):
        self.bill()
        for headers in [[(b'payment-proof',self.proof()),(b'Payment-Proof',self.proof())],
                        [(b'payment-proof',b'bad-secret!?')],[(b'payment-proof',b'A'*8193)]]:
            code,body,_=self.fetch(headers=headers)
            self.assertEqual(400,code,body)
            self.assertNotIn('bad-secret',compact(body))
        self.assertEqual([],self.orders.get(self.order_id)['attempts'])

    def test_unknown_channel_result_returns_pending_not_new_payment_demand(self):
        self.bill();self.client._enabled=True
        with patch.object(self.client,'_post',side_effect=TimeoutError) as post:
            code,state,headers=self.fetch(headers=[(b'payment-proof',self.proof())])
            self.fetch(headers=[(b'payment-proof',self.proof())])
        self.assertEqual(202,code);self.assertNotIn(b'payment-needed',headers)
        self.assertFalse(state['paid_verified']);self.assertEqual(1,post.call_count)
        self.assertEqual(100,self.budget.summary()['held_cents'])

    def test_revocation_during_query_does_not_return_resource(self):
        self.bill();self.client._enabled=True
        def response(*args):
            self.identities.revoke_key(self.issued['key_id'])
            return self.signed()
        with patch.object(self.client,'_post',side_effect=response):
            code,body,_=self.fetch(headers=[(b'payment-proof',self.proof())])
        self.assertEqual(401,code);self.assertNotIn('content',body)
        self.assertTrue(self.orders.get(self.order_id)['paid_verified'])

    def test_status_is_readable_while_budget_paused(self):
        self.bill();self.ledger.set_state('root','paused',1)
        code,state,_=self.fetch({'request_id':'buy'},path='/v1/purchases/status')
        self.assertEqual(202,code);self.assertEqual(self.order_id,state['order_id'])
        self.assertEqual(403,self.fetch()[0])

    def test_other_buyer_and_injected_scope_fields_are_refused(self):
        self.identities.create_account('bob','buyer');other=self.identities.issue_key('bob')
        original=self.token;self.token=other['token']
        self.assertEqual(403,self.fetch()[0]);self.token=original
        self.assertEqual(400,self.fetch({**self.payload,'mandate_id':'other'})[0])

    def test_revocation_during_request_body_is_rechecked_before_purchase(self):
        async def receive():
            self.identities.revoke_key(self.issued['key_id'])
            return {'type':'http.request','body':compact(self.payload).encode()}
        self.assertEqual(401,self.fetch(receive=receive)[0])
        self.assertEqual(0,self.budget.summary()['held_cents'])

    def test_real_loopback_http_402_proof_and_resource_response(self):
        import http.client
        import socket
        import threading
        import time
        import uvicorn
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        self.app.hosts=frozenset({f'127.0.0.1:{port}'})
        server=uvicorn.Server(uvicorn.Config(self.app,host='127.0.0.1',port=port,http='h11',ws='none',
            proxy_headers=False,access_log=False,log_level='critical',lifespan='on',timeout_graceful_shutdown=3))
        thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
        def stop():
            server.should_exit=True;thread.join(5);sock.close()
            self.assertFalse(thread.is_alive())
        self.addCleanup(stop)
        deadline=time.monotonic()+5
        while not server.started and thread.is_alive() and time.monotonic()<deadline:time.sleep(0.01)
        self.assertTrue(server.started)
        def request(proof=None):
            conn=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
            headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'}
            if proof:headers['Payment-Proof']=proof.decode('ascii')
            try:
                conn.request('POST','/v1/purchases/resource',body=compact(self.payload).encode(),headers=headers)
                response=conn.getresponse()
                return response.status,json.loads(response.read()),dict(response.getheaders())
            finally:conn.close()
        status,body,headers=request()
        self.assertEqual(402,status);self.assertIn('payment-needed',headers)
        self.order_id=body['order_id'];self.record=self.orders.get(self.order_id)
        self.body.update(self.record['prepared']['channel_identifiers']);self.client._enabled=True
        with patch.object(self.client,'_post',return_value=self.signed()) as verify:
            status,body,headers=request(self.proof())
            again=request(self.proof())[1]
        self.assertEqual(200,status);self.assertEqual('read',body['status'])
        self.assertEqual(body['usage_id'],again['usage_id']);self.assertEqual(1,verify.call_count)
        self.assertEqual(100,self.budget.summary()['spent_cents'])


class ProofHeaderTests(unittest.TestCase):
    def test_strict_base64_json_and_bounds(self):
        good={'protocol':{'trade_no':'fixture','payment_proof':'fictional-only'},'method':{'client_session':'会话'}}
        raw=compact(good).encode()
        for encode in (base64.b64encode,base64.urlsafe_b64encode):
            for value in (encode(raw),encode(raw).rstrip(b'=')):
                self.assertEqual({**good['protocol'],**good['method']},decode_payment_proof(value))
        invalid=[b'bad?',b'AAAA=',b'  e30=',b'++++----',b'\xff',b'A'*8193,
                 base64.b64encode(b'{"protocol":{},"protocol":{}}'),base64.b64encode(b'[]'),
                 base64.b64encode(compact({'protocol':{'trade_no':'x','payment_proof':'x','paid':True}}).encode()),
                 base64.b64encode(compact({'protocol':{'trade_no':'x','payment_proof':'x'*65}}).encode())]
        for value in invalid:
            with self.subTest(length=len(value)),self.assertRaises(MarketError) as caught:decode_payment_proof(value)
            self.assertEqual('invalid_payment_header',caught.exception.code)


if __name__=='__main__':unittest.main()
