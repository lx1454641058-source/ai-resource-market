"""来源：本项目原创。按角色提供机器接口；角色由宿主注入，工具不能自行提权。"""

from copy import deepcopy
import sqlite3

from .common import MarketError, error_result, fields, integer, text
from .exchange import CHECKS, USES, KINDS
from .tools import SPECS, string
from .contracts import CONTRACT_SCHEMA
from .origin import ORIGIN_SCHEMA
from .api_product import API_SCHEMA


META = {'type': 'object', 'additionalProperties': False, 'required': [
    'title', 'summary', 'kind', 'keywords', 'input', 'output', 'check', 'license_name',
    'license_text', 'source_statement', 'allowed_uses', 'price_cents'], 'properties': {
        'title': string(64), 'summary': string(180), 'kind': string(enum=KINDS),
        'keywords': {'type': 'array', 'minItems': 1, 'maxItems': 30, 'items': string(64)},
        'input': string(400), 'output': string(400), 'check': string(400),
        'license_name': string(100), 'license_text': {'type': 'string', 'minLength': 1, 'maxLength': 8000},
        'source_statement': string(1200),
        'allowed_uses': {'type': 'array', 'minItems': 1, 'maxItems': 2, 'uniqueItems': True,
                         'items': string(enum=USES)},
        'price_cents': {'type': 'integer', 'minimum': 0, 'maximum': 100_000_000},
        'usage_contract': CONTRACT_SCHEMA, 'origin': ORIGIN_SCHEMA, 'api': API_SCHEMA}}
CONTENT = {'type': 'string', 'minLength': 1, 'maxLength': 24000}
REVISION = {'type': 'integer', 'minimum': 1, 'maximum': 1_000_000}
IDENTITY = {'id': string(), 'version': string()}
REQUEST = {'request_id': string()}
LIST = {'state': string(enum=('draft', 'pending_review', 'approved', 'rejected')),
        'offset': {'type': 'integer', 'minimum': 0, 'maximum': 1_000_000},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50}}


def validate(value, schema, name='arguments'):
    kind = schema['type']
    if isinstance(kind, list):
        if value is None and 'null' in kind:
            return
        kind = next(t for t in kind if t != 'null')
    if kind == 'object':
        fields(value, schema.get('required', ()), set(schema['properties']) - set(schema.get('required', ())))
        for key, item in value.items():
            validate(item, schema['properties'][key], key)
    elif kind == 'array':
        if not isinstance(value, list) or not schema['minItems'] <= len(value) <= schema['maxItems']:
            raise MarketError('invalid_params', f'{name}数组长度无效。')
        for item in value:
            validate(item, schema['items'], name)
        if schema.get('uniqueItems') and len(set(value)) != len(value):
            raise MarketError('invalid_params', f'{name}不能重复。')
    elif kind == 'integer':
        integer(value, schema['minimum'], schema['maximum'], name)
    elif kind == 'boolean':
        if type(value) is not bool:
            raise MarketError('invalid_params', f'{name}必须是布尔值。')
    elif kind == 'string':
        if not isinstance(value, str) or not schema.get('minLength', 0) <= len(value) <= schema['maxLength']:
            raise MarketError('invalid_params', f'{name}字符串长度或类型无效。')
        # Body fields explicitly support newlines; domain validation also checks UTF-8 byte size.
        if name not in ('content', 'license_text'):
            text(value, name, schema['maxLength'])
    if 'enum' in schema and value not in schema['enum']:
        raise MarketError('invalid_params', f'{name}不在允许的选项中。')


