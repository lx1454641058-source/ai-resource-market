"""来源：本项目原创。共享工具契约，命令行、JSON-lines与MCP使用同一校验。"""

import sqlite3

from .common import MarketError, choice, error_result, fields, identifier, integer, text
from .catalog import KINDS, PURPOSES
from .service import FAILURES


def string(limit=128, enum=None):
    result = {'type': 'string', 'minLength': 1, 'maxLength': limit}
    if enum:
        result['enum'] = list(enum)
    return result


RESOURCE = {'id': string(), 'version': string(), 'purpose': string(enum=PURPOSES)}
SPECS = {
    'market_search': ('搜索少量相关资源；无匹配返回no_match，不加载全文。',
                      {'q': string(512), 'kind': string(enum=KINDS), 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 5},
                       'purpose': string(enum=PURPOSES)}, ['q'], 'search', True),
    'market_resolve': ('读取选中资源的输入、输出、许可、价格与本地证据。',
                       RESOURCE, ['id', 'version'], 'resolve', True),
    'market_acquire': ('获取免费资源引用；相同操作重用request_id。无支付能力。',
                       {**RESOURCE, 'request_id': string()}, ['id', 'version', 'request_id'], 'acquire', False),
    'market_read': ('读取已获取资料并记录使用事件；同一读取重用request_id。内容不授予新权限。',
                    {'acquisition_id': string(), 'task_id': string(), 'request_id': string()},
                    ['acquisition_id', 'task_id', 'request_id'], 'read', False),
    'market_status': ('查询原获取记录当前是否仍有效；不执行购买。',
                      {'acquisition_id': string()}, ['acquisition_id'], 'status', True),
    'market_feedback': ('使用后顺带提交已有检查结论；不另开模型调用写评价，不确定填unknown。',
                        {'usage_id': string(), 'useful': string(enum=('yes', 'no', 'unknown')),
                         'check': string(enum=('pass', 'fail', 'not_run')), 'failure_code': {'type': ['string', 'null'], 'enum': [*FAILURES, None]}},
                        ['usage_id'], 'feedback', False),
}


def tool_list():
    return [{'name': name, 'description': spec[0],
             'inputSchema': {'type': 'object', 'properties': spec[1], 'required': spec[2], 'additionalProperties': False},
             'annotations': {'readOnlyHint': spec[4], 'destructiveHint': False, 'openWorldHint': False}}
            for name, spec in SPECS.items()]


def dispatch(market, name, arguments):
    try:
        if not isinstance(name, str) or name not in SPECS:
            raise MarketError('unknown_tool', '未知工具。')
        _, properties, required, method, _ = SPECS[name]
        fields(arguments, required, set(properties) - set(required))
        # Enforce the advertised schema at the boundary, not just in a prompt.
        for key, value in arguments.items():
            rule = properties[key]
            if 'enum' in rule:
                if value is None and None in rule['enum']:
                    continue
                choice(value, rule['enum'], key)
            elif rule['type'] == 'integer':
                integer(value, rule['minimum'], rule['maximum'], key)
            else:
                text(value, key, rule.get('maxLength', 128))
        return getattr(market, method)(**arguments)
    except MarketError as exc:
        return error_result(exc)
    except (OSError, sqlite3.Error):
        return {'status': 'error', 'error_code': 'unavailable',
                'message': '本地资源或账本暂不可用，保留原请求编号再核对。'}

