"""来源：本项目原创。任务入口与真实本机接入；支付响应仅为合成夹具。"""
import csv
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from ai_supermarket.common import MarketError, compact
from ai_supermarket.purchase_download import PurchaseRequestSnapshot, PurchaseResponse
from ai_supermarket.remote_task import RemoteTaskResources, RemoteTaskInterface
from ai_supermarket.tests import test_purchase_download as download_fixture
from ai_supermarket.tests import test_machine_pay_http as machine_fixture


class TaskEntryTests(unittest.TestCase):
    def setUp(self):
        self.f=download_fixture.DownloadClientTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.remote=self.f.remote
        self.remote.tool_list=Mock(return_value={'role':'buyer','bound_task_id':'task',
            'purchase_api':{'status':'host_configured','task_id':'task','purpose':'personal_use'}})
        self.remote.call=Mock(return_value={'status':'no_match','items':[]})

    def create(self,goal='整理资料\n制作报告',**kwargs):
        return RemoteTaskResources(self.remote,'task',goal,'personal_use',self.f.output,**kwargs)

    def pin(self,host):
        self.remote.call.return_value={'status':'resolved','id':'fixture:table','version':'1.0.0',
            'release_digest':'a'*64,'compatibility':{'status':'ready'}}
        return host.resolve('fixture:table','1.0.0')

    def test_startup_search_once_including_restart_and_short_summary(self):
        first=self.create();again=self.create()
        self.assertEqual(first.initial,again.initial)
        self.assertEqual(1,self.remote.tool_list.call_count);self.assertEqual(1,self.remote.call.call_count)
        self.remote.call.assert_called_with('market_search',{'q':'整理资料 制作报告','purpose':'personal_use','limit':3})
        self.assertIn('initial_discovery',RemoteTaskInterface(first).instructions)
        self.assertEqual(2,len(list((first.directory/'calls').iterdir())))

    def test_binding_cannot_change_goal_or_reset_call_cap(self):
        self.create(max_calls=2)
        for kwargs in ({'goal':'换目标','max_calls':2},{'max_calls':3}):
            with self.assertRaises(MarketError):self.create(**kwargs)
        host=self.create(max_calls=2)
        with self.assertRaises(MarketError) as caught:host.search('补查')
        self.assertEqual('task_call_limit',caught.exception.code)
        self.assertEqual(1,self.remote.call.call_count)

    def test_failed_initial_probe_is_not_retried_on_restart(self):
        self.remote.call.side_effect=MarketError('transport_unknown','虚构网络失败')
        first=self.create();self.create()
        self.assertEqual('transport_unknown',first.initial['error_code'])
        self.assertEqual(1,self.remote.call.call_count)

    def test_concurrent_probe_claim_runs_operation_only_once(self):
        host=self.create();entered=threading.Event();release=threading.Event();results=[]
        def operation():entered.set();release.wait(5);return {'status':'ok'}
        worker=threading.Thread(target=lambda:results.append(host._once('fixture-probe',operation)))
        worker.start();self.assertTrue(entered.wait(5))
        try:
            unknown=host._once('fixture-probe',lambda: self.fail('duplicate probe'))
            self.assertEqual('task_probe_unknown',unknown['error_code'])
        finally:release.set();worker.join(5)
        self.assertEqual([{'status':'ok'}],results)

    def test_wrong_remote_task_stops_before_search(self):
        self.remote.tool_list.return_value['bound_task_id']='other'
        host=self.create();self.assertEqual('error',host.connection['status'])
        self.assertEqual(0,self.remote.call.call_count)
        with self.assertRaises(MarketError):host.search('资料')

    def test_resolve_required_and_changed_release_preserves_original(self):
        host=self.create()
        with self.assertRaises(MarketError) as caught:host.use('fixture:table','1.0.0','read')
        self.assertEqual('resolve_required',caught.exception.code)
        self.pin(host);self.remote.call.return_value['release_digest']='b'*64
        with self.assertRaises(MarketError):host.resolve('fixture:table','1.0.0')
        self.assertEqual('a'*64,host._pin('fixture:table','1.0.0')['release_digest'])

    def test_configured_purchase_resolve_routes_to_use_without_payment_authorization(self):
        host=self.create()
        detail={'status':'resolved','id':'fixture:table','version':'1.0.0','release_digest':'a'*64,
                'format':'asset','price_cents':1,'owned':False,'compatibility':{'status':'ready'},
                'next_action':'payment_unavailable'}
        self.remote.call.return_value=detail
        with patch.object(host.downloader,'resource') as purchase:
            result=host.resolve('fixture:table','1.0.0')
            purchase.assert_not_called()
        self.assertEqual('market_use',result['next_action'])
        self.assertEqual('trusted_host_payment_handoff',result['payment_next_action'])
        self.assertFalse(result['wallet_payment_authorized']);self.assertFalse(result['payment_sent'])
        self.assertEqual('payment_unavailable',detail['next_action'])  # Do not alter ordinary catalog response.
        self.assertEqual('a'*64,host._pin('fixture:table','1.0.0')['release_digest'])

    def test_resolve_hint_keeps_incompatible_nonpurchase_and_unconfigured_rejections(self):
        detail={'status':'resolved','id':'fixture:table','version':'1.0.0','release_digest':'a'*64,
                'format':'asset','price_cents':1,'owned':False,'compatibility':{'status':'ready'},
                'next_action':'payment_unavailable'}
        host=self.create(max_calls=30)
        for change,expected in [({'compatibility':{'status':'missing_capabilities'},
                                 'next_action':'stop_or_choose_other_resource'},'stop_or_choose_other_resource'),
                                ({'price_cents':0},'payment_unavailable'),
                                ({'owned':True,'next_action':'read'},'read'),
                                ({'format':'api'},'requires_other_host_delivery_mode')]:
            self.remote.call.return_value={**detail,**change}
            result=host.resolve('fixture:table','1.0.0')
            self.assertEqual(expected,result['next_action']);self.assertNotIn('payment_next_action',result)
        for mode in ('free_text','api','purchase'):
            output=self.f.root/('hint-'+mode);output.mkdir()
            descriptor={'role':'buyer','bound_task_id':'task','task_budget_required':True}
            if mode=='api':descriptor['resource_api']={'status':'host_configured','version':1,
                'task_id':'task','purpose':'personal_use','providers':['fixture']}
            self.remote.tool_list.return_value=descriptor
            self.remote.call.return_value={'status':'no_match','items':[]}
            entry=RemoteTaskResources(self.remote,'task','资料','personal_use',output,mode=mode)
            self.remote.call.return_value=detail
            if mode=='purchase':
                with self.assertRaises(MarketError):entry.resolve('fixture:table','1.0.0')
            else:
                result=entry.resolve('fixture:table','1.0.0')
                self.assertEqual('requires_other_host_delivery_mode',result['next_action'])
                self.assertNotIn('payment_next_action',result)

    def test_model_surface_cannot_supply_proof_task_or_endpoint(self):
        interface=RemoteTaskInterface(self.create())
        self.assertEqual(6,len(interface.tool_list()))
        for key in ('payment_proof','task_id','purpose','endpoint','output_root'):
            result=interface.call('market_use',{'id':'fixture:table','version':'1.0.0','request_id':'read',key:'other'})
            self.assertEqual('invalid_params',result['error_code'])
        self.assertEqual('unknown_tool',interface.call('resume_payment',{})['error_code'])
        self.assertEqual('unknown_tool',interface.call('receive_after_agent_payment',{})['error_code'])
        self.assertEqual('unknown_tool',interface.call('execute_agent_payment',{})['error_code'])
        self.assertEqual('unknown_tool',interface.call([], {})['error_code'])

    def reconciliation_host(self):
        host=self.create();self.pin(host)
        with patch.object(host.downloader,'resource',return_value=PurchaseResponse(
                {'status':'payment_needed','request_id':'read'},'synthetic-bill')):
            host.use('fixture:table','1.0.0','read')
        return host

    def test_agent_pending_or_unknown_does_not_download_or_repay(self):
        host=self.reconciliation_host()
        pending={'status':'purchase_pending','request_id':'read','order_id':'po_fixture',
                 'paid_verified':False,'entitlement_created':False}
        with patch.object(self.remote,'request',return_value=pending), patch.object(host.downloader,'resource') as fetch:
            result=host.receive_after_agent_payment('read',expected_order_id='po_fixture')
            self.assertEqual('host_reconcile_original_payment_no_repay',result['next_action'])
            fetch.assert_not_called()
        self.assertNotIn('read',host.wallet_handoffs)
        with patch.object(self.remote,'request',side_effect=MarketError('transport_unknown','synthetic')):
            with self.assertRaises(MarketError):host.receive_after_agent_payment('read',expected_order_id='po_fixture')

    def test_agent_wrong_order_or_status_types_reject_before_download(self):
        host=self.reconciliation_host()
        paid={'status':'delivered_to_host','request_id':'read','order_id':'po_fixture',
              'paid_verified':True,'entitlement_created':True}
        with patch.object(host.downloader,'resource') as fetch:
            for change in ({'order_id':'po_other'},{'request_id':'other'},{'paid_verified':'true'},
                           {'entitlement_created':1},{'status':'success'}):
                with patch.object(self.remote,'request',return_value={**paid,**change}):
                    with self.assertRaises(MarketError):host.receive_after_agent_payment('read',expected_order_id='po_fixture')
            fetch.assert_not_called()

    def test_agent_repeated_402_never_reexports_wallet_handoff(self):
        host=self.reconciliation_host()
        paid={'status':'purchase_pending','request_id':'read','order_id':'po_fixture',
              'paid_verified':True,'entitlement_created':False}
        with patch.object(self.remote,'request',return_value=paid), patch.object(host.downloader,'resource',
                return_value=PurchaseResponse({'status':'payment_needed'},'synthetic-second-bill')) as fetch:
            result=host.receive_after_agent_payment('read',expected_order_id='po_fixture')
            self.assertEqual('host_reconcile_original_payment_no_repay',result['next_action'])
            fetch.assert_called_once_with('fixture:table','1.0.0','read','a'*64)
        self.assertNotIn('read',host.wallet_handoffs)

    def test_host_executes_only_original_goal_and_first_selected_snapshot(self):
        from dataclasses import replace
        host=self.reconciliation_host();goal='整理资料\n制作报告'
        snapshot=PurchaseRequestSnapshot(host.endpoint+'/v1/purchases/resource','POST',b'{}',
            (('Authorization','Bearer '+self.f.token),),b'synthetic',host.task_id,host.purpose,
            'po_fixture','read','a'*64,'fixture:table','1.0.0',host.key_id)
        reply=PurchaseResponse({'status':'payment_needed'},'synthetic',snapshot)
        kwargs=dict(session_id='session',directory=self.f.root/'payment',execution_root=self.f.root,
                    executable=self.f.root/'not-executed.exe',authorization={})
        with patch('ai_supermarket.alipay_agent.execute_alipay_agent') as execute:
            for invalid in (replace(snapshot,request_id='other'),replace(snapshot,task_id='other'),
                            replace(snapshot,release_digest='b'*64),replace(snapshot,headers=(('Authorization','changed'),))):
                host.wallet_handoffs['read']=PurchaseResponse({},'synthetic',invalid)
                with self.assertRaises(MarketError):host.execute_agent_payment('read',**kwargs)
            execute.assert_not_called()
            host.wallet_handoffs['read']=reply
            result=host.execute_agent_payment('read',**kwargs)
            self.assertIs(result,execute.return_value)
            self.assertEqual(goal,execute.call_args.kwargs['user_goal'])
            self.assertIs(snapshot,execute.call_args.args[0])
            self.assertNotIn('read',host.wallet_handoffs)
            with self.assertRaises(MarketError):host.execute_agent_payment('read',**kwargs)
            execute.assert_called_once()

    def test_download_selection_feedback_and_no_secret_local_state(self):
        host=self.create();self.pin(host)
        result=host.use('fixture:table','1.0.0','read')
        self.assertEqual('downloaded',result['status'])
        self.remote.call.return_value={'status':'recorded'}
        self.assertEqual('recorded',host.feedback(result['usage_id'],useful='unknown')['status'])
        with self.assertRaises(OSError):host.feedback('u_other',useful='unknown')
        for path in host.directory.rglob('*.json'):self.assertNotIn(self.f.token.encode(),path.read_bytes())

    def test_bill_stays_outside_model_reply_and_can_be_recovered(self):
        host=self.create();self.pin(host)
        reply=PurchaseResponse({'status':'payment_needed','request_id':'read'},'fictional-wallet-bill')
        with patch.object(host.downloader,'resource',return_value=reply):
            result=host.use('fixture:table','1.0.0','read')
        self.assertNotIn(reply.payment_needed,compact(result))
        again=self.create();self.assertEqual({},again.wallet_handoffs)
        with patch.object(again.downloader,'handoff',return_value=reply):
            self.assertEqual(reply,again.recover_wallet_handoff('read'))
        with patch.object(again.downloader,'handoff',return_value=PurchaseResponse({'status':'purchase_pending'})):
            self.assertIsNone(again.recover_wallet_handoff('read'))
        for path in host.directory.rglob('*.json'):self.assertNotIn(reply.payment_needed.encode(),path.read_bytes())

    def test_same_request_cannot_select_another_resolved_resource(self):
        host=self.create();self.pin(host);host.use('fixture:table','1.0.0','read')
        self.remote.call.return_value['id']='fixture:other';host.resolve('fixture:other','1.0.0')
        calls=self.remote.opener.open.call_count
        with self.assertRaises(MarketError):host.use('fixture:other','1.0.0','read')
        self.assertEqual(calls,self.remote.opener.open.call_count)

    def test_text_from_server_without_selected_release_is_rejected(self):
        host=self.create();self.pin(host)
        response=download_fixture.Response(compact({'status':'read','content':'fixture','usage_id':'u_fixture'}).encode(),
            200,[('Content-Type','application/json')])
        self.remote.opener.open.side_effect=lambda *a,**k:response
        with self.assertRaises(MarketError) as caught:host.use('fixture:table','1.0.0','read')
        self.assertEqual('download_scope_mismatch',caught.exception.code)

    def test_changed_identity_stops_before_network(self):
        host=self.create();self.remote.base_url='https://other.example'
        with self.assertRaises(MarketError) as caught:host.search('资料')
        self.assertEqual('task_scope_mismatch',caught.exception.code)
        self.assertEqual(1,self.remote.call.call_count)


class PinnedTextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):machine_fixture.MachinePayHTTPTests.setUpClass()

    def setUp(self):
        self.f=machine_fixture.MachinePayHTTPTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def test_wrong_expected_release_does_not_hold_funds_or_open_order(self):
        f=self.f;code,result,_=f.fetch({**f.payload,'expected_release_digest':'0'*64})
        self.assertEqual('release_changed',result['error_code'])
        self.assertEqual(0,f.budget.summary()['held_cents'])
        self.assertEqual(0,f.count('acquisitions'))

    def test_text_response_carries_pinned_release_and_paid_replay(self):
        f=self.f;digest=f.buyer.resolve(f.resource,'1.0.0','personal_use')['release_digest']
        payload={**f.payload,'expected_release_digest':digest}
        code,bill,_=f.fetch(payload);self.assertEqual(402,code)
        f.order_id=bill['order_id'];f.record=f.orders.get(f.order_id)
        f.body.update(f.record['prepared']['channel_identifiers']);f.client._enabled=True
        with patch.object(f.client,'_post',return_value=f.signed()):
            code,result,_=f.fetch(payload,headers=[(b'payment-proof',f.proof())])
        self.assertEqual(200,code);self.assertEqual('read',result['status'])
        self.assertEqual(digest,result['release_digest'])
        self.assertEqual(100,f.budget.summary()['spent_cents'])


class LiveTaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):download_fixture.LiveDownloadTests.setUpClass()

    def setUp(self):
        self.case=download_fixture.LiveDownloadTests();self.case.setUp();self.addCleanup(self.case.doCleanups)

    def host(self):
        c=self.case
        return RemoteTaskResources(c.remote,'task','观察资料','personal_use',c.downloader.root)

    def test_external_wallet_replay_then_original_order_reconciliation(self):
        # An independent downloader stands in for CLI replay. Only the local
        # HTTP and market are real; the payment proof/channel are synthetic.
        c=self.case;c.connect();host=self.host();f=c.f
        host.resolve(f.resource,'1.0.0');first=host.use(f.resource,'1.0.0','buy')
        pending=host.receive_after_agent_payment('buy',expected_order_id=first['order_id'])
        self.assertFalse(pending['paid_verified'])
        f.order_id=first['order_id'];f.record=f.orders.get(f.order_id)
        f.body.update(f.record['prepared']['channel_identifiers']);f.client._enabled=True
        external=c.downloader
        with patch.object(f.client,'_post',return_value=f.signed()) as channel:
            reply=external.resource(f.resource,'1.0.0','buy',host._selection('buy')['release_digest'],
                                    payment_proof=f.proof())
            self.assertEqual('downloaded',reply.result['status'])
            # Restart loses in-memory CLI output and first bill; original order
            # alone plus authenticated market state permits download recovery.
            host=self.host()
            result=host.receive_after_agent_payment('buy',expected_order_id=first['order_id'])
            self.assertEqual(1,channel.call_count)
        self.assertEqual('downloaded',result['status']);self.assertTrue(result['local_files_verified'])
        self.assertTrue(Path(result['license_path']).is_file())
        self.assertEqual(100,f.budget.summary()['spent_cents']);self.assertEqual(1,f.count('acquisitions'))
        self.assertFalse(result['task_completed']);self.assertEqual({},host.wallet_handoffs)

    def test_paid_file_restart_original_bill_receipt_and_artifact(self):
        c=self.case;c.connect();host=self.host();f=c.f
        host.resolve(f.resource,'1.0.0');first=host.use(f.resource,'1.0.0','buy')
        self.assertEqual('payment_needed',first['status']);bill=host.wallet_handoffs['buy'].payment_needed
        host=self.host();restored=host.recover_wallet_handoff('buy')
        self.assertEqual(bill,restored.payment_needed);self.assertEqual(first['order_id'],restored.result['order_id'])
        f.order_id=first['order_id'];f.record=f.orders.get(f.order_id)
        f.body.update(f.record['prepared']['channel_identifiers']);f.client._enabled=True
        with patch.object(f.client,'_post',return_value=f.signed()) as channel:
            done=host.resume_payment('buy',f.proof());self.assertEqual(1,channel.call_count)
        self.assertEqual('downloaded',done['status'])
        receipt=host.acknowledge('buy');self.assertTrue(receipt['server_acknowledged'])
        rows=list(csv.DictReader(Path(done['path']).read_text(encoding='utf-8').splitlines()))
        self.assertEqual([{'编号':'001','观察状态':'未知'}],rows)
        report=c.source.root/'observation-report.md'
        report.write_text('# 原创夹具观察报告\n\n共1条记录；编号001的观察状态为“未知”，不能推断已验证。\n'
                          '\n来源：本项目原创测试数据。许可保留于下载目录LICENSE.txt。\n',encoding='utf-8')
        self.assertIn('不能推断已验证',report.read_text(encoding='utf-8'))
        self.assertEqual(done,self.host().use(f.resource,'1.0.0','buy'))
        self.assertEqual(100,f.budget.summary()['spent_cents']);self.assertEqual(1,f.count('acquisitions'))
        self.assertFalse(receipt['task_completed'])

    def test_real_cli_mcp_from_foreign_directory_free_asset(self):
        c=self.case;c.connect(asset=True)
        goal=c.source.root/'goal.txt';goal.write_text('素材\n获取音频',encoding='utf-8')
        messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25',
            'capabilities':{},'clientInfo':{'name':'fixture','version':'1'}}},
            {'jsonrpc':'2.0','method':'notifications/initialized'},
            {'jsonrpc':'2.0','id':2,'method':'tools/list'},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'market_resolve',
                'arguments':{'id':c.f.resource,'version':'1.0.0'}}},
            {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'market_use',
                'arguments':{'id':c.f.resource,'version':'1.0.0','request_id':'buy'}}},
            {'jsonrpc':'2.0','id':5,'method':'tools/call','params':{'name':'market_receipt','arguments':{'request_id':'buy'}}}]
        cli=Path(__file__).resolve().parents[1]/'remote_cli.py'
        command=[sys.executable,'-B',str(cli),'--url',c.remote.base_url,'--credential-file',str(c.remote.credential_file),
            'task-mcp','--task-id','task','--purpose','personal_use','--goal-file',str(goal),'--output-root',str(c.downloader.root)]
        process=subprocess.run(command,cwd=c.source.root,input=('\n'.join(map(compact,messages))+'\n').encode(),capture_output=True,timeout=20)
        self.assertEqual(0,process.returncode,process.stderr.decode(errors='replace'))
        replies=[json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(5,len(replies));self.assertIn('initial_discovery',replies[0]['result']['instructions'])
        self.assertEqual(6,len(replies[1]['result']['tools']))
        resource=replies[3]['result']['structuredContent'];self.assertEqual('downloaded',resource['status'])
        self.assertTrue(Path(resource['path']).is_file())
        self.assertTrue(replies[4]['result']['structuredContent']['server_acknowledged'])
        self.assertNotIn(c.f.token.encode(),process.stdout)
        self.assertEqual(0,c.f.budget.summary()['spent_cents'])


if __name__=='__main__':unittest.main()
