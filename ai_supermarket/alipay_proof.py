"""来源：本项目原创。RSA2原始响应验签和订单匹配，不联网或发放权益。

公钥、商户映射和金额口径只能由可信宿主注入。验签不能单独证明响应
新鲜度、商户资格、当前可履约状态或真实资金到账，不能代替完整支付链路。
"""
import base64
import binascii
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import re

from .common import MarketError, choice, fields, identifier, integer, parse_json, text
from .payment_preparation import _canonical


METHOD = 'alipay.aipay.agent.payment.verify'
RESPONSE_KEY = 'alipay_aipay_agent_payment_verify_response'
MAX_RESPONSE = 65536


def request_digest(request, merchant):
    fields(request, ('trade_no', 'payment_proof'), ('client_session',))
    text(request['trade_no'], 'trade_no', 32)
    text(request['payment_proof'], 'payment_proof', 64)
    if 'client_session' in request:
        text(request['client_session'], 'client_session', 1024)
    return hashlib.sha256(_canonical({'method': METHOD, 'merchant': merchant,
                                     'biz_content': request}).encode('utf-8')).hexdigest()


def _signed_response(raw, response_key=RESPONSE_KEY):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_RESPONSE:
        raise MarketError('invalid_response', '渠道原始响应大小或类型无效。')
    envelope = parse_json(raw)
    fields(envelope, (response_key, 'sign'), ('sign_type',))
    if 'sign_type' in envelope and envelope['sign_type'] != 'RSA2':
        raise MarketError('unsupported_signature', '仅允许宿主固定的RSA2算法。')
    if not isinstance(envelope[response_key], dict):
        raise MarketError('invalid_response', '渠道业务响应必须是对象。')
    if not isinstance(envelope['sign'], str) or len(envelope['sign']) > 2048:
        raise MarketError('invalid_signature', '渠道签名格式无效。')
    try:
        signature = base64.b64decode(envelope['sign'], validate=True)
    except (ValueError, binascii.Error):
        raise MarketError('invalid_signature', '渠道签名格式无效。') from None
    # Parse top-level token boundaries, preserving original escapes/whitespace.
    # Never locate the node by regex or reserialize parsed business fields.
    source = raw.decode('utf-8')
    decoder = json.JSONDecoder()
    pos = source.index('{') + 1
    signed = None
    for _ in range(len(envelope)):
        while source[pos] in ' \r\n\t': pos += 1
        key, pos = decoder.raw_decode(source, pos)
        while source[pos] in ' \r\n\t': pos += 1
        pos += 1  # Colon; the complete document was strictly parsed above.
        while source[pos] in ' \r\n\t': pos += 1
        start = pos
        _, pos = decoder.raw_decode(source, pos)
        if key == response_key:
            signed = source[start:pos].encode('utf-8')
        while source[pos] in ' \r\n\t': pos += 1
        if source[pos] == ',': pos += 1
    if signed is None:
        raise MarketError('invalid_response', '找不到唯一的业务响应原文。')
    return envelope[response_key], signed, signature


class AlipayProofVerifier:
    def __init__(self, public_key_pem, merchant, *, amount_unit, policy_reference):
        fields(merchant, ('market_id','market_seller','seller_id','app_id','service_id','environment'))
        for name, value in merchant.items(): identifier(value, name)
        choice(merchant['environment'], ('sandbox','production'), 'environment')
        choice(amount_unit, ('CNY_major','CNY_minor'), 'amount_unit')
        identifier(policy_reference, 'policy_reference')
        if not isinstance(public_key_pem, bytes) or len(public_key_pem) > 16384:
            raise MarketError('invalid_verification_key', '需要宿主预先配置的验签公钥。')
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
        except ImportError:
            raise MarketError('payment_dependency_missing', '尚未安装可选验签依赖，停止核验。') from None
        try:
            key = serialization.load_pem_public_key(public_key_pem)
        except (ValueError, TypeError):
            raise MarketError('invalid_verification_key', '验签公钥格式无效。') from None
        if not isinstance(key, rsa.RSAPublicKey) or not 2048 <= key.key_size <= 8192:
            raise MarketError('invalid_verification_key', '仅支持2048～8192位RSA验签公钥。')
        der = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self._key, self._merchant = key, deepcopy(merchant)
        self.amount_unit, self.policy_reference = amount_unit, policy_reference
        self.key_sha256 = hashlib.sha256(der).hexdigest()

    def verify(self, raw, prepared, request):
        if not isinstance(prepared, dict) or not isinstance(prepared.get('binding'), dict):
            raise MarketError('invalid_order', '需要原订单准备记录。')
        binding = prepared['binding']
        digest = hashlib.sha256(_canonical(binding).encode('utf-8')).hexdigest()
        if (digest != prepared.get('binding_sha256') or binding.get('merchant') != self._merchant
                or binding.get('currency') != 'CNY'):
            raise MarketError('order_changed', '订单摘要、商户或币种不符。')
        integer(binding.get('price_cents'), 1, 100_000_000, 'price_cents')
        expected_ids = {'resource_id': hashlib.sha256(('resource:'+digest).encode()).hexdigest()[:32],
                        'out_trade_no': hashlib.sha256(('order:'+digest).encode()).hexdigest()[:32]}
        if prepared.get('channel_identifiers') != expected_ids:
            raise MarketError('order_changed', '渠道编号与原订单摘要不符。')
        requested = request_digest(request, self._merchant)
        body, signed, signature = _signed_response(raw)
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        try:
            self._key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        except (InvalidSignature, ValueError):
            raise MarketError('invalid_signature', '原始渠道响应验签失败。') from None
        if body.get('code') != '10000' or body.get('active') is not True or body.get('sub_code'):
            raise MarketError('payment_not_verified', '渠道业务结果或凭证有效性未通过。')
        for name, expected in {**expected_ids, 'trade_no':request['trade_no']}.items():
            if body.get(name) != expected:
                raise MarketError('payment_binding_mismatch', '渠道交易与原请求、订单或资源不匹配。')
        for name, expected in {'currency':'CNY','seller_id':self._merchant['seller_id'],
                               'app_id':self._merchant['app_id']}.items():
            if name in body and body[name] != expected:
                raise MarketError('payment_binding_mismatch', '渠道币种或主体字段不符。')
        value = body.get('amount')
        # Preserve exact decimal text. Numeric JSON values are refused until the
        # actual channel encoding is established; never round floats or guess units.
        pattern = r'(0|[1-9][0-9]{0,8})(\.[0-9]{1,2})?' if self.amount_unit=='CNY_major' else r'(0|[1-9][0-9]{0,8})'
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise MarketError('invalid_channel_amount', '渠道金额编码不符合宿主固定口径。')
        cents = int(Decimal(value) * (100 if self.amount_unit=='CNY_major' else 1))
        if cents != binding['price_cents']:
            raise MarketError('payment_amount_mismatch', '渠道金额与原订单不符。')
        return {'status':'signature_and_order_match', 'order_id':expected_ids['out_trade_no'],
                'trade_no':body['trade_no'], 'resource_id':expected_ids['resource_id'],
                'amount_cents':cents, 'currency':'CNY', 'environment':self._merchant['environment'],
                'binding_sha256':digest, 'request_sha256':requested,
                'response_sha256':hashlib.sha256(raw).hexdigest(), 'public_key_sha256':self.key_sha256,
                'amount_unit':self.amount_unit, 'policy_reference':self.policy_reference,
                'response_freshness_verified':False, 'merchant_relationship_verified':False,
                'paid_verified':False, 'entitlement_created':False}
