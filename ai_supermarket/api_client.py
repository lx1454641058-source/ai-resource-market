"""本项目原创。API远程结果接收；固定任务/身份，原请求恢复，正文不返回模型。"""
from pathlib import Path
import uuid

from .api_quota import canonical, checksum, MAX_INPUT
from .common import MarketError, fields, identifier, integer, parse_json
from .http_client import RemoteMarket
from .origin import _safe_path
from .purchase_download import _read, _save, digest


def validate_envelope(reply, intent):
    fields(reply,('status','manifest','result','license_text','usage_id','input_sha256','task_completed'))
    if reply['status']!='api_result' or reply['task_completed'] is not False:
        raise MarketError('invalid_response','API结果状态无效。')
    manifest=reply['manifest']
    fields(manifest,('format','id','version','acquisition_id','release_digest','task_id','request_id','purpose',
                     'provider','operation_id','sha256','bytes','media_type','license_sha256','expires_at'))
    expected={**{k:intent['target'][k] for k in ('id','version','acquisition_id','release_digest','license_sha256')},
              **{k:intent[k] for k in ('task_id','purpose','request_id')}}
    if any(manifest[key]!=value for key,value in expected.items()):
        raise MarketError('api_scope_mismatch','API结果不属于原任务、权益、许可或版本。')
    if (manifest['format']!='market-api-result-v1' or manifest['media_type']!='application/json'
            or reply['input_sha256']!=checksum(canonical(intent['input'],MAX_INPUT))):
        raise MarketError('api_scope_mismatch','API输入或文件格式与原请求不符。')
    for field in ('provider','operation_id'):identifier(manifest[field],field)
    identifier(reply['usage_id'],'usage_id'); integer(manifest['bytes'],1,65536,'bytes')
    integer(manifest['expires_at'],1,9999999999,'expires_at')
    digest(manifest['sha256']);digest(manifest['license_sha256'])
    raw=canonical(reply['result'],65536).encode('utf-8')
    license_text=reply['license_text']
    if not isinstance(license_text,str):raise MarketError('invalid_response','API许可文本无效。')
    try:license_raw=license_text.encode('utf-8')
    except UnicodeError:raise MarketError('invalid_response','API许可需有效UTF-8。') from None
    if (not 0<len(license_raw)<=8000 or checksum(license_text)!=manifest['license_sha256']
            or len(raw)!=manifest['bytes'] or checksum(raw.decode())!=manifest['sha256']):
        raise MarketError('integrity_failed','API结果或许可摘要不符。')
    return raw,license_raw,manifest


