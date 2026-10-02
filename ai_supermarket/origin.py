"""来源：本项目原创。原站文件交付候选；不支付、不运行文件，不自动重试。

供应商只在市场保存经审核的文件描述。原文件从宿主批准的供应商端点
直接写入任务目录，字节不进入工具JSON或市场账本。尚不是生产协议。
"""
import hashlib
import hmac
import ipaddress
import math
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
import uuid

from .common import MarketError, compact, fields, identifier, integer, parse_json
from .http_client import NoRedirect


MAX_FILE_BYTES = 2_097_152
MEDIA = ('text/markdown', 'text/csv', 'application/pdf', 'image/png', 'audio/wav')
ORIGIN_SCHEMA = {'type': 'object', 'additionalProperties': False,
    'required': ['provider', 'resource_key', 'sha256', 'size', 'media_type'], 'properties': {
        'provider': {'type': 'string', 'minLength': 1, 'maxLength': 128},
        'resource_key': {'type': 'string', 'minLength': 1, 'maxLength': 128},
        'sha256': {'type': 'string', 'minLength': 64, 'maxLength': 64},
        'size': {'type': 'integer', 'minimum': 1, 'maximum': MAX_FILE_BYTES},
        'media_type': {'type': 'string', 'minLength': 1, 'maxLength': 100, 'enum': list(MEDIA)}}}


def validate_origin(origin):
    fields(origin, ORIGIN_SCHEMA['required'])
    identifier(origin['provider'], 'origin.provider')
    identifier(origin['resource_key'], 'origin.resource_key')
    if not isinstance(origin['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', origin['sha256']):
        raise MarketError('invalid_params', '原站摘要需为SHA256。')
    integer(origin['size'], 1, MAX_FILE_BYTES, 'origin.size')
    if origin['media_type'] not in MEDIA:
        raise MarketError('invalid_params', '不支持此文件格式。')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return compact(value).encode('utf-8')


def verify_origin_request(raw, signature, key, entitlement_check, now=None):
    """Provider-side ingress helper, separate from the buyer's assertions.

    entitlement_check MUST consult provider/market authoritative entitlement
    records. A valid delivery signature is not a payment receipt. Providers
    persist (order_id, fingerprint) and reject a conflicting replay themselves.
    """
    if not isinstance(raw, bytes) or len(raw) > 16384 or not isinstance(key, bytes) or len(key) < 32:
        raise MarketError('invalid_request', '原站请求或供应商配置无效。')
    expected = hmac.new(key, raw, hashlib.sha256).hexdigest()
    if (not isinstance(signature, str) or not re.fullmatch('[0-9a-f]{64}', signature)
            or not hmac.compare_digest(signature, expected)):
        raise MarketError('forbidden', '原站请求签名不符。')
    request = parse_json(raw)
    fields(request, ('order_id', 'buyer', 'acquisition_id', 'task_id', 'request_id', 'id',
                     'version', 'purpose', 'origin', 'issued_at', 'expires_at'))
    for field in ('order_id', 'buyer', 'acquisition_id', 'task_id', 'request_id', 'id', 'version'):
        identifier(request[field], field)
    if request['purpose'] not in ('personal_use', 'commercial_use'):
        raise MarketError('invalid_request', '用途无效。')
    validate_origin(request['origin'])
    for field in ('issued_at', 'expires_at'):
        integer(request[field], 0, 9_999_999_999, field)
    now = time.time() if now is None else now
    if not request['issued_at'] <= now <= request['expires_at'] or not 0 < request['expires_at'] - request['issued_at'] <= 60:
        raise MarketError('expired', '原站交付请求已过期或有效期无效。')
    if entitlement_check(request) is not True:
        raise MarketError('entitlement_denied', '供应商未确认原资源权益，不能交付。')
    fingerprint = sha(encoded({k: v for k, v in request.items() if k not in ('issued_at', 'expires_at')}))
    return request, fingerprint


class OriginClient:
    """Trusted host configuration. Nothing here is a model-callable tool.

    key is a provider-specific delivery credential held by the trusted host.
    The provider verifies existing entitlements independently of this signature;
    that trust arrangement and key provisioning need a real supplier agreement.
    It is neither an Alipay token nor evidence of any money being transferred.
    """
    def __init__(self, provider, endpoint, key, binding_id, timeout=10):
        self.provider = identifier(provider, 'provider')
        self.binding_id = identifier(binding_id, 'binding_id')
        if not isinstance(endpoint, str) or any(ord(c) <= 32 or ord(c) == 127 for c in endpoint):
            raise MarketError('invalid_endpoint', '供应商端点无效。')
        try:
            parsed = urlsplit(endpoint)
            if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.query or parsed.fragment or parsed.port == 0):
                raise ValueError
            if parsed.scheme == 'http' and not ipaddress.ip_address(parsed.hostname).is_loopback:
                raise ValueError
        except ValueError:
            raise MarketError('invalid_endpoint', '端点需为HTTPS，HTTP仅允许本机IP；禁止地址携带凭据。') from None
        if not isinstance(key, bytes) or len(key) < 32:
            raise MarketError('invalid_params', '供应商交付凭据不可用。')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise MarketError('invalid_params', '等待上限应为0～30秒。')
        self.endpoint, self.key, self.timeout = endpoint, key, timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def fetch(self, request, origin):
        if origin['provider'] != self.provider:
            raise MarketError('provider_mismatch', '资源供应商与宿主连接不一致。')
        payload = encoded(request)
        signature = hmac.new(self.key, payload, hashlib.sha256).hexdigest()
        wire = Request(self.endpoint, data=payload, method='POST', headers={
            'Content-Type': 'application/json', 'Accept': origin['media_type'],
            'X-Market-Signature': signature})
        try:
            with self.opener.open(wire, timeout=self.timeout) as response:
                if (response.status != 200 or response.headers.get_content_type() != origin['media_type']
                        or response.headers.get('Content-Encoding', 'identity') != 'identity'):
                    raise MarketError('invalid_delivery', '供应商返回的状态或格式不符。')
                raw = response.read(origin['size'] + 1)
        except HTTPError as error:
            error.close()
            raise MarketError('origin_unconfirmed', '供应商未交付或要求跳转；保留原订单，不自动重试。') from None
        except (URLError, OSError, ValueError):
            raise MarketError('origin_unconfirmed', '原站响应未知；查询供应商原订单，不换编号重购。') from None
        if len(raw) != origin['size'] or sha(raw) != origin['sha256']:
            raise MarketError('integrity_failed', '文件大小或摘要与已审核版本不符。')
        return raw


