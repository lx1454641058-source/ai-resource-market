"""本项目原创。供应商在线核对原操作；固定卖方身份/商品白名单，无共享数据库或支付。"""
import re
import uuid

from .api_provider import _request as validate_provider_request
from .api_quota import MAX_INPUT,canonical,checksum
from .common import MarketError,fields,identifier,parse_json
from .exchange import Exchange
from .http_client import RemoteMarket
from .purchase_download import digest

ROUTE='/v1/provider/authorize'
PREFIX='api_operation_authority:'


def _original(host,operation_id):
    with host.quota._db() as db:
        row=db.execute('SELECT * FROM calls WHERE id=?',(operation_id,)).fetchone()
    if not row or row['principal']!=host.market.principal or row['task_id']!=host.task_id:
        raise MarketError('entitlement_denied','没有获准的原API操作。')
    request=parse_json(row['request'])
    if checksum(canonical([host.provider,host.market.principal,request],MAX_INPUT+1024))!=operation_id:
        raise MarketError('integrity_failed','原操作编号与请求不符。')
    if row['state'] not in ('reserved','completed'):
        raise MarketError('entitlement_denied','原操作不允许继续执行或查询。')
    host.authority.inspect(host.market.principal,request)
    return request


def record_operation(apis,token,provider,operation_id,operation,inputs):
    """Trusted dispatch hook, after the quota reservation and before provider IO."""
    identity,providers=apis._host(token,provider);host=providers[provider]
    request=_original(host,operation_id)
    if request['operation']!=operation or request['input']!=inputs:
        raise MarketError('api_scope_mismatch','派发操作与原预留不符。')
    with apis.gateway.budgets.store.connect() as db:binding=apis.gateway._binding(db,identity)
    record={'key_id':identity['key_id'],'principal':identity['principal'],'root_id':binding['root_id'],
            'task_id':host.task_id,'provider':provider,'binding_id':host.binding_id,
            'operation_id':operation_id,'request_sha256':checksum(canonical(request,MAX_INPUT))}
    with host.market.store.connect(write=True) as db:
        key=PREFIX+operation_id;raw=canonical(record,4096)
        old=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        if old and old[0]!=raw:raise MarketError('idempotency_conflict','原调用授权绑定已存在且不同。')
        apis._host(token,provider)
        if not old:db.execute('INSERT INTO meta VALUES (?,?)',(key,raw))


def _active_identity(apis,key_id):
    # This server-local lookup does not authenticate a supplied key id. Its caller
    # obtains the id exclusively from the immutable, trusted dispatch record.
    if not isinstance(key_id,str) or not re.fullmatch(r'ak_[0-9a-f]{32}',key_id):
        raise MarketError('entitlement_denied','原授权绑定无效。')
    with apis.gateway.identities.store.connect() as db:
        row=db.execute('''SELECT a.principal,a.role,a.policy,a.disabled,k.expires_at,k.revoked_at
            FROM accounts a JOIN api_keys k ON k.principal=a.principal WHERE k.id=?''',(key_id,)).fetchone()
    if (not row or row['disabled'] or row['revoked_at'] is not None
            or row['expires_at']<=apis.gateway.identities.clock() or row['role']!='buyer'):
        raise MarketError('entitlement_denied','原买方授权已失效。')
    return {'principal':row['principal'],'role':row['role'],'policy':parse_json(row['policy']),'key_id':key_id}


