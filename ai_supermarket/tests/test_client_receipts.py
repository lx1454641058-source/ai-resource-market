"""来源：本项目原创。客户端收货声明、原单履约门槛和实际本机HTTP；不调用真实渠道。"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from ai_supermarket.alipay_fulfillment import AlipayFulfillmentClient
from ai_supermarket.client_receipts import ClientReceipts
from ai_supermarket.common import MarketError, compact
from ai_supermarket.payment_orders import _get, _put
from ai_supermarket.purchase_download import PurchaseDownloader
from ai_supermarket.tests import test_resource_bundle_http as bundle_fixture
from ai_supermarket.tests import test_purchase_download as download_fixture


class ClientReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):bundle_fixture.ResourceBundleHTTPTests.setUpClass()

    def setUp(self):
        self.case=bundle_fixture.ResourceBundleHTTPTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.f=self.case.f

    def download(self,paid=False):
        if paid:self.case.origin();response=self.case.paid()
        else:self.case.asset();response=self.case.fetch()
        self.assertEqual(200,response[0],response[1]);self.digest=response[2][b'x-bundle-sha256'].decode()
        self.receipts=ClientReceipts(self.f.host,self.f.issued['key_id'])
        return response

    def call(self,action='receipt',**values):
        value={'request_id':'buy'}
        if action=='receipt':value['bundle_sha256']=self.digest
        value.update(values)
        return self.f.fetch(value,path='/v1/purchases/'+action)

    def test_prepared_file_is_not_acknowledged_and_original_statement_replays(self):
        self.download();code,status,_=self.call('receipt_status')
        self.assertEqual(200,code);self.assertEqual('awaiting_client_receipt',status['status'])
        self.assertFalse(status['server_acknowledged'])
        code,first,_=self.call();self.assertEqual(200,code)
        self.assertTrue(first['server_acknowledged']);self.assertFalse(first['channel_fulfillment_confirmed'])
        self.assertFalse(first['task_completed']);self.assertEqual('authenticated_client_statement',first['evidence'])
        self.f.now[0]+=5
        self.assertEqual(first,self.call()[1]);self.assertEqual(first,self.call('receipt_status')[1])
        self.assertEqual(1,self.f.count('usage_events'))

    def test_paid_file_channel_preparation_requires_matching_client_statement(self):
        self.download(paid=True);client=AlipayFulfillmentClient(self.f.fulfillment,self.f.client)
        with self.assertRaises(MarketError) as caught:client.prepare(self.f.order_id)
        self.assertEqual('client_receipt_required',caught.exception.code)
        self.call();queued=self.f.host.prepare_channel_receipt('buy',client)
        self.assertEqual('pending',queued['status']);self.assertEqual(0,queued['attempt_count'])
        self.assertFalse(queued['channel_fulfillment_confirmed']);self.assertEqual(100,self.f.budget.summary()['spent_cents'])

    def test_acknowledgement_before_a_bundle_is_prepared_is_refused(self):
        self.case.asset();self.digest='a'*64
        code,body,_=self.call();self.assertNotEqual(200,code);self.assertEqual('receipt_not_found',body['error_code'])
        self.assertEqual(0,self.f.count('usage_events'))

    def test_wrong_digest_or_request_never_changes_receipt(self):
        self.download()
        for values in ({'bundle_sha256':'a'*64},{'request_id':'other'},{'bundle_sha256':'x'}):
            self.assertNotEqual(200,self.call(**values)[0])
        self.assertFalse(self.call('receipt_status')[1]['server_acknowledged'])

    def test_key_and_task_binding_revocation_block_acknowledgement(self):
        self.download();self.f.app.gateway.revoke_binding(self.f.issued['key_id'])
        self.assertEqual(403,self.call()[0]);self.assertFalse(self.receipts.status('buy')['server_acknowledged'])

    def test_another_valid_key_for_same_buyer_cannot_ack_original_key_delivery(self):
        self.download();other=self.f.identities.issue_key('buyer')
        self.f.app.purchases.hosts[other['key_id']]=self.f.host
        self.f.app.gateway.bind_key(other['key_id'],'root','task','buyer','other-fixture-binding')
        self.f.token=other['token']
        self.assertEqual('receipt_not_found',self.call()[1]['error_code'])
        self.assertFalse(self.receipts.status('buy')['server_acknowledged'])

    def test_paused_budget_can_record_prior_delivery_without_new_spending(self):
        self.download(paid=True);self.f.ledger.set_state('root','paused',1)
        self.assertEqual(200,self.call()[0]);self.assertEqual(100,self.f.budget.summary()['spent_cents'])
        self.assertEqual(403,self.case.fetch()[0])

    def test_proof_or_extra_scope_in_ack_is_refused(self):
        self.download()
        self.assertEqual(400,self.call(task_id='other')[0])
        code,body,_=self.f.fetch({'request_id':'buy','bundle_sha256':self.digest},path='/v1/purchases/receipt',
                                 headers=[(b'payment-proof',b'ignored')])
        self.assertEqual(400,code);self.assertFalse(self.receipts.status('buy')['server_acknowledged'])

    def test_same_statement_concurrently_has_one_immutable_receipt(self):
        self.download()
        with ThreadPoolExecutor(max_workers=3) as pool:
            results=list(pool.map(lambda _:self.receipts.acknowledge('buy',self.digest),range(6)))
        self.assertTrue(all(value==results[0] for value in results))

    def test_corrupted_offer_is_refused(self):
        self.download()
        with self.f.buyer.store.connect(write=True) as db:
            row=db.execute("SELECT key FROM meta WHERE key LIKE 'client_delivery:%'").fetchone()
            record=_get(db,row['key']);record['offer']['bundle_sha256']='c'*64;_put(db,row['key'],record)
        self.assertEqual('receipt_corrupt',self.call()[1]['error_code'])

    def test_started_local_channel_action_is_not_retroactively_gated(self):
        self.case.origin();self.f.bill();self.f.seed_evidence();self.f.host.resume('buy')
        channel=AlipayFulfillmentClient(self.f.fulfillment,self.f.client,network_enabled=True)
        with patch.object(self.f.client,'_post',side_effect=TimeoutError):
            self.assertEqual('unknown',channel.send(self.f.order_id)['status'])
        self.assertEqual(200,self.case.fetch()[0])
        with self.f.buyer.store.connect() as db:
            self.assertIsNone(_get(db,'remote_file_delivery:'+self.f.order_id))
        self.assertEqual('unknown',channel.history(self.f.order_id)['status'])

    def test_changed_usage_binding_prevents_statement(self):
        self.download()
        with self.f.buyer.store.connect(write=True) as db:
            db.execute("UPDATE usage_events SET task_id='other-task'")
        self.assertEqual('receipt_binding_mismatch',self.call()[1]['error_code'])

    def test_disconnect_after_headers_leaves_unacknowledged_offer(self):
        import asyncio
        self.case.asset()
        async def disconnect(message):
            if message['type']=='http.response.body':raise OSError('fixture disconnect')
        messages=asyncio.run(self.case.request(send_hook=disconnect))
        self.assertEqual(1,len(messages))
        status=self.call('receipt_status')[1]
        self.assertEqual('awaiting_client_receipt',status['status']);self.assertFalse(status['server_acknowledged'])


class LiveClientReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):download_fixture.LiveDownloadTests.setUpClass()

    def setUp(self):
        self.case=download_fixture.LiveDownloadTests();self.case.setUp();self.addCleanup(self.case.doCleanups)

    def ready(self,paid=False):
        self.case.connect(asset=not paid);f=self.case.f;self.f=f
        client=self.case.downloader;args=self.case.args
        first=client.resource(*args)
        if paid:
            f.order_id=first.result['order_id'];f.record=f.orders.get(f.order_id)
            f.body.update(f.record['prepared']['channel_identifiers']);f.client._enabled=True
            with patch.object(f.client,'_post',return_value=f.signed()):first=client.resource(*args,payment_proof=f.proof())
        self.client=client;self.args=args;self.downloaded=first.result
        return first

    def test_actual_http_client_statement_recorded_and_recovers_after_restart(self):
        self.ready(paid=True)
        first=self.client.acknowledge(*self.args)
        self.assertEqual('client_receipt_recorded',first['status']);self.assertTrue(first['server_acknowledged'])
        file=Path(self.downloaded['receipt_path']).with_name('server-receipt.json')
        self.assertEqual(first,json.loads(file.read_bytes()))
        self.client=PurchaseDownloader(self.case.remote,'task','personal_use',self.client.root)
        self.assertEqual(first,self.client.acknowledge(*self.args))
        self.assertFalse(first['channel_fulfillment_confirmed']);self.assertEqual(100,self.f.budget.summary()['spent_cents'])

    def test_local_file_tamper_blocks_ack_before_network(self):
        self.ready();Path(self.downloaded['path']).write_bytes(b'changed')
        with patch.object(self.case.remote,'request',side_effect=AssertionError('must not send')):
            with self.assertRaises(MarketError):self.client.acknowledge(*self.args)
        status=self.case.remote.request('/v1/purchases/receipt_status',{'request_id':'buy'})
        self.assertFalse(status['server_acknowledged'])

    def test_lost_response_after_server_commit_replays_original_declaration(self):
        self.ready();original=self.case.remote.request;sent=[]
        def lost(path,value):
            sent.append(value);original(path,value)
            raise MarketError('transport_unknown','fixture lost response')
        with patch.object(self.case.remote,'request',side_effect=lost):
            with self.assertRaises(MarketError):self.client.acknowledge(*self.args)
        folder=Path(self.downloaded['path']).parent
        self.assertFalse((folder/'server-receipt.json').exists())
        before=original('/v1/purchases/receipt_status',{'request_id':'buy'})
        self.assertTrue(before['server_acknowledged'])
        after=self.client.acknowledge(*self.args);self.assertEqual(before,after);self.assertEqual(1,len(sent))

    def test_mismatched_server_ack_is_not_saved(self):
        self.ready()
        result={'status':'client_receipt_recorded','request_id':'other','bundle_sha256':'a'*64,
                'acquisition_id':'ap_wrong','usage_id':'u_wrong','receipt_id':'cr_'+'a'*64,'acknowledged_at':1000,
                'evidence':'authenticated_client_statement','server_acknowledged':True,
                'channel_fulfillment_confirmed':False,'task_completed':False}
        with patch.object(self.case.remote,'request',return_value=result):
            with self.assertRaises(MarketError):self.client.acknowledge(*self.args)
        self.assertFalse(Path(self.downloaded['path']).with_name('server-receipt.json').exists())


if __name__=='__main__':unittest.main()
