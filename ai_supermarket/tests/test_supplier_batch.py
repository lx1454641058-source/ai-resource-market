"""来源：本项目原创。供应商批量准备、真实本机发送及不重复提交验证。"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ai_supermarket.common import MarketError, compact
from ai_supermarket.tests import test_http_live as live_fixture
from ai_supermarket.tests.test_exchange import CHECKS
from tools.market_seller import sample_request
from tools.market_supplier_batch import prepare, load_plan, send, draft_request


def manifest(count=2):
    items=[]
    for index in range(count):
        request=sample_request();request.pop('request_id');request['slug']='item-'+str(index)
        request['metadata']['price_cents']=index*100
        items.append({'source_id':'sku-'+str(index),'request':request})
    return {'format':'market-supplier-batch-v1','seller':'vendor','batch_id':'original-test','items':items}


class PreparationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)

    def prepare(self,value):
        path=self.root/'manifest.json';path.write_text(compact(value),encoding='utf-8')
        return prepare(path,self.root/'prepared')

    def test_mixed_valid_invalid_rows_keep_prices_and_report_without_body(self):
        value=manifest(3);value['items'][2]['request']['metadata'].pop('license_text')
        result=self.prepare(value);self.assertEqual({'not_started':2,'needs_changes':1},result['counts'])
        _,plan=load_plan(self.root/'prepared',result['plan_sha256'])
        self.assertEqual(100,plan['items'][1]['request']['metadata']['price_cents'])
        self.assertNotIn('request',plan['items'][2]);self.assertNotIn('content',compact(result))
        self.assertFalse(result['server_contacted']);self.assertFalse(result['rights_verified'])

    def test_duplicate_sources_and_resource_versions_disable_all_conflicts(self):
        value=manifest(4);value['items'][1]['source_id']='sku-0'
        value['items'][3]['request']['slug']='item-2'
        result=self.prepare(value);self.assertEqual({'needs_changes':4},result['counts'])
        self.assertTrue(all(r['errors'][0]['code']=='duplicate_item' for r in result['items']))

    def test_tampered_plan_or_existing_output_stops_without_overwrite(self):
        result=self.prepare(manifest());directory=self.root/'prepared'
        with self.assertRaises(FileExistsError):prepare(self.root/'manifest.json',directory)
        original=(directory/'plan.json').read_bytes();(directory/'plan.json').write_bytes(original+b' ')
        with self.assertRaises(MarketError) as caught:load_plan(directory,result['plan_sha256'])
        self.assertEqual('plan_changed',caught.exception.code)

    def test_stable_request_ids_survive_export_key_order_and_new_batch(self):
        value=manifest()['items'][0]['request']
        reordered=json.loads(json.dumps(value,sort_keys=True))
        self.assertEqual(draft_request('vendor','sku-0',value)['request_id'],
                         draft_request('vendor','sku-0',reordered)['request_id'])
        reordered['content']+='changed'
        self.assertNotEqual(draft_request('vendor','sku-0',value)['request_id'],
                            draft_request('vendor','sku-0',reordered)['request_id'])

    def test_unknown_or_malformed_row_is_isolated_without_echo(self):
        value=manifest();value['items'][0]={'secret-value':'do-not-echo'}
        result=self.prepare(value)
        self.assertEqual('needs_changes',result['items'][0]['status'])
        self.assertNotIn('do-not-echo',compact(result));self.assertEqual('not_started',result['items'][1]['status'])

    def test_whole_manifest_bound_and_empty_batch_rejected(self):
        value=manifest();value['items']=[]
        with self.assertRaises(MarketError):self.prepare(value)
        self.assertFalse((self.root/'prepared').exists())

    def test_origin_file_metadata_and_full_license_preserved_without_fetch(self):
        from ai_supermarket.tests.test_origin import OriginTests, MANIFEST
        origin=OriginTests();origin.setUp();self.addCleanup(origin.doCleanups)
        value=manifest(1);value['items'][0]['request'].update(metadata=origin.metadata(),content=MANIFEST)
        result=self.prepare(value);self.assertEqual({'not_started':1},result['counts'])
        _,plan=load_plan(self.root/'prepared',result['plan_sha256'])
        self.assertEqual(origin.metadata()['origin'],plan['items'][0]['request']['metadata']['origin'])
        self.assertEqual([],origin.requests)


class LiveBatchTests(unittest.TestCase):
    def setUp(self):
        self.f=live_fixture.LiveHTTPTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.remote=self.f.client('vendor');self.directory=self.f.root/'prepared';self.state=self.f.root/'send-state'
        self.value=manifest();self.plan()

    def plan(self):
        path=self.f.root/'manifest.json';path.write_text(compact(self.value),encoding='utf-8')
        self.report=prepare(path,self.directory)

    def send(self,**kwargs):return send(self.directory,self.report['plan_sha256'],self.remote,self.state,**kwargs)

    def counts(self):
        from ai_supermarket.exchange import Exchange
        with Exchange(self.f.db,'operator','reviewer').store.connect() as db:
            return (db.execute('SELECT COUNT(*) FROM submissions').fetchone()[0],
                    db.execute('SELECT COUNT(*) FROM offers').fetchone()[0])

    def test_real_batch_to_review_replay_without_duplicates_then_explicit_review(self):
        first=self.send();self.assertEqual('batch_submitted',first['status'],first)
        self.assertEqual({'submitted_for_review':2},first['counts']);self.assertEqual((2,0),self.counts())
        with patch.object(self.remote,'call',side_effect=AssertionError('completed steps must not repeat')):
            self.assertEqual(first,self.send())
        pending=self.remote.call('seller_status',{'id':'vendor:item-1','version':'1.0.0'})
        self.assertEqual(100,pending['metadata']['price_cents'])
        reviewer=self.f.client('operator')
        reply=reviewer.call('review_decide',{'id':'vendor:item-1','version':'1.0.0','revision':1,
            'decision':'approve','checks':CHECKS,'note':'original isolated fixture review','request_id':'review-item-1'})
        self.assertEqual('approved',reply['status']);self.assertEqual((2,1),self.counts())
        reviewer.call('review_decide',{'id':'vendor:item-0','version':'1.0.0','revision':1,
            'decision':'approve','checks':CHECKS,'note':'original free fixture','request_id':'review-item-0'})
        buyer=self.f.client('alice')
        detail=buyer.call('market_resolve',{'id':'vendor:item-0','version':'1.0.0','purpose':'personal_use'})
        acquired=buyer.call('market_acquire',{'id':'vendor:item-0','version':'1.0.0','purpose':'personal_use',
            'expected_release_digest':detail['release_digest'],'request_id':'read-imported'})
        used=buyer.call('market_read',{'acquisition_id':acquired['acquisition_id'],'task_id':'task','request_id':'read-imported'})
        self.assertEqual(self.value['items'][0]['request']['content'],used['content'])

    def test_wrong_authenticated_seller_stops_before_any_draft(self):
        self.f.identities.create_account('other','seller')
        issued=self.f.identities.issue_key('other');path=self.f.root/'other.json'
        path.write_text(compact({'token':issued['token']}),encoding='utf-8')
        from ai_supermarket.http_client import RemoteMarket
        self.remote=RemoteMarket(self.f.url,path)
        with self.assertRaises(MarketError) as caught:self.send()
        self.assertEqual('seller_mismatch',caught.exception.code);self.assertEqual((0,0),self.counts())

    def test_lost_draft_response_recovers_original_request_and_order(self):
        original=self.remote.call;once=[True];draft_requests=[]
        def dropped(tool,args):
            result=original(tool,args)
            if tool=='seller_draft':
                draft_requests.append(args['request_id'])
                if once[0]:once[0]=False;raise MarketError('transport_unknown','fixture dropped response')
            return result
        with patch.object(self.remote,'call',side_effect=dropped):
            failed=self.send();self.assertEqual('batch_partial',failed['status']);self.assertEqual((1,0),self.counts())
            recovered=self.send()
        self.assertEqual('batch_submitted',recovered['status']);self.assertEqual(draft_requests[0],draft_requests[1])
        self.assertEqual((2,0),self.counts())

    def test_lost_submit_response_resumes_same_revision_without_new_draft(self):
        original=self.remote.call;once=[True]
        def dropped(tool,args):
            result=original(tool,args)
            if tool=='seller_submit' and once[0]:once[0]=False;raise MarketError('transport_unknown','fixture')
            return result
        with patch.object(self.remote,'call',side_effect=dropped):
            self.assertEqual('batch_partial',self.send()['status'])
        with patch.object(self.remote,'call',wraps=original) as calls:
            self.assertEqual('batch_submitted',self.send()['status'])
        self.assertEqual('seller_submit',calls.call_args_list[0].args[0]);self.assertEqual((2,0),self.counts())

    def test_chunk_limit_can_continue_original_plan(self):
        self.assertEqual('batch_partial',self.send(max_items=1)['status']);self.assertEqual((1,0),self.counts())
        self.assertEqual('batch_submitted',self.send(max_items=1)['status']);self.assertEqual((2,0),self.counts())

    def test_unknown_attempts_are_bounded_and_never_automatic(self):
        with patch.object(self.remote,'call',side_effect=MarketError('transport_unknown','fictional-secret')) as calls:
            for _ in range(4):self.assertEqual('transport_unknown',self.send()['interruption']['error_code'])
            result=self.send();self.assertEqual('attempt_limit',result['interruption']['error_code'])
            self.assertEqual(4,calls.call_count);self.assertNotIn('fictional-secret',compact(result))

    def test_time_limit_stops_before_new_mutation(self):
        result=self.send(max_seconds=60,clock=Mock(side_effect=[0,61]))
        self.assertEqual('time_limit',result['interruption']['error_code']);self.assertEqual((0,0),self.counts())

    def test_state_cannot_move_to_another_endpoint_or_key(self):
        self.send(max_items=1)
        self.remote.base_url='https://other.example'
        with patch.object(self.remote,'tool_list',side_effect=AssertionError('must not connect')):
            with self.assertRaises(MarketError):self.send()

    def test_corrupted_saved_revision_blocks_recovery(self):
        self.send(max_items=1)
        path=self.state/'000-draft-ok.json';value=json.loads(path.read_bytes());value['revision']=2
        path.write_text(compact(value),encoding='utf-8')
        with self.assertRaises(MarketError):self.send()

    def test_rate_limit_returns_guidance_and_stops_remaining_items(self):
        with patch.object(self.remote,'call',return_value={'status':'error','error_code':'rate_limited','retry_after_seconds':12}) as call:
            result=self.send()
        self.assertEqual(12,result['interruption']['retry_after_seconds']);self.assertEqual(1,call.call_count)
        self.assertEqual((0,0),self.counts())

    def test_second_export_batch_reuses_original_server_receipts(self):
        self.send()
        self.value['batch_id']='another-export';self.value=json.loads(json.dumps(self.value,sort_keys=True))
        self.directory=self.f.root/'next-plan';self.state=self.f.root/'next-state';self.plan()
        self.assertEqual('batch_submitted',self.send()['status']);self.assertEqual((2,0),self.counts())

    def test_existing_version_change_is_not_silently_revised(self):
        self.send();self.value['items'][0]['request']['metadata']['price_cents']=700
        self.directory=self.f.root/'changed-plan';self.state=self.f.root/'changed-state';self.plan()
        result=self.send();self.assertEqual('version_exists',result['interruption']['error_code'])
        detail=self.remote.call('seller_status',{'id':'vendor:item-0','version':'1.0.0'})
        self.assertEqual(0,detail['metadata']['price_cents']);self.assertEqual('pending_review',detail['status'])

    def test_invalid_row_does_not_prevent_other_valid_rows(self):
        self.value['items'][0]['request']['metadata'].pop('license_text')
        self.directory=self.f.root/'mixed-plan';self.plan()
        result=self.send();self.assertEqual('batch_needs_changes',result['status'])
        self.assertEqual({'needs_changes':1,'submitted_for_review':1},result['counts'])
        self.assertEqual((1,0),self.counts())

    def test_cli_from_foreign_directory_uses_fixed_seller_and_plan(self):
        import subprocess,sys
        cli=Path(__file__).resolve().parents[2]/'tools/market_supplier_batch.py'
        result=subprocess.run([sys.executable,'-B',str(cli),'send','--directory',str(self.directory),
            '--expected-plan-sha256',self.report['plan_sha256'],'--url',self.f.url,
            '--credential-file',str(self.remote.credential_file),'--state-directory',str(self.state)],
            cwd=self.f.root,input=b'',capture_output=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stderr.decode(errors='replace'))
        self.assertEqual('batch_submitted',json.loads(result.stdout)['status'])
        self.assertNotIn(self.f.issued['vendor']['token'].encode(),result.stdout)
        self.assertEqual((2,0),self.counts())

    def test_batch_origin_file_reaches_buyer_after_separate_review(self):
        from ai_supermarket.tests.test_origin import OriginTests, MANIFEST, RAW, POLICY
        from ai_supermarket.exchange import Exchange
        from ai_supermarket.origin import deliver_origin
        source=OriginTests();source.setUp();self.addCleanup(source.doCleanups)
        self.value=manifest(1)
        self.value['items'][0]['request'].update(metadata=source.metadata(),content=MANIFEST)
        self.directory=self.f.root/'origin-plan';self.plan()
        self.assertEqual('batch_submitted',self.send()['status']);self.assertEqual([],source.requests)
        self.f.client('operator').call('review_decide',{'id':'vendor:item-0','version':'1.0.0','revision':1,
            'decision':'approve','checks':CHECKS,'note':'original origin fixture','request_id':'origin-review'})
        self.f.identities.create_account('buyer','buyer',POLICY)
        issued=self.f.identities.issue_key('buyer');credential=self.f.root/'origin-buyer.json'
        credential.write_text(compact({'token':issued['token']}),encoding='utf-8');self.f.credentials['buyer']=credential
        buyer=self.f.client('buyer')
        detail=buyer.call('market_resolve',{'id':'vendor:item-0','version':'1.0.0','purpose':'personal_use'})
        acquired=buyer.call('market_acquire',{'id':'vendor:item-0','version':'1.0.0','purpose':'personal_use',
            'expected_release_digest':detail['release_digest'],'request_id':'origin-get'})
        local=Exchange(self.f.db,'buyer',host_policy=POLICY)
        source.buyer=local
        done=deliver_origin(local,acquired['acquisition_id'],'task','origin-read',source.client,self.f.root/'delivered')
        self.assertEqual(RAW,Path(done['path']).read_bytes());self.assertEqual(1,len(source.requests))


if __name__=='__main__':unittest.main()
