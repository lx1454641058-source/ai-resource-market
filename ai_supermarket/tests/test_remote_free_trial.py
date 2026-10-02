"""来源：本项目原创。免费任务接入与原请求恢复，不使用商户、钱包或模型调用。"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_supermarket.budget import BudgetLedger
from ai_supermarket.common import MarketError
from ai_supermarket.remote_task import RemoteTaskResources, RemoteTaskInterface
from ai_supermarket.task_budget_gateway import TaskBudgetGateway
from ai_supermarket.tests import test_http_live as live_fixture
from tools.market_remote_try import prepare, application


class FreeTaskTests(unittest.TestCase):
    def setUp(self):
        self.f=live_fixture.LiveHTTPTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.ref=self.f.publish();self.remote=self.f.client('alice')
        self.ledger=BudgetLedger(self.f.root/'budget.sqlite')
        self.ledger.create('root','owner',0,'free-trial');self.ledger.grant_task('root','main','alice')
        self.f.app.gateway=TaskBudgetGateway(self.f.db,self.f.root/'identities.sqlite',self.f.root/'budget.sqlite')
        self.f.app.gateway.bind_key(self.f.issued['alice']['key_id'],'root','main','alice','free-trial')
        self.output=self.f.root/'output';self.output.mkdir()

    def host(self,**kwargs):
        return RemoteTaskResources(self.remote,'main','简报','personal_use',self.output,mode='free_text',**kwargs)

    def test_real_free_task_without_purchase_host_preserves_license_and_rights(self):
        self.assertIsNone(self.f.app.purchases)
        host=self.host();self.assertEqual('ready',host.connection['status'])
        detail=host.resolve(**self.ref);done=host.use(**self.ref,request_id='brief')
        self.assertEqual('read',done['status']);self.assertEqual(detail['release_digest'],done['release_digest'])
        self.assertEqual(detail['license_text'],Path(done['license_path']).read_text(encoding='utf-8'))
        self.assertEqual(done['content'],Path(done['path']).read_text(encoding='utf-8'))
        self.assertFalse(done['task_completed']);self.assertFalse(done['execution_authorized'])
        self.assertEqual(done,self.host().use(**self.ref,request_id='brief'))
        with self.f.app.gateway.budgets.store.connect() as db:
            self.assertEqual(0,db.execute('SELECT COUNT(*) FROM reservations').fetchone()[0])
        with self.f.app.gateway.identities.store.connect() as db:
            self.assertEqual(4,db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0])

    def test_free_tool_surface_and_host_payment_methods_are_closed(self):
        host=self.host();interface=RemoteTaskInterface(host)
        names=[t['name'] for t in interface.tool_list()]
        self.assertEqual(['market_search','market_resolve','market_use','market_feedback'],names)
        self.assertEqual('unknown_tool',interface.call('market_receipt',{'request_id':'brief'})['error_code'])
        for method in (host.status,host.acknowledge):
            with self.assertRaises(MarketError):method('brief')
        host.resolve(**self.ref);host.use(**self.ref,request_id='brief')
        with self.assertRaises(MarketError):host.resume_payment('brief','not-a-real-proof')
        with self.assertRaises(MarketError):host.recover_wallet_handoff('brief')

    def test_paid_or_dependency_or_file_details_are_not_pinned(self):
        host=self.host();original=self.remote.call
        detail=original('market_resolve',{**self.ref,'purpose':'personal_use'})
        for kind in ('price','dependency','permissions','file'):
            result=deepcopy(detail)
            if kind=='price':result['price_cents']=10
            if kind=='dependency':result['usage_contract']['dependencies']=[{'id':'tool'}]
            if kind=='permissions':result['usage_contract']['permissions']=['code_execution']
            if kind=='file':result['host_transfer_required']=True
            with patch.object(self.remote,'call',return_value=result):
                self.assertFalse(host.resolve(**self.ref)['task_entry_compatible'])
            with self.assertRaises(MarketError):host._pin(**self.ref)

    def test_price_change_after_resolve_cannot_charge(self):
        host=self.host();host.resolve(**self.ref)
        self.f.client('vendor').call('seller_reprice',{**self.ref,'offer_revision':1,'price_cents':10,'request_id':'raise'})
        result=host.use(**self.ref,request_id='brief')
        self.assertEqual('payment_unavailable',result['status'])
        self.assertEqual([],list(self.output.glob('text-*')))

    def test_revocation_blocks_cached_delivery(self):
        host=self.host();host.resolve(**self.ref);done=host.use(**self.ref,request_id='brief')
        self.f.identities.revoke_key(self.f.issued['alice']['key_id'])
        result=self.host().use(**self.ref,request_id='brief')
        self.assertEqual('unauthenticated',result['error_code']);self.assertNotIn('content',result)
        self.assertTrue(Path(done['path']).exists())

    def test_changed_body_is_not_saved_or_returned(self):
        host=self.host();host.resolve(**self.ref);original=self.remote.call
        def changed(name,args):
            result=original(name,args)
            return {**result,'content':'changed'} if name=='market_read' else result
        with patch.object(self.remote,'call',side_effect=changed):
            with self.assertRaises(MarketError) as caught:host.use(**self.ref,request_id='brief')
        self.assertEqual('download_scope_mismatch',caught.exception.code)
        self.assertEqual([],list(self.output.glob('text-*')))

    def test_user_edited_text_is_preserved(self):
        host=self.host();host.resolve(**self.ref);done=host.use(**self.ref,request_id='brief')
        target=Path(done['path']);target.write_text('user edited',encoding='utf-8')
        with self.assertRaises(MarketError):self.host().use(**self.ref,request_id='brief')
        self.assertEqual('user edited',target.read_text(encoding='utf-8'))

    def test_project_length_output_root_avoids_double_digest_nesting(self):
        size=max(1,105-len(str(self.output))-1)
        self.output=self.output/('p'*size);self.output.mkdir()
        host=self.host();host.resolve(**self.ref);done=host.use(**self.ref,request_id='brief')
        self.assertEqual('read',done['status']);self.assertTrue(Path(done['path']).is_file())
        self.assertEqual(self.output,Path(done['path']).parent.parent)

    def test_every_free_acquire_and_read_consumes_a_call_slot(self):
        host=self.host(max_calls=4);host.resolve(**self.ref)
        with self.assertRaises(MarketError) as caught:host.use(**self.ref,request_id='brief')
        self.assertEqual('task_call_limit',caught.exception.code)
        self.assertEqual(4,len(list((host.directory/'calls').iterdir())))
        self.assertEqual([],list((host.directory/'usages').iterdir()))

    def test_mode_cannot_change_same_task_state(self):
        self.host()
        with self.assertRaises(MarketError):
            RemoteTaskResources(self.remote,'main','简报','personal_use',self.output)

    def test_free_mode_still_requires_bound_task(self):
        self.f.app.gateway.revoke_binding(self.f.issued['alice']['key_id'])
        host=self.host();self.assertEqual('error',host.connection['status'])
        with self.assertRaises(MarketError):host.resolve(**self.ref)


class RemoteTrialPreparationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)/'trial'

    def test_new_isolated_zero_budget_trial_checks_without_merchant(self):
        result=prepare(self.root,18769);app,marker=application(self.root)
        self.assertEqual('prepared',result['status']);self.assertIsNone(app.purchases)
        self.assertFalse(result['payment_enabled']);self.assertFalse(result['server_started'])
        config=json.loads((self.root/'remote-buyer.mcp.json').read_bytes())
        self.assertIn('free_text',config['mcpServers']['ai-supermarket-task']['args'])
        self.assertEqual(0,marker['limit_cents'])
        with self.assertRaises(FileExistsError):prepare(self.root,18769)

    def test_changed_or_missing_configuration_is_not_recreated(self):
        prepare(self.root,18769)
        marker=json.loads((self.root/'remote-trial.json').read_bytes());marker['limit_cents']=10
        (self.root/'remote-trial.json').write_text(json.dumps(marker),encoding='utf-8')
        with self.assertRaises(MarketError):application(self.root)
        missing=self.root.parent/'absent'
        with self.assertRaises(OSError):application(missing)
        self.assertFalse(missing.exists())

    def test_check_cli_from_foreign_directory_does_not_expose_credentials(self):
        prepare(self.root,18769)
        cli=Path(__file__).resolve().parents[2]/'tools/market_remote_try.py'
        result=subprocess.run([sys.executable,'-B',str(cli),'check','--directory',str(self.root)],
            cwd=self.root,capture_output=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stderr.decode(errors='replace'))
        reply=json.loads(result.stdout);self.assertEqual('checked',reply['status'])
        self.assertNotIn(b'token',result.stdout);self.assertNotIn(b'ak_',result.stdout)


if __name__=='__main__':unittest.main()
