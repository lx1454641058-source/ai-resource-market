"""本项目原创。认证API机器入口、真实本机HTTP、客户端和任务MCP；无真实支付。"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from ai_supermarket.api_client import RemoteApiResources
from ai_supermarket.api_quota import checksum
from ai_supermarket.budget import BudgetLedger
from ai_supermarket.common import MarketError, compact
from ai_supermarket.http_app import MarketHTTP
from ai_supermarket.http_client import RemoteMarket
from ai_supermarket.identity import Identities
from ai_supermarket.remote_task import RemoteTaskResources, RemoteTaskInterface
from ai_supermarket.tests import test_api_product as fixture
from ai_supermarket.tests import test_http_app as http_fixture


class ApiRemoteTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ApiProductTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.root=self.f.root;self.host=self.f.host()
        self.identities=Identities(self.root/'identities.sqlite')
        self.identities.create_account('buyer','buyer',fixture.POLICY)
        self.issued=self.identities.issue_key('buyer');self.token=self.issued['token']
        self.credentials=self.root/'credentials.json'
        self.credentials.write_text(compact({'token':self.token}),encoding='utf-8')
        self.ledger=BudgetLedger(self.root/'budget.sqlite')
        self.ledger.create('root','owner',0,'original-test-authorization')
        self.ledger.grant_task('root','task','buyer')
        self.app=self.make_app()
        self.app.gateway.bind_key(self.issued['key_id'],'root','task','buyer','original-task-binding')
        self.acquired=self.f.acquire()['acquisition_id']
        self.payload={'acquisition_id':self.acquired,'request_id':'call-one','input':{'warehouse':'fictional'},
                      'expected_release_digest':self.f.digest}
        self.target={'id':'vendor:stock','version':'1.0.0','acquisition_id':self.acquired,
                     'release_digest':self.f.digest,'license_sha256':checksum(self.f.meta['license_text'])}

    def make_app(self,**changes):
        options={'allowed_hosts':['market.test:8765'],'budget_database':self.ledger.store.path,'enforce_task_budget':True,
                 'api_hosts':{self.issued['key_id']:{'provider-test':self.host}}}
        return MarketHTTP(self.f.path,self.identities.store.path,**{**options,**changes})

    def fetch(self,action='call',value=None,**kwargs):
        kwargs.setdefault('path','/v1/api/'+action)
        return asyncio.run(http_fixture.HTTPTests.request(self,self.payload if value is None else value,**kwargs))

    def live(self):
        import uvicorn
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        self.app.hosts=frozenset([f'127.0.0.1:{port}','market.test:8765'])
        server=uvicorn.Server(uvicorn.Config(self.app,host='127.0.0.1',port=port,http='h11',ws='none',
            proxy_headers=False,access_log=False,log_level='critical',lifespan='on',timeout_graceful_shutdown=5))
        thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
        def stop():
            server.should_exit=True;thread.join(8);sock.close()
            self.assertFalse(thread.is_alive())
        self.addCleanup(stop)
        deadline=time.monotonic()+5
        while not server.started and thread.is_alive() and time.monotonic()<deadline:time.sleep(.01)
        self.assertTrue(server.started)
        return RemoteMarket(f'http://127.0.0.1:{port}',self.credentials)

    def test_default_routes_closed_and_task_budget_is_required(self):
        self.app=self.make_app(api_hosts=None)
        self.assertEqual(404,self.fetch()[0])
        with self.assertRaises(MarketError):self.make_app(enforce_task_budget=False)
        self.assertEqual([],self.f.calls)

    def test_discovery_auth_and_fixed_task_provider_without_private_paths(self):
        code,reply,_=self.fetch(path='/v1/tools',method='GET')
        self.assertEqual(200,code)
        self.assertEqual('task',reply['resource_api']['task_id'])
        self.assertEqual('host_configured',reply['resource_api']['status'])
        self.assertEqual('/v1/api/result',reply['resource_api']['routes']['result'])
        self.assertNotIn(self.token,compact(reply));self.assertNotIn(str(self.root),compact(reply))
        self.assertEqual(401,self.fetch(token=False)[0])
        self.assertEqual([],self.f.calls)

    def test_missing_original_quota_is_not_recreated_by_request_host(self):
        self.assertEqual('api_call_completed', self.fetch()[1]['status'])
        path = self.host.quota.path
        path.rename(path.with_name('retained-original-quota.sqlite'))
        code, result, _ = self.fetch()
        self.assertGreaterEqual(code, 400, result)
        self.assertEqual('error', result['status'])
        self.assertFalse(path.exists())
        self.assertEqual(1, len(self.f.calls))

    def test_empty_original_quota_is_not_initialized_by_request_host(self):
        self.assertEqual('api_call_completed', self.fetch()[1]['status'])
        path = self.host.quota.path
        path.rename(path.with_name('retained-original-quota.sqlite'))
        path.write_bytes(b'')
        code, result, _ = self.fetch()
        self.assertGreaterEqual(code, 400, result)
        self.assertEqual('error', result['status'])
        self.assertEqual(b'', path.read_bytes())
        self.assertEqual(1, len(self.f.calls))

    def test_call_status_and_result_are_separate_and_result_never_dispatches(self):
        self.assertEqual(404,self.fetch('result')[0]);self.assertEqual([],self.f.calls)
        code,reply,_=self.fetch()
        self.assertEqual((200,'api_call_completed'),(code,reply['status']))
        self.assertNotIn('path',reply);self.assertNotIn('result',reply)
        self.assertFalse(reply['client_result_received'])
        self.assertEqual('api_call_completed',self.fetch('status')[1]['status'])
        code,result,_=self.fetch('result')
        self.assertEqual((200,'api_result'),(code,result['status']))
        self.assertEqual({'count':7,'warehouse':'fictional'},result['result'])
        self.assertEqual(self.f.meta['license_text'],result['license_text'])
        self.assertEqual(1,len(self.f.calls))

    def test_wrong_scope_and_payment_headers_do_not_dispatch(self):
        for change in ({'task_id':'other'},{'provider':'other'},{'buyer':'other'},
                       {'expected_release_digest':'b'*64},{'input':{'warehouse':'x','extra':'never send'}}):
            with self.subTest(change=change):self.assertGreaterEqual(self.fetch(value={**self.payload,**change})[0],400)
        self.assertEqual(400,self.fetch(headers=[(b'payment-proof',b'not-accepted')])[0])
        self.assertEqual([],self.f.calls)

    def test_wrong_host_task_or_policy_does_not_execute(self):
        self.host.task_id='other'
        self.assertEqual('api_host_mismatch',self.fetch()[1]['error_code'])
        self.assertEqual([],self.f.calls)

    def test_other_authenticated_buyer_cannot_use_original_entitlement(self):
        from ai_supermarket.exchange import Exchange
        self.identities.create_account('other','buyer',fixture.POLICY)
        issued=self.identities.issue_key('other')
        self.ledger.grant_task('root','task-2','other')
        self.app.gateway.bind_key(issued['key_id'],'root','task-2','other','other-authorized-task')
        other=Exchange(self.f.path,'other',host_policy=fixture.POLICY,clock=lambda:self.f.now)
        host=self.f.host(market=other,task_id='task-2',task_check=lambda *a:True)
        self.app.apis.hosts[issued['key_id']]={'provider-test':host}
        self.token=issued['token']
        self.assertEqual(404,self.fetch()[0]);self.assertEqual([],self.f.calls)

    def test_revoked_credential_or_binding_blocks_cached_results(self):
        self.fetch()
        self.app.gateway.revoke_binding(self.issued['key_id'])
        self.assertEqual(403,self.fetch('result')[0])
        self.identities.revoke_key(self.issued['key_id'])
        self.assertEqual(401,self.fetch('result')[0])
        self.assertEqual(1,len(self.f.calls))

    def test_paused_budget_denies_all_api_work_including_cached_result(self):
        self.fetch();self.ledger.set_state('root','paused',1)
        for action in ('call','status','result','recover'):
            with self.subTest(action=action):self.assertEqual(403,self.fetch(action)[0])
        self.assertEqual(1,len(self.f.calls))

    def test_revocation_during_execution_hides_result_and_retains_original_hold(self):
        def execute(*args):
            result=self.f.execute(*args);self.identities.revoke_key(self.issued['key_id']);return result
        self.host.execute=execute
        code,reply,_=self.fetch()
        self.assertEqual(401,code);self.assertNotIn('result',reply)
        self.assertEqual(1,len(self.f.calls));self.assertEqual(0,self.f.count('usage_events'))
        # Local fixture access, not an endpoint bypass, proves no completed result was recorded.
        self.assertEqual('reserved',self.f.host().status(self.acquired,{'warehouse':'fictional'},'call-one')['status'])

    def test_unknown_status_does_not_query_provider_until_explicit_recover(self):
        queried=[]
        def execute(*args):self.f.execute(*args);raise TimeoutError()
        def lookup(operation_id):queried.append(operation_id);return self.f.lookup(operation_id)
        self.host.execute=execute;self.host.lookup=lookup
        code,reply,_=self.fetch()
        self.assertEqual((202,'reserved'),(code,reply['status']))
        self.fetch('status');self.fetch('result');self.assertEqual([],queried)
        self.assertEqual('api_call_completed',self.fetch('recover')[1]['status'])
        self.assertEqual((1,1),(len(self.f.calls),len(queried)))

    def test_real_http_client_receives_files_and_rechecks_auth_on_cache(self):
        remote=self.live();client=RemoteApiResources(remote,'task','personal_use',self.root/'client')
        result=client.invoke(self.target,{'warehouse':'fictional'},'client-call')
        self.assertEqual('api_result_received',result['status']);self.assertNotIn('result',result)
        self.assertEqual(7,json.loads(Path(result['path']).read_text(encoding='utf-8'))['count'])
        self.assertEqual(result,client.receive('client-call'))
        self.identities.revoke_key(self.issued['key_id'])
        self.assertEqual('unauthenticated',client.receive('client-call')['error_code'])
        self.assertEqual(1,len(self.f.calls))

    def test_lost_http_response_recovers_original_without_new_provider_call(self):
        remote=self.live();client=RemoteApiResources(remote,'task','personal_use',self.root/'client')
        original=remote.request
        def lost(path,value=None):
            reply=original(path,value)
            if path=='/v1/api/call':raise MarketError('transport_unknown','fixture lost response')
            return reply
        with patch.object(remote,'request',side_effect=lost),self.assertRaises(MarketError):
            client.invoke(self.target,{'warehouse':'fictional'},'lost')
        restarted=RemoteApiResources(remote,'task','personal_use',self.root/'client')
        self.assertEqual('api_call_completed',restarted.status('lost')['status'])
        self.assertEqual('api_result_received',restarted.receive('lost')['status'])
        self.assertEqual(1,len(self.f.calls))

    def test_client_rejects_wrong_scope_input_license_and_result_digest(self):
        remote=self.live();client=RemoteApiResources(remote,'task','personal_use',self.root/'client')
        client.invoke(self.target,{'warehouse':'fictional'},'one');original=remote.request
        def change_manifest(reply):reply['manifest']['task_id']='other'
        def change_input(reply):reply['input_sha256']='b'*64
        def change_license(reply):reply['license_text']='wrong license'
        def change_body(reply):reply['result']['count']=100
        for change in (change_manifest,change_input,change_license,change_body):
            def changed(path,value):
                reply=original(path,value);change(reply);return reply
            with self.subTest(change=change.__name__),patch.object(remote,'request',side_effect=changed),self.assertRaises(MarketError):
                client.receive('one')
        self.assertEqual(1,len(self.f.calls))

    def test_client_call_limit_survives_restart_and_changed_intent_is_refused(self):
        remote=self.live();client=RemoteApiResources(remote,'task','personal_use',self.root/'client',max_calls=2)
        client.invoke(self.target,{'warehouse':'fictional'},'one')
        restarted=RemoteApiResources(remote,'task','personal_use',self.root/'client',max_calls=2)
        with self.assertRaises(MarketError):restarted.status('one')
        with self.assertRaises(MarketError):restarted.invoke(self.target,{'warehouse':'changed'},'one')
        with self.assertRaises(MarketError):RemoteApiResources(remote,'task','personal_use',self.root/'client',max_calls=3)
        self.assertEqual(1,len(self.f.calls))

    def test_client_preserves_changed_local_output_without_another_provider_call(self):
        remote=self.live();client=RemoteApiResources(remote,'task','personal_use',self.root/'client')
        result=client.invoke(self.target,{'warehouse':'fictional'},'one')
        Path(result['path']).write_text('user changed',encoding='utf-8')
        with self.assertRaises(MarketError):client.receive('one')
        self.assertEqual('user changed',Path(result['path']).read_text(encoding='utf-8'))
        self.assertEqual(1,len(self.f.calls))

    def test_task_entry_initial_search_selection_call_and_feedback_scope(self):
        remote=self.live();output=self.root/'task-client'
        output.mkdir()
        task=RemoteTaskResources(remote,'task','仓库查询','personal_use',output,mode='api')
        interface=RemoteTaskInterface(task)
        self.assertEqual('vendor:stock',task.initial['items'][0]['id'])
        selected={'id':'vendor:stock','version':'1.0.0'}
        self.assertEqual('resolved',interface.call('market_resolve',selected)['status'])
        acquired=interface.call('market_use',{**selected,'request_id':'choose'})
        self.assertEqual('market_api_call',acquired['next_action'])
        reply=interface.call('market_api_call',{'acquisition_id':acquired['acquisition_id'],
            'input_json':'{"warehouse":"fictional"}','request_id':'query'})
        self.assertEqual('api_result_received',reply['status'],reply)
        self.assertNotIn('market_receipt',{t['name'] for t in interface.tool_list()})
        self.assertEqual(6,len(list((task.directory/'calls').iterdir())))
        restarted=RemoteTaskResources(remote,'task','仓库查询','personal_use',output,mode='api')
        self.assertEqual(task.initial,restarted.initial)
        self.assertEqual(1,len(self.f.calls))

    def test_task_rejects_unselected_right_extra_fields_and_does_not_spend(self):
        (self.root/'task-client').mkdir()
        remote=self.live();task=RemoteTaskResources(remote,'task','仓库查询','personal_use',self.root/'task-client',mode='api')
        api=RemoteTaskInterface(task)
        reply=api.call('market_api_call',{'acquisition_id':self.acquired,'input_json':'{}','request_id':'one'})
        self.assertEqual('error',reply['status']);self.assertEqual([],self.f.calls)
        self.assertEqual('unknown_tool',api.call('seller_draft',{})['error_code'])

    def test_cli_mcp_api_mode_real_subprocess_discovers_and_calls_api(self):
        remote=self.live();goal=self.root/'goal.txt';goal.write_text('仓库查询',encoding='utf-8')
        (self.root/'cli').mkdir()
        request='\n'.join(compact(value) for value in [
            {'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'test','version':'1'}}},
            {'jsonrpc':'2.0','method':'notifications/initialized'},
            {'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'market_resolve','arguments':{'id':'vendor:stock','version':'1.0.0'}}},
            {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'market_use','arguments':{'id':'vendor:stock','version':'1.0.0','request_id':'choose'}}},
            {'jsonrpc':'2.0','id':5,'method':'tools/call','params':{'name':'market_api_call','arguments':{
                'acquisition_id':self.acquired,'input_json':'{"warehouse":"fictional"}','request_id':'cli-call'}}}])+'\n'
        cli=Path(__file__).resolve().parents[1]/'remote_cli.py'
        proc=subprocess.run([sys.executable,'-X','utf8','-B',str(cli),
            '--url',remote.base_url,'--credential-file',str(self.credentials),'task-mcp','--task-id','task',
            '--purpose','personal_use','--goal-file',str(goal),'--output-root',str(self.root/'cli'),'--mode','api'],
            input=request,text=True,encoding='utf-8',capture_output=True,timeout=20,cwd=self.root)
        self.assertEqual(0,proc.returncode,proc.stderr)
        replies=[json.loads(line) for line in proc.stdout.splitlines()]
        names={tool['name'] for reply in replies if reply.get('id')==2 for tool in reply['result']['tools']}
        self.assertIn('market_api_call',names);self.assertIn('market_api_recover',names)
        self.assertNotIn(self.token,proc.stdout)
        result=next(reply['result']['structuredContent'] for reply in replies if reply.get('id')==5)
        self.assertEqual('api_result_received',result['status'],result)
        self.assertEqual(7,json.loads(Path(result['path']).read_text(encoding='utf-8'))['count'])
        self.assertEqual(1,len(self.f.calls))


if __name__=='__main__':unittest.main()
