"""本项目原创。供应商通过HTTP核对原调用，不读取市场数据库；全部原创隔离数据。"""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch

from ai_supermarket.api_client import RemoteApiResources
from ai_supermarket.api_provider import ApiProviderClient,FORMAT
from ai_supermarket.api_quota import canonical,checksum
from ai_supermarket.common import MarketError,compact
from ai_supermarket.http_client import RemoteMarket
from ai_supermarket.provider_authorization import RemoteProviderAuthorization,ROUTE,PREFIX
from ai_supermarket.provider_operations import ProviderOperations
from ai_supermarket.tests import test_api_remote as fixture


class ProviderAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ApiRemoteTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.root=self.f.root;self.remote=self.f.live()
        self.f.identities.create_account('vendor','seller')
        self.seller=self.f.identities.issue_key('vendor')
        credential=self.root/'seller.json';credential.write_text(compact({'token':self.seller['token']}),encoding='utf-8')
        self.seller_remote=RemoteMarket(self.remote.base_url,credential)
        self.authorizer=RemoteProviderAuthorization(self.seller_remote,'provider-test','fixed-provider',
            {('vendor:stock','1.0.0'):self.f.f.digest})
        self.key=b'original-remote-authorization-test-key-123'
        self.auth_calls=0;test=self
        def authorization(request):
            self.auth_calls+=1
            return self.authorizer(request)
        self.operations=ProviderOperations(self.root/'supplier-operations.sqlite','provider-test','fixed-provider',
            self.key,authorization,self.f.f.execute,clock=lambda:1000)
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    length=int(self.headers['Content-Length'])
                    if not 0<length<=20480:raise MarketError('invalid_request','oversize')
                    raw=self.rfile.read(length)
                    body,signature=test.operations.handle(raw,self.headers.get('X-Market-Signature'))
                    self.send_response(200);self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(body)))
                    self.send_header('X-Market-Response-Signature',signature);self.end_headers();self.wfile.write(body)
                except MarketError:
                    self.send_response(403);self.send_header('Content-Length','0');self.end_headers()
            def log_message(self,*args):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.thread.join,3);self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.provider=ApiProviderClient('provider-test',f'http://127.0.0.1:{self.server.server_port}/api',
            self.key,'fixed-provider',['stock'],clock=lambda:1000)
        self.f.host.execute=self.provider.execute;self.f.host.lookup=self.provider.lookup
        self.client=RemoteApiResources(self.remote,'task','personal_use',self.root/'client')

    def invoke(self):return self.client.invoke(self.f.target,{'warehouse':'fictional'},'one')

    def request(self,operation_id,**changes):
        return {'format':FORMAT,'provider':'provider-test','binding_id':'fixed-provider','operation_id':operation_id,
            'action':'execute','issued_at':1000,'expires_at':1060,'operation':'stock','input':{'warehouse':'fictional'},**changes}

    def test_actual_nested_http_authorization_result_and_receipt(self):
        result=self.invoke();self.assertEqual('api_result_received',result['status'],result)
        self.assertEqual(1,len(self.f.f.calls));self.assertEqual(4,self.auth_calls)
        ack=self.client.acknowledge('one');self.assertEqual('api_receipt_recorded',ack['status'])
        self.assertEqual(4,self.auth_calls)
        with self.f.f.buyer.store.connect() as db:
            rows=db.execute("SELECT value FROM meta WHERE key GLOB 'api_operation_authority:*'").fetchall()
        self.assertEqual(1,len(rows));self.assertEqual(self.f.issued['key_id'],json.loads(rows[0][0])['key_id'])

    def test_fixed_release_not_approved_by_supplier_leaves_original_hold(self):
        self.authorizer.releases={('vendor:stock','1.0.0'):'b'*64}
        result=self.invoke();self.assertEqual('reserved',result['status'])
        self.assertEqual([],self.f.f.calls);self.assertEqual(1,self.auth_calls)

    def test_forged_operation_signed_with_valid_connection_key_denied(self):
        with self.assertRaises(MarketError):self.provider.execute('forged','stock',{'warehouse':'fictional'})
        self.assertEqual([],self.f.f.calls)
        with self.operations._db() as db:self.assertEqual(0,db.execute('SELECT COUNT(*) FROM operations').fetchone()[0])

    def test_wrong_input_provider_and_binding_denied(self):
        self.invoke();operation=self.f.f.calls[0]
        self.assertFalse(self.authorizer(self.request(operation,input={'warehouse':'changed'})))
        self.assertFalse(self.authorizer(self.request(operation,provider='another')))
        self.assertFalse(self.authorizer(self.request(operation,binding_id='another')))
        self.assertEqual(1,len(self.f.f.calls))

    def test_revoked_original_buyer_key_cannot_be_replaced_by_other_active_key(self):
        self.invoke();operation=self.f.f.calls[0]
        second=self.f.identities.issue_key('buyer')
        self.f.app.gateway.bind_key(second['key_id'],'root','task','buyer','rotated-key')
        self.f.app.apis.hosts[second['key_id']]={'provider-test':self.f.host}
        self.f.identities.revoke_key(self.f.issued['key_id'])
        self.assertFalse(self.authorizer(self.request(operation)))
        self.assertEqual(1,len(self.f.f.calls))

    def test_paused_task_stops_queries(self):
        self.invoke();operation=self.f.f.calls[0]
        self.f.ledger.set_state('root','paused',1)
        self.assertFalse(self.authorizer(self.request(operation)))

    def test_buyer_cannot_query_supplier_route_or_supply_payment_proof(self):
        self.invoke();operation=self.f.f.calls[0]
        query={'provider':'provider-test','binding_id':'fixed-provider','operation_id':operation,
               'action':'lookup','nonce':'one'}
        self.assertEqual('forbidden',self.remote.request(ROUTE,query)['error_code'])
        self.f.token=self.seller['token']
        code,reply,_=self.f.fetch(value=query,path=ROUTE,headers=[(b'payment-proof',b'not-accepted')])
        self.assertEqual(400,code);self.assertEqual('unexpected_payment_proof',reply['error_code'])

    def test_saturated_market_denies_callback_without_dispatch_or_retry(self):
        from ai_supermarket.http_app import Limits
        self.f.app.limits=Limits(concurrent=1)
        result=self.invoke()
        self.assertEqual('reserved',result['status']);self.assertEqual([],self.f.f.calls)
        self.assertEqual(1,self.auth_calls)

    def test_expired_entitlement_and_suspended_product_deny_lookup(self):
        self.invoke();operation=self.f.f.calls[0]
        self.f.f.now=5000
        self.assertFalse(self.authorizer(self.request(operation)))
        self.f.f.now=1000
        self.f.f.reviewer.availability('vendor:stock','1.0.0',1,'suspended','原创商品隔离测试','suspend')
        self.assertFalse(self.authorizer(self.request(operation)))

    def test_revoked_seller_and_other_seller_denied(self):
        self.invoke();operation=self.f.f.calls[0]
        self.f.identities.create_account('other','seller');other=self.f.identities.issue_key('other')
        path=self.root/'other.json';path.write_text(compact({'token':other['token']}),encoding='utf-8')
        another=RemoteProviderAuthorization(RemoteMarket(self.remote.base_url,path),'provider-test','fixed-provider',
            {('vendor:stock','1.0.0'):self.f.f.digest})
        self.assertFalse(another(self.request(operation)))
        self.f.identities.revoke_key(self.seller['key_id'])
        self.assertFalse(self.authorizer(self.request(operation)))

    def test_response_loss_recovers_original_provider_record_without_reexecution(self):
        original=self.provider.execute
        def lost(*args):original(*args);raise TimeoutError()
        self.f.host.execute=lost
        self.assertEqual('reserved',self.invoke()['status']);self.assertEqual(1,len(self.f.f.calls))
        count=self.auth_calls;self.client.status('one');self.assertEqual(count,self.auth_calls)
        self.assertEqual('api_result_received',self.client.recover('one')['status'])
        self.assertEqual(1,len(self.f.f.calls));self.assertEqual(count+2,self.auth_calls)

    def test_revocation_during_execution_hides_result_and_does_not_grant_retry(self):
        original=self.operations.execute
        def revoke(*args):
            result=original(*args);self.f.identities.revoke_key(self.f.issued['key_id']);return result
        self.operations.execute=revoke
        reply=self.invoke();self.assertEqual('error',reply['status']);self.assertEqual(1,len(self.f.f.calls))
        with self.operations._db() as db:self.assertEqual('reserved',db.execute('SELECT state FROM operations').fetchone()[0])

    def test_query_failure_does_not_execute_or_automatically_retry(self):
        with patch.object(self.seller_remote,'request',side_effect=MarketError('transport_unknown','unknown')) as call:
            result=self.invoke()
        self.assertEqual('reserved',result['status']);self.assertEqual(1,call.call_count);self.assertEqual([],self.f.f.calls)

    def test_no_identity_or_input_leak_and_response_binding_checked(self):
        self.invoke();operation=self.f.f.calls[0]
        original=self.seller_remote.request;seen=[]
        def changed(path,value):
            reply=original(path,value);seen.append((value,dict(reply)));reply['request_sha256']='b'*64;return reply
        with patch.object(self.seller_remote,'request',side_effect=changed),self.assertRaises(MarketError):
            self.authorizer(self.request(operation))
        wire=compact(seen)
        for private in (self.f.token,self.f.issued['key_id'],self.f.acquired,'warehouse','fictional'):
            self.assertNotIn(private,wire)

    def test_lookup_after_restart_uses_original_record(self):
        self.invoke();operation=self.f.f.calls[0]
        self.operations=ProviderOperations(self.root/'supplier-operations.sqlite','provider-test','fixed-provider',
            self.key,self.authorizer,self.f.f.execute,clock=lambda:1000)
        self.assertEqual('completed',self.provider.lookup(operation)['state'])
        self.assertEqual(1,len(self.f.f.calls))

    def test_authorization_binding_never_recreated_for_legacy_unknown_call(self):
        with patch.object(self.f.host,'execute',side_effect=TimeoutError):self.invoke()
        with self.f.host.quota._db() as db:operation=db.execute('SELECT id FROM calls').fetchone()[0]
        with self.f.f.buyer.store.connect(write=True) as db:
            db.execute('UPDATE meta SET key=? WHERE key=?',('legacy-hidden-record',PREFIX+operation))
        self.assertFalse(self.authorizer(self.request(operation)))
        self.assertEqual([],self.f.f.calls)