def authorize(apis,token,value):
    fields(value,('provider','binding_id','operation_id','action','nonce'),('operation','input_sha256'))
    for field in ('provider','binding_id','operation_id','nonce'):identifier(value[field],field)
    if value['action']=='execute':
        fields(value,('provider','binding_id','operation_id','action','nonce','operation','input_sha256'))
        identifier(value['operation'],'operation');digest(value['input_sha256'])
    elif value['action']!='lookup' or 'operation' in value or 'input_sha256' in value:
        raise MarketError('invalid_params','供应商授权查询动作无效。')
    seller=apis.gateway.identities.authenticate(token)
    if seller['role']!='seller':raise MarketError('forbidden','仅卖方可核对自己资源的原操作。')
    market=Exchange(apis.gateway.database,seller['principal'],'seller')
    with market.store.connect() as db:
        saved=db.execute('SELECT value FROM meta WHERE key=?',(PREFIX+value['operation_id'],)).fetchone()
    if not saved:raise MarketError('entitlement_denied','没有获准的原API操作。')
    record=parse_json(saved[0])
    fields(record,('key_id','principal','root_id','task_id','provider','binding_id','operation_id','request_sha256'))
    if any(record[field]!=value[field] for field in ('provider','binding_id','operation_id')):
        raise MarketError('entitlement_denied','原操作范围不符。')
    identity=_active_identity(apis,record['key_id'])
    _,providers=apis._bound_host(identity,value['provider']);host=providers[value['provider']]
    with apis.gateway.budgets.store.connect() as db:binding=apis.gateway._binding(db,identity)
    if (host.binding_id!=record['binding_id'] or identity['principal']!=record['principal']
            or host.task_id!=record['task_id'] or binding['root_id']!=record['root_id']):
        raise MarketError('entitlement_denied','原任务或供应商连接不符。')
    request=_original(host,value['operation_id'])
    if checksum(canonical(request,MAX_INPUT))!=record['request_sha256']:
        raise MarketError('integrity_failed','原调用内容发生变化。')
    with market.store.connect() as db:market._submission(db,request['resource_id'],request['version'])
    if value['action']=='execute' and (value['operation']!=request['operation']
            or value['input_sha256']!=checksum(canonical(request['input'],MAX_INPUT))):
        raise MarketError('entitlement_denied','供应商输入或操作与原预留不符。')
    # Recheck both parties immediately before returning the live decision.
    if apis.gateway.identities.authenticate(token)!=seller:
        raise MarketError('entitlement_denied','卖方身份范围变化。')
    apis._bound_host(_active_identity(apis,record['key_id']),value['provider'])
    return {'status':'authorized','provider':value['provider'],'binding_id':value['binding_id'],
            'operation_id':value['operation_id'],'action':value['action'],
            'request_sha256':checksum(canonical(value,4096)),
            'id':request['resource_id'],'version':request['version'],'release_digest':request['release_digest'],
            'scope':'current_original_operation','payment_executed':False}


class RemoteProviderAuthorization:
    """Provider-side callback. Only approved releases; no cached decisions/retries."""
    def __init__(self,remote,provider,binding_id,releases):
        if type(remote) is not RemoteMarket:raise MarketError('invalid_client','需要固定市场连接。')
        identifier(provider,'provider');identifier(binding_id,'binding_id')
        if not isinstance(releases,dict) or not 1<=len(releases)<=1000:
            raise MarketError('invalid_configuration','供应商需明确允许的商品版本与发行摘要。')
        for key,value in releases.items():
            if not isinstance(key,tuple) or len(key)!=2:raise MarketError('invalid_configuration','商品绑定格式无效。')
            identifier(key[0],'id');identifier(key[1],'version');digest(value)
        self.remote=remote;self.provider=provider;self.binding_id=binding_id;self.releases=dict(releases)
        self.endpoint=remote.base_url;self.key_id=remote.credential().split('.')[0]

    def _check(self):
        if self.remote.base_url!=self.endpoint or self.remote.credential().split('.')[0]!=self.key_id:
            raise MarketError('connection_changed','供应商的原授权连接发生变化。')

    def __call__(self,request):
        self._check();validate_provider_request(request)
        if request['provider']!=self.provider or request['binding_id']!=self.binding_id:return False
        value={k:request[k] for k in ('provider','binding_id','operation_id','action')}
        value['nonce']=uuid.uuid4().hex
        if request['action']=='execute':value.update(operation=request['operation'],input_sha256=checksum(canonical(request['input'],MAX_INPUT)))
        reply=self.remote.request(ROUTE,value);self._check()
        if reply.get('status')=='error':return False
        fields(reply,('status','provider','binding_id','operation_id','action','request_sha256','id','version','release_digest','scope','payment_executed'))
        if (reply['status']!='authorized' or reply['scope']!='current_original_operation' or reply['payment_executed'] is not False
                or any(reply[k]!=value[k] for k in ('provider','binding_id','operation_id','action'))
                or reply['request_sha256']!=checksum(canonical(value,4096))):
            raise MarketError('invalid_response','市场授权响应与原查询不符。')
        identifier(reply['id'],'id');identifier(reply['version'],'version');digest(reply['release_digest'])
        return self.releases.get((reply['id'],reply['version']))==reply['release_digest']
