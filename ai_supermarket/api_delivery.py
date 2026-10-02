"""本项目原创。固定任务的API交付宿主，结果和许可证落盘，小摘要返回AI。"""
from pathlib import Path
import uuid

from .api_product import MarketApiAuthority
from .api_quota import ProviderQuota, canonical, checksum, MAX_INPUT
from .common import MarketError, compact, identifier
from .origin import _safe_path
from .purchase_download import _save


class ApiDeliveryHost:
    """Explicit trusted host, not a public remote API or wallet.

    execute and lookup are configured provider adapters with bounded IO. The
    model cannot replace them, ledger, output root, task or provider binding.
    task_check must check the active task and authorized data transfer scope.
    """
    def __init__(self, market, provider, task_id, purpose, ledger, output_root,
                 binding_id, task_check, execute, lookup, *, require_existing_ledger=False):
        self.authority=MarketApiAuthority(market,provider,task_check)
        self.market=market; self.provider=provider
        self.task_id=identifier(task_id,'task_id'); self.binding_id=identifier(binding_id,'binding_id')
        if purpose not in ('personal_use','commercial_use'):
            raise MarketError('invalid_params','API宿主用途无效。')
        if not callable(execute) or not callable(lookup):
            raise MarketError('invalid_configuration','需要固定供应商执行器与原调用查询器。')
        self.purpose=purpose; self.execute=execute; self.lookup=lookup
        self.root=_safe_path(output_root)
        self.quota=ProviderQuota(ledger,provider,self.authority,clock=market.clock,
                                 require_existing=require_existing_ledger)

    def _request(self, acquisition_id, value, request_id):
        from .exchange import PublishedCatalog
        identifier(request_id,'request_id')
        with self.market.store.connect() as db:
            db.execute('BEGIN')
            acquired=self.market._owned(db,acquisition_id)
            if acquired['purpose']!=self.purpose:
                raise MarketError('entitlement_denied','原权益与固定任务用途不符。')
            item=PublishedCatalog(db,allow_existing=True).get(acquired['resource_id'],acquired['version'],self.purpose)
            if 'api' not in item: raise MarketError('not_api','该权益不是API套餐。')
            request={'acquisition_id':acquisition_id,'resource_id':item['id'],'version':item['version'],
                     'purpose':self.purpose,'release_digest':self.market._check_release_binding(db,acquired),
                     'task_id':self.task_id,'request_id':request_id,'operation':item['api']['operation'],'input':value}
        return request

    def status(self, acquisition_id, value, request_id):
        """Local ledger lookup only; no provider query, file write or execution."""
        request=self._request(acquisition_id,value,request_id)
        result=self.quota.status(self.market.principal,request)
        return {key:val for key,val in result.items() if key!='result'}

    def use(self, acquisition_id, value, request_id, *, recover=False, existing_only=False):
        """Invoke once, or explicitly query an unknown original call; never renew."""
        if type(recover) is not bool: raise MarketError('invalid_params','recover需要布尔值。')
        if type(existing_only) is not bool or (recover and existing_only):
            raise MarketError('invalid_params','既有结果读取不能同时请求供应商恢复。')
        request=self._request(acquisition_id,value,request_id)
        grant,item=self.authority.inspect(self.market.principal,request)
        binding={'format':'market-api-delivery-v1','database':str(Path(self.market.store.path).absolute()),
                 'principal':self.market.principal,'provider':self.provider,'binding_id':self.binding_id,
                 'ledger':str(self.quota.path),'request':request}
        raw_binding=canonical(binding,MAX_INPUT+4096)
        folder=_safe_path(self.root/('api-'+checksum(compact([
            binding['database'],self.market.principal,self.task_id,request_id]))))
        folder.mkdir(parents=True,exist_ok=True)
        if not (folder/'binding.json').exists() and any(folder.iterdir()):
            raise MarketError('output_conflict','API目录缺少原调用绑定，保留文件。')
        _save(folder/'binding.json',raw_binding.encode('utf-8'))

        def execute(operation_id,operation,inputs):
            result=self.execute(operation_id,operation,inputs)
            canonical(result,item['api']['result_max_bytes'])
            return result

        def lookup(operation_id):
            evidence=self.lookup(operation_id)
            if isinstance(evidence,dict) and evidence.get('state')=='completed' and 'result' in evidence:
                canonical(evidence['result'],item['api']['result_max_bytes'])
            return evidence

        if existing_only:
            result=self.quota.status(self.market.principal,request)
        elif recover:
            result=self.quota.recover(self.market.principal,request,lookup)
        else:
            result=self.quota.invoke(self.market.principal,request,execute)
        summary={key:val for key,val in result.items() if key!='result'}
        if result['status']!='completed':
            return {**summary,'next_action':'check_original_provider_call' if result['status']=='reserved' else 'original_call_not_executed'}
        # Recheck task/access and immutable conditions before writing user files.
        self.authority.inspect(self.market.principal,request)
        raw=canonical(result['result'],item['api']['result_max_bytes']).encode('utf-8')
        if checksum(raw.decode('utf-8'))!=result['result_sha256']:
            raise MarketError('integrity_failed','API结果摘要与原调用不符。')
        license_bytes=item['license_text'].encode('utf-8')
        manifest={'format':'market-api-result-v1','id':item['id'],'version':item['version'],
                  'acquisition_id':acquisition_id,'release_digest':request['release_digest'],
                  'task_id':self.task_id,'request_id':request_id,'purpose':self.purpose,
                  'provider':self.provider,'operation_id':result['operation_id'],
                  'sha256':result['result_sha256'],'bytes':len(raw),'media_type':'application/json',
                  'license_sha256':checksum(item['license_text']),'expires_at':grant['expires_at']}
        _save(folder/'result.json',raw); _save(folder/'LICENSE.txt',license_bytes)
        _save(folder/'manifest.json',canonical(manifest,8192).encode('utf-8'))
        self.authority.inspect(self.market.principal,request)
        usage_request=checksum(compact(['api-call',request_id]))
        with self.market.store.connect(write=True) as db:
            from .exchange import PublishedCatalog
            acquired=self.market._owned(db,acquisition_id)
            catalog=PublishedCatalog(db,allow_existing=True)
            catalog.read(catalog.get(acquired['resource_id'],acquired['version'],self.purpose))
            old=db.execute('SELECT * FROM usage_events WHERE principal=? AND task_id=? AND request_id=?',
                (self.market.principal,self.task_id,usage_request)).fetchone()
            payload=canonical(manifest,8192)
            if old:
                saved=db.execute('SELECT value FROM meta WHERE key=?',('api_usage:'+old['id'],)).fetchone()
                if old['acquisition_id']!=acquisition_id or saved is None or saved[0]!=payload:
                    raise MarketError('idempotency_conflict','原API使用记录不符，保留证据。')
                usage_id=old['id']
            else:
                usage_id='u_'+uuid.uuid4().hex
                db.execute('INSERT INTO usage_events VALUES (?,?,?,?,?,?)',
                    (usage_id,acquisition_id,self.market.principal,self.task_id,usage_request,self.market.clock()))
                db.execute('INSERT INTO meta VALUES (?,?)',('api_usage:'+usage_id,payload))
        return {**summary,'status':'api_result_delivered','usage_id':usage_id,'acquisition_id':acquisition_id,
                'path':str(folder/'result.json'),'license_path':str(folder/'LICENSE.txt'),
                'manifest_path':str(folder/'manifest.json'),'release_digest':request['release_digest'],
                'evidence':'local_api_result_and_license','channel_fulfillment_confirmed':False,
                'task_completed':False}
