"""本项目原创。供应商持久原操作、真实进程/HTTP与保守恢复，无真实供应商或付款。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from ai_supermarket.api_provider import ApiProviderClient, FORMAT
from ai_supermarket.common import MarketError, compact
from ai_supermarket.provider_operations import ProviderOperations
from ai_supermarket.remote_task import RemoteTaskResources
from ai_supermarket.tests import test_api_remote as remote_fixture


class ProviderOperationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.path=self.root/'operations.sqlite'
        self.key=b'original-fixture-provider-key-only-1234';self.calls=[];self.queries=[]
        self.results={};self.allowed=True;self.now=1000
        self.service=self.make()

    def authorize(self,request):return self.allowed

    def test_runtime_missing_operations_never_creates_replacement(self):
        self.call(); retained=self.path.with_name('retained.sqlite');self.path.rename(retained)
        with self.assertRaises(MarketError) as caught:self.call()
        self.assertEqual('ledger_unavailable',caught.exception.code)
        self.assertFalse(self.path.exists());self.assertEqual(1,len(self.calls))
        retained.rename(self.path);self.call();self.assertEqual(1,len(self.calls))

    def test_explicit_reopen_rejects_missing_empty_and_tableless_operations(self):
        path=self.path.with_name('missing.sqlite')
        for expected in ('ledger_unavailable','ledger_unavailable','ledger_mismatch'):
            if path.exists():before=path.read_bytes()
            else:before=None
            with self.assertRaises(MarketError) as caught:self.make(path=path,require_existing=True)
            self.assertEqual(expected,caught.exception.code)
            if before is None:self.assertFalse(path.exists());path.write_bytes(b'')
            elif not before:
                self.assertEqual(before,path.read_bytes())
                with closing(sqlite3.connect(path)) as db:db.execute('VACUUM')
            else:self.assertEqual(before,path.read_bytes())

    def test_runtime_loss_after_file_check_does_not_recreate_operations(self):
        connect=sqlite3.connect
        def remove_before_open(path,*args,**kwargs):
            self.assertTrue(str(path).endswith('?mode=rw'));self.assertTrue(kwargs['uri'])
            self.path.rename(self.path.with_name('retained-race.sqlite'))
            return connect(path,*args,**kwargs)
        with patch('ai_supermarket.provider_operations.sqlite3.connect',side_effect=remove_before_open):
            with self.assertRaises(MarketError) as caught:self.call()
        self.assertEqual('ledger_unavailable',caught.exception.code)
        self.assertFalse(self.path.exists());self.assertEqual([],self.calls)

    def test_loss_after_dispatch_keeps_unknown_operation_after_restoration(self):
        retained=self.path.with_name('retained-after-dispatch.sqlite')
        def execute(*args):
            result=self.execute(*args);self.path.rename(retained);return result
        self.service=self.make(execute=execute,require_existing=True)
        with self.assertRaises(MarketError) as caught:self.call()
        self.assertEqual('ledger_unavailable',caught.exception.code)
        self.assertFalse(self.path.exists());self.assertEqual(1,len(self.calls))
        retained.rename(self.path)
        self.assertEqual('unknown',self.call()['state']);self.assertEqual(1,len(self.calls))

    def execute(self,operation_id,operation,inputs):
        self.calls.append(operation_id);self.results[operation_id]={'count':7,'warehouse':inputs['warehouse']}
        return self.results[operation_id]

    def lookup(self,operation_id):
        self.queries.append(operation_id)
        if operation_id not in self.results:return {'state':'unknown'}
        return {'state':'completed','result':self.results[operation_id],'evidence_ref':'original-backend-record'}

    def make(self,**changes):
        args={'path':self.path,'provider':'provider-test','binding_id':'connection-one','key':self.key,
              'authorize':self.authorize,'execute':self.execute,'lookup':self.lookup,'clock':lambda:self.now}
        return ProviderOperations(**{**args,**changes})

    def request(self,action='execute',**changes):
        value={'format':FORMAT,'provider':'provider-test','binding_id':'connection-one','action':action,
               'operation_id':'operation-one','issued_at':self.now,'expires_at':self.now+60}
        if action=='execute':value.update(operation='stock',input={'warehouse':'fictional'})
        value.update(changes);raw=compact(value).encode()
        return raw,hmac.new(self.key,raw,hashlib.sha256).hexdigest()

    def call(self,action='execute',**changes):
        raw,signature=self.service.handle(*self.request(action,**changes))
        self.assertEqual(hmac.new(self.key,raw,hashlib.sha256).hexdigest(),signature)
        return json.loads(raw)

    def test_replay_and_restart_return_original_result(self):
        first=self.call();self.now+=2;self.service=self.make()
        second=self.call();self.assertEqual(first['result'],second['result'])
        self.assertEqual(['operation-one'],self.calls)
        self.assertEqual('completed',self.call('lookup')['state']);self.assertEqual([],self.queries)

    def test_changed_input_same_id_stops_before_execute(self):
        self.call()
        with self.assertRaises(MarketError) as caught:self.call(input={'warehouse':'other'})
        self.assertEqual('idempotency_conflict',caught.exception.code);self.assertEqual(1,len(self.calls))

    def test_concurrent_same_operation_has_one_dispatch(self):
        entered=threading.Event();release=threading.Event()
        def execute(*args):
            entered.set();self.assertTrue(release.wait(3));return self.execute(*args)
        self.service=self.make(execute=execute)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(self.call)
            try:
                self.assertTrue(entered.wait(2));self.assertEqual('unknown',pool.submit(self.call).result(2)['state'])
            finally:release.set()
            self.assertEqual('completed',first.result(2)['state'])
        self.assertEqual(1,len(self.calls))

    def test_query_missing_never_dispatches_or_creates_record(self):
        self.assertEqual('unknown',self.call('lookup')['state'])
        self.assertEqual([],self.calls);self.assertEqual([],self.queries)
        with closing(sqlite3.connect(self.path)) as db:self.assertEqual(0,db.execute('SELECT COUNT(*) FROM operations').fetchone()[0])

    def test_exception_retains_unknown_and_recovery_uses_original_backend(self):
        def lost(*args):self.execute(*args);raise OSError('fixture-private-value-not-for-response')
        self.service=self.make(execute=lost)
        self.assertEqual('unknown',self.call()['state']);self.service=self.make()
        self.assertEqual('unknown',self.call()['state']);self.assertEqual(1,len(self.calls))
        result=self.call('lookup')
        self.assertEqual('completed',result['state']);self.assertEqual(7,result['result']['count'])
        self.assertEqual(['operation-one'],self.queries);self.assertEqual(1,len(self.calls))

    def test_missing_or_invalid_recovery_cannot_release_unknown(self):
        self.service=self.make(execute=lambda *args:(_ for _ in ()).throw(TimeoutError()))
        self.call()
        for lookup in (None,lambda op:{'state':'not_executed','evidence_ref':'not-found'},
                       lambda op:{'state':'not_executed','evidence_ref':'not-found','fenced':False},
                       lambda op:{'state':'completed','result':'x'*65536,'evidence_ref':'too-large'}):
            self.service=self.make(lookup=lookup)
            self.assertEqual('unknown',self.call('lookup')['state'])
        self.assertEqual([],self.calls)

    def test_authoritative_fence_is_terminal_and_not_reexecuted(self):
        self.service=self.make(execute=lambda *args:(_ for _ in ()).throw(TimeoutError()),
            lookup=lambda op:{'state':'not_executed','evidence_ref':'backend-fenced','fenced':True})
        self.call();reply=self.call('lookup')
        self.assertEqual('not_executed',reply['state']);self.assertTrue(reply['fenced'])
        self.service=self.make();self.assertEqual('unknown',self.call()['state'])
        self.assertEqual('not_executed',self.call('lookup')['state']);self.assertEqual([],self.calls)

    def test_valid_signature_still_needs_authorization_and_cache_rechecks(self):
        self.allowed=False
        with self.assertRaises(MarketError):self.call()
        self.allowed=True;self.call();self.allowed=False
        for action in ('execute','lookup'):
            with self.assertRaises(MarketError):self.call(action)
        self.assertEqual(1,len(self.calls))

    def test_revocation_during_execution_preserves_unknown(self):
        def revoke(*args):result=self.execute(*args);self.allowed=False;return result
        self.service=self.make(execute=revoke)
        with self.assertRaises(MarketError):self.call()
        self.allowed=True;self.service=self.make()
        self.assertEqual('unknown',self.call()['state'])
        self.assertEqual('completed',self.call('lookup')['state']);self.assertEqual(1,len(self.calls))

    def test_invalid_result_never_becomes_completed(self):
        self.service=self.make(execute=lambda *args:'x'*65536)
        self.assertEqual('unknown',self.call()['state'])
        self.service=self.make();self.assertEqual('unknown',self.call()['state']);self.assertEqual([],self.calls)

    def test_binding_and_other_database_rejected_without_migration(self):
        for change in ({'provider':'other'},{'binding_id':'other'}):
            with self.assertRaises(MarketError):self.make(**change)
        other=self.root/'other.sqlite'
        with closing(sqlite3.connect(other)) as db:db.execute('CREATE TABLE user_data (value TEXT)');db.commit()
        with self.assertRaises(MarketError):self.make(path=other)
        with closing(sqlite3.connect(other)) as db:
            self.assertEqual([('user_data',)],db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())

    def test_corrupt_request_or_result_is_not_reexecuted(self):
        self.call()
        with closing(sqlite3.connect(self.path)) as db:db.execute("UPDATE operations SET result='{}'");db.commit()
        with self.assertRaises(MarketError):self.call('lookup')
        with closing(sqlite3.connect(self.path)) as db:db.execute("UPDATE operations SET fingerprint=?",('b'*64,));db.commit()
        with self.assertRaises(MarketError):self.call()
        self.assertEqual(1,len(self.calls))

    def test_response_lost_after_commit_replays_original(self):
        import ai_supermarket.provider_operations as module
        original=module.provider_response;counter=[0]
        def interrupted(*args):
            counter[0]+=1
            if counter[0]==2:raise OSError('simulated wire failure after commit')
            return original(*args)
        with patch.object(module,'provider_response',side_effect=interrupted):
            with self.assertRaises(OSError):self.call()
        self.service=self.make();self.assertEqual('completed',self.call()['state']);self.assertEqual(1,len(self.calls))

    def test_conflicting_completion_does_not_replace_first_evidence(self):
        entered=threading.Event();release=threading.Event()
        def execute(*args):
            entered.set();self.assertTrue(release.wait(3));return self.execute(*args)
        self.service=self.make(execute=execute,
            lookup=lambda op:{'state':'completed','result':{'count':8},'evidence_ref':'backend-claim'})
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(self.call)
            try:
                self.assertTrue(entered.wait(2));self.assertEqual(8,self.call('lookup')['result']['count'])
            finally:release.set()
            with self.assertRaises(MarketError) as caught:pending.result(2)
        self.assertEqual('settlement_conflict',caught.exception.code)
        self.assertEqual(8,self.call('lookup')['result']['count']);self.assertEqual(1,len(self.calls))

    def process(self,crash=False):
        source='''import sys,os,time,hmac,hashlib
from pathlib import Path
from ai_supermarket.provider_operations import ProviderOperations
from ai_supermarket.common import compact
key=b'original-fixture-provider-key-only-1234'
def execute(*args):
    with open(sys.argv[2],'a') as handle:handle.write('executed\\n')
    if sys.argv[3]=='crash':os._exit(19)
    time.sleep(.15)
    return {'count':7}
service=ProviderOperations(sys.argv[1],'provider-test','connection-one',key,lambda req:True,execute,clock=lambda:1000)
raw=compact({'format':'market-provider-api-v1','provider':'provider-test','binding_id':'connection-one',
 'action':'execute','operation_id':'operation-one','operation':'stock','input':{'warehouse':'fictional'},
 'issued_at':1000,'expires_at':1060}).encode()
body,signature=service.handle(raw,hmac.new(key,raw,hashlib.sha256).hexdigest())
print(body.decode())
'''
        return subprocess.Popen([sys.executable,'-X','utf8','-B','-c',source,str(self.path),str(self.root/'executed.txt'),
            'crash' if crash else 'normal'],cwd=Path(__file__).resolve().parents[2],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            text=True,encoding='utf-8')

    def test_two_real_processes_share_one_dispatch(self):
        one=self.process();two=self.process()
        first,err1=one.communicate(timeout=10);second,err2=two.communicate(timeout=10)
        self.assertEqual((0,0),(one.returncode,two.returncode),(err1,err2))
        self.assertIn('completed',[json.loads(first)['state'],json.loads(second)['state']])
        self.assertEqual(['executed'],(self.root/'executed.txt').read_text().splitlines())

    def test_process_crash_leaves_original_reservation_no_redispatch(self):
        proc=self.process(crash=True);proc.communicate(timeout=10);self.assertEqual(19,proc.returncode)
        self.service=self.make();self.assertEqual('unknown',self.call()['state'])
        self.assertEqual('unknown',self.call('lookup')['state']);self.assertEqual([],self.calls)
        self.assertEqual(['executed'],(self.root/'executed.txt').read_text().splitlines())

    def test_real_http_client_quota_and_restarted_provider(self):
        test=self;seen=[];lose=[True]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw=self.rfile.read(int(self.headers['Content-Length']));seen.append(json.loads(raw)['action'])
                try:body,signature=test.service.handle(raw,self.headers['X-Market-Signature'])
                except MarketError:self.send_response(403);self.end_headers();return
                if lose[0]:lose[0]=False;self.close_connection=True;return
                self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)));self.send_header('X-Market-Response-Signature',signature)
                self.end_headers();self.wfile.write(body)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join,2);self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        client=ApiProviderClient('provider-test',f'http://127.0.0.1:{server.server_port}/api',self.key,
                                 'connection-one',['stock'],clock=lambda:1000)
        f=remote_fixture.ApiRemoteTests();f.setUp();self.addCleanup(f.doCleanups)
        f.host.execute=client.execute;f.host.lookup=client.lookup
        directory=f.root/'client';directory.mkdir()
        task=RemoteTaskResources(f.live(),'task','仓库查询','personal_use',directory,mode='api')
        self.assertEqual('vendor:stock',task.initial['items'][0]['id'])
        task.resolve('vendor:stock','1.0.0')
        acquired=task.use('vendor:stock','1.0.0','choose')['acquisition_id']
        self.assertEqual('reserved',task.api_call(acquired,'{"warehouse":"fictional"}','call-one')['status'])
        self.service=self.make()
        result=task.api_recover('call-one')
        self.assertEqual('api_result_received',result['status'])
        self.assertEqual(7,json.loads(Path(result['path']).read_text())['count'])
        self.assertEqual(f.f.meta['license_text'],Path(result['license_path']).read_text(encoding='utf-8'))
        self.assertEqual(['execute','lookup'],seen);self.assertEqual(1,len(self.calls))


if __name__=='__main__':unittest.main()
