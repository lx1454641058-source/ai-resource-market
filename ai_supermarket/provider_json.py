"""本项目原创。供应商固定JSON查询接口适配；不重试、不付款、不签发权益。"""
from copy import deepcopy
import http.client
import ipaddress
import math
import re
import time
from urllib.parse import quote, quote_plus, urlencode, urlsplit

from .api_product import validate_api, validate_input
from .api_quota import canonical
from .common import MarketError, identifier, parse_json


def _parameter(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,63}', value):
        raise MarketError('invalid_configuration', '原站查询字段名无效。')


def _value(value):
    if type(value) not in (str, int, bool):
        raise MarketError('invalid_configuration', '固定查询参数需要文本、整数或布尔值。')
    return ('true' if value else 'false') if type(value) is bool else str(value)


class ProviderJsonQuery:
    """Fixed provider-side GET backend for ProviderOperations.execute.

    The provider must approve the endpoint, input scope and data transfer, and
    supply a business result_check. HTTP 200 alone is not successful delivery.
    No generic fetch tool, proxy, redirect, pagination or backend lookup exists.
    """
    def __init__(self, api, endpoint, query_map, *, result_check, fixed_query=None,
                 headers=None, timeout=10, enabled=False, private_query=None):
        validate_api(api)
        if type(enabled) is not bool or not callable(result_check):
            raise MarketError('invalid_configuration', '需要明确启用配置和固定业务结果检查器。')
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30):
            raise MarketError('invalid_configuration', '网络等待上限需在0至30秒之间。')
        if (not isinstance(endpoint, str) or len(endpoint) > 2048
                or any(ord(c) <= 32 or ord(c) >= 127 for c in endpoint)):
            raise MarketError('invalid_endpoint', '原站接口需要固定ASCII地址。')
        try:
            parsed = urlsplit(endpoint)
            if (parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.port == 0
                    or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
                raise ValueError
            if parsed.scheme == 'http' and not ipaddress.ip_address(parsed.hostname).is_loopback:
                raise ValueError
        except ValueError:
            raise MarketError('invalid_endpoint', '原站需HTTPS，本机IP可用HTTP；地址不能带凭据、查询串或片段。') from None
        names = {entry['name'] for entry in api['input_fields']}
        if not isinstance(query_map, dict) or set(query_map) != names:
            raise MarketError('invalid_configuration', '原站参数映射须覆盖且仅覆盖已批准的商品输入字段。')
        for target in query_map.values(): _parameter(target)
        if len(set(query_map.values())) != len(query_map):
            raise MarketError('invalid_configuration', '原站参数映射不能重复。')
        fixed_query = {} if fixed_query is None else fixed_query
        if not isinstance(fixed_query, dict) or len(fixed_query) > 16:
            raise MarketError('invalid_configuration', '固定查询参数需为最多16项对象。')
        for name, value in fixed_query.items():
            _parameter(name); _value(value)
        if set(fixed_query) & set(query_map.values()):
            raise MarketError('invalid_configuration', '输入不能覆盖固定原站参数。')
        # Provider-only secrets are separate from model inputs and ordinary fixed
        # parameters. Neither source may override their names.
        private_query = {} if private_query is None else private_query
        if not isinstance(private_query, dict) or len(private_query) > 4:
            raise MarketError('invalid_configuration', '私有查询认证需为最多4项对象。')
        for name, value in private_query.items():
            _parameter(name)
            if (not isinstance(value, str) or not 8 <= len(value) <= 2048
                    or any(ord(c) < 33 or ord(c) >= 127 for c in value)):
                raise MarketError('invalid_configuration', '私有查询认证值格式无效。')
        if set(private_query) & (set(fixed_query) | set(query_map.values())):
            raise MarketError('invalid_configuration', '私有认证参数不能与固定或输入参数重名。')
        headers = {} if headers is None else headers
        if not isinstance(headers, dict) or set(headers) - {'Authorization', 'X-API-Key'}:
            raise MarketError('invalid_configuration', '仅允许供应商固定的Authorization或X-API-Key头。')
        for value in headers.values():
            if (not isinstance(value, str) or not 1 <= len(value) <= 4096
                    or any(ord(c) < 32 or ord(c) >= 127 for c in value)):
                raise MarketError('invalid_configuration', '原站认证头格式无效。')
        self._api, self._map, self._fixed = deepcopy(api), deepcopy(query_map), deepcopy(fixed_query)
        self._headers, self._parsed = deepcopy(headers), parsed
        self._private_query = deepcopy(private_query)
        self._secret_forms = frozenset(form for value in private_query.values()
            for form in (value, quote(value, safe=''), quote_plus(value, safe='')))
        self._check, self._timeout, self._enabled = result_check, timeout, enabled
        self._target({})  # Bound static parameters before any network activity.

    def _target(self, inputs):
        pairs = [(name, _value(value)) for name, value in self._fixed.items()]
        pairs.extend(self._private_query.items())
        pairs.extend((self._map[name], _value(value)) for name, value in inputs.items())
        query = urlencode(pairs)
        target = (self._parsed.path or '/') + ('?' + query if query else '')
        if len(target) > 16384:
            raise MarketError('invalid_params', '编码后的原站查询超出16KiB上限。')
        return target

    def execute(self, operation_id, operation, inputs):
        identifier(operation_id, 'operation_id')
        if not self._enabled:
            raise MarketError('provider_backend_disabled', '原站查询尚未由供应商启用。')
        if operation != self._api['operation']:
            raise MarketError('operation_denied', '原站查询不属于固定商品操作。')
        validate_input(self._api, inputs)
        target = self._target(inputs)
        raw = self._get(target)
        try:
            result = parse_json(raw)
            canonical(result, self._api['result_max_bytes'])
            # Reject recognizable echoes before the checker, operation cache or
            # buyer sees them. This is not detection of arbitrary encodings.
            self._reject_secret_echo(result)
            # Let validation inspect copies, so it cannot rewrite the returned data.
            if self._check(deepcopy(result), deepcopy(inputs)) is not True:
                raise ValueError
        except Exception:
            raise MarketError('provider_result_unknown', '原站结果未通过业务检查，保留原操作，不自动重试。') from None
        return result

    def _reject_secret_echo(self, result):
        pending = [result]
        while pending and self._secret_forms:
            value = pending.pop()
            if isinstance(value, dict):
                pending.extend(value.keys()); pending.extend(value.values())
            elif isinstance(value, list): pending.extend(value)
            elif any(secret in str(value) for secret in self._secret_forms):
                raise ValueError('private credential echo')

    def _get(self, target):
        parsed, maximum = self._parsed, self._api['result_max_bytes']
        connection = (http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection)(
            parsed.hostname, parsed.port, timeout=self._timeout)
        deadline, response = time.monotonic() + self._timeout, None
        try:
            connection.request('GET', target, headers={'Accept': 'application/json', 'Accept-Encoding': 'identity',
                               'Connection': 'close', **self._headers})
            sock = connection.sock
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise TimeoutError
            sock.settimeout(remaining); response = connection.getresponse()
            for name in ('Content-Type', 'Content-Encoding', 'Content-Length', 'Transfer-Encoding'):
                if len(response.headers.get_all(name, [])) > 1: raise ValueError
            if (response.status != 200 or response.headers.get_content_type() != 'application/json'
                    or response.headers.get('Content-Encoding', 'identity') != 'identity'
                    or response.headers.get_content_charset() not in (None, 'utf-8')):
                raise ValueError
            length = response.headers.get('Content-Length')
            transfer = response.headers.get('Transfer-Encoding')
            if transfer is not None and (transfer.lower() != 'chunked' or length is not None): raise ValueError
            if length is not None and (not re.fullmatch('[0-9]+', length) or int(length) > maximum): raise ValueError
            body = bytearray()
            while not response.isclosed():
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise TimeoutError
                sock.settimeout(remaining)
                chunk = response.read1(min(4096, maximum + 1 - len(body)))
                if not chunk: break
                body.extend(chunk)
                if len(body) > maximum: raise ValueError
            if length is not None and len(body) != int(length): raise ValueError
            return bytes(body)
        except (OSError, ValueError, http.client.HTTPException):
            raise MarketError('provider_result_unknown', '原站响应未知或无效，保留原操作，不自动重试。') from None
        finally:
            if response is not None: response.close()
            connection.close()
