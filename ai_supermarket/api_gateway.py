"""本项目原创。默认关闭的任务API机器接口；不付款、不接收模型回调或任意URL。"""
from pathlib import Path
import re

from .api_delivery import ApiDeliveryHost
from .api_quota import canonical, checksum, MAX_INPUT
from .common import MarketError, compact, fields, identifier, parse_json
from .contracts import HostPolicy
from .exchange import Exchange, PublishedCatalog
from .purchase_download import _read, digest
from .task_budget_gateway import TaskBudgetGateway


ACTIONS=('call','status','recover','result','receipt','receipt_status')


def validate_request(value):
    fields(value,('acquisition_id','request_id','input','expected_release_digest'))
    identifier(value['acquisition_id'],'acquisition_id');identifier(value['request_id'],'request_id')
    digest(value['expected_release_digest'])
    if not isinstance(value['input'],dict): raise MarketError('invalid_params','API输入需要JSON对象。')
    canonical(value,MAX_INPUT)


class ApiGateway:
    def __init__(self,gateway,hosts):
        if type(gateway) is not TaskBudgetGateway or not isinstance(hosts,dict) or not hosts:
            raise MarketError('api_host_mismatch','API入口必须绑定身份、任务预算及固定供应商宿主。')
        self.gateway=gateway;self.hosts={}
        for key,providers in hosts.items():
            if not isinstance(key,str) or not re.fullmatch('ak_[0-9a-f]{32}',key) or not isinstance(providers,dict) or not providers:
                raise MarketError('api_host_mismatch','API映射需要凭据编号与提供方，不接受Token。')
            self.hosts[key]=dict(providers)
            for provider,host in providers.items():
                identifier(provider,'provider')
                if type(host) is not ApiDeliveryHost or host.provider!=provider:
                    raise MarketError('api_host_mismatch','API宿主与提供方不符。')

    def _host(self,token,provider=None):
        identity=self.gateway.identities.authenticate(token)
        return self._bound_host(identity,provider)

    def _bound_host(self,identity,provider=None):
        providers=self.hosts.get(identity['key_id'])
        if identity['role']!='buyer' or providers is None:
            raise MarketError('forbidden','此身份未绑定API任务宿主。')
        with self.gateway.budgets.store.connect() as db:
            binding=self.gateway._binding(db,identity)
            self.gateway.budgets._task(db,binding['root_id'],binding['task_id'],identity['principal'],active=True)
        selected=providers if provider is None else {provider:providers.get(provider)}
        for host in selected.values():
            if (host is None or host.market.principal!=identity['principal']
                    or host.market.host_policy!=HostPolicy.from_json(identity['policy'])
                    or host.task_id!=binding['task_id']
                    or Path(host.market.store.path).resolve()!=Path(self.gateway.database).resolve()):
                raise MarketError('api_host_mismatch','API宿主的主体、策略、市场或任务绑定不符。')
        return identity,selected

    def describe(self,token):
        identity=self.gateway.identities.authenticate(token)
        if identity['key_id'] not in self.hosts or identity['role']!='buyer':return None
        _,providers=self._host(token)
        purposes={host.purpose for host in providers.values()}
        if len(purposes)!=1:raise MarketError('api_host_mismatch','同一任务API宿主需使用同一用途。')
        return {'status':'host_configured','version':1,'task_id':next(iter(providers.values())).task_id,
                'purpose':next(iter(purposes)),'providers':sorted(providers),
                'routes':{action:'/v1/api/'+action for action in ACTIONS},
                'new_purchase_available':False,'automatic_retry':False}

    def call(self,token,action,value):
        if action not in ACTIONS:raise MarketError('not_found','API入口不存在。')
        acknowledgement=action in ('receipt','receipt_status')
        if acknowledgement:
            fields(value,('acquisition_id','request_id','input','expected_release_digest','usage_id','manifest_sha256'))
            identifier(value['usage_id'],'usage_id');digest(value['manifest_sha256'])
            validate_request({k:v for k,v in value.items() if k not in ('usage_id','manifest_sha256')})
        else:validate_request(value)
        identity,_=self._host(token)
        market=Exchange(self.gateway.database,identity['principal'],clock=self.gateway.clock,host_policy=identity['policy'])
        with market.store.connect() as db:
            acquired=market._owned(db,value['acquisition_id'])
            item=PublishedCatalog(db,allow_existing=True).get(acquired['resource_id'],acquired['version'],acquired['purpose'])
            if 'api' not in item:raise MarketError('not_api','此权益不是API套餐。')
            if market._check_release_binding(db,acquired)!=value['expected_release_digest']:
                raise MarketError('release_changed','API请求与原发行摘要不符。')
            provider=item['api']['provider']
        _,providers=self._host(token,provider);configured=providers[provider]

        def check_task(principal,task,selected_provider):
            fresh,now=self._host(token,provider)
            return (now[provider] is configured and fresh['principal']==principal
                    and task==configured.task_id and selected_provider==provider
                    and configured.authority.task_check(principal,task,provider) is True)

        def execute(operation_id,operation,inputs):
            from .provider_authorization import record_operation
            record_operation(self,token,provider,operation_id,operation,inputs)
            return configured.execute(operation_id,operation,inputs)

        # Request-local authorization closure observes credential/budget revocation
        # during provider work, without mutating the shared host or storing a token.
        host=ApiDeliveryHost(configured.market,provider,configured.task_id,configured.purpose,
             configured.quota.path,configured.root,configured.binding_id,check_task,execute,configured.lookup,
             require_existing_ledger=True)
        args=(value['acquisition_id'],value['input'],value['request_id'])
        if acknowledgement:return self._receipt(token,identity,host,value,action=='receipt')
        if action=='status':result=host.status(*args)
        else:result=host.use(*args,recover=action=='recover',existing_only=action=='result')
        self._host(token,provider)
        if action=='result' and result['status']=='api_result_delivered':
            raw=_read(Path(result['path']),65536)
            manifest=parse_json(_read(Path(result['manifest_path']),8192))
            license_text=_read(Path(result['license_path']),8000).decode('utf-8')
            body=parse_json(raw)
            if (canonical(body,65536).encode()!=raw or checksum(raw.decode())!=manifest['sha256']
                    or len(raw)!=manifest['bytes'] or checksum(license_text)!=manifest['license_sha256']):
                raise MarketError('integrity_failed','API结果或许可发生变化，未发送。')
            self._host(token,provider)
            return {'status':'api_result','manifest':manifest,'result':body,'license_text':license_text,
                    'usage_id':result['usage_id'],'input_sha256':checksum(canonical(value['input'],MAX_INPUT)),
                    'task_completed':False}
        # Server-local paths never cross this boundary. completed is execution
        # evidence, not evidence that this remote client has received any file.
        reply={key:result[key] for key in ('operation_id','reserved_units','used_units','remaining_units','usage_id') if key in result}
        reply.update(status='api_call_completed' if result['status'] in ('completed','api_result_delivered') else result['status'],
                     acquisition_id=value['acquisition_id'],request_id=value['request_id'],
                     task_id=host.task_id,purpose=host.purpose,release_digest=value['expected_release_digest'],
                     task_completed=False,payment_executed=False,client_result_received=False)
        return reply

    def _receipt(self,token,identity,host,value,write):
        # Only local original-call evidence is read. Never invoke/recover a provider.
        request=host._request(value['acquisition_id'],value['input'],value['request_id'])
        grant,item=host.authority.inspect(host.market.principal,request)
        result=host.quota.status(host.market.principal,request)
        if result['status']!='completed':raise MarketError('delivery_required','原API调用尚未完成。')
        with self.gateway.budgets.store.connect() as budget_db:
            binding=self.gateway._binding(budget_db,identity)
        scope={'principal':identity['principal'],'key_id':identity['key_id'],
               'root_id':binding['root_id'],'task_id':host.task_id}
        key='api_client_receipt:'+checksum(canonical([scope,value['request_id']],4096))
        with host.market.store.connect(write=write) as db:
            host.market._owned(db,value['acquisition_id'])
            usage=db.execute('SELECT * FROM usage_events WHERE id=?',(value['usage_id'],)).fetchone()
            saved=db.execute('SELECT value FROM meta WHERE key=?',('api_usage:'+value['usage_id'],)).fetchone()
            if (not usage or not saved or usage['principal']!=identity['principal']
                    or usage['task_id']!=host.task_id or usage['acquisition_id']!=value['acquisition_id']
                    or usage['request_id']!=checksum(compact(['api-call',value['request_id']]))):
                raise MarketError('receipt_binding_mismatch','没有匹配的原API交付事件。')
            manifest=parse_json(saved[0])
            if (checksum(canonical(manifest,8192))!=value['manifest_sha256']
                    or manifest.get('operation_id')!=result['operation_id']
                    or manifest.get('sha256')!=result['result_sha256']
                    or any(manifest.get(k)!=v for k,v in dict(acquisition_id=value['acquisition_id'],
                        task_id=host.task_id,request_id=value['request_id'],purpose=host.purpose,
                        release_digest=value['expected_release_digest'],id=item['id'],version=item['version'],
                        provider=host.provider,license_sha256=checksum(item['license_text']),
                        expires_at=grant['expires_at'],media_type='application/json',format='market-api-result-v1',
                        bytes=len(canonical(result['result'],65536).encode('utf-8'))).items())):
                raise MarketError('receipt_binding_mismatch','收货声明与原结果清单不符。')
            offer={'scope':scope,'request_id':value['request_id'],'usage_id':value['usage_id'],
                   'manifest_sha256':value['manifest_sha256'],'input_sha256':checksum(canonical(value['input'],MAX_INPUT))}
            old=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
            record=parse_json(old[0]) if old else None
            if record is not None:
                fields(record,('offer','receipt_id','acknowledged_at'))
                if (record['offer']!=offer or record['receipt_id']!='ar_'+checksum(canonical(offer,8192))
                        or type(record['acknowledged_at']) not in (int,float)
                        or not 0<=record['acknowledged_at']<10**12):
                    raise MarketError('receipt_corrupt','原API收货记录不符，保留原记录。')
            self._host(token,host.provider)
            if write and record is None:
                record={'offer':offer,'receipt_id':'ar_'+checksum(canonical(offer,8192)),
                        'acknowledged_at':host.market.clock()}
                db.execute('INSERT INTO meta VALUES (?,?)',(key,canonical(record,8192)))
        self._host(token,host.provider)
        return {'status':'api_receipt_recorded' if record else 'awaiting_api_receipt',
                'acquisition_id':value['acquisition_id'],'request_id':value['request_id'],
                'task_id':host.task_id,'purpose':host.purpose,'release_digest':value['expected_release_digest'],
                'usage_id':value['usage_id'],'manifest_sha256':value['manifest_sha256'],
                'receipt_id':record['receipt_id'] if record else None,
                'acknowledged_at':record['acknowledged_at'] if record else None,
                'evidence':'authenticated_client_statement' if record else 'delivery_record_only',
                'server_acknowledged':record is not None,'channel_fulfillment_confirmed':False,
                'package_fulfillment_confirmed':False,'task_completed':False,'payment_executed':False}
