"""本项目原创。API商品的有界输入声明及市场原权益核验。"""
import re

from .api_quota import canonical, MAX_INPUT
from .common import MarketError, fields, identifier, integer, text
from .contracts import preflight


def string(maximum=128, enum=None):
    value = {'type':'string','minLength':1,'maxLength':maximum}
    if enum is not None: value['enum'] = list(enum)
    return value


API_SCHEMA = {'type':'object','additionalProperties':False,
    'required':['provider','operation','total_units','validity_seconds','input_fields','result_max_bytes'],
    'properties':{
        'provider':string(), 'operation':string(),
        'total_units':{'type':'integer','minimum':1,'maximum':1000000},
        'validity_seconds':{'type':'integer','minimum':1,'maximum':31536000},
        'result_max_bytes':{'type':'integer','minimum':1,'maximum':65536},
        'input_fields':{'type':'array','minItems':0,'maxItems':16,'items':{
            'type':'object','additionalProperties':False,
            'required':['name','type','required','limit','description'],
            'properties':{'name':string(32),'type':string(16,('string','integer','boolean')),
                          'required':{'type':'boolean'},
                          'limit':{'type':'integer','minimum':1,'maximum':100000000},
                          'description':string(180)}}}}}


def validate_api(value):
    fields(value, API_SCHEMA['required'])
    identifier(value['provider'],'api.provider'); identifier(value['operation'],'api.operation')
    integer(value['total_units'],1,1000000,'api.total_units')
    integer(value['validity_seconds'],1,31536000,'api.validity_seconds')
    integer(value['result_max_bytes'],1,65536,'api.result_max_bytes')
    entries=value['input_fields']
    if not isinstance(entries,list) or len(entries)>16:
        raise MarketError('invalid_params','API输入字段需为0至16项。')
    names=set()
    for entry in entries:
        fields(entry,('name','type','required','limit','description'))
        name=entry['name']
        if not isinstance(name,str) or not re.fullmatch('[a-z][a-z0-9_]{0,31}',name) or name in names:
            raise MarketError('invalid_params','API字段名无效或重复。')
        names.add(name)
        if entry['type'] not in ('string','integer','boolean') or type(entry['required']) is not bool:
            raise MarketError('invalid_params','API字段类型或必填声明无效。')
        maximum={'string':8192,'integer':100000000,'boolean':1}[entry['type']]
        integer(entry['limit'],1,maximum,'api.input.limit')
        text(entry['description'],'api.input.description',180)


def validate_input(api, value):
    validate_api(api)
    required=[row['name'] for row in api['input_fields'] if row['required']]
    optional=[row['name'] for row in api['input_fields'] if not row['required']]
    fields(value, required, optional)
    canonical(value,MAX_INPUT)
    for row in api['input_fields']:
        if row['name'] not in value: continue
        item=value[row['name']]
        if row['type']=='integer': integer(item,-row['limit'],row['limit'],'api.input')
        elif row['type']=='boolean':
            if type(item) is not bool: raise MarketError('invalid_params','API输入需要布尔值。')
        elif not isinstance(item,str) or len(item.encode('utf-8'))>row['limit']:
            raise MarketError('invalid_params','API输入文本类型或字节上限不符。')


class MarketApiAuthority:
    """Trusted supplier-side adapter to this market's existing acquisition ledger.

    market is the buyer context already authenticated by the host. task_check must
    consult that host's active task/budget and provider transfer authorization.
    This local adapter does not authenticate an arbitrary remote buyer or install
    provider credentials. Production providers must independently trust this ledger.
    """
    def __init__(self, market, provider, task_check):
        market._require('buyer')
        self.market=market; self.provider=identifier(provider,'provider')
        if not callable(task_check): raise MarketError('invalid_configuration','必须配置可信任务与传输授权检查。')
        self.task_check=task_check

    def inspect(self, principal, request):
        from .exchange import PublishedCatalog
        from .api_quota import request_value
        request=request_value(request)
        self.market._require('buyer')
        if principal!=self.market.principal or self.task_check(principal,request['task_id'],self.provider) is not True:
            raise MarketError('forbidden','买方身份、任务或供应商传输范围未获准。')
        with self.market.store.connect() as db:
            db.execute('BEGIN')
            acquired=self.market._owned(db,request['acquisition_id'])
            release=self.market._check_release_binding(db,acquired)
            if release is None or release!=request['release_digest']:
                raise MarketError('release_binding_required','API调用需绑定原发行摘要。')
            for field,column in (('resource_id','resource_id'),('version','version'),('purpose','purpose')):
                if request[field]!=acquired[column]: raise MarketError('entitlement_denied','API请求与原权益不符。')
            catalog=PublishedCatalog(db,allow_existing=True)
            item=catalog.get(acquired['resource_id'],acquired['version'],acquired['purpose'])
            catalog.read(item)
            if item['sha256']!=acquired['content_hash'] or 'api' not in item:
                raise MarketError('entitlement_denied','原权益不是匹配的API商品。')
            api=item['api']; validate_input(api,request['input'])
            if api['provider']!=self.provider or api['operation']!=request['operation']:
                raise MarketError('entitlement_denied','API提供方或操作与原商品不符。')
            compatible=preflight(item['usage_contract'],self.market.host_policy)
            if compatible['status']!='ready': raise MarketError(compatible['status'],'宿主不能调用此API。')
            expires=int(acquired['created_at'])+api['validity_seconds']
            if self.market.clock()>=expires: raise MarketError('entitlement_expired','原API套餐已过期，不自动续购。')
            grant={'provider':self.provider,'principal':principal,'acquisition_id':acquired['id'],
                   'resource_id':item['id'],'version':item['version'],'purpose':acquired['purpose'],
                   'release_digest':release,'operation':api['operation'],
                   'total_units':api['total_units'],'expires_at':expires}
        return grant,item

    def __call__(self, principal, request):
        return self.inspect(principal,request)[0]
