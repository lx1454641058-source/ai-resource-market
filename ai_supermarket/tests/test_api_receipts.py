"""本项目原创。API逐次收货声明；真实本机HTTP，无钱包或模型调用。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import unittest
from unittest.mock import patch

from ai_supermarket.api_client import RemoteApiResources
from ai_supermarket.api_quota import canonical,checksum
from ai_supermarket.common import MarketError
from ai_supermarket.remote_task import RemoteTaskResources,RemoteTaskInterface
from ai_supermarket.tests import test_api_remote as fixture


class ApiReceiptTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ApiRemoteTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def delivered(self):
        self.f.fetch()
        reply=self.f.fetch('result')[1]
        return {**self.f.payload,'usage_id':reply['usage_id'],
                'manifest_sha256':checksum(canonical(reply['manifest'],8192))}

    def client(self,max_calls=24):
        remote=self.f.live()
        return RemoteApiResources(remote,'task','personal_use',self.f.root/'client',max_calls=max_calls)

    def test_idempotent_status_restart_and_no_provider_or_fulfillment(self):
        request=self.delivered()
        self.assertEqual('awaiting_api_receipt',self.f.fetch('receipt_status',request)[1]['status'])
        first=self.f.fetch('receipt',request)[1]
        self.assertEqual('api_receipt_recorded',first['status'],first)
        self.f.app=self.f.make_app()
        self.assertEqual(first,self.f.fetch('receipt',request)[1])
        self.assertEqual(first,self.f.fetch('receipt_status',request)[1])
        self.assertTrue(first['server_acknowledged'])
        for field in ('package_fulfillment_confirmed','channel_fulfillment_confirmed','task_completed','payment_executed'):
            self.assertFalse(first[field])
        self.assertEqual(1,len(self.f.f.calls))
        with self.f.f.buyer.store.connect() as db:
            self.assertEqual(1,db.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'api_client_receipt:%'").fetchone()[0])
            self.assertEqual(0,db.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'order_client_receipt:%'").fetchone()[0])

    def test_wrong_digest_usage_input_and_request_cannot_ack(self):
        request=self.delivered()
        for change in ({'manifest_sha256':'b'*64},{'usage_id':'u_missing'},
                       {'input':{'warehouse':'different'}},{'request_id':'other'}):
            with self.subTest(change=change):
                self.assertGreaterEqual(self.f.fetch('receipt',{**request,**change})[0],400)
        self.assertEqual('awaiting_api_receipt',self.f.fetch('receipt_status',request)[1]['status'])
        self.assertEqual(1,len(self.f.f.calls))

    def test_no_completed_call_does_not_dispatch(self):
        request={**self.f.payload,'usage_id':'u_missing','manifest_sha256':'b'*64}
        for action in ('receipt','receipt_status'):
            self.assertGreaterEqual(self.f.fetch(action,request)[0],400)
        self.assertEqual([],self.f.f.calls)

    def test_revocation_and_expiry_block_existing_receipt(self):
        request=self.delivered();self.f.fetch('receipt',request)
        self.f.f.now=5000
        self.assertGreaterEqual(self.f.fetch('receipt_status',request)[0],400)
        self.f.f.now=1000
        self.f.identities.revoke_key(self.f.issued['key_id'])
        self.assertEqual(401,self.f.fetch('receipt_status',request)[0])

    def test_parallel_ack_saves_one_identical_record(self):
        request=self.delivered()
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies=list(pool.map(lambda _:self.f.app.apis.call(self.f.token,'receipt',request),range(4)))
        self.assertTrue(all(reply==replies[0] for reply in replies))
        self.assertEqual(1,len(self.f.f.calls))

    def test_new_key_cannot_read_other_keys_acknowledgement(self):
        request=self.delivered();first=self.f.fetch('receipt',request)[1]
        issued=self.f.identities.issue_key('buyer')
        self.f.app.gateway.bind_key(issued['key_id'],'root','task','buyer','new-key-binding')
        self.f.app.apis.hosts[issued['key_id']]={'provider-test':self.f.host}
        self.f.token=issued['token']
        self.assertEqual('awaiting_api_receipt',self.f.fetch('receipt_status',request)[1]['status'])
        second=self.f.fetch('receipt',request)[1]
        self.assertNotEqual(first['receipt_id'],second['receipt_id'])

    def test_corrupt_saved_record_is_not_overwritten(self):
        request=self.delivered();self.f.fetch('receipt',request)
        with self.f.f.buyer.store.connect(write=True) as db:
            db.execute("UPDATE meta SET value='{}' WHERE key LIKE 'api_client_receipt:%'")
        self.assertGreaterEqual(self.f.fetch('receipt',request)[0],400)
        with self.f.f.buyer.store.connect() as db:
            self.assertEqual('{}',db.execute("SELECT value FROM meta WHERE key LIKE 'api_client_receipt:%'").fetchone()[0])

    def test_unknown_call_receipt_never_looks_up_provider(self):
        def unknown(*args):raise TimeoutError()
        self.f.host.execute=unknown
        self.f.fetch()
        request={**self.f.payload,'usage_id':'u_missing','manifest_sha256':'b'*64}
        with patch.object(self.f.host,'lookup') as lookup:
            self.assertGreaterEqual(self.f.fetch('receipt',request)[0],400)
        lookup.assert_not_called()

    def test_real_client_files_ack_and_restart(self):
        client=self.client();local=client.invoke(self.f.target,{'warehouse':'fictional'},'one')
        self.assertEqual('awaiting_api_receipt',client.acknowledge('one',status_only=True)['status'])
        reply=client.acknowledge('one')
        self.assertEqual('api_receipt_recorded',reply['status'],reply)
        again=RemoteApiResources(client.remote,'task','personal_use',self.f.root/'client')
        self.assertEqual(reply,again.acknowledge('one',status_only=True))
        self.assertTrue((Path(local['path']).parent/'server-receipt.json').is_file())
        self.assertEqual(local,again.receive('one'))
        self.assertEqual(1,len(self.f.f.calls))

    def test_changed_local_files_rejected_before_network(self):
        client=self.client();local=client.invoke(self.f.target,{'warehouse':'fictional'},'one')
        file=Path(local['path']);file.write_text('{"count":99}',encoding='utf-8')
        with patch.object(client.remote,'request') as network,self.assertRaises(MarketError):
            client.acknowledge('one')
        network.assert_not_called()
        self.assertEqual('{"count":99}',file.read_text(encoding='utf-8'))

    def test_lost_ack_response_query_does_not_repeat_execution(self):
        client=self.client();client.invoke(self.f.target,{'warehouse':'fictional'},'one')
        original=client.remote.request
        def lost(path,value):
            original(path,value)
            raise MarketError('transport_unknown','模拟响应丢失')
        with patch.object(client.remote,'request',side_effect=lost),self.assertRaises(MarketError):
            client.acknowledge('one')
        self.assertEqual('api_receipt_recorded',client.acknowledge('one',status_only=True)['status'])
        self.assertEqual(1,len(self.f.f.calls))

    def test_ack_respects_persistent_network_limit(self):
        client=self.client(max_calls=2);client.invoke(self.f.target,{'warehouse':'fictional'},'one')
        with self.assertRaises(MarketError) as error:client.acknowledge('one')
        self.assertEqual('api_call_limit',error.exception.code)

    def test_task_tool_reuses_receive_action_and_shared_budget(self):
        remote=self.f.live();root=self.f.root/'task-client';root.mkdir()
        task=RemoteTaskResources(remote,'task','仓库查询','personal_use',root,mode='api')
        api=RemoteTaskInterface(task);selected={'id':'vendor:stock','version':'1.0.0'}
        api.call('market_resolve',selected)
        right=api.call('market_use',{**selected,'request_id':'choose'})
        api.call('market_api_call',{'acquisition_id':right['acquisition_id'],'input_json':'{"warehouse":"fictional"}','request_id':'one'})
        before=len(list((task.directory/'calls').iterdir()))
        reply=api.call('market_api_receive',{'request_id':'one','action':'receipt'})
        self.assertEqual('api_receipt_recorded',reply['status'],reply)
        self.assertEqual(before+1,len(list((task.directory/'calls').iterdir())))
        self.assertEqual(8,len(api.tool_list()))


class PaidApiReceiptTests(unittest.TestCase):
    def test_synthetic_paid_right_ack_does_not_change_order_or_call_channel(self):
        from ai_supermarket.tests.test_unified_api_task import UnifiedApiTaskTests
        UnifiedApiTaskTests.setUpClass()
        f=UnifiedApiTaskTests();f.setUp();self.addCleanup(f.doCleanups)
        task=f.task();bill=f.select(task);right=f.pay(task,bill)
        task.api_call(right['acquisition_id'],'{"warehouse":"fictional"}','one')
        original=f.f.orders.get(bill['order_id'])
        with patch.object(f.f.client,'_post',side_effect=AssertionError('不应调用支付渠道')) as channel:
            reply=task.api_receive('one',action='receipt')
            again=task.api_receive('one',action='receipt_status')
        self.assertEqual('api_receipt_recorded',reply['status'],reply)
        self.assertEqual(reply,again);channel.assert_not_called()
        self.assertEqual(original,f.f.orders.get(bill['order_id']))
        self.assertEqual(1,len(f.calls))
        self.assertEqual(100,f.f.budget.summary()['spent_cents'])


if __name__=='__main__':unittest.main()
