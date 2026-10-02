"""来源：本项目原创。供应商差异、价格版本与原请求恢复；仅本机隔离数据。"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from ai_supermarket.common import MarketError, compact
from ai_supermarket.tests import test_supplier_batch as batch_fixture
from ai_supermarket.tests.test_exchange import CHECKS
from tools.market_supplier_batch import prepare
from tools.market_supplier_sync import preview, apply, load_delta


class SupplierSyncTests(unittest.TestCase):
    def setUp(self):
        self.f=batch_fixture.LiveBatchTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.remote=self.f.remote;self.directory=self.f.directory;self.plan_sha=self.f.report['plan_sha256']
        self.output=self.f.f.root/'delta';self.state=self.f.f.root/'sync-state'

    def preview(self,**kwargs):return preview(self.directory,self.plan_sha,self.remote,self.output,**kwargs)
    def apply(self,sha,**kwargs):return apply(self.output,sha,self.remote,self.state,**kwargs)

    def update_plan(self,value):
        path=self.f.f.root/'changed.json';path.write_text(compact(value),encoding='utf-8')
        self.directory=self.f.f.root/'changed-plan'
        self.plan_sha=prepare(path,self.directory)['plan_sha256']

    def approve(self,index):
        return self.f.f.client('operator').call('review_decide',{'id':f'vendor:item-{index}','version':'1.0.0',
            'revision':1,'decision':'approve','checks':CHECKS,'note':'original sync fixture','request_id':f'approve-{index}'})

    def status(self,index):return self.remote.call('seller_status',{'id':f'vendor:item-{index}','version':'1.0.0'})

    def test_missing_resources_create_and_submit_with_separate_review(self):
        result=self.preview();self.assertEqual({'create':2},result['counts']);self.assertEqual((0,0),self.f.counts())
        done=self.apply(result['delta_sha256']);self.assertEqual('sync_applied',done['status'])
        self.assertEqual((2,0),self.f.counts());self.assertTrue(all(row['completed'] for row in done['items']))
        with patch.object(self.remote,'call',side_effect=AssertionError('must not replay completed mutations')):
            self.assertEqual(done,self.apply(result['delta_sha256']))

    def test_chunked_preview_resumes_cached_rows_without_requery(self):
        first=self.preview(max_items=1);self.assertEqual('preview_partial',first['status']);self.assertNotIn('delta_sha256',first)
        with patch.object(self.remote,'call',wraps=self.remote.call) as calls:
            last=self.preview(max_items=1)
        self.assertEqual('preview_ready',last['status']);self.assertEqual(1,calls.call_count)
        self.assertEqual('vendor:item-1',calls.call_args.args[1]['id'])

    def test_pending_review_is_not_resubmitted_or_revised(self):
        self.f.send();changed=deepcopy(self.f.value);changed['items'][1]['request']['content']+='new'
        self.update_plan(changed);result=self.preview()
        self.assertEqual({'waiting_review':1,'review_in_progress':1},result['counts'])
        with patch.object(self.remote,'call',side_effect=AssertionError('no mutations')):
            self.assertEqual('sync_needs_attention',self.apply(result['delta_sha256'])['status'])

    def test_approved_price_uses_effective_offer_not_original_metadata(self):
        self.f.send();self.approve(0);self.approve(1)
        self.remote.call('seller_reprice',{'id':'vendor:item-0','version':'1.0.0','offer_revision':1,'price_cents':30,'request_id':'existing-price'})
        changed=deepcopy(self.f.value);changed['items'][0]['request']['metadata']['price_cents']=30
        changed['items'][1]['request']['metadata']['price_cents']=150;self.update_plan(changed)
        result=self.preview();self.assertEqual({'unchanged':1,'reprice':1},result['counts'])
        self.assertEqual(100,result['items'][1]['old_price_cents'])
        done=self.apply(result['delta_sha256']);self.assertEqual('sync_applied',done['status'])
        self.assertEqual(150,self.status(1)['offer']['price_cents']);self.assertEqual(100,self.status(1)['metadata']['price_cents'])

    def test_approved_body_or_license_changes_require_new_version(self):
        self.f.send();self.approve(0);self.approve(1)
        changed=deepcopy(self.f.value);changed['items'][0]['request']['content']+='changed'
        changed['items'][1]['request']['metadata']['license_text']+='changed';self.update_plan(changed)
        result=self.preview();self.assertEqual({'new_version_required':2},result['counts'])
        with patch.object(self.remote,'call',side_effect=AssertionError('no overwrite')):
            self.assertEqual('sync_needs_attention',self.apply(result['delta_sha256'])['status'])

    def test_draft_revision_and_matching_draft_submission(self):
        from tools.market_supplier_batch import load_plan
        _,plan=load_plan(self.directory,self.plan_sha)
        for row in plan['items']:self.remote.call('seller_draft',row['request'])
        changed=deepcopy(self.f.value);changed['items'][1]['request']['content']+='new content';self.update_plan(changed)
        result=self.preview();self.assertEqual({'submit_existing':1,'revise_and_submit':1},result['counts'])
        self.assertEqual('sync_applied',self.apply(result['delta_sha256'])['status'])
        self.assertEqual(1,self.status(0)['revision']);self.assertEqual(2,self.status(1)['revision'])
        self.assertEqual('pending_review',self.status(1)['status'])

    def test_unchanged_rejected_content_does_not_auto_resubmit(self):
        self.f.send()
        for index in range(2):
            self.f.f.client('operator').call('review_decide',{'id':f'vendor:item-{index}','version':'1.0.0','revision':1,
                'decision':'reject','checks':CHECKS,'note':'fixture rejected','request_id':f'reject-{index}'})
        changed=deepcopy(self.f.value);changed['items'][1]['request']['content']+='correction';self.update_plan(changed)
        result=self.preview();self.assertEqual({'review_rejected':1,'revise_and_submit':1},result['counts'])
        self.assertEqual('sync_needs_attention',self.apply(result['delta_sha256'])['status'])
        self.assertEqual('rejected',self.status(0)['status']);self.assertEqual('pending_review',self.status(1)['status'])

    def test_concurrent_price_change_refuses_stale_preview(self):
        self.f.send();self.approve(0)
        changed=deepcopy(self.f.value);changed['items'][0]['request']['metadata']['price_cents']=50;self.update_plan(changed)
        result=self.preview()
        self.remote.call('seller_reprice',{'id':'vendor:item-0','version':'1.0.0','offer_revision':1,'price_cents':60,'request_id':'concurrent'})
        done=self.apply(result['delta_sha256']);self.assertEqual('revision_conflict',done['interruption']['error_code'])
        self.assertEqual(60,self.status(0)['offer']['price_cents'])

    def test_lost_price_response_replays_original_operation_once(self):
        self.f.send();self.approve(0)
        changed=deepcopy(self.f.value);changed['items'][0]['request']['metadata']['price_cents']=50;self.update_plan(changed)
        result=self.preview();original=self.remote.call
        def dropped(tool,args):
            response=original(tool,args)
            if tool=='seller_reprice':raise MarketError('transport_unknown','fixture')
            return response
        with patch.object(self.remote,'call',side_effect=dropped):
            self.assertEqual('sync_partial',self.apply(result['delta_sha256'])['status'])
        self.assertEqual(2,self.status(0)['offer']['revision'])
        self.assertEqual('sync_applied',self.apply(result['delta_sha256'])['status'])
        self.assertEqual(2,self.status(0)['offer']['revision'])

    def test_lost_revise_response_recovers_then_submits_original_revision(self):
        from tools.market_supplier_batch import load_plan
        _,plan=load_plan(self.directory,self.plan_sha)
        self.remote.call('seller_draft',plan['items'][0]['request'])
        changed=deepcopy(self.f.value);changed['items'][0]['request']['content']+='new';self.update_plan(changed)
        result=self.preview();original=self.remote.call
        def dropped(tool,args):
            response=original(tool,args)
            if tool=='seller_revise':raise MarketError('transport_unknown','fixture')
            return response
        with patch.object(self.remote,'call',side_effect=dropped):self.assertEqual('sync_partial',self.apply(result['delta_sha256'])['status'])
        self.assertEqual('sync_applied',self.apply(result['delta_sha256'])['status'])
        self.assertEqual(2,self.status(0)['revision']);self.assertEqual('pending_review',self.status(0)['status'])

    def test_withdrawn_and_suspended_items_are_not_reactivated(self):
        self.f.send();self.approve(0);self.approve(1)
        self.remote.call('seller_withdraw',{'id':'vendor:item-0','version':'1.0.0','offer_revision':1,'reason':'fixture','request_id':'withdraw'})
        self.f.f.client('operator').call('review_availability',{'id':'vendor:item-1','version':'1.0.0','offer_revision':1,
            'state':'suspended','reason':'fixture','request_id':'suspend'})
        result=self.preview();self.assertEqual({'inactive_requires_operator':2},result['counts'])
        self.assertEqual('sync_needs_attention',self.apply(result['delta_sha256'])['status'])

    def test_unknown_status_cannot_become_create_and_has_no_auto_retry(self):
        with patch.object(self.remote,'call',return_value={'status':'error','error_code':'unavailable'}) as calls:
            result=self.preview()
        self.assertEqual('preview_partial',result['status']);self.assertEqual(1,calls.call_count)
        self.assertNotIn('create',result['counts']);self.assertFalse((self.output/'delta.json').exists())

    def test_wrong_identity_or_tampered_delta_cannot_execute(self):
        result=self.preview();before=(self.output/'delta.json').read_bytes()
        (self.output/'delta.json').write_bytes(before+b' ')
        with self.assertRaises(MarketError):self.apply(result['delta_sha256'])
        with patch.object(self.remote,'tool_list',return_value={'role':'seller','seller_principal':'other'}):
            with self.assertRaises(MarketError):self.preview()
        self.assertEqual((0,0),self.f.counts())

    def test_apply_limits_and_state_cannot_change_delta(self):
        result=self.preview();done=self.apply(result['delta_sha256'],max_items=1)
        self.assertEqual('sync_partial',done['status']);self.assertEqual((1,0),self.f.counts())
        self.assertEqual('sync_applied',self.apply(result['delta_sha256'])['status'])
        self.remote.base_url='https://other.example'
        with self.assertRaises(MarketError):self.apply(result['delta_sha256'])

    def test_invalid_rows_remain_visible_without_executable_steps(self):
        changed=deepcopy(self.f.value);changed['items'][1]['request']['metadata'].pop('license_text');self.update_plan(changed)
        result=self.preview();self.assertEqual({'create':1,'needs_changes':1},result['counts'])
        done=self.apply(result['delta_sha256']);self.assertEqual('sync_needs_attention',done['status']);self.assertEqual((1,0),self.f.counts())

    def test_preview_and_apply_cli_from_foreign_directory(self):
        cli=Path(__file__).resolve().parents[2]/'tools/market_supplier_sync.py'
        prefix=[sys.executable,'-B',str(cli)]
        common=['--url',self.f.f.url,'--credential-file',str(self.remote.credential_file)]
        inspected=subprocess.run([*prefix,'preview','--directory',str(self.directory),'--expected-plan-sha256',self.plan_sha,
            '--output',str(self.output),*common],cwd=self.f.f.root,capture_output=True,timeout=15)
        self.assertEqual(0,inspected.returncode,inspected.stderr.decode(errors='replace'));report=json.loads(inspected.stdout)
        executed=subprocess.run([*prefix,'apply','--directory',str(self.output),'--expected-delta-sha256',report['delta_sha256'],
            '--state-directory',str(self.state),*common],cwd=self.f.f.root,capture_output=True,timeout=15)
        self.assertEqual(0,executed.returncode,executed.stderr.decode(errors='replace'))
        self.assertEqual('sync_applied',json.loads(executed.stdout)['status'])
        self.assertNotIn(self.f.f.issued['vendor']['token'].encode(),executed.stdout)

    def test_reprice_preserves_prior_entitlement_without_new_purchase(self):
        self.f.send();self.approve(0)
        buyer=self.f.f.client('alice');ref={'id':'vendor:item-0','version':'1.0.0','purpose':'personal_use'}
        detail=buyer.call('market_resolve',ref)
        acquired=buyer.call('market_acquire',{**ref,'request_id':'before-price',
            'expected_release_digest':detail['release_digest']})
        changed=deepcopy(self.f.value);changed['items'][0]['request']['metadata']['price_cents']=50;self.update_plan(changed)
        result=self.preview();self.assertEqual('sync_applied',self.apply(result['delta_sha256'])['status'])
        read=buyer.call('market_read',{'acquisition_id':acquired['acquisition_id'],'task_id':'task','request_id':'after-price'})
        self.assertEqual('read',read['status']);self.assertEqual(detail['release_digest'],buyer.call('market_resolve',ref)['release_digest'])
        other=self.f.f.client('bob').call('market_acquire',{**ref,'request_id':'new-buyer',
            'expected_release_digest':detail['release_digest']})
        self.assertEqual('payment_unavailable',other['status']);self.assertEqual(50,other['price_cents'])

    def test_changed_draft_revision_stops_without_replacing_other_edit(self):
        from tools.market_supplier_batch import load_plan
        _,plan=load_plan(self.directory,self.plan_sha);request=plan['items'][0]['request']
        self.remote.call('seller_draft',request)
        changed=deepcopy(self.f.value);changed['items'][0]['request']['content']+='planned';self.update_plan(changed)
        result=self.preview()
        self.remote.call('seller_revise',{'id':'vendor:item-0','version':'1.0.0','revision':1,
            'metadata':request['metadata'],'content':request['content']+'concurrent','request_id':'other-editor'})
        response=self.apply(result['delta_sha256']);self.assertEqual('revision_conflict',response['interruption']['error_code'])
        self.assertTrue(self.status(0)['content'].endswith('concurrent'))


if __name__=='__main__':unittest.main()
