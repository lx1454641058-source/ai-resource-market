"""本项目原创。卖方历史收货、原站编号映射、真实本机HTTP和CLI，无资金或外部发布。"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from ai_supermarket.api_quota import canonical,checksum
from ai_supermarket.common import MarketError,compact
from ai_supermarket.exchange import Exchange
from ai_supermarket.exchange_tools import ExchangeInterface
from ai_supermarket.http_client import RemoteMarket
from ai_supermarket.tests import test_api_remote as fixture
from tools.market_supplier_batch import prepare
from tools.market_supplier_receipts import collect


class SupplierReceiptTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ApiRemoteTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.root=self.f.root;self.seller=self.f.f.seller
        self.f.identities.create_account('vendor','seller')
        self.issued=self.f.identities.issue_key('vendor')
        self.credential=self.root/'seller-credential.json'
        self.credential.write_text(compact({'token':self.issued['token']}),encoding='utf-8')
        value={'format':'market-supplier-batch-v1','seller':'vendor','batch_id':'stock-source',
               'items':[{'source_id':'original-stock','request':{'slug':'stock','version':'1.0.0',
               'metadata':self.f.f.meta,'content':fixture.fixture.CONTENT}}]}
        source=self.root/'source.json';source.write_text(compact(value),encoding='utf-8')
        self.plan=self.root/'plan';self.plan_sha=prepare(source,self.plan)['plan_sha256']

    def ack(self,request='call-one'):
        payload={**self.f.payload,'request_id':request}
        self.assertEqual(200,self.f.fetch('call',payload)[0])
        result=self.f.fetch('result',payload)[1]
        payload.update(usage_id=result['usage_id'],manifest_sha256=checksum(canonical(result['manifest'],8192)))
        reply=self.f.fetch('receipt',payload)[1]
        self.assertEqual('api_receipt_recorded',reply['status'],reply)
        return reply

    def remote(self):return RemoteMarket(self.f.live().base_url,self.credential)

    def read(self,**kwargs):return self.seller.api_receipts('vendor:stock','1.0.0',**kwargs)

    def test_only_acknowledged_events_and_minimal_fields(self):
        self.f.fetch();self.assertEqual([],self.read()['items'])
        ack=self.ack();page=self.read();self.assertEqual(1,len(page['items']))
        item=page['items'][0];self.assertEqual(ack['receipt_id'],item['receipt_id'])
        self.assertEqual(self.f.f.calls[0],item['operation_id'])
        self.assertEqual({'receipt_id','operation_id','provider','release_digest','manifest_sha256','result_sha256','acknowledged_at','evidence'},set(item))
        for secret in (self.f.acquired,self.f.token,self.f.issued['key_id'],'fictional','warehouse'):
            self.assertNotIn(secret,compact(page))
        self.assertFalse(page['package_fulfillment_confirmed']);self.assertFalse(page['payment_executed'])

    def test_role_ownership_and_extra_scope_denied(self):
        self.ack()
        other=Exchange(self.f.f.path,'other','seller')
        self.assertEqual('not_found',ExchangeInterface(other).call('seller_api_receipts',{'id':'vendor:stock','version':'1.0.0'})['error_code'])
        for market in (self.f.f.buyer,self.f.f.reviewer):
            self.assertEqual('unknown_tool',ExchangeInterface(market).call('seller_api_receipts',{})['error_code'])
        self.assertEqual('invalid_params',ExchangeInterface(self.seller).call('seller_api_receipts',{
            'id':'vendor:stock','version':'1.0.0','buyer':'buyer'})['error_code'])

    def test_paging_incremental_empty_and_new_receipt(self):
        first=self.ack();second=self.ack('second')
        page=self.read(limit=1);self.assertTrue(page['has_more']);self.assertEqual(first['receipt_id'],page['items'][0]['receipt_id'])
        next_page=self.read(after=page['next_after'],limit=1)
        self.assertFalse(next_page['has_more']);self.assertEqual(second['receipt_id'],next_page['items'][0]['receipt_id'])
        empty=self.read(after=next_page['next_after'])
        self.assertEqual([],empty['items']);self.assertEqual(next_page['next_after'],empty['next_after'])
        issued=self.f.identities.issue_key('buyer')
        self.f.app.gateway.bind_key(issued['key_id'],'root','task','buyer','new-key-binding')
        self.f.app.apis.hosts[issued['key_id']]={'provider-test':self.f.host};self.f.token=issued['token']
        third=self.ack()
        added=self.read(after=empty['next_after'])
        self.assertEqual(third['receipt_id'],added['items'][0]['receipt_id']);self.assertEqual(2,len(self.f.f.calls))

    def test_historical_receipt_survives_buyer_expiry_and_product_withdrawal(self):
        self.ack();self.f.f.now=5000;self.f.identities.revoke_key(self.f.issued['key_id'])
        self.seller.availability('vendor:stock','1.0.0',1,'withdrawn','停止新获取','withdraw')
        page=self.read();self.assertEqual(1,len(page['items']))
        self.assertEqual('historical_client_statements',page['scope'])

    def test_corrupt_binding_fails_without_partial_page(self):
        self.ack()
        with self.seller.store.connect(write=True) as db:
            row=db.execute("SELECT key,value FROM meta WHERE key GLOB 'api_client_receipt:*'").fetchone()
            record=json.loads(row['value']);record['receipt_id']='ar_wrong'
            db.execute('UPDATE meta SET value=? WHERE key=?',(compact(record),row['key']))
        reply=ExchangeInterface(self.seller).call('seller_api_receipts',{'id':'vendor:stock','version':'1.0.0'})
        self.assertEqual('receipt_corrupt',reply['error_code']);self.assertNotIn('items',reply)

    def test_read_only_does_not_execute_or_change_receipts(self):
        self.ack()
        with self.seller.store.connect() as db:before=[tuple(row) for row in db.execute('SELECT * FROM meta ORDER BY key')]
        for _ in range(3):self.read()
        with self.seller.store.connect() as db:after=[tuple(row) for row in db.execute('SELECT * FROM meta ORDER BY key')]
        self.assertEqual(before,after);self.assertEqual(1,len(self.f.f.calls))

    def test_actual_http_source_mapping_export_and_revoked_seller(self):
        self.ack();remote=self.remote();output=self.root/'export'
        result=collect(self.plan,self.plan_sha,'original-stock',remote,output)
        self.assertEqual('original-stock',result['source_id']);self.assertEqual('vendor:stock',result['page']['id'])
        self.assertEqual(1,len(result['page']['items']));self.assertFalse(result['settlement_executed'])
        self.assertNotIn(self.issued['token'],Path(result['path']).read_text(encoding='utf-8'))
        self.f.identities.revoke_key(self.issued['key_id'])
        self.assertEqual('unauthenticated',remote.call('seller_api_receipts',{'id':'vendor:stock','version':'1.0.0'})['error_code'])

    def test_cli_uses_plan_without_resubmitting_or_model(self):
        self.ack();remote=self.remote();tool=Path(__file__).resolve().parents[2]/'tools/market_supplier_receipts.py'
        proc=subprocess.run([sys.executable,'-X','utf8','-B',str(tool),'--directory',str(self.plan),
            '--expected-plan-sha256',self.plan_sha,'--source-id','original-stock','--url',remote.base_url,
            '--credential-file',str(self.credential),'--output',str(self.root/'cli')],cwd=self.root,
            capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(0,proc.returncode,proc.stdout+proc.stderr)
        reply=json.loads(proc.stdout);self.assertEqual('exported',reply['status']);self.assertEqual(0,reply['model_calls'])
        self.assertEqual(1,len(self.f.f.calls))

    def test_invalid_plan_or_source_stops_before_network(self):
        remote=self.remote()
        for sha,source in [('b'*64,'original-stock'),(self.plan_sha,'missing')]:
            with patch.object(remote,'request') as network,self.assertRaises(MarketError):
                collect(self.plan,sha,source,remote,self.root/'bad')
            network.assert_not_called()

    def test_unexpected_sensitive_field_in_remote_page_refused(self):
        self.ack();remote=self.remote();original=remote.call
        def changed(*args):
            result=original(*args);result['items'][0]['buyer']='private'
            return result
        output=self.root/'invalid'
        with patch.object(remote,'call',side_effect=changed),self.assertRaises(MarketError):
            collect(self.plan,self.plan_sha,'original-stock',remote,output)
        self.assertFalse((output/'receipts.json').exists())

    def test_changed_page_preserves_previous_snapshot(self):
        self.ack();remote=self.remote();output=self.root/'snapshot'
        collect(self.plan,self.plan_sha,'original-stock',remote,output)
        before=(output/'receipts.json').read_bytes();self.ack('second')
        with self.assertRaises(MarketError):collect(self.plan,self.plan_sha,'original-stock',remote,output)
        self.assertEqual(before,(output/'receipts.json').read_bytes())


if __name__=='__main__':unittest.main()