class ExchangeInterface:
    def __init__(self, market):
        self.market = market
        self.specs = {}
        if market.role == 'buyer':
            self.specs = deepcopy(SPECS)
            self.specs['market_search'][1]['kind'] = string(enum=KINDS)
            for spec in self.specs.values():
                if 'purpose' in spec[1]:
                    spec[1]['purpose'] = string(enum=USES)
            self.specs['market_acquire'][1]['quote_id'] = string()
            self.specs['market_acquire'][1]['expected_release_digest'] = string(64)
            spec = self.specs['market_acquire']
            self.specs['market_acquire'] = (spec[0] + ' 将resolve的release_digest作为expected_release_digest传入以绑定版本和使用条件；变化即停止。', *spec[1:])
            self.add('market_quote', '生成绑定本主体的报价，5分钟有效；不付款，费用规则为草案。',
                     {**IDENTITY, **REQUEST, 'purpose': string(enum=USES)}, ['id', 'version', 'purpose', 'request_id'], 'quote')
            self.instructions = '任务前search；resolve查看许可/价格/compatibility及release_digest，ready后acquire传入原expected_release_digest。摘要变化则停，不自动替换；read仍核当前状态。声明不授予执行权限。payment_unavailable不是已购买。'
        elif market.role == 'seller':
            self.add('seller_draft', '创建自己的商品草稿；正文限24KB，仅Markdown，不自动公开。',
                     {'slug': string(48), 'version': string(), 'metadata': META, 'content': CONTENT, **REQUEST},
                     ['slug', 'version', 'metadata', 'content', 'request_id'], 'draft')
            self.add('seller_revise', '修订自己的草稿或被驳回版本；待审和已批准内容不可覆盖。',
                     {**IDENTITY, 'revision': REVISION, 'metadata': META, 'content': CONTENT, **REQUEST},
                     ['id', 'version', 'revision', 'metadata', 'content', 'request_id'], 'revise')
            self.add('seller_submit', '提交含usage_contract的当前草稿；待审核不代表上架。',
                     {**IDENTITY, 'revision': REVISION, **REQUEST}, ['id', 'version', 'revision', 'request_id'], 'submit')
            self.add('seller_status', '查看自己的正文、审核意见和当前报价。', IDENTITY, ['id', 'version'], 'inspect_submission', True)
            self.add('seller_list', '分批列出自己的商品，下一页使用next_offset。', LIST, [], 'list_submissions', True)
            self.add('seller_api_receipts','按自己的API商品核对历史逐次收货声明；不含买方/正文，不代表整包履约或结算。',
                     {**IDENTITY,'after':{'type':'integer','minimum':0,'maximum':9223372036854775807},
                      'limit':{'type':'integer','minimum':1,'maximum':50}},['id','version'],'api_receipts',True)
            self.add('seller_reprice', '自主修改售价，可免费；不会修改已购权益，无强制参考价。',
                     {**IDENTITY, 'offer_revision': REVISION, 'price_cents': META['properties']['price_cents'], **REQUEST},
                     ['id', 'version', 'offer_revision', 'price_cents', 'request_id'], 'reprice')
            self.add('seller_withdraw', '停止新获取，保留已购版本可读；不能绕过平台隔离。',
                     {**IDENTITY, 'offer_revision': REVISION, 'reason': string(1200), **REQUEST},
                     ['id', 'version', 'offer_revision', 'reason', 'request_id'], 'withdraw')
            self.add('seller_pricing_reference', '获取非强制定价方法；无可靠成交数据时不编造参考价。',
                     {'kind': string(enum=KINDS)}, [], 'pricing_reference', True)
            self.instructions = '先draft再submit，按status意见修订；卖家自主定价，平台参考非强制，无摊位费。费用以当前报价和已核验协议为准，渠道费未知不当作0或最终到账承诺。不得自批、修改别人的商品或伪造许可。'
        else:
            self.add('review_list', '列出提交记录；建议state=pending_review。', LIST, [], 'list_submissions', True)
            self.add('review_inspect', '读取指定提交的正文、来源许可和修订号，供实际审核。', IDENTITY, ['id', 'version'], 'inspect_submission', True)
            self.add('review_decide', '记录实际来源/样例/内容审核后批准或驳回；不得审核自己商品。',
                     {**IDENTITY, 'revision': REVISION, 'decision': string(enum=('approve', 'reject')),
                      'checks': {'type': 'object', 'additionalProperties': False, 'required': list(CHECKS),
                                 'properties': {key: {'type': 'boolean'} for key in CHECKS}},
                      'note': string(1200), **REQUEST},
                     ['id', 'version', 'revision', 'decision', 'checks', 'note', 'request_id'], 'review')
            self.add('review_availability', '运营隔离或恢复商品；隔离停止已购读取，恢复需留复核原因。',
                     {**IDENTITY, 'offer_revision': REVISION, 'state': string(enum=('active', 'withdrawn', 'suspended')),
                      'reason': string(1200), **REQUEST},
                     ['id', 'version', 'offer_revision', 'state', 'reason', 'request_id'], 'availability')
            self.instructions = '此为受信运营审核入口。检查真实正文、来源许可与样例后记录结果。不能把模型判断冒充专业签核，不能审核自己的商品。'

    def add(self, name, description, properties, required, method, readonly=False):
        self.specs[name] = (description, properties, required, method, readonly)

    def tool_list(self):
        return [{'name': name, 'description': spec[0],
                 'inputSchema': {'type': 'object', 'properties': spec[1], 'required': spec[2], 'additionalProperties': False},
                 'annotations': {'readOnlyHint': spec[4], 'destructiveHint': not spec[4], 'openWorldHint': False}}
                for name, spec in self.specs.items()]

    def call(self, name, arguments):
        try:
            if not isinstance(name, str) or name not in self.specs:
                raise MarketError('unknown_tool', '此角色未提供该工具。')
            _, properties, required, method, _ = self.specs[name]
            validate(arguments, {'type': 'object', 'properties': properties, 'required': required})
            if method == 'withdraw':
                return self.market.availability(**arguments, state='withdrawn')
            return getattr(self.market, method)(**arguments)
        except MarketError as exc:
            return error_result(exc)
        except (OSError, sqlite3.Error):
            return {'status': 'error', 'error_code': 'unavailable',
                    'message': '市场或账本暂不可用，保留原请求编号；不要猜测执行结果。'}
