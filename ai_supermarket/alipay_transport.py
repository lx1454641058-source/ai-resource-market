"""来源：本项目原创。支付宝凭证核验请求适配，不创建付款或发放权益。

只允许可信宿主配置。真实网关默认禁用；测试仅显式使用127.0.0.1。
原始付款凭证和签名报文只在内存中使用，账本只留条件与摘要。
"""
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import math
import ssl
import time
from urllib.parse import urlencode, urlsplit

from .alipay_proof import AlipayProofVerifier, MAX_RESPONSE, METHOD, request_digest
from .common import MarketError, identifier
from .payment_orders import _get, _put
from .payment_preparation import _canonical


GATEWAYS = {
    'production': 'https://openapi.alipay.com/gateway.do',
    'sandbox': 'https://openapi-sandbox.dl.alipaydev.com/gateway.do',
}
SHANGHAI = timezone(timedelta(hours=8))


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _now(clock):
    value = clock()
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < 253402214400:
        raise MarketError('invalid_clock', '渠道请求时间不可用。')
    return value


class RSA2RequestSigner:
    """In-memory PEM supplied by the host; never reads a file or prints a key."""
    def __init__(self, private_key_pem):
        if not isinstance(private_key_pem, bytes) or len(private_key_pem) > 16384:
            raise MarketError('invalid_signing_key', '需要宿主配置的RSA签名密钥。')
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.exceptions import UnsupportedAlgorithm
        except ImportError:
            raise MarketError('payment_dependency_missing', '尚未安装可选签名依赖。') from None
        try:
            key = serialization.load_pem_private_key(private_key_pem, password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm):
            raise MarketError('invalid_signing_key', '签名密钥格式不受支持。') from None
        if not isinstance(key, rsa.RSAPrivateKey) or not 2048 <= key.key_size <= 8192:
            raise MarketError('invalid_signing_key', '仅支持2048～8192位RSA签名密钥。')
        self._key = key
        self.public_key_sha256 = _sha(key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))

    def _request(self, request, merchant, timestamp):
        # Validate before signing. Keep exact business JSON bytes used on the wire.
        request_digest(request, merchant)
        params = {'app_id': merchant['app_id'], 'method': METHOD, 'format': 'json',
                  'charset': 'utf-8', 'sign_type': 'RSA2', 'timestamp': timestamp,
                  'version': '1.0', 'biz_content': _canonical(request)}
        params['sign'] = self._sign_fields(params)
        # Official common parameters go in URL, business JSON in POST form body.
        body = urlencode({'biz_content': params.pop('biz_content')}).encode('ascii')
        return urlencode(params), body

    def _sign_fields(self, params):
        """Internal protocol signing only; never expose an arbitrary signing tool."""
        source = '&'.join(key+'='+params[key] for key in sorted(params)).encode('utf-8')
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        return base64.b64encode(
            self._key.sign(source, padding.PKCS1v15(), hashes.SHA256())).decode('ascii')