def _safe_path(path):
    path = Path(path).absolute()
    if str(path).startswith(('\\\\', '//')):
        raise MarketError('invalid_path', '仅写入本机任务目录。')
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise MarketError('invalid_path', '不允许链接目录。')
    return path


def _inspect(market, db, acquisition_id, task_id, request_id):
    from .contracts import preflight
    from .exchange import PublishedCatalog
    market._require('buyer')
    acquired = market._owned(db, acquisition_id)
    catalog = PublishedCatalog(db, allow_existing=True)
    item = catalog.get(acquired['resource_id'], acquired['version'], acquired['purpose'])
    catalog.read(item)
    if 'origin' not in item or item['sha256'] != acquired['content_hash']:
        raise MarketError('version_changed', '原站资源描述与获取记录不一致。')
    if not acquired['id'].startswith('ap_'):
        raise MarketError('release_binding_required', '原站交付必须绑定原发行摘要。')
    compatibility = preflight(item['usage_contract'], market.host_policy)
    if compatibility['status'] != 'ready':
        raise MarketError(compatibility['status'], '宿主能力或权限不足。')
    old = db.execute('SELECT * FROM usage_events WHERE principal=? AND task_id=? AND request_id=?',
                     (market.principal, task_id, request_id)).fetchone()
    if old and old['acquisition_id'] != acquisition_id:
        raise MarketError('idempotency_conflict', '原交付请求已用于其他资源。')
    return dict(acquired), item, dict(old) if old else None