class PaidProviderAuthorizationTests(unittest.TestCase):
    def test_paid_original_right_checked_without_new_channel_action(self):
        from ai_supermarket.tests.test_unified_api_task import UnifiedApiTaskTests
        UnifiedApiTaskTests.setUpClass();f=UnifiedApiTaskTests();f.setUp();self.addCleanup(f.doCleanups)
        task=f.task();bill=f.select(task);right=f.pay(task,bill)
        task.api_call(right['acquisition_id'],'{"warehouse":"fictional"}','paid-operation')
        principal=f.f.seller.principal;f.identities.create_account(principal,'seller')
        seller=f.identities.issue_key(principal);credential=f.root/'seller-auth.json'
        credential.write_text(compact({'token':seller['token']}),encoding='utf-8')
        release=f.f.buyer.resolve(f.f.resource,'1.0.0','personal_use')['release_digest']
        auth=RemoteProviderAuthorization(RemoteMarket(task.remote.base_url,credential),'provider-test','fixed-provider',
                                        {(f.f.resource,'1.0.0'):release})
        request={'format':FORMAT,'provider':'provider-test','binding_id':'fixed-provider','operation_id':f.calls[0],
                 'action':'lookup','issued_at':1000,'expires_at':1060}
        before=f.f.orders.get(bill['order_id'])
        with patch.object(f.f.client,'_post',side_effect=AssertionError('不应新增支付渠道请求')) as channel:
            self.assertTrue(auth(request))
            with f.f.buyer.store.connect(write=True) as db:
                db.execute('UPDATE meta SET key=? WHERE key=?',('invalidated-test-payment-link','paid_entitlement:'+right['acquisition_id']))
            self.assertFalse(auth(request));channel.assert_not_called()
        self.assertEqual(before,f.f.orders.get(bill['order_id']))
        self.assertEqual(100,f.f.budget.summary()['spent_cents']);self.assertEqual(1,len(f.calls))


if __name__=='__main__':unittest.main()