class RemoteApiResources:
    def __init__(self,remote,task_id,purpose,output_root,*,max_calls=24,before_request=None):
        if type(remote) is not RemoteMarket:raise MarketError('invalid_client','需要固定的市场连接。')
        identifier(task_id,'task_id');integer(max_calls,1,100,'max_calls')
        if purpose not in ('personal_use','commercial_use'):raise MarketError('invalid_params','API用途无效。')
        self.remote=remote;self.task_id=task_id;self.purpose=purpose;self.max_calls=max_calls
        if before_request is not None and not callable(before_request):raise MarketError('invalid_configuration','调用计数器无效。')
        self.before_request=before_request
        self.endpoint=remote.base_url;self.key_id=remote.credential().split('.')[0]
        self.root=_safe_path(output_root);self.root.mkdir(parents=True,exist_ok=True)
        self.directory=_safe_path(self.root/('api-task-'+checksum(canonical([self.endpoint,self.key_id,task_id],4096))))
        self.directory.mkdir(exist_ok=True)
        binding={'format':'remote-api-task-v1','endpoint':self.endpoint,'key_id':self.key_id,
                 'task_id':task_id,'purpose':purpose,'max_calls':max_calls}
        if not (self.directory/'binding.json').exists() and any(self.directory.iterdir()):
            raise MarketError('state_conflict','API客户端目录缺少原绑定。')
        _save(self.directory/'binding.json',canonical(binding,4096).encode())

    def _check(self):
        if self.remote.base_url!=self.endpoint or self.remote.credential().split('.')[0]!=self.key_id:
            raise MarketError('api_scope_mismatch','API任务连接或凭据发生变化。')

    def _path(self,request_id):
        identifier(request_id,'request_id')
        return self.directory/('intent-'+checksum(request_id)[:24]+'.json')

    def _load(self,request_id):
        self._check()
        intent=parse_json(_read(self._path(request_id),MAX_INPUT+4096))
        fields(intent,('target','request_id','task_id','purpose','input'))
        if (intent['request_id']!=request_id or intent['task_id']!=self.task_id or intent['purpose']!=self.purpose):
            raise MarketError('api_scope_mismatch','保存的API请求不属于原任务。')
        self._target(intent['target']);canonical(intent['input'],MAX_INPUT)
        return intent

    @staticmethod
    def _target(target):
        fields(target,('id','version','acquisition_id','release_digest','license_sha256'))
        for name in ('id','version','acquisition_id'):identifier(target[name],name)
        digest(target['release_digest']);digest(target['license_sha256'])

    def _request(self,action,intent,proof=None):
        self._check()
        if self.before_request is not None:self.before_request(action,intent['request_id'])
        # One bounded persistent network-attempt budget, including queries/results.
        for index in range(self.max_calls):
            path=self.directory/f'attempt-{index:03d}.json'
            if path.exists():continue
            try:_save(path,canonical({'action':action,'request_id':intent['request_id'],'claim':uuid.uuid4().hex},1024).encode())
            except MarketError as exc:
                if exc.code=='output_conflict':continue
                raise
            break
        else:raise MarketError('api_call_limit','API客户端网络尝试上限已用尽，不自动追加。')
        result=self.remote.request('/v1/api/'+action,{'acquisition_id':intent['target']['acquisition_id'],
            'request_id':intent['request_id'],'input':intent['input'],
            'expected_release_digest':intent['target']['release_digest'],**(proof or {})})
        if result.get('status')=='error':return result
        if action!='result':
            for key,expected in (('acquisition_id',intent['target']['acquisition_id']),('request_id',intent['request_id']),
                 ('task_id',self.task_id),('purpose',self.purpose),('release_digest',intent['target']['release_digest'])):
                if result.get(key)!=expected:raise MarketError('api_scope_mismatch','API状态不符合原任务请求。')
            allowed=('api_receipt_recorded','awaiting_api_receipt') if action in ('receipt','receipt_status') else ('api_call_completed','reserved','not_executed')
            if result.get('status') not in allowed:
                raise MarketError('invalid_response','API状态不可识别。')
        return result

    def invoke(self,target,inputs,request_id):
        self._check();self._target(target);identifier(request_id,'request_id')
        if not isinstance(inputs,dict):raise MarketError('invalid_params','API输入需要JSON对象。')
        intent={'target':target,'request_id':request_id,'task_id':self.task_id,'purpose':self.purpose,'input':inputs}
        _save(self._path(request_id),canonical(intent,MAX_INPUT+4096).encode('utf-8'))
        result=self._request('call',intent)
        return self.receive(request_id) if result.get('status')=='api_call_completed' else result

    def status(self,request_id):return self._request('status',self._load(request_id))

    def recover(self,request_id):
        result=self._request('recover',self._load(request_id))
        return self.receive(request_id) if result.get('status')=='api_call_completed' else result

    def receive(self,request_id):
        intent=self._load(request_id);result=self._request('result',intent)
        if result.get('status')!='api_result':return result
        raw,license_raw,manifest=validate_envelope(result,intent)
        folder=_safe_path(self.root/('api-result-'+checksum(canonical([self.endpoint,self.key_id,self.task_id,request_id],4096))))
        folder.mkdir(exist_ok=True)
        _save(folder/'binding.json',canonical(intent,MAX_INPUT+4096).encode())
        _save(folder/'result.json',raw);_save(folder/'LICENSE.txt',license_raw)
        _save(folder/'manifest.json',canonical(manifest,8192).encode())
        receipt={'status':'api_result_received','acquisition_id':manifest['acquisition_id'],'usage_id':result['usage_id'],
                 'task_id':self.task_id,'request_id':request_id,'release_digest':manifest['release_digest'],
                 'sha256':manifest['sha256'],'path':str(folder/'result.json'),'license_path':str(folder/'LICENSE.txt'),
                 'manifest_path':str(folder/'manifest.json'),'task_completed':False,'server_acknowledged':False}
        _save(folder/'receipt.json',canonical(receipt,8192).encode())
        return receipt

    def acknowledge(self,request_id,*,status_only=False):
        """Explicit per-result client statement; local files checked before networking."""
        if type(status_only) is not bool:raise MarketError('invalid_params','status_only需要布尔值。')
        intent=self._load(request_id)
        folder=_safe_path(self.root/('api-result-'+checksum(canonical([self.endpoint,self.key_id,self.task_id,request_id],4096))))
        if parse_json(_read(folder/'binding.json',MAX_INPUT+4096))!=intent:
            raise MarketError('api_scope_mismatch','本地API结果与原请求不符。')
        receipt=parse_json(_read(folder/'receipt.json',8192))
        manifest=parse_json(_read(folder/'manifest.json',8192))
        raw=_read(folder/'result.json',65536)
        envelope={'status':'api_result','manifest':manifest,'result':parse_json(raw),
                  'license_text':_read(folder/'LICENSE.txt',8000).decode('utf-8'),
                  'usage_id':receipt.get('usage_id'),'input_sha256':checksum(canonical(intent['input'],MAX_INPUT)),
                  'task_completed':False}
        validated,_,_=validate_envelope(envelope,intent)
        if raw!=validated:raise MarketError('integrity_failed','本地结果不是原规范JSON字节。')
        proof={'usage_id':envelope['usage_id'],'manifest_sha256':checksum(canonical(manifest,8192))}
        reply=self._request('receipt_status' if status_only else 'receipt',intent,proof)
        if reply.get('status')=='error':return reply
        recorded=reply['status']=='api_receipt_recorded'
        if (any(reply.get(k)!=v for k,v in proof.items())
                or reply.get('server_acknowledged') is not recorded
                or any(reply.get(k) is not False for k in ('task_completed','payment_executed',
                    'channel_fulfillment_confirmed','package_fulfillment_confirmed'))
                or reply.get('evidence')!=('authenticated_client_statement' if recorded else 'delivery_record_only')):
            raise MarketError('invalid_response','API收货回执不符。')
        if recorded:
            identifier(reply.get('receipt_id'),'receipt_id')
            if type(reply.get('acknowledged_at')) not in (int,float) or not 0<=reply['acknowledged_at']<10**12:
                raise MarketError('invalid_response','API收货时间无效。')
            _save(folder/'server-receipt.json',canonical(reply,8192).encode())
        elif not status_only:
            raise MarketError('invalid_response','市场尚未保存收货声明。')
        return reply
