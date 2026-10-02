"""来源：本项目原创。机器HTTP客户端，无自动重试、重定向、环境代理或证书降级。"""

import ipaddress
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request

from .common import MarketError, compact, fields, parse_json


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RemoteMarket:
    """Trusted host holds the credential. Model arguments never select URL, role or credentials."""
    def __init__(self, base_url, credential_file, timeout=15):
        if not isinstance(base_url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in base_url):
            raise MarketError('invalid_endpoint', '服务地址不能包含空白或控制字符。')
        try:
            parsed = urlsplit(base_url)
        except ValueError:
            raise MarketError('invalid_endpoint', '服务地址格式无效。') from None
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise MarketError('invalid_endpoint', '需要不含凭据、路径或查询参数的服务地址。')
        if parsed.scheme == 'http':
            try:
                local = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                local = False
            if not local:
                raise MarketError('invalid_endpoint', 'HTTP仅允许本机IP；其他地址必须使用HTTPS。')
        try:
            parsed.port
        except ValueError:
            raise MarketError('invalid_endpoint', '服务端口无效。') from None
        if type(timeout) not in (int, float) or not 0 < timeout <= 60:
            raise MarketError('invalid_params', '请求等待时间需为0～60秒。')
        self.base_url = base_url.rstrip('/')
        self.credential_file = Path(credential_file)
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def credential(self):
        try:
            with self.credential_file.open('rb') as handle:
                raw = handle.read(4097)
            if len(raw) > 4096:
                raise ValueError('size')
            value = parse_json(raw)
            fields(value, ('token',))
            token = value['token']
            if not isinstance(token, str) or not re.fullmatch(r'ak_[0-9a-f]{32}\.[A-Za-z0-9_-]{43}', token):
                raise ValueError('format')
            return token
        except (OSError, ValueError):
            raise MarketError('credential_unavailable', '本地凭据文件不可用；未发送请求。') from None

    def request(self, path, value=None):
        token = self.credential()
        return self._send(path, value, {'Authorization': 'Bearer ' + token})

    def _send(self, path, value, authorization):
        """Shared transport; only trusted client methods choose path and auth headers."""
        data = compact(value).encode('utf-8') if value is not None else None
        if data is not None and len(data) > 131072:
            raise MarketError('invalid_size', '请求超过128KB，未发送。')
        return self._send_bytes(path, data, authorization, 'application/json')

    def _send_bytes(self, path, data, authorization, content_type):
        headers = {**authorization, 'Accept': 'application/json'}
        if data is not None:
            headers['Content-Type'] = content_type
        request = Request(self.base_url + path, data=data, headers=headers, method='POST' if data is not None else 'GET')
        try:
            try:
                response = self.opener.open(request, timeout=self.timeout)
            except HTTPError as exc:
                response = exc
            with response:
                if 300 <= response.code < 400:
                    raise MarketError('redirect_denied', '服务要求重定向；未转发凭据。')
                if response.headers.get_content_type() != 'application/json':
                    raise MarketError('invalid_response', '服务返回了非JSON结果；操作状态可能未知。')
                raw = response.read(1_048_577)
                if len(raw) > 1_048_576:
                    raise MarketError('invalid_response', '服务响应过大；操作状态可能未知。')
                result = parse_json(raw)
                if not isinstance(result, dict):
                    raise MarketError('invalid_response', '服务响应格式无效；操作状态可能未知。')
                if response.code >= 400 and result.get('status') not in ('error', 'unavailable', 'payment_unavailable'):
                    raise MarketError('invalid_response', '错误响应未提供可识别状态。')
                retry = response.headers.get('Retry-After', '')
                if response.code in (429, 503) and retry.isdigit() and len(retry) <= 6:
                    result['retry_after_seconds'] = int(retry)
                return result
        except MarketError:
            raise
        except (URLError, OSError, ValueError):
            raise MarketError('transport_unknown', '未获得可验证响应；保留原操作编号核验，不自动重试或重新购买。') from None

    def tool_list(self):
        return self.request('/v1/tools')

    def upload_asset(self, request, raw):
        from .asset_upload import encode, describe, ROUTE, MEDIA_TYPE
        body = encode(request, raw)
        token = self.credential()
        auth = {'Authorization': 'Bearer ' + token}
        discovery = self._send('/v1/tools', None, auth)
        if (discovery.get('role') != 'seller' or discovery.get('seller_principal') != request['seller']
                or discovery.get('asset_upload') != describe()):
            raise MarketError('asset_upload_unavailable', '身份或素材提交能力不匹配；未发送素材。')
        result = self._send_bytes(ROUTE, body, auth, MEDIA_TYPE)
        if result.get('status') == 'error':
            return result
        if (result.get('status') != 'draft' or result.get('id') != request['seller'] + ':' + request['slug']
                or result.get('version') != request['version'] or result.get('revision') != 1
                or result.get('asset_sha256') != request['metadata']['asset']['sha256']):
            raise MarketError('invalid_response', '素材提交回执与请求不符；保留原编号核验。')
        return result

    def call(self, tool, arguments):
        result = self.request('/v1/tools/call', {'tool': tool, 'arguments': arguments})
        if not isinstance(result.get('status'), str) or not result['status']:
            raise MarketError('invalid_response', '工具响应缺少业务状态；操作结果可能未知。')
        return result
