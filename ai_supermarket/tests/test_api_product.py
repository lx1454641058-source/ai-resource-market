"""本项目原创。API商品、真实市场权益、次数与本地文件交接；无真实付款。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_supermarket.api_delivery import ApiDeliveryHost
from ai_supermarket.api_product import MarketApiAuthority, validate_api, validate_input
from ai_supermarket.api_quota import ProviderQuota
from ai_supermarket.common import MarketError, compact
from ai_supermarket.contracts import HostPolicy
from ai_supermarket.exchange import Exchange, validate_listing
from ai_supermarket.exchange_tools import ExchangeInterface
from ai_supermarket.tests.test_exchange import listing, CHECKS
from tools.market_seller import check_request
from tools.market_supplier_batch import prepare


POLICY={'capabilities':['invoke_api'],'permissions':['network','file_write'],'dependencies':[],'extra_budget_cents':0}
CONTENT='# 虚构仓库查询API\n用于本项目本地测试，不代表真实库存。'


def metadata(price=0):
    value=listing(price)
    value.update(kind='api',title='虚构仓库查询',keywords=['仓库查询','stock'],
                 input='fictional仓库编号',output='JSON库存记录',check='count为整数')
    value['usage_contract'].update(version=4,delivery='provider_api',capabilities=['invoke_api'],
                                   permissions=['network','file_write'])
    value['api']={'provider':'provider-test','operation':'stock','total_units':2,'validity_seconds':3600,
                  'result_max_bytes':1024,'input_fields':[{'name':'warehouse','type':'string','required':True,
                                                        'limit':32,'description':'原创虚构仓库编号'}]}
    return value


class ApiProductTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root=Path(temp.name); self.path=self.root/'market.sqlite'; self.now=1000
        self.seller=Exchange(self.path,'vendor','seller',clock=lambda:self.now)
        self.reviewer=Exchange(self.path,'operator','reviewer',clock=lambda:self.now)
        self.buyer=Exchange(self.path,'buyer',clock=lambda:self.now,host_policy=POLICY)
        self.meta=metadata(); self.allowed=True; self.calls=[]; self.results={}
        self.publish()
        self.digest=self.buyer.resolve('vendor:stock','1.0.0','personal_use')['release_digest']

    def publish(self):
        reply=ExchangeInterface(self.seller).call('seller_draft',{'slug':'stock','version':'1.0.0',
            'metadata':self.meta,'content':CONTENT,'request_id':'draft'})
        self.assertEqual('draft',reply['status'],reply)
        self.seller.submit('vendor:stock','1.0.0',1,'submit')
        self.reviewer.review('vendor:stock','1.0.0',1,'approve',CHECKS,'原创隔离API契约检查','review')

    def acquire(self, request='acquire'):
        return self.buyer.acquire('vendor:stock','1.0.0',request,purpose='personal_use',expected_release_digest=self.digest)

    def authorize_task(self, principal, task, provider):
        return self.allowed and principal=='buyer' and task in ('task','task-2') and provider=='provider-test'

    def execute(self, operation_id, operation, inputs):
        self.calls.append(operation_id)
        self.results[operation_id]={'count':7,'warehouse':inputs['warehouse']}
        return self.results[operation_id]

    def lookup(self, operation_id):
        if operation_id not in self.results: return {'state':'unknown'}
        return {'state':'completed','result':self.results[operation_id],'evidence_ref':'original-provider-record'}

    def host(self, **changes):
        options={'market':self.buyer,'provider':'provider-test','task_id':'task','purpose':'personal_use',
                 'ledger':self.root/'provider.sqlite','output_root':self.root/'outputs','binding_id':'fixed-provider',
                 'task_check':self.authorize_task,'execute':self.execute,'lookup':self.lookup}
        return ApiDeliveryHost(**{**options,**changes})

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(MarketError) as caught: function(*args,**kwargs)
        self.assertEqual(code,caught.exception.code)

    def count(self, table):
        with self.buyer.store.connect() as db: return db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]

    def test_real_role_tools_search_resolve_and_offline_supplier_preparation(self):
        result=ExchangeInterface(self.buyer).call('market_search',{'q':'仓库查询','kind':'api','purpose':'personal_use'})
        self.assertEqual('api',result['items'][0]['kind'])
        detail=self.buyer.resolve('vendor:stock','1.0.0','personal_use')
        self.assertEqual(('api','prepaid_call_bundle',4,'ready'),
            (detail['format'],detail['billing'],detail['resource_protocol_version'],detail['compatibility']['status']))
        request={'slug':'stock','version':'1.0.0','metadata':metadata(),'content':CONTENT,'request_id':'check'}
        self.assertEqual([],check_request(request,'vendor')['errors'])
        request.pop('request_id')
        source=self.root/'batch.json'
        source.write_text(compact({'format':'market-supplier-batch-v1','seller':'vendor','batch_id':'api-batch',
            'items':[{'source_id':'original-api','request':request}]}),encoding='utf-8')
        self.assertIn('plan_sha256',prepare(source,self.root/'prepared'))

    def test_explicit_capability_and_release_required_before_new_entitlement(self):
        self.error('release_binding_required',self.buyer.acquire,'vendor:stock','1.0.0','unpinned',purpose='personal_use')
        unsupported=Exchange(self.path,'legacy',clock=lambda:self.now)
        self.error('incompatible',unsupported.acquire,'vendor:stock','1.0.0','unsupported',
                   purpose='personal_use',expected_release_digest=self.digest)
        self.assertEqual(0,self.count('acquisitions'))

    def test_repeated_and_concurrent_acquisition_never_mint_quota_alias(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            acquired=list(pool.map(lambda i:self.acquire('acquire-'+str(i)),range(4)))
        self.assertEqual(1,len({item['acquisition_id'] for item in acquired}))
        self.assertEqual(1,self.count('acquisitions'))
        self.assertEqual(acquired[0]['acquisition_id'],self.acquire('acquire-0')['acquisition_id'])
        self.error('idempotency_conflict',self.buyer.acquire,'vendor:stock','1.0.0','acquire-1',
                   purpose='commercial_use',expected_release_digest=self.digest)

    def test_read_requires_api_host_without_creating_usage_or_returning_description_as_result(self):
        acquired=self.acquire(); reply=self.buyer.read(acquired['acquisition_id'],'task','read')
        self.assertEqual('trusted_host_invoke_api',reply['next_action'])
        self.assertNotIn('content',reply); self.assertEqual(0,self.count('usage_events'))

    def test_api_result_license_and_small_reply_replay_survive_host_restart(self):
        acquired=self.acquire()['acquisition_id']; host=self.host()
        reply=host.use(acquired,{'warehouse':'fictional'},'one')
        self.assertEqual('api_result_delivered',reply['status']); self.assertNotIn('result',reply)
        self.assertEqual({'count':7,'warehouse':'fictional'},json.loads(Path(reply['path']).read_text(encoding='utf-8')))
        self.assertEqual(self.meta['license_text'],Path(reply['license_path']).read_text(encoding='utf-8'))
        manifest=json.loads(Path(reply['manifest_path']).read_text(encoding='utf-8'))
        self.assertEqual(self.digest,manifest['release_digest'])
        self.assertEqual(reply,self.host().use(acquired,{'warehouse':'fictional'},'one'))
        self.assertEqual((1,1),(len(self.calls),self.count('usage_events')))
        self.assertFalse(reply['task_completed']);self.assertFalse(reply['channel_fulfillment_confirmed'])

    def test_new_acquire_request_and_different_task_do_not_reset_quota_or_expiration(self):
        acquired=self.acquire()['acquisition_id']
        self.host().use(acquired,{'warehouse':'fictional'},'one')
        alias=self.acquire('another')['acquisition_id']
        self.host(task_id='task-2').use(alias,{'warehouse':'fictional'},'two')
        self.error('quota_exhausted',self.host().use,alias,{'warehouse':'fictional'},'three')
        self.now=4600
        detail=self.buyer.resolve('vendor:stock','1.0.0','personal_use')
        self.assertEqual((4600,'expired','stop_no_automatic_renewal'),
            (detail['entitlement_expires_at'],detail['entitlement_state'],detail['next_action']))
        self.error('entitlement_expired',self.host().use,alias,{'warehouse':'fictional'},'one')
        self.assertEqual(acquired,self.acquire('late')['acquisition_id'])
        self.assertEqual(2,len(self.calls))

    def test_changed_input_rejects_saved_request_before_consumption(self):
        acquired=self.acquire()['acquisition_id']; host=self.host()
        host.use(acquired,{'warehouse':'fictional'},'one')
        with self.assertRaises(MarketError): host.use(acquired,{'warehouse':'changed'},'one')
        self.assertEqual(1,len(self.calls))

    def test_unknown_execution_recovers_original_result_without_second_execution(self):
        acquired=self.acquire()['acquisition_id']
        def lost(*args):
            self.execute(*args); raise TimeoutError()
        result=self.host(execute=lost).use(acquired,{'warehouse':'fictional'},'one')
        self.assertEqual('reserved',result['status']);self.assertEqual(0,self.count('usage_events'))
        result=self.host().use(acquired,{'warehouse':'fictional'},'one',recover=True)
        self.assertEqual('api_result_delivered',result['status'])
        self.assertEqual((1,1),(len(self.calls),self.count('usage_events')))

    def test_local_output_failure_recovers_cached_result_without_consuming_again(self):
        acquired=self.acquire()['acquisition_id']; host=self.host()
        from ai_supermarket.api_delivery import _save
        def fail(path,raw):
            if path.name=='result.json': raise OSError('fixture unavailable')
            return _save(path,raw)
        with patch('ai_supermarket.api_delivery._save',side_effect=fail),self.assertRaises(OSError):
            host.use(acquired,{'warehouse':'fictional'},'one')
        self.assertEqual(0,self.count('usage_events'))
        self.assertEqual('api_result_delivered',self.host().use(acquired,{'warehouse':'fictional'},'one')['status'])
        self.assertEqual(1,len(self.calls))

    def test_input_schema_unknown_fields_types_and_limits_block_provider_call(self):
        acquired=self.acquire()['acquisition_id']; host=self.host()
        for value in ({},{'warehouse':1},{'warehouse':'x'*33},{'warehouse':'ok','secret':'do not transmit'},[] ):
            with self.subTest(value=value),self.assertRaises(MarketError): host.use(acquired,value,'bad')
        self.assertEqual([],self.calls)

    def test_wrong_principal_provider_task_or_permission_is_refused(self):
        acquired=self.acquire()['acquisition_id']
        other=Exchange(self.path,'other',host_policy=POLICY,clock=lambda:self.now)
        self.error('not_found',self.host(market=other).use,acquired,{'warehouse':'fictional'},'one')
        self.error('forbidden',self.host(provider='different',ledger=self.root/'other-provider.sqlite').use,
                   acquired,{'warehouse':'fictional'},'one')
        self.allowed=False
        self.error('forbidden',self.host().use,acquired,{'warehouse':'fictional'},'one')
        self.allowed=True; self.buyer.host_policy=HostPolicy.from_json({**POLICY,'permissions':['file_write']})
        self.error('permission_required',self.host().use,acquired,{'warehouse':'fictional'},'one')
        self.assertEqual([],self.calls)

    def test_suspension_or_task_revocation_blocks_cached_delivery(self):
        acquired=self.acquire()['acquisition_id']; host=self.host()
        host.use(acquired,{'warehouse':'fictional'},'one')
        self.allowed=False
        self.error('forbidden',host.use,acquired,{'warehouse':'fictional'},'one')
        self.allowed=True
        self.reviewer.availability('vendor:stock','1.0.0',1,'suspended','fixture isolate','suspend')
        self.error('not_found',host.use,acquired,{'warehouse':'fictional'},'one')
        self.assertEqual(1,len(self.calls))

    def test_reprice_and_withdraw_preserve_original_quota_and_license(self):
        acquired=self.acquire()['acquisition_id']
        self.seller.reprice('vendor:stock','1.0.0',1,100,'price')
        self.seller.availability('vendor:stock','1.0.0',2,'withdrawn','fixture withdraw','withdraw')
        self.assertEqual('api_result_delivered',self.host().use(acquired,{'warehouse':'fictional'},'one')['status'])
        self.assertEqual(acquired,self.acquire('reused')['acquisition_id'])

    def test_contract_changes_cannot_silently_increase_old_quota(self):
        acquired=self.acquire()['acquisition_id']; self.meta['api']['total_units']=100
        with self.buyer.store.connect(write=True) as db:
            db.execute('UPDATE submissions SET metadata=? WHERE resource_id=?',(compact(self.meta),'vendor:stock'))
        self.error('release_changed',self.host().use,acquired,{'warehouse':'fictional'},'one')
        self.assertEqual([],self.calls)

    def test_non_api_entitlement_is_never_a_call_grant(self):
        self.seller.draft('text','1.0.0',listing(),CONTENT,'text-draft')
        self.seller.submit('vendor:text','1.0.0',1,'text-submit')
        self.reviewer.review('vendor:text','1.0.0',1,'approve',CHECKS,'original','text-review')
        self.buyer.host_policy=HostPolicy.from_json({**POLICY,'capabilities':['read_markdown','invoke_api']})
        digest=self.buyer.resolve('vendor:text','1.0.0','personal_use')['release_digest']
        acquired=self.buyer.acquire('vendor:text','1.0.0','text',purpose='personal_use',expected_release_digest=digest)
        self.error('not_api',self.host().use,acquired['acquisition_id'],{'warehouse':'fictional'},'one')

    def test_invalid_api_metadata_cannot_be_published(self):
        cases=[]
        for mutation in ('mixed','wrong-kind','no-descriptor','wrong-contract','extra-cost','duplicate','wrong-limit'):
            item=metadata()
            if mutation=='mixed':item['origin']={}
            elif mutation=='wrong-kind':item['kind']='template'
            elif mutation=='no-descriptor':del item['api']
            elif mutation=='wrong-contract':item['usage_contract']['version']=1
            elif mutation=='extra-cost':item['usage_contract']['dependencies']=[{'id':'extra','license_name':'MIT','max_cost_cents':1}]
            elif mutation=='duplicate':item['api']['input_fields']*=2
            else:item['api']['input_fields'][0]['limit']=8193
            cases.append(item)
        for item in cases:
            with self.subTest(item=item),self.assertRaises(MarketError):validate_listing(item,CONTENT)

    def test_result_limit_keeps_unknown_occupancy_without_success_record(self):
        acquired=self.acquire()['acquisition_id']
        result=self.host(execute=lambda *a:{'large':'x'*1025}).use(acquired,{'warehouse':'fictional'},'one')
        self.assertEqual(('reserved',1),(result['status'],result['reserved_units']))
        self.assertEqual(0,self.count('usage_events'))

    def test_corrupt_output_is_preserved_without_new_api_call(self):
        acquired=self.acquire()['acquisition_id']; host=self.host()
        result=host.use(acquired,{'warehouse':'fictional'},'one')
        Path(result['path']).write_text('keep user changes',encoding='utf-8')
        with self.assertRaises(MarketError):host.use(acquired,{'warehouse':'fictional'},'one')
        self.assertEqual('keep user changes',Path(result['path']).read_text(encoding='utf-8'))
        self.assertEqual(1,len(self.calls))

    def test_integer_boolean_optional_inputs_use_strict_types_and_limits(self):
        api=metadata()['api']
        api['input_fields']=[{'name':'count','type':'integer','required':True,'limit':5,'description':'最多5'},
                             {'name':'detail','type':'boolean','required':False,'limit':1,'description':'展开标记'}]
        validate_input(api,{'count':5}); validate_input(api,{'count':-5,'detail':False})
        for value in ({'count':True},{'count':6},{'count':1,'detail':1}):
            with self.subTest(value=value),self.assertRaises(MarketError):validate_input(api,value)

    def test_paid_api_requires_original_verified_lineage_then_uses_same_quota(self):
        from ai_supermarket.tests.test_payment_fulfillment import PaymentFulfillmentTests
        fixture=PaymentFulfillmentTests(); fixture.setUpClass()
        self.addCleanup(fixture.doCleanups)
        def draft(price=0,version='1.0.0',slug='brief'):
            fixture.buyer.host_policy=HostPolicy.from_json(POLICY)
            return fixture.seller.draft(slug,version,metadata(price),CONTENT,'api-draft-'+version+'-'+slug)
        fixture.draft=draft; fixture.setUp()
        before=fixture.buyer.acquire(fixture.resource,'1.0.0','not-paid',purpose='personal_use',
                                    expected_release_digest=fixture.digest)
        self.assertEqual('payment_unavailable',before['status'])
        fixture.seed_evidence(); fixture.confirm(); acquired=fixture.grant()['acquisition_id']
        host=ApiDeliveryHost(fixture.buyer,'provider-test','task','personal_use',self.root/'paid-provider.sqlite',
             self.root/'paid-output','fixed-paid-provider',
             lambda p,t,s:p==fixture.buyer.principal and t=='task' and s=='provider-test',self.execute,self.lookup)
        result=host.use(acquired,{'warehouse':'fictional'},'one')
        self.assertEqual('api_result_delivered',result['status'])
        repeated=fixture.buyer.acquire(fixture.resource,'1.0.0','reuse-paid',purpose='personal_use',
                                      expected_release_digest=fixture.digest)
        self.assertEqual(acquired,repeated['acquisition_id'])
        self.assertEqual(result,host.use(repeated['acquisition_id'],{'warehouse':'fictional'},'one'))
        self.assertEqual('paid_entitlement_granted',fixture.orders.get(fixture.order_id)['status'])
        with fixture.buyer.store.connect(write=True) as db:
            db.execute("UPDATE meta SET value='{}' WHERE key=?",('paid_entitlement:'+acquired,))
        self.error('integrity_failed',host.use,acquired,{'warehouse':'fictional'},'one')
        self.assertEqual(1,len(self.calls))

    def test_market_entitlement_through_loopback_provider_to_result_file(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        from urllib.request import Request,urlopen
        import threading
        posts=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                posts.append(data)
                raw=compact({'count':7,'warehouse':data['input']['warehouse']}).encode()
                self.send_response(200); self.send_header('Content-Length',str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join,2);self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        def execute(operation_id,operation,value):
            raw=compact({'operation_id':operation_id,'operation':operation,'input':value}).encode()
            with urlopen(Request(f'http://127.0.0.1:{server.server_port}',data=raw,method='POST'),timeout=2) as response:
                return json.load(response)
        acquired=self.acquire()['acquisition_id'];host=self.host(execute=execute)
        first=host.use(acquired,{'warehouse':'fictional'},'network')
        self.assertEqual(7,json.loads(Path(first['path']).read_text(encoding='utf-8'))['count'])
        self.assertEqual(first,host.use(acquired,{'warehouse':'fictional'},'network'))
        self.assertEqual(1,len(posts));self.assertEqual(1,self.count('usage_events'))


if __name__=='__main__':unittest.main()
