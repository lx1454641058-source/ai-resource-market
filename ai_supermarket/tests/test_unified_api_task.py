"""本项目原创。统一采购/API任务，真实本机HTTP与合成签名付款，无真实钱包。"""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from ai_supermarket.api_delivery import ApiDeliveryHost
from ai_supermarket.common import MarketError, compact
from ai_supermarket.contracts import HostPolicy
from ai_supermarket.http_app import MarketHTTP
from ai_supermarket.identity import Identities
from ai_supermarket.remote_task import RemoteTaskResources, RemoteTaskInterface
from ai_supermarket.tests import test_purchase_host as purchase
from ai_supermarket.tests.test_api_product import CONTENT, POLICY, metadata
from ai_supermarket.tests import test_api_remote as api_remote
from ai_supermarket.tests import test_machine_pay_http as machine_http


class UnifiedApiTaskTests(unittest.TestCase):
    live=api_remote.ApiRemoteTests.live

    @classmethod
    def setUpClass(cls):purchase.PurchaseHostTests.setUpClass()

    def setUp(self):
        self.f=purchase.PurchaseHostTests()
        def draft(price=0,version='1.0.0',slug='brief'):
            self.f.buyer.host_policy=HostPolicy.from_json(POLICY)
            return self.f.seller.draft(slug,version,metadata(price),CONTENT,'draft-'+version+'-'+slug)
        self.f.draft=draft
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.root=self.f.database.parent;self.calls=[];self.results={}
        self.api_host=ApiDeliveryHost(self.f.buyer,'provider-test','task','personal_use',
            self.root/'api-quota.sqlite',self.root/'provider-results','fixed-provider',
            lambda principal,task,provider: (principal,task,provider)==('alice','task','provider-test'),
            self.execute,self.lookup)
        self.identities=Identities(self.root/'identities.sqlite')
        self.identities.create_account('alice','buyer',POLICY)
        self.issued=self.identities.issue_key('alice');self.token=self.issued['token']
        self.credentials=self.root/'credentials.json'
        self.credentials.write_text(compact({'token':self.token}),encoding='utf-8')
        self.app=MarketHTTP(self.f.database,self.identities.store.path,allowed_hosts=['market.test:8765'],
            budget_database=self.f.ledger.store.path,enforce_task_budget=True,
            purchase_hosts={self.issued['key_id']:self.f.host},
            api_hosts={self.issued['key_id']:{'provider-test':self.api_host}})
        self.app.gateway.bind_key(self.issued['key_id'],'root','task','alice','fictional-task-binding')
        self.output=self.root/'client';self.output.mkdir()

    def execute(self,operation_id,operation,inputs):
        self.calls.append(operation_id)
        self.results[operation_id]={'count':7,'warehouse':inputs['warehouse']}
        return self.results[operation_id]

    def lookup(self,operation_id):
        if operation_id not in self.results:return {'state':'unknown'}
        return {'state':'completed','result':self.results[operation_id],'evidence_ref':'fictional-original-operation'}

    def task(self,remote=None):
        return RemoteTaskResources(remote or self.live(),'task','仓库查询','personal_use',self.output)

    def select(self,task,resource=None):
        resource=resource or self.f.resource
        self.assertEqual('resolved',task.resolve(resource,'1.0.0')['status'])
        return task.use(resource,'1.0.0','buy')

    def pay(self,task,bill):
        self.f.order_id=bill['order_id'];self.f.record=self.f.orders.get(self.f.order_id)
        self.f.body.update(self.f.record['prepared']['channel_identifiers'])
        self.f.client._enabled=True
        with patch.object(self.f.client,'_post',return_value=self.f.signed()) as post:
            result=task.resume_payment('buy',machine_http.MachinePayHTTPTests.proof(self.f).decode())
        self.assertEqual(1,post.call_count)
        self.assertEqual('market_api_call',result.get('next_action'),result)
        return result

    def test_one_task_paid_purchase_invoke_file_and_replay(self):
        task=self.task();interface=RemoteTaskInterface(task)
        names={t['name'] for t in interface.tool_list()}
        self.assertEqual(10,len(names));self.assertIn('market_purchase_status',names)
        bill=self.select(task);self.assertEqual('payment_needed',bill['status'])
        self.assertEqual([],self.calls);self.assertNotIn('headers',bill)
        self.assertNotIn('payment_proof',compact(interface.tool_list()))
        acquired=self.pay(task,bill)
        result=interface.call('market_api_call',{'acquisition_id':acquired['acquisition_id'],
            'input_json':'{"warehouse":"fictional"}','request_id':'call-one'})
        self.assertEqual('api_result_received',result['status'],result)
        self.assertEqual({'count':7,'warehouse':'fictional'},json.loads(Path(result['path']).read_text()))
        self.assertEqual(metadata()['license_text'],Path(result['license_path']).read_text(encoding='utf-8'))
        restarted=self.task(task.remote)
        self.assertEqual(acquired['acquisition_id'],restarted.use(self.f.resource,'1.0.0','buy')['acquisition_id'])
        self.assertEqual(result['sha256'],restarted.api_receive('call-one')['sha256'])
        self.assertEqual(1,len(self.calls));self.assertEqual(1,self.f.count('acquisitions'))
        self.assertEqual(100,self.f.budget.summary()['spent_cents'])
        self.assertEqual(0,self.f.budget.summary()['held_cents'])
        self.assertFalse(result['task_completed'])
        self.assertNotEqual('paid_delivered_to_host',self.f.orders.get(bill['order_id'])['status'])

    def test_free_api_in_same_purchase_mode_no_bill(self):
        resource=self.f.publish(price=0,slug='free')
        task=self.task();acquired=self.select(task,resource)
        self.assertEqual('market_api_call',acquired['next_action']);self.assertEqual({},task.wallet_handoffs)
        result=task.api_call(acquired['acquisition_id'],'{"warehouse":"free"}','call-free')
        self.assertEqual('api_result_received',result['status']);self.assertEqual(0,self.f.budget.summary()['spent_cents'])

    def test_unknown_payment_keeps_order_no_api_rights_or_execution(self):
        task=self.task();bill=self.select(task)
        self.f.order_id=bill['order_id'];self.f.record=self.f.orders.get(self.f.order_id)
        self.f.body.update(self.f.record['prepared']['channel_identifiers']);self.f.client._enabled=True
        with patch.object(self.f.client,'_post',side_effect=TimeoutError) as post:
            result=task.resume_payment('buy',machine_http.MachinePayHTTPTests.proof(self.f).decode())
            again=task.use(self.f.resource,'1.0.0','buy')
            state=task.status('buy')
        self.assertEqual(1,post.call_count);self.assertEqual(bill['order_id'],again['order_id'])
        self.assertFalse(result['paid_verified']);self.assertFalse(state['paid_verified'])
        self.assertEqual([],list((task.directory/'api-rights').glob('*.json')))
        self.assertEqual(0,self.f.count('acquisitions'));self.assertEqual([],self.calls)
        self.assertEqual(100,self.f.budget.summary()['held_cents'])
        # The trusted host explicitly rechecks the same order; this is not an
        # automatic model retry, wallet payment, or replacement order.
        with patch.object(self.f.client,'_post',return_value=self.f.signed()) as post:
            self.f.host.verify_payment('buy','host-recheck',self.f.request)
        self.assertEqual(1,post.call_count)
        recovered=task.use(self.f.resource,'1.0.0','buy')
        self.assertEqual(bill['order_id'],recovered['order_id'])
        self.assertEqual('market_api_call',recovered['next_action'])
        self.assertEqual(100,self.f.budget.summary()['spent_cents'])
        self.assertEqual(0,self.f.budget.summary()['held_cents']);self.assertEqual([],self.calls)

    def test_api_unknown_recovers_original_call_without_new_purchase(self):
        task=self.task();acquired=self.pay(task,self.select(task))
        def lost(operation,method,value):
            self.execute(operation,method,value)
            raise TimeoutError('synthetic response loss')
        self.api_host.execute=lost
        result=task.api_call(acquired['acquisition_id'],'{"warehouse":"fictional"}','call-one')
        self.assertEqual('reserved',result['status'])
        self.assertEqual('reserved',task.api_status('call-one')['status']);self.assertEqual(1,len(self.calls))
        restarted=self.task(task.remote)
        result=restarted.api_recover('call-one')
        self.assertEqual('api_result_received',result['status'],result)
        self.assertEqual(1,len(self.calls));self.assertEqual(1,self.f.count('acquisitions'))
        self.assertEqual(100,self.f.budget.summary()['spent_cents'])

    def test_missing_api_host_rejects_selection_before_paid_order(self):
        self.app.apis.hosts={}
        task=self.task();self.assertEqual(6,len(RemoteTaskInterface(task).tool_list()))
        detail=task.resolve(self.f.resource,'1.0.0')
        self.assertFalse(detail['task_entry_compatible'])
        with self.assertRaises(MarketError):task.use(self.f.resource,'1.0.0','buy')
        self.assertEqual(0,self.f.budget.summary()['held_cents']);self.assertEqual([],self.calls)

    def test_no_new_tools_for_saved_legacy_purchase_discovery(self):
        remote=self.live();original=remote.tool_list
        def legacy():
            reply=original();reply.pop('resource_api',None);return reply
        with patch.object(remote,'tool_list',side_effect=legacy):task=self.task(remote)
        with patch.object(remote,'tool_list',side_effect=AssertionError('do not rediscover')):
            restarted=self.task(remote)
        self.assertFalse(restarted.api_enabled);self.assertEqual(6,len(RemoteTaskInterface(restarted).tool_list()))

    def test_mismatched_transfer_descriptor_cannot_save_rights(self):
        task=self.task();bill=self.select(task);acquired=self.pay(task,bill)
        wrong=deepcopy(acquired);wrong['api']['total_units']=999
        with self.assertRaises(MarketError) as caught:task._api_right(task._selection('buy'),wrong)
        self.assertEqual('api_scope_mismatch',caught.exception.code);self.assertEqual([],self.calls)

    def test_call_budget_shared_with_purchase_across_restart(self):
        remote=self.live()
        task=RemoteTaskResources(remote,'task','仓库查询','personal_use',self.output,max_calls=5)
        acquired=self.pay(task,self.select(task))
        with self.assertRaises(MarketError) as caught:
            task.api_call(acquired['acquisition_id'],'{"warehouse":"fictional"}','call-one')
        self.assertEqual('task_call_limit',caught.exception.code);self.assertEqual([],self.calls)
        self.assertEqual(100,self.f.budget.summary()['spent_cents'])

    def test_provider_outside_discovered_scope_cannot_start_purchase(self):
        remote=self.live();original=remote.tool_list
        def discovery():
            reply=original();reply['resource_api']['providers']=['other-provider'];return reply
        with patch.object(remote,'tool_list',side_effect=discovery):task=self.task(remote)
        self.assertEqual('requires_configured_api_provider',task.resolve(self.f.resource,'1.0.0')['next_action'])
        with self.assertRaises(MarketError):task.use(self.f.resource,'1.0.0','buy')
        self.assertEqual(0,self.f.budget.summary()['held_cents'])

    def test_other_task_descriptor_does_not_enable_api_tools(self):
        remote=self.live();original=remote.tool_list
        def discovery():
            reply=original();reply['resource_api']['task_id']='other-task';return reply
        with patch.object(remote,'tool_list',side_effect=discovery):task=self.task(remote)
        self.assertFalse(task.api_enabled)
        self.assertNotIn('market_api_call',{tool['name'] for tool in RemoteTaskInterface(task).tool_list()})

    def test_repeated_use_does_not_buy_more_units_when_bundle_exhausted(self):
        task=self.task();acquired=self.pay(task,self.select(task))
        for request in ('call-one','call-two'):
            self.assertEqual('api_result_received',task.api_call(acquired['acquisition_id'],
                '{"warehouse":"fictional"}',request)['status'])
        again=task.use(self.f.resource,'1.0.0','other-selection')
        self.assertEqual(acquired['acquisition_id'],again['acquisition_id'])
        result=task.api_call(acquired['acquisition_id'],'{"warehouse":"fictional"}','call-three')
        self.assertEqual('error',result['status']);self.assertEqual(2,len(self.calls))
        self.assertEqual(1,self.f.count('acquisitions'));self.assertEqual(100,self.f.budget.summary()['spent_cents'])

    def test_transfer_with_wrong_release_cannot_select_entitlement(self):
        task=self.task();acquired=self.pay(task,self.select(task))
        with self.assertRaises(MarketError) as caught:
            task._api_right(task._selection('buy'),{**acquired,'release_digest':'b'*64})
        self.assertEqual('api_scope_mismatch',caught.exception.code);self.assertEqual([],self.calls)


if __name__=='__main__':unittest.main()
