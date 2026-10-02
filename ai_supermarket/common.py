"""来源：本项目原创。严格JSON与输入校验，仅使用Python标准库。"""

import json
import re


MAX_REQUEST_BYTES = 16384


class MarketError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate field')
        result[key] = value
    return result


def parse_json(raw):
    try:
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8')
        value = json.loads(raw, object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('non-finite')))
        compact(value).encode('utf-8')
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise MarketError('invalid_json', '需要有效UTF-8 JSON；禁止重复字段和非有限数。') from exc


def text(value, name, limit=128):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise MarketError('invalid_params', f'{name}需要1～{limit}字符的字符串。')
    if any(ord(c) < 32 for c in value):
        raise MarketError('invalid_params', f'{name}不能包含控制字符。')
    return value


def identifier(value, name):
    value = text(value, name)
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}', value):
        raise MarketError('invalid_params', f'{name}必须是字母、数字、点、横线、冒号或下划线。')
    return value


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise MarketError('invalid_params', '参数缺失或包含未允许字段。')


def choice(value, options, name):
    if not isinstance(value, str) or value not in options:
        raise MarketError('invalid_params', f'{name}不在允许的选项中。')
    return value


def integer(value, minimum, maximum, name):
    if type(value) is not int or not minimum <= value <= maximum:
        raise MarketError('invalid_params', f'{name}需要{minimum}～{maximum}的整数。')
    return value


def error_result(exc):
    return {'status': 'error', 'error_code': exc.code, 'message': str(exc)}

