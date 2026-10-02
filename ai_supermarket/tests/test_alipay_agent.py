"""本项目原创。官方付款交接准备器隔离测试，未调用CLI/钱包/网络。"""
from dataclasses import FrozenInstanceError, replace
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from ai_supermarket.alipay_agent import (prepare_alipay_agent, execute_alipay_agent,
                                       CLI_VERSION, CLI_SHA256, _verify_executable, _capture_process)
from ai_supermarket.common import MarketError, compact
from ai_supermarket.http_client import RemoteMarket
from ai_supermarket.purchase_download import PurchaseDownloader, _save, sha
from ai_supermarket.tests.test_purchase_download import Response


class AlipayAgentPreparationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.token='ak_'+'1'*32+'.'+'x'*43
        credential=self.root/'credential.json'
        credential.write_text(compact({'token':self.token}),encoding='utf-8')
        self.remote=RemoteMarket('https://market.example',credential)
        output=self.root/'downloads';output.mkdir()
        self.client=PurchaseDownloader(self.remote,'task','personal_use',output)
        self.bill='ZmljdGlvbmFsLXBheW1lbnQ='  # Original synthetic header, not a real bill.
        self.result={'status':'payment_needed','request_id':'buy','release_digest':'a'*64,'order_id':'order'}
        self.remote.opener=Mock()
        self.remote.opener.open.side_effect=lambda *a,**k:Response(compact(self.result).encode(),402,
            [('Content-Type','application/json'),('Payment-Needed',self.bill)])
        self.args=('fixture:item','1.0.0','buy','a'*64)
        self.goal='为纸舟整理资料，保留 "引号"、$原文和`符号`\n并记录许可'

    def fetch(self):return self.client.resource(*self.args)

    def prepare(self,snapshot,**kwargs):
        return prepare_alipay_agent(snapshot,user_goal=kwargs.pop('user_goal',self.goal),
            session_id=kwargs.pop('session_id','session-1'),directory=kwargs.pop('directory',self.root/'payment'),**kwargs)

    def test_exact_sent_request_is_frozen_and_not_exposed_in_repr(self):
        response=self.fetch();snapshot=response.request_snapshot
        request=self.remote.opener.open.call_args.args[0]
        self.assertEqual(request.full_url,snapshot.url)
        self.assertEqual(request.get_method(),snapshot.method)
        self.assertEqual(request.data,snapshot.body)
        self.assertEqual(tuple(request.header_items()),snapshot.headers)
        self.assertEqual(self.bill.encode('ascii'),snapshot.payment_needed)
        self.assertEqual(('task','personal_use','order','buy','a'*64),
            (snapshot.task_id,snapshot.purpose,snapshot.order_id,snapshot.request_id,snapshot.release_digest))
        for representation in (repr(response),repr(snapshot)):
            self.assertNotIn(self.token,representation);self.assertNotIn(self.bill,representation)
        with self.assertRaises(FrozenInstanceError):snapshot.url='https://other.example/v1/purchases/resource'
        request.data=b'changed';request.add_header('Authorization','changed')
        self.assertNotEqual(request.data,snapshot.body)
        self.assertIn(('Authorization','Bearer '+self.token),snapshot.headers)

    def test_preserves_original_bill_post_headers_and_goal_without_secret_persistence(self):
        snapshot=self.fetch().request_snapshot
        prepared=self.prepare(snapshot,bill_basename='402_needed_round-1.txt')
        args=prepared.argv
        self.assertEqual('402-buyer-pay',args[0])
        self.assertEqual(snapshot.url,args[args.index('-r')+1])
        self.assertEqual('POST',args[args.index('-m')+1])
        self.assertEqual(snapshot.body,args[args.index('-d')+1].encode('utf-8'))
        self.assertEqual('原始请求：'+self.goal,args[args.index('--intent-summary')+1])
        actual_headers=[args[index+1] for index,value in enumerate(args) if value=='-H']
        self.assertEqual([name+':'+value for name,value in snapshot.headers],actual_headers)
        folder=Path(prepared.cwd)
        self.assertEqual(snapshot.payment_needed,(folder/'402_needed_round-1.txt').read_bytes())
        manifest=json.loads(Path(prepared.manifest_path).read_bytes())
        self.assertEqual('order',manifest['binding']['order_id'])
        self.assertEqual('prepared_not_executed',manifest['status'])
        self.assertFalse(manifest['payment_executed']);self.assertFalse(prepared.execution_authorized)
        for value in (self.token,self.bill,self.goal,compact(list(args))):
            self.assertNotIn(value,Path(prepared.manifest_path).read_text(encoding='utf-8'))
            self.assertNotIn(value,repr(prepared))
        for file in folder.iterdir():self.assertNotIn(self.token.encode(),file.read_bytes())
        self.assertEqual(1,self.remote.opener.open.call_count)

    def test_only_first_resource_without_proof_has_snapshot(self):
        self.assertIsNotNone(self.fetch().request_snapshot)
        self.assertIsNone(self.fetch().request_snapshot)
        restarted=PurchaseDownloader(self.remote,'task','personal_use',self.client.root)
        self.assertIsNone(restarted.resource(*self.args).request_snapshot)

    def test_concurrent_first_intent_has_only_one_snapshot(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        barrier=threading.Barrier(2)
        def competing_save(path,raw):
            if Path(path).name=='intent.json':barrier.wait(timeout=5)
            return _save(path,raw)
        with patch('ai_supermarket.purchase_download._save',side_effect=competing_save):
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses=list(pool.map(lambda _:self.fetch(),range(2)))
        self.assertEqual(1,sum(response.request_snapshot is not None for response in responses))

    def test_handoff_has_bill_but_no_payment_snapshot_and_cannot_be_prepared(self):
        response=self.client.handoff(*self.args)
        self.assertEqual(self.bill,response.payment_needed);self.assertIsNone(response.request_snapshot)
        with self.assertRaises(MarketError) as caught:self.prepare(response.request_snapshot)
        self.assertEqual('original_request_required',caught.exception.code)
        self.assertFalse((self.root/'payment').exists())

    def test_proof_bearing_402_does_not_create_another_payment_snapshot(self):
        import base64
        proof=base64.b64encode(compact({'protocol':{'trade_no':'trade','payment_proof':'proof'}}).encode()).decode()
        response=self.client.resource(*self.args,payment_proof=proof)
        self.assertIsNone(response.request_snapshot)

    def test_invalid_and_nonresource_endpoints_refused_before_files(self):
        snapshot=self.fetch().request_snapshot
        endpoints=['http://market.example/v1/purchases/resource','https://localhost/v1/purchases/resource',
            'https://127.0.0.1/v1/purchases/resource','https://10.0.0.1/v1/purchases/resource',
            'https://user:secret@market.example/v1/purchases/resource',
            'https://market.example/v1/purchases/handoff','https://market.example/v1/purchases/resource?changed=1',
            'https://market.example/v1/purchases/resource#fragment','https://market.example:0/v1/purchases/resource']
        for url in endpoints:
            with self.subTest(url=url),self.assertRaises(MarketError):self.prepare(replace(snapshot,url=url))
        self.assertFalse((self.root/'payment').exists())

    def test_changed_body_identity_or_proof_header_refused(self):
        snapshot=self.fetch().request_snapshot
        changes=[{'body':b'{}'},{'request_id':'other'},{'release_digest':'b'*64},{'key_id':'ak_'+'2'*32},
                 {'method':'GET'},{'payment_needed':b'bad\nheader'},
                 {'headers':snapshot.headers+(('Payment-Proof','proof'),)},
                 {'headers':snapshot.headers+(('x-custom','bad\r\nheader'),)}]
        for change in changes:
            with self.subTest(field=next(iter(change))),self.assertRaises(MarketError):self.prepare(replace(snapshot,**change))
        self.assertFalse((self.root/'payment').exists())

    def test_goal_session_and_basename_validation_preserve_no_files(self):
        snapshot=self.fetch().request_snapshot
        for goal in ('','   ','未知','未明确','unknown','bad\x00goal'):
            with self.subTest(goal=goal),self.assertRaises(MarketError):self.prepare(snapshot,user_goal=goal)
        for name in ('../bill','x/y','C:\\bill','..','manifest.json','CON.txt','a.','a;cmd.txt'):
            with self.subTest(name=name),self.assertRaises(MarketError):self.prepare(snapshot,bill_basename=name)
        with self.assertRaises(MarketError):self.prepare(snapshot,session_id='-bad')
        self.assertFalse((self.root/'payment').exists())

    def test_duplicate_directory_preserves_original_files_and_session(self):
        snapshot=self.fetch().request_snapshot;prepared=self.prepare(snapshot)
        before={file.name:file.read_bytes() for file in Path(prepared.cwd).iterdir()}
        with self.assertRaises(MarketError) as caught:self.prepare(snapshot,session_id='session-2')
        self.assertEqual('output_conflict',caught.exception.code)
        self.assertEqual(before,{file.name:file.read_bytes() for file in Path(prepared.cwd).iterdir()})

    def test_missing_parent_and_existing_empty_directory_are_not_overwritten(self):
        snapshot=self.fetch().request_snapshot
        with self.assertRaises(MarketError):self.prepare(snapshot,directory=self.root/'missing'/'child')
        empty=self.root/'empty';empty.mkdir()
        with self.assertRaises(MarketError):self.prepare(snapshot,directory=empty)
        self.assertEqual([],list(empty.iterdir()))


class FakeProcess:
    """Pipe stand-in only. No executable is ever started in these tests."""
    def __init__(self,stdout=b'opaque result',stderr=b'',returncode=0,hang=False):
        self.stdout=io.BytesIO(stdout);self.stderr=io.BytesIO(stderr)
        self.returncode=returncode;self.hang=hang;self.killed=False
    def poll(self):return None if self.hang and not self.killed else self.returncode
    def kill(self):self.killed=True;self.hang=False
    def wait(self,timeout=None):
        if self.hang:raise subprocess.TimeoutExpired('fixture',timeout)
        return self.returncode


class AlipayAgentExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture=AlipayAgentPreparationTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.root;self.snapshot=self.fixture.fetch().request_snapshot
        self.goal=self.fixture.goal;self.executable=self.root/'alipay-bot.exe'
        self.executable.write_bytes(b'MZsynthetic-NEVER-EXECUTE')
        self.execution_root=self.root/'execution';self.execution_root.mkdir()
        self.authorization={key:getattr(self.snapshot,key) for key in ('url','task_id','purpose','order_id',
            'request_id','key_id','resource_id','version','release_digest')}
        self.authorization.update(bill_sha256=sha(self.snapshot.payment_needed),user_goal_sha256=sha(self.goal.encode()),
            amount_cents=1,currency='CNY',payee='fictional-merchant',expires_at=int(time.time())+300,
            authorization_ref='fixture:authorization',quote_ref='fixture:quote',
            executable_path=str(self.executable.absolute()),compatibility={'cli_version':CLI_VERSION,
                'cli_sha256':CLI_SHA256,'redirect_policy_evidence_ref':'fixture:redirect',
                'authorization_forwarding_evidence_ref':'fixture:headers','zip_delivery_evidence_ref':'fixture:zip'})

    def execute(self,**overrides):
        options={'user_goal':self.goal,'session_id':'session-1','directory':self.root/'run-1',
                 'execution_root':self.execution_root,'executable':self.executable,
                 'authorization':self.authorization,'timeout':1}
        options.update(overrides)
        return execute_alipay_agent(self.snapshot,**options)

    def test_fixed_binary_verifier_rejects_fake_changed_suffix_and_link_path(self):
        with self.assertRaises(MarketError):_verify_executable(self.executable)
        text_file=self.root/'alipay-bot.cmd';text_file.write_bytes(b'MZ')
        with self.assertRaises(MarketError):_verify_executable(text_file)
        with patch.object(Path,'is_symlink',return_value=True),self.assertRaises(MarketError):
            _verify_executable(self.executable)
        with patch('ai_supermarket.alipay_agent.subprocess.Popen') as popen:
            with self.assertRaises(MarketError):self.execute()
            popen.assert_not_called()
        self.assertEqual([],list(self.execution_root.iterdir()))

    def test_explicit_one_time_execution_hides_secrets_and_exit_zero_is_not_paid(self):
        private=b'opaque '+self.fixture.token.encode()
        process=FakeProcess(private,b'private stderr')
        with patch('ai_supermarket.alipay_agent._verify_executable',return_value=(1,2,3,4)),\
             patch('ai_supermarket.alipay_agent.subprocess.Popen',return_value=process) as popen:
            result=self.execute(allowed_env={'AIPAY_MODEL':'fixture-model'})
        args,kwargs=popen.call_args
        self.assertEqual(str(self.executable),args[0][0]);self.assertEqual('402-buyer-pay',args[0][1])
        self.assertEqual({'AIPAY_MODEL':'fixture-model'},kwargs['env']);self.assertFalse(kwargs['shell'])
        self.assertEqual(subprocess.DEVNULL,kwargs['stdin'])
        if os.name=='nt':self.assertEqual(subprocess.CREATE_NO_WINDOW,kwargs['creationflags'])
        self.assertEqual(private,result.stdout);self.assertEqual('unknown',result.payment_status)
        self.assertFalse(result.resource_received);self.assertNotIn(self.fixture.token,repr(result))
        for file in self.execution_root.iterdir():
            raw=file.read_text(encoding='utf-8');self.assertNotIn(self.fixture.token,raw)
            self.assertNotIn('private stderr',raw);self.assertNotIn(self.goal,raw)
        record=json.loads(Path(result.record_path).read_bytes())
        self.assertEqual(0,record['returncode']);self.assertEqual('unknown',record['payment_status'])

    def test_duplicate_session_directory_and_restart_cannot_repay(self):
        with patch('ai_supermarket.alipay_agent._verify_executable',return_value=(1,2,3,4)),\
             patch('ai_supermarket.alipay_agent.subprocess.Popen',return_value=FakeProcess()) as popen:
            self.execute()
            with self.assertRaises(MarketError) as caught:
                self.execute(session_id='other-session',directory=self.root/'run-2')
            self.assertEqual('payment_execution_already_claimed',caught.exception.code)
            self.assertEqual(1,popen.call_count)

    def test_concurrent_same_order_only_one_launch_different_order_separate(self):
        from concurrent.futures import ThreadPoolExecutor
        def invoke(index):
            try:return self.execute(session_id='session-'+str(index),directory=self.root/('run-'+str(index))).status
            except MarketError as error:return error.code
        with patch('ai_supermarket.alipay_agent._verify_executable',return_value=(1,2,3,4)),\
             patch('ai_supermarket.alipay_agent.subprocess.Popen',side_effect=lambda *a,**k:FakeProcess()) as popen:
            with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(invoke,(1,2)))
            self.assertEqual(1,popen.call_count)
            self.assertIn('payment_execution_already_claimed',results)
            self.snapshot=replace(self.snapshot,order_id='order-2')
            self.authorization['order_id']='order-2'
            self.execute(directory=self.root/'different-order')
            self.assertEqual(2,popen.call_count)

    def test_unknown_start_failure_is_recorded_without_exception_secret_or_retry(self):
        with patch('ai_supermarket.alipay_agent._verify_executable',return_value=(1,2,3,4)),\
             patch('ai_supermarket.alipay_agent.subprocess.Popen',side_effect=OSError(self.fixture.token)) as popen:
            result=self.execute();self.assertEqual('not_started',result.status)
            self.assertEqual('unknown',result.payment_status)
            with self.assertRaises(MarketError):self.execute(directory=self.root/'try-again')
            self.assertEqual(1,popen.call_count)
        self.assertNotIn(self.fixture.token,Path(result.record_path).read_text())

    def test_timeout_kills_process_keeps_unknown_and_does_not_retry(self):
        process=FakeProcess(hang=True)
        with patch('ai_supermarket.alipay_agent._verify_executable',return_value=(1,2,3,4)),\
             patch('ai_supermarket.alipay_agent.subprocess.Popen',return_value=process):
            result=self.execute(timeout=.02)
        self.assertTrue(process.killed);self.assertTrue(result.timed_out)
        self.assertEqual('execution_unknown',result.status);self.assertEqual('unknown',result.payment_status)

    def test_output_limit_kills_and_keeps_only_bounded_opaque_bytes(self):
        process=FakeProcess(stdout=b'x'*8192,stderr=b'y'*8192,hang=True)
        with patch('ai_supermarket.alipay_agent._verify_executable',return_value=(1,2,3,4)),\
             patch('ai_supermarket.alipay_agent.subprocess.Popen',return_value=process):
            result=self.execute(max_output_bytes=128)
        self.assertTrue(process.killed);self.assertTrue(result.output_limited)
        self.assertLessEqual(len(result.stdout)+len(result.stderr),128)
        self.assertEqual('unknown',result.payment_status)

    def test_scope_price_expiry_and_compatibility_required_before_launch(self):
        changes=[{'bill_sha256':'0'*64},{'url':'https://other.example/v1/purchases/resource'},
            {'task_id':'other'},{'order_id':'other'},{'request_id':'other'},{'key_id':'other'},
            {'resource_id':'other'},{'version':'other'},{'release_digest':'b'*64},
            {'user_goal_sha256':'0'*64},{'amount_cents':True},{'amount_cents':0},{'currency':'USD'},
            {'payee':''},{'expires_at':int(time.time())-1},{'authorization_ref':True},
            {'quote_ref':'pending'},{'compatibility':False},{'executable_path':str(self.root/'changed.exe')}]
        with patch('ai_supermarket.alipay_agent.subprocess.Popen') as popen:
            for change in changes:
                auth={**self.authorization,**change}
                with self.subTest(field=next(iter(change))),self.assertRaises(MarketError):self.execute(authorization=auth)
            auth={**self.authorization,'compatibility':{**self.authorization['compatibility'],
                'redirect_policy_evidence_ref':False}}
            with self.assertRaises(MarketError):self.execute(authorization=auth)
            popen.assert_not_called()
        self.assertEqual([],list(self.execution_root.iterdir()))

    def test_no_environment_inheritance_invalid_timeout_and_unknown_env_rejected(self):
        with patch('ai_supermarket.alipay_agent.subprocess.Popen') as popen:
            for timeout in (0,61,True,float('nan')):
                with self.subTest(timeout=timeout),self.assertRaises(MarketError):self.execute(timeout=timeout)
            with self.assertRaises(MarketError):self.execute(allowed_env={'PATH':'unapproved'})
            popen.assert_not_called()

    def test_changed_binary_during_preparation_cannot_launch_and_claim_stays(self):
        with patch('ai_supermarket.alipay_agent._verify_executable',side_effect=[(1,2,3,4),(1,2,3,5)]),\
             patch('ai_supermarket.alipay_agent.subprocess.Popen') as popen:
            result=self.execute();popen.assert_not_called()
        self.assertEqual('not_started',result.status)
        self.assertEqual(2,len(list(self.execution_root.iterdir())))

    def test_existing_crash_claim_without_result_refuses_new_launch(self):
        from ai_supermarket import alipay_agent as agent
        original=agent._exclusive_json
        def stopped_after_claim(path,value):
            original(path,value)
            raise OSError('simulated interruption after persisted intent')
        with patch('ai_supermarket.alipay_agent._verify_executable',return_value=(1,2,3,4)),\
             patch('ai_supermarket.alipay_agent.subprocess.Popen') as popen:
            with patch('ai_supermarket.alipay_agent._exclusive_json',side_effect=stopped_after_claim):
                with self.assertRaises(MarketError):self.execute()
            with self.assertRaises(MarketError) as caught:self.execute(directory=self.root/'restart')
            self.assertEqual('payment_execution_already_claimed',caught.exception.code)
            popen.assert_not_called()
        self.assertEqual(1,len(list(self.execution_root.iterdir())))

    def test_reader_exception_is_opaque_unknown_and_does_not_print_secrets(self):
        process=FakeProcess(hang=True)
        process.stdout=Mock();process.stdout.read.side_effect=RuntimeError(self.fixture.token)
        with patch('ai_supermarket.alipay_agent._verify_executable',return_value=(1,2,3,4)),\
             patch('ai_supermarket.alipay_agent.subprocess.Popen',return_value=process):
            result=self.execute()
        self.assertTrue(process.killed);self.assertEqual('execution_unknown',result.status)
        self.assertNotIn(self.fixture.token,repr(result))
        self.assertNotIn(self.fixture.token,Path(result.record_path).read_text())

    def test_real_local_python_pipe_is_bounded_and_direct_child_terminated(self):
        # This executes only Python-generated bytes locally, never the official CLI or network.
        flags=getattr(subprocess,'CREATE_NO_WINDOW',0) if os.name=='nt' else 0
        process=subprocess.Popen([sys.executable,'-X','utf8','-B','-c',
            "import sys,time;sys.stdout.buffer.write(b'x'*65536);sys.stdout.flush();time.sleep(10)"],
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            shell=False,env={},bufsize=0,creationflags=flags)
        try:
            code,timed_out,limited,unknown,stdout,stderr=_capture_process(process,2,4096)
            self.assertTrue(limited);self.assertFalse(timed_out)
            self.assertIsNotNone(process.poll());self.assertIsNotNone(code)
            self.assertLessEqual(len(stdout)+len(stderr),4096)
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=2)


if __name__=='__main__':unittest.main()
