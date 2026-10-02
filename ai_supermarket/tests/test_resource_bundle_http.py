"""来源：本项目原创。真实本机文件交接；付款响应为隔离合成，不访问支付渠道。"""
import asyncio
import base64
import hashlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

from ai_supermarket.alipay_transport import AlipayVerificationClient
from ai_supermarket.alipay_proof import AlipayProofVerifier
from ai_supermarket.budget import BudgetLedger
from ai_supermarket.common import compact
from ai_supermarket.identity import Identities
from ai_supermarket.payment_orders import PaymentOrders
from ai_supermarket.payment_fulfillment import PaymentFulfillment
from ai_supermarket.tests import test_machine_pay_http as machine_fixture
from ai_supermarket.tests import test_origin as origin_fixture
from ai_supermarket.tests import test_assets as asset_fixture


class ResourceBundleHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):machine_fixture.MachinePayHTTPTests.setUpClass()

    def setUp(self):
        self.f=machine_fixture.MachinePayHTTPTests()
        self.f.setUp();self.addCleanup(self.f.doCleanups)

    def install(self, source, resource, policy, **host_args):
        f=self.f
        f.buyer=source.buyer;f.buyer.clock=lambda:f.now[0]
        f.database=source.db;f.resource=resource
        f.merchant={**f.merchant,'market_seller':source.seller.principal}
        f.verifier=AlipayProofVerifier(f.pem,f.merchant,amount_unit='CNY_major',policy_reference='fixture-only')
        f.orders=PaymentOrders(f.buyer,f.merchant)
        f.configure();f.policy['signing_public_key_sha256']=f.signer.public_key_sha256
        f.fulfillment=PaymentFulfillment(f.orders,f.policy,enabled=True)
        f.ledger=BudgetLedger(source.root/'budget.sqlite',clock=lambda:f.now[0])
        f.ledger.create('root','fixture-owner',300,'fictional-budget-consent')
        f.ledger.grant_task('root','task','buyer');f.budget=f.ledger.scope('root','task','buyer')
        f.authorize(allowed_sellers=['vendor','fixture']);f.biller=f.make_biller()
        f.client=AlipayVerificationClient(f.orders,f.verifier,f.signer,clock=lambda:f.now[0])
        f.host=f.make_host(**host_args)
        f.identity_path=source.root/'identities.sqlite';f.identities=Identities(f.identity_path)
        f.identities.create_account('buyer','buyer',policy);f.issued=f.identities.issue_key('buyer')
        f.token=f.issued['token'];f.app=f.make_app()
        f.app.gateway.bind_key(f.issued['key_id'],'root','task','buyer','fictional-task-binding')
        f.payload={'id':resource,'version':'1.0.0','request_id':'buy'}

    def origin(self,price=100,connected=True):
        source=origin_fixture.OriginTests();source.setUp();self.addCleanup(source.doCleanups)
        args={'origin_clients':{'provider-demo':source.client},'output_root':source.root} if connected else {}
        self.install(source,source.publish(price),origin_fixture.POLICY,**args)
        self.source=source
        return source

    def asset(self,price=0,audio=False):
        source=asset_fixture.AssetTests();source.setUp();self.addCleanup(source.doCleanups)
        draft=source.draft(audio);source.approve(draft)
        if price:source.seller.reprice(draft['id'],'1.0.0',1,price,'fixture-price')
        self.install(source,draft['id'],asset_fixture.POLICY)
        self.source=source
        return source

    async def request(self, *, proof=None, accept=b'application/zip', value=None, send_hook=None, token=True):
        f=self.f
        headers=[(b'host',b'market.test:8765'),(b'content-type',b'application/json')]
        if token:headers.append((b'authorization',b'Bearer '+f.token.encode()))
        if accept is not None:headers.append((b'accept',accept))
        if proof is not None:headers.append((b'payment-proof',proof))
        messages=[]
        async def receive():return {'type':'http.request','body':compact(value or f.payload).encode()}
        async def send(message):
            if send_hook is not None:await send_hook(message)
            messages.append(message)
        await f.app({'type':'http','method':'POST','path':'/v1/purchases/resource','scheme':'http',
                     'client':('127.0.0.1',1234),'headers':headers,'query_string':b''},receive,send)
        return messages

    def fetch(self,**kwargs):
        messages=asyncio.run(self.request(**kwargs))
        return messages[0]['status'],messages[1]['body'],dict(messages[0]['headers'])

    def paid(self):
        f=self.f;f.bill();f.client._enabled=True
        with patch.object(f.client,'_post',return_value=f.signed()) as post:
            result=self.fetch(proof=f.proof())
        self.assertEqual(1,post.call_count)
        return result

    def unpack(self,result):
        status,raw,headers=result
        self.assertEqual(200,status,raw)
        self.assertEqual(b'application/zip',headers[b'content-type'])
        self.assertEqual(str(len(raw)).encode(),headers[b'content-length'])
        self.assertEqual(hashlib.sha256(raw).hexdigest().encode(),headers[b'x-bundle-sha256'])
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            manifest=json.loads(archive.read('manifest.json'))
            name=manifest['resource']['path']
            self.assertEqual([name,'LICENSE.txt','manifest.json'],archive.namelist())
            resource=archive.read(name);license_bytes=archive.read('LICENSE.txt')
            for info in archive.infolist():
                self.assertEqual(zipfile.ZIP_STORED,info.compress_type)
                self.assertEqual(0o100644,info.external_attr>>16)
        for name,body in [('resource',resource),('license',license_bytes)]:
            self.assertEqual(len(body),manifest[name]['size'])
            self.assertEqual(hashlib.sha256(body).hexdigest(),manifest[name]['sha256'])
        self.assertFalse(manifest['task_completed']);self.assertFalse(manifest['client_receipt_confirmed'])
        self.assertNotIn(str(self.source.root),compact(manifest))
        self.assertNotIn(self.f.token,compact(manifest))
        return resource,license_bytes,manifest

    def test_paid_origin_zip_contains_exact_resource_and_license_and_replays(self):
        source=self.origin();first=self.paid();raw,license_bytes,manifest=self.unpack(first)
        self.assertEqual(origin_fixture.RAW,raw)
        encoded=first[2][b'payment-validation']
        validation=json.loads(base64.urlsafe_b64decode(encoded+b'='*((-len(encoded))%4)))
        self.assertEqual(True,validation['validated'])
        self.assertEqual(self.f.record['prepared']['channel_identifiers']['resource_id'],validation['resource_id'])
        self.assertEqual(self.f.request['trade_no'],validation['trade_no'])
        self.assertEqual(source.metadata()['license_text'].encode(),license_bytes)
        calls=len(source.requests);source.mode='error'
        second=self.fetch();self.assertEqual(first[1],second[1]);self.assertEqual(calls,len(source.requests))
        self.assertEqual(encoded,second[2][b'payment-validation'])
        self.assertEqual(100,self.f.budget.summary()['spent_cents'])
        self.assertEqual(1,self.f.count('usage_events'));self.assertEqual(1,self.f.count('acquisitions'))
        with self.f.buyer.store.connect() as db:
            self.assertEqual(0,db.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'channel_fulfillment:%'").fetchone()[0])

    def test_free_origin_uses_same_bundle_without_payment(self):
        self.origin(price=0);first=self.fetch();self.unpack(first)
        self.assertEqual(first[1],self.fetch()[1]);self.assertEqual(0,self.f.budget.summary()['spent_cents'])
        self.assertNotIn(b'payment-validation',first[2])

    def test_free_png_and_wav_include_rights(self):
        for audio in (False,True):
            self.asset(audio=audio);first=self.fetch();raw,license_bytes,manifest=self.unpack(first)
            self.assertEqual('resource.wav' if audio else 'resource.png',manifest['resource']['path'])
            self.assertIn('attribution',manifest['rights']);self.assertTrue(license_bytes)
            self.assertEqual(first[1],self.fetch()[1])
            self.assertEqual(1,self.f.count('usage_events'))

    def test_paid_asset_uses_original_confirmed_order_and_one_usage(self):
        self.asset(price=100);first=self.paid();self.unpack(first)
        self.assertEqual(first[1],self.fetch()[1]);self.assertEqual(1,self.f.count('acquisitions'))
        self.assertEqual('paid_delivered_to_host',self.f.orders.get(self.f.order_id)['status'])
        self.assertEqual(100,self.f.budget.summary()['spent_cents'])

    def test_no_zip_opt_in_preserves_json_transfer_response(self):
        self.asset();code,raw,headers=self.fetch(accept=None)
        self.assertEqual(202,code);self.assertEqual('host_transfer_required',json.loads(raw)['status'])
        self.assertEqual(b'application/json; charset=utf-8',headers[b'content-type'])
        self.assertEqual(0,self.f.count('usage_events'))

    def test_missing_origin_connection_stays_pending_without_file(self):
        self.origin(connected=False);code,raw,headers=self.paid()
        self.assertEqual(202,code);self.assertEqual('host_transfer_required',json.loads(raw)['status'])
        self.assertNotIn(b'x-bundle-sha256',headers)
        self.assertNotIn(b'payment-validation',headers)

    def test_disconnect_before_body_releases_slot_and_resumes_without_new_purchase(self):
        self.origin();first=self.paid()
        async def disconnect(message):
            if message['type']=='http.response.body':
                self.assertEqual(1,self.f.app.active)
                raise OSError('fictional disconnected client')
        messages=asyncio.run(self.request(send_hook=disconnect))
        self.assertEqual(1,len(messages));self.assertEqual(0,self.f.app.active)
        self.assertEqual(first[1],self.fetch()[1]);self.assertEqual(1,self.f.count('acquisitions'))

    def test_cached_file_tamper_and_license_tamper_fail_closed(self):
        self.origin();self.paid()
        file=next(self.source.root.glob('*/resource.csv'));original=file.read_bytes()
        file.write_bytes(b'changed')
        self.assertEqual('integrity_failed',json.loads(self.fetch()[1])['error_code'])
        file.write_bytes(original)
        file.with_name('LICENSE.txt').write_text('changed',encoding='utf-8')
        self.assertEqual('output_conflict',json.loads(self.fetch()[1])['error_code'])

    def test_revoked_identity_after_packaging_never_returns_bytes(self):
        from ai_supermarket.resource_bundle import resource_bundle
        self.asset()
        def revoked(*args):
            result=resource_bundle(*args)
            self.f.identities.revoke_key(self.f.issued['key_id'])
            return result
        with patch('ai_supermarket.resource_bundle.resource_bundle',side_effect=revoked):
            code,body,headers=self.fetch()
        self.assertEqual(401,code);self.assertNotIn(b'x-bundle-sha256',headers)

    def test_unsupported_accept_and_unauthorized_requests_do_not_acquire(self):
        self.asset()
        self.assertEqual(406,self.fetch(accept=b'text/html')[0])
        self.assertEqual(401,self.fetch(token=False)[0]);self.assertEqual(0,self.f.count('acquisitions'))

    def test_withdrawal_serves_paid_rights_but_suspension_blocks_download(self):
        source=self.origin();first=self.paid()
        source.seller.availability(self.f.resource,'1.0.0',1,'withdrawn','fixture withdrawal','withdraw')
        self.assertEqual(first[1],self.fetch()[1])
        source.reviewer.availability(self.f.resource,'1.0.0',2,'suspended','fixture quarantine','suspend')
        code,body,headers=self.fetch()
        self.assertNotEqual(200,code);self.assertNotIn(b'x-bundle-sha256',headers)

    def test_slow_file_response_holds_slot_and_cancellation_frees_it(self):
        from dataclasses import replace
        self.asset();self.f.app.limits=replace(self.f.app.limits,concurrent=1)
        async def scenario():
            started=asyncio.Event();release=asyncio.Event()
            async def slow(message):
                if message['type']=='http.response.body':
                    started.set();await release.wait()
            task=asyncio.create_task(self.request(send_hook=slow))
            try:
                await asyncio.wait_for(started.wait(),3)
                self.assertEqual(1,self.f.app.active)
                other=await self.request()
                self.assertEqual(503,other[0]['status'])
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
            self.assertEqual(0,self.f.app.active)
        asyncio.run(scenario());self.unpack(self.fetch())

    def test_real_http_transmits_zip_resource_license_and_digest(self):
        import http.client
        import socket
        import threading
        import time
        import uvicorn
        self.origin(price=100)
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        self.f.app.hosts=frozenset({f'127.0.0.1:{port}'})
        server=uvicorn.Server(uvicorn.Config(self.f.app,http='h11',ws='none',proxy_headers=False,
            access_log=False,log_level='critical',lifespan='on',timeout_graceful_shutdown=3))
        thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
        def stop():
            server.should_exit=True;thread.join(5);sock.close();self.assertFalse(thread.is_alive())
        self.addCleanup(stop)
        deadline=time.monotonic()+5
        while not server.started and thread.is_alive() and time.monotonic()<deadline:time.sleep(.01)
        self.assertTrue(server.started)
        def request(proof=None):
            conn=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
            headers={'Authorization':'Bearer '+self.f.token,'Content-Type':'application/json','Accept':'application/zip'}
            if proof:headers['Payment-Proof']=proof.decode('ascii')
            try:
                conn.request('POST','/v1/purchases/resource',compact(self.f.payload).encode(),headers)
                response=conn.getresponse();raw=response.read()
                return response.status,raw,{k.lower().encode():v.encode() for k,v in response.getheaders()}
            finally:conn.close()
        code,raw,headers=request();self.assertEqual(402,code);self.assertIn(b'payment-needed',headers)
        f=self.f;f.order_id=json.loads(raw)['order_id'];f.record=f.orders.get(f.order_id)
        f.body.update(f.record['prepared']['channel_identifiers']);f.client._enabled=True
        with patch.object(f.client,'_post',return_value=f.signed()) as post:
            result=request(f.proof());again=request()
        resource,_,_=self.unpack(result);self.assertEqual(origin_fixture.RAW,resource)
        self.assertEqual(result[1],again[1]);self.assertEqual(1,post.call_count)


if __name__=='__main__':unittest.main()
