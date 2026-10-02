"""本项目原创。供应商固定HTTP交接、验签与原操作恢复；全部原创虚构数据。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import hmac
import json
from pathlib import Path
import socket
import threading
import time
import unittest
from unittest.mock import patch

from ai_supermarket.api_provider import (ApiProviderClient, FORMAT, MAX_WIRE,
    provider_response, verify_provider_request)
from ai_supermarket.common import MarketError, compact
from ai_supermarket.tests import test_api_product as fixture


class ApiProviderTests(unittest.TestCase):
    def setUp(self):
        self.key=b'original-fixture-provider-key-only-1234';self.mode='normal'
        self.received=[];self.executions=[];self.records={};self.allowed=True;self.now=1000
        test=self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw=self.rfile.read(int(self.headers['Content-Length']))
                try:
                    value,fingerprint=verify_provider_request(raw,self.headers.get('X-Market-Signature'),
                        test.key,'provider-test','connection-one',lambda request:test.allowed,now=test.now)
                except MarketError:
                    self.send_response(403);self.end_headers();return
                test.received.append(value)
                operation=value['operation_id']
                if test.mode=='redirect':
                    self.send_response(302);self.send_header('Location','/never-follow');self.end_headers();return
                if value['action']=='execute':
                    if operation not in test.records:
                        test.executions.append(operation)
                        test.records[operation]=(fingerprint,{'warehouse':value['input']['warehouse'],'count':7})
                    elif test.records[operation][0]!=fingerprint:
                        self.send_response(409);self.end_headers();return
                    if test.mode=='lost':
                        self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
                evidence=({'state':'completed','result':test.records[operation][1],'evidence_ref':'original-record'}
                          if operation in test.records else {'state':'unknown'})
                if test.mode=='fenced':evidence={'state':'not_executed','evidence_ref':'cancelled-and-fenced','fenced':True}
                body,signature=provider_response(raw,test.key,evidence)
                if test.mode=='tampered':body=body.replace(b'"count":7',b'"count":8')
                if test.mode in ('wrong-id','wrong-request'):
                    changed=json.loads(body)
                    if test.mode=='wrong-id':changed['operation_id']='another-operation'
                    else:changed['request_sha256']='b'*64
                    body=compact(changed).encode();signature=hmac.new(test.key,body,hashlib.sha256).hexdigest()
                if test.mode=='oversize':body=b' '*(MAX_WIRE+1)
                self.send_response(200)
                self.send_header('Content-Type','text/html' if test.mode=='html' else 'application/json')
                self.send_header('Content-Length',str(len(body)+1 if test.mode=='truncated' else len(body)))
                self.send_header('X-Market-Response-Signature',signature)
                if test.mode=='duplicate':self.send_header('X-Market-Response-Signature',signature)
                if test.mode=='compressed':self.send_header('Content-Encoding','gzip')
                self.end_headers()
                if test.mode=='slow':
                    time.sleep(.15)
                try:self.wfile.write(body)
                except (BrokenPipeError,ConnectionResetError):pass
            def log_message(self,*args):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.thread.join,2);self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.endpoint=f'http://127.0.0.1:{self.server.server_port}/api'
        self.client=self.make_client()

    def make_client(self,**changes):
        args={'provider':'provider-test','endpoint':self.endpoint,'key':self.key,'binding_id':'connection-one',
              'operations':['stock'],'clock':lambda:1000}
        return ApiProviderClient(**{**args,**changes})

    def request(self,**changes):
        return {'format':FORMAT,'provider':'provider-test','binding_id':'connection-one','action':'execute',
            'operation_id':'operation-one','issued_at':1000,'expires_at':1060,'operation':'stock',
            'input':{'warehouse':'fictional'},**changes}

    def verify(self,value,**changes):
        raw=compact(value).encode();sig=hmac.new(self.key,raw,hashlib.sha256).hexdigest()
        args={'raw':raw,'signature':sig,'key':self.key,'provider':'provider-test','binding_id':'connection-one',
              'authorize':lambda request:True,'now':1000}
        return verify_provider_request(**{**args,**changes})

    def test_signed_request_and_response_real_http(self):
        result=self.client.execute('operation-one','stock',{'warehouse':'fictional'})
        self.assertEqual({'count':7,'warehouse':'fictional'},result)
        found=self.client.lookup('operation-one')
        self.assertEqual(result,found['result']);self.assertEqual('completed',found['state'])
        self.assertEqual(['execute','lookup'],[v['action'] for v in self.received])
        self.assertEqual(['operation-one'],self.executions)
        self.assertNotIn(self.key.decode(),compact(self.received))

    def test_stable_id_and_fingerprint_across_timestamp_refresh(self):
        first=self.verify(self.request())[1]
        second=self.verify(self.request(issued_at=1001,expires_at=1061),now=1001)[1]
        self.assertEqual(first,second)
        self.client.execute('operation-one','stock',{'warehouse':'fictional'})
        self.now=1001
        self.make_client(clock=lambda:1001).execute('operation-one','stock',{'warehouse':'fictional'})
        self.assertEqual(1,len(self.executions))

    def test_signature_does_not_replace_provider_authorization(self):
        with self.assertRaises(MarketError):self.verify(self.request(),authorize=lambda request:False)
        self.allowed=False
        with self.assertRaises(MarketError):self.client.execute('operation-one','stock',{'warehouse':'fictional'})
        self.assertEqual([],self.executions)

    def test_signature_expiry_scope_and_lookup_input_rejected(self):
        cases=[({'signature':'b'*64},self.request()),({'now':1061},self.request()),
               ({},self.request(provider='other')),({},self.request(binding_id='other')),
               ({},self.request(action='lookup')),({},self.request(expires_at=1061)),
               ({},self.request(paid=True))]
        for changes,value in cases:
            with self.subTest(changes=changes,value=value),self.assertRaises(MarketError):self.verify(value,**changes)

    def test_fixed_endpoint_and_operation_limits(self):
        for endpoint in ('http://example.com/api','https://user:pass@example.com/api','https://example.com/api?key=x',
                         'file:///tmp/file','https://example.com/api#fragment','https://example.com/\n'):
            with self.subTest(endpoint=endpoint),self.assertRaises(MarketError):self.make_client(endpoint=endpoint)
        for changes in ({'key':b'short'},{'timeout':float('nan')},{'operations':[]}):
            with self.assertRaises(MarketError):self.make_client(**changes)
        with self.assertRaises(MarketError):self.client.execute('operation-one','other',{})
        self.assertEqual([],self.received)

    def test_redirect_and_bad_framing_are_not_retried(self):
        for mode in ('redirect','html','compressed','duplicate','oversize','truncated'):
            self.mode=mode;before=len(self.received)
            with self.subTest(mode=mode),self.assertRaises(MarketError):
                self.client.execute('operation-one','stock',{'warehouse':'fictional'})
            self.assertEqual(before+1,len(self.received))

    def test_signed_wrong_operation_and_unsigned_tampering_rejected(self):
        for mode in ('tampered','wrong-id','wrong-request'):
            self.mode=mode
            with self.subTest(mode=mode),self.assertRaises(MarketError):
                self.client.execute('operation-one','stock',{'warehouse':'fictional'})

    def test_timeout_keeps_unknown_without_retry_or_error_leak(self):
        self.mode='slow'
        with self.assertRaises(MarketError) as caught:
            self.make_client(timeout=.05).execute('operation-one','stock',{'warehouse':'fictional'})
        self.assertEqual('provider_result_unknown',caught.exception.code)
        self.assertNotIn(self.endpoint,str(caught.exception));self.assertEqual(1,len(self.executions))

    def test_missing_lookup_is_unknown_not_free_quota(self):
        self.assertEqual({'state':'unknown'},self.client.lookup('missing'))
        self.assertEqual([],self.executions)

    def test_not_executed_requires_explicit_fence_and_lookup(self):
        value=self.request();value.pop('operation');value.pop('input');value['action']='lookup'
        raw=compact(value).encode()
        for evidence in ({'state':'not_executed','evidence_ref':'absent'},
                         {'state':'not_executed','evidence_ref':'absent','fenced':False}):
            with self.assertRaises(MarketError):provider_response(raw,self.key,evidence)
        self.mode='fenced'
        self.assertEqual({'state':'not_executed','evidence_ref':'cancelled-and-fenced'},self.client.lookup('missing'))

    def test_quota_market_to_supplier_http_and_license_files(self):
        f=fixture.ApiProductTests();f.setUp();self.addCleanup(f.doCleanups)
        host=f.host(execute=self.client.execute,lookup=self.client.lookup)
        acquired=f.acquire()['acquisition_id']
        result=host.use(acquired,{'warehouse':'fictional'},'call-one')
        self.assertEqual('api_result_delivered',result['status'],result)
        self.assertEqual(7,json.loads(Path(result['path']).read_text())['count'])
        self.assertEqual(f.meta['license_text'],Path(result['license_path']).read_text(encoding='utf-8'))
        self.assertEqual(result['usage_id'],host.use(acquired,{'warehouse':'fictional'},'call-one')['usage_id'])
        self.assertEqual(1,len(self.executions));self.assertEqual(1,len(self.received))

    def test_lost_execute_response_recovered_by_read_only_lookup(self):
        f=fixture.ApiProductTests();f.setUp();self.addCleanup(f.doCleanups)
        host=f.host(execute=self.client.execute,lookup=self.client.lookup)
        acquired=f.acquire()['acquisition_id'];self.mode='lost'
        first=host.use(acquired,{'warehouse':'fictional'},'call-one')
        self.assertEqual('reserved',first['status']);self.assertEqual(1,len(self.executions))
        self.assertEqual('reserved',host.status(acquired,{'warehouse':'fictional'},'call-one')['status'])
        self.assertEqual(1,len(self.received))
        self.mode='normal'
        recovered=host.use(acquired,{'warehouse':'fictional'},'call-one',recover=True)
        self.assertEqual('api_result_delivered',recovered['status'])
        self.assertEqual(['execute','lookup'],[r['action'] for r in self.received])
        self.assertEqual(1,len(self.executions))

    def test_same_operation_cannot_change_input_at_provider(self):
        self.client.execute('operation-one','stock',{'warehouse':'fictional'})
        with self.assertRaises(MarketError):
            self.client.execute('operation-one','stock',{'warehouse':'changed'})
        self.assertEqual(1,len(self.executions))
        self.assertEqual('fictional',self.client.lookup('operation-one')['result']['warehouse'])

    def test_wire_limits_and_invalid_clock_rejected_before_send(self):
        with self.assertRaises(MarketError):
            self.client.execute('operation-one','stock',{'warehouse':'x'*16384})
        with self.assertRaises(MarketError):
            self.make_client(clock=lambda:float('inf')).lookup('operation-one')
        with self.assertRaises(MarketError):
            provider_response(compact(self.request()).encode(),self.key,
                {'state':'completed','result':'x'*65536,'evidence_ref':'record'})
        self.assertEqual([],self.received)

    def test_environment_proxy_is_not_used(self):
        with patch.dict('os.environ',{'HTTP_PROXY':'http://127.0.0.1:1','HTTPS_PROXY':'http://127.0.0.1:1','NO_PROXY':''}):
            self.assertEqual(7,self.client.execute('operation-one','stock',{'warehouse':'fictional'})['count'])


if __name__=='__main__':unittest.main()