class AlipayVerificationClient:
    """One explicit verification query; no retries, redirects or payment methods.

    network_enabled is host configuration, not a model argument or buyer consent.
    It authorizes only this query adapter, never purchases, publishing or grants.
    """
    def __init__(self, orders, verifier, signer, *, network_enabled=False,
                 loopback_endpoint=None, timeout=10, clock=time.time):
        if type(verifier) is not AlipayProofVerifier or type(signer) is not RSA2RequestSigner:
            raise MarketError('invalid_adapter', '需要可信宿主固定的签名与验签实现。')
        if orders.merchant != verifier._merchant:
            raise MarketError('order_changed', '核验器与订单宿主商户映射不一致。')
        if type(network_enabled) is not bool:
            raise MarketError('invalid_params', 'network_enabled必须为布尔值。')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise MarketError('invalid_params', '核验超时必须在0～30秒之间。')
        self._merchant = deepcopy(orders.merchant)
        self._endpoint = GATEWAYS[self._merchant['environment']]
        self._test = loopback_endpoint is not None
        if self._test:
            try:
                parsed = urlsplit(loopback_endpoint)
                valid = (self._merchant['environment'] == 'sandbox' and parsed.scheme == 'http'
                         and parsed.hostname == '127.0.0.1' and parsed.port is not None
                         and 0 < parsed.port < 65536 and parsed.username is None and parsed.password is None
                         and parsed.path == '/gateway.do' and not parsed.query and not parsed.fragment)
            except (ValueError, TypeError, AttributeError):
                valid = False
            if not valid:
                raise MarketError('invalid_endpoint', '测试端点仅允许sandbox环境的127.0.0.1网关。')
            self._endpoint = loopback_endpoint
        self.orders, self._verifier, self._signer = orders, verifier, signer
        self._enabled, self._timeout, self._clock = network_enabled, timeout, clock

    @staticmethod
    def _key(order_id, attempt_id):
        identifier(order_id, 'order_id'); identifier(attempt_id, 'attempt_id')
        return 'payment_transport:'+order_id+':'+attempt_id

    def _reserve(self, order_id, attempt_id, digest, metadata):
        key = self._key(order_id, attempt_id)
        with self.orders.market.store.connect(write=True) as db:
            order = self.orders._order(db, order_id)
            attempt = next((a for a in order['attempts'] if a['id'] == attempt_id), None)
            if attempt is None or attempt['request_sha256'] != digest or attempt['outcome'] != 'pending':
                raise MarketError('verification_request_mismatch', '发送条件与原核验记录不符。')
            if _get(db, key) is not None:
                raise MarketError('transport_reserved', '原请求已有发送意图，不能重发。')
            _put(db, key, metadata)

    def _finish(self, order_id, attempt_id, updates):
        key = self._key(order_id, attempt_id)
        with self.orders.market.store.connect(write=True) as db:
            self.orders._order(db, order_id)
            value = _get(db, key)
            if value is None or value['outcome'] != 'reserved':
                raise MarketError('transport_conflict', '原发送记录不存在或已结案。')
            _put(db, key, {**value, **updates})

    def history(self, order_id, attempt_id):
        key = self._key(order_id, attempt_id)
        with self.orders.market.store.connect() as db:
            db.execute('BEGIN')
            order = self.orders._order(db, order_id)
            value = _get(db, key)
        return {'order_id': order_id, 'attempt_id': attempt_id, 'order_status': order['status'],
                'transport': value, 'paid_verified': 'confirmation' in order,
                'entitlement_created': 'entitlement_id' in order,
                'merchant_relationship_verified': False}

    def _post(self, query, body):
        parsed = urlsplit(self._endpoint)
        deadline = time.monotonic()+self._timeout
        conn = (http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=self._timeout)
                if self._test else http.client.HTTPSConnection(parsed.hostname, 443,
                    timeout=self._timeout, context=ssl.create_default_context()))
        response = None
        try:
            conn.connect()
            def remaining():
                value = deadline-time.monotonic()
                if value <= 0: raise TimeoutError()
                conn.sock.settimeout(value)
            remaining()
            conn.request('POST', parsed.path+'?'+query, body=body, headers={
                'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json',
                'Accept-Encoding': 'identity', 'Connection': 'close'})
            remaining()
            response_socket = conn.sock
            response = conn.getresponse()
            # No redirects, compression, arbitrary URLs, cookies, or environment proxy.
            if response.status != 200:
                raise MarketError('channel_http_error', '渠道HTTP结果不确定，保留原订单。')
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise MarketError('invalid_response', '拒绝压缩渠道响应。')
            length = response.getheader('Content-Length')
            if length is not None and (not length.isascii() or not length.isdecimal() or int(length) > MAX_RESPONSE):
                raise MarketError('invalid_response', '渠道响应长度无效。')
            raw = bytearray()
            # read1 keeps a slow body bounded by the remaining wall-clock budget.
            # The socket is retained by HTTPResponse when Connection: close is used.
            while not response.isclosed():
                remaining_time = deadline-time.monotonic()
                if remaining_time <= 0: raise TimeoutError()
                response_socket.settimeout(remaining_time)
                chunk = response.read1(min(4096, MAX_RESPONSE+1-len(raw)))
                if not chunk: break
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE:
                    raise MarketError('invalid_response', '渠道响应超过大小限制。')
            if length is not None and len(raw) != int(length):
                raise MarketError('invalid_response', '渠道响应不完整。')
            return bytes(raw)
        finally:
            if response is not None:
                response.close()
            conn.close()

    def verify(self, order_id, attempt_id, request):
        if not self._enabled:
            raise MarketError('channel_disabled', '宿主未启用渠道核验。')
        # Own a snapshot; caller mutations must not change signed or verified data.
        request = deepcopy(request)
        if self.orders.merchant != self._merchant or self._verifier._merchant != self._merchant:
            raise MarketError('order_changed', '运行期间商户映射变化。')
        digest = request_digest(request, self._merchant)
        issued_at = _now(self._clock)
        timestamp = datetime.fromtimestamp(issued_at, SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')
        query, body = self._signer._request(request, self._merchant, timestamp)
        record = self.orders.begin_verification(order_id, attempt_id, digest)
        if record.get('attempt_replayed'):
            return {**self.history(order_id, attempt_id), 'request_sent_this_call': False, 'replayed': True}
        metadata = {'endpoint': self._endpoint, 'method': METHOD, 'request_sha256': digest,
                    'wire_sha256': _sha((self._endpoint+'?'+query+'\n').encode()+body),
                    'request_timestamp': timestamp, 'issued_at': issued_at,
                    'signing_public_key_sha256': self._signer.public_key_sha256,
                    'verification_public_key_sha256': self._verifier.key_sha256,
                    'amount_unit': self._verifier.amount_unit, 'policy_reference': self._verifier.policy_reference,
                    'loopback_test': self._test, 'outcome': 'reserved',
                    'https_response_received': False, 'response_sha256': None}
        self._reserve(order_id, attempt_id, digest, metadata)
        started = time.monotonic()
        try:
            raw = self._post(query, body)
        except (OSError, http.client.HTTPException, MarketError):
            # Do not log/return an exception message containing URL, proof or keys.
            self.orders.observe(order_id, attempt_id, 'unknown')
            self._finish(order_id, attempt_id, {'outcome':'unknown', 'error':'channel_result_unknown'})
            return {**self.history(order_id, attempt_id), 'request_sent_this_call': None, 'replayed': False}
        receipt = {'response_sha256':_sha(raw), 'https_response_received':not self._test,
                   'received_at':_now(self._clock),
                   'elapsed_ms':round((time.monotonic()-started)*1000)}
        try:
            self.orders.record_signed_response(order_id, attempt_id, raw, request, self._verifier)
        except MarketError:
            self.orders.observe(order_id, attempt_id, 'unverified_response', _sha(raw))
            self._finish(order_id, attempt_id, {**receipt, 'outcome':'rejected', 'error':'response_not_verified'})
        else:
            self._finish(order_id, attempt_id, {**receipt, 'outcome':'signature_and_order_match'})
        return {**self.history(order_id, attempt_id), 'request_sent_this_call': True, 'replayed': False}