def deliver_origin(market, acquisition_id, task_id, request_id, client, directory):
    """Trusted-host transfer; paid acquisitions must already exist in the market.

    Current Exchange deliberately cannot create new paid acquisitions. This
    function never accepts a paid=true claim and never bypasses that boundary.
    On an unknown response, leave the binding file and stop. An explicit retry
    must use the original arguments, after checking the provider's order state.
    """
    identifier(task_id, 'task_id')
    identifier(request_id, 'request_id')
    with market.store.connect() as db:
        acquired, item, _ = _inspect(market, db, acquisition_id, task_id, request_id)
    origin = item['origin']
    if client.provider != origin['provider']:
        raise MarketError('provider_mismatch', '宿主绑定了其他供应商。')
    # Include the trusted connection identity and market path: independent
    # markets, tasks and suppliers cannot share an output binding accidentally.
    binding = {'schema': 'origin-delivery-candidate-v1', 'database': str(Path(market.store.path).absolute()),
        'principal': market.principal, 'acquisition_id': acquisition_id, 'task_id': task_id,
        'request_id': request_id, 'provider_binding': client.binding_id, 'endpoint': client.endpoint,
        'id': item['id'], 'version': item['version'], 'purpose': acquired['purpose'],
        'content_hash': acquired['content_hash'], 'origin': origin}
    order_id = sha(encoded(binding))
    folder = _safe_path(directory)
    descriptor = folder / 'binding.json'
    raw_binding = encoded(binding)
    if folder.exists():
        _safe_path(descriptor)
        if not descriptor.is_file() or descriptor.read_bytes() != raw_binding:
            raise MarketError('output_conflict', '目录不是当前原请求的交付目录。')
    else:
        folder.mkdir()
        with descriptor.open('xb') as handle:
            handle.write(raw_binding)
    suffix = {'text/markdown': '.md', 'text/csv': '.csv', 'application/pdf': '.pdf',
              'image/png': '.png', 'audio/wav': '.wav'}[origin['media_type']]
    target = _safe_path(folder / ('resource' + suffix))
    cached = target.exists()
    if cached:
        with target.open('rb') as handle:
            raw = handle.read(origin['size'] + 1)
        if len(raw) != origin['size'] or sha(raw) != origin['sha256']:
            raise MarketError('integrity_failed', '已有文件不完整或变化；保留，不覆盖。')
    else:
        request = {'order_id': order_id, 'buyer': market.principal, 'acquisition_id': acquisition_id,
            'task_id': task_id, 'request_id': request_id, 'id': item['id'], 'version': item['version'],
            'purpose': acquired['purpose'], 'origin': origin,
            'issued_at': int(time.time()), 'expires_at': int(time.time()) + 60}
        raw = client.fetch(request, origin)
        # Do not record a successful use if the resource was isolated or its
        # permission changed while the provider request was in flight.
        with market.store.connect() as db:
            _inspect(market, db, acquisition_id, task_id, request_id)
        with target.open('xb') as handle:
            handle.write(raw)
        if target.read_bytes() != raw:
            raise MarketError('integrity_failed', '本地写入核验失败。')
    license_path = _safe_path(folder / 'LICENSE.txt')
    license_bytes = item['license_text'].encode('utf-8')
    if license_path.exists():
        if license_path.read_bytes() != license_bytes:
            raise MarketError('output_conflict', '许可文件不一致，保留原文件。')
    else:
        with license_path.open('xb') as handle:
            handle.write(license_bytes)
    with market.store.connect(write=True) as db:
        _, current, old = _inspect(market, db, acquisition_id, task_id, request_id)
        if current['sha256'] != item['sha256']:
            raise MarketError('version_changed', '交付过程中版本变化。')
        usage_id = old['id'] if old else 'u_' + uuid.uuid4().hex
        if not old:
            db.execute('INSERT INTO usage_events VALUES (?,?,?,?,?,?)',
                       (usage_id, acquisition_id, market.principal, task_id, request_id, market.clock()))
    return {'status': 'delivered', 'path': str(target), 'license_path': str(license_path),
        'sha256': origin['sha256'], 'size': origin['size'], 'order_id': order_id, 'usage_id': usage_id,
        'cache_hit': cached, 'evidence': 'verified_bytes_only', 'task_completed': False,
        'real_payments': False, 'execution_authorized': False}
