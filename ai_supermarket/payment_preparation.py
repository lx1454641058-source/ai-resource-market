"""来源：本项目原创。Machine Pay接入前的可信宿主订单绑定示例。

仅读取现有市场报价；不生成Payment-Needed、不签名、不联网、不授予权益。
渠道金额口径、身份资格和资金链路仍未核实，因此任何结果都不可用于付款。
"""

import hashlib
import json
import math
import re

from ai_supermarket.commerce import MAX_PRICE_CENTS
from ai_supermarket.commission_agreements import AGREEMENT_POLICY, quote_fee
from ai_supermarket.common import MarketError, choice, fields, identifier, integer
from ai_supermarket.contracts import preflight
from ai_supermarket.exchange import PublishedCatalog


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def prepare_order(market, quote_id, expected_release_digest, merchant):
    """Read one authoritative snapshot, never trust buyer-provided price/merchant.

    merchant comes from host configuration, not a model argument. market_id must
    permanently identify one marketplace; seller_id/app_id/service_id must refer
    to a separately verified channel relationship before any future live usage.
    """
    if market.role != 'buyer':
        raise MarketError('forbidden', '付款订单准备需要买方宿主。')
    identifier(quote_id, 'quote_id')
    if not isinstance(expected_release_digest, str) or not re.fullmatch('[0-9a-f]{64}', expected_release_digest):
        raise MarketError('invalid_params', '需要选品时保留的原发行摘要。')
    names = ('market_id', 'market_seller', 'seller_id', 'app_id', 'service_id', 'environment')
    fields(merchant, names)
    for name in names:
        identifier(merchant[name], name)
    choice(merchant['environment'], ('sandbox', 'production'), 'environment')
    # Freeze the trusted mapping for this invocation; later edits must not change
    # the identity included in a previously returned order binding.
    merchant = dict(merchant)
    with market.store.connect() as db:
        db.execute('BEGIN')
        row = db.execute('SELECT * FROM quotes WHERE id=? AND buyer=?',
                         (quote_id, market.principal)).fetchone()
        if not row:
            raise MarketError('quote_not_found', '当前买方没有此报价。')
        now = market.clock()
        if not isinstance(now, (int, float)) or isinstance(now, bool) or not math.isfinite(now):
            raise MarketError('invalid_clock', '宿主时间不可用。')
        if not row['created_at'] <= now < row['expires_at']:
            raise MarketError('quote_expired', '报价不在有效期内；未准备付款订单。')
        integer(row['price_cents'], 1, MAX_PRICE_CENTS, 'price_cents')
        item = PublishedCatalog(db).get(row['resource_id'], row['version'], row['purpose'])
        fees = quote_fee(db, row, item['seller'], timestamp=now)
        if (row['content_hash'] != item['sha256'] or row['offer_revision'] != item['offer_revision']
                or row['price_cents'] != item['price_cents']):
            raise MarketError('quote_changed', '报价与当前商品版本或售价不符。')
        if merchant['market_seller'] != item['seller']:
            raise MarketError('seller_mismatch', '渠道商户映射不属于当前商品卖方。')
        if market._release_digest(db, row['resource_id'], row['version']) != expected_release_digest:
            raise MarketError('release_changed', '发行条件变化，不能静默接受新许可或版本。')
        if preflight(item.get('usage_contract'), market.host_policy)['status'] != 'ready':
            raise MarketError('incompatible', '宿主尚不能接收该资源，未准备付款。')
        owned = db.execute('''SELECT 1 FROM acquisitions WHERE principal=? AND resource_id=?
            AND version=? AND purpose=? AND content_hash=?''',
            (market.principal, row['resource_id'], row['version'], row['purpose'], row['content_hash'])).fetchone()
        if owned:
            raise MarketError('already_owned', '已有同用途版本权益，应复用而非准备新采购。')
        binding = {'format': 'machine-pay-order-preparation-v1', 'merchant': merchant,
            'buyer': market.principal, 'quote_id': quote_id, 'resource': row['resource_id'],
            'version': row['version'], 'purpose': row['purpose'], 'content_hash': row['content_hash'],
            'release_digest': expected_release_digest, 'offer_revision': row['offer_revision'],
            'price_cents': row['price_cents'], 'currency': 'CNY', 'expires_at': row['expires_at'],
            'fee_policy_version': row['policy_version']}
        if row['policy_version'] == AGREEMENT_POLICY:
            binding['format'] = 'machine-pay-order-preparation-v2'
            binding['fee_policy'] = fees
    return _prepared_record(binding)


def _prepared_record(binding):
    """Serialize already validated frozen terms; no payment or current-state check."""
    digest = hashlib.sha256(_canonical(binding).encode('utf-8')).hexdigest()
    # The full digest is retained. A future persistent channel-order registry MUST
    # reject a short-ID collision bound to a different digest, never truncate the
    # market resource name or accept the short ID alone as an entitlement.
    resource = hashlib.sha256(('resource:' + digest).encode()).hexdigest()[:32]
    order = hashlib.sha256(('order:' + digest).encode()).hexdigest()[:32]
    return {'status': 'prepared_not_payable', 'binding': binding, 'binding_sha256': digest,
            'channel_identifiers': {'resource_id': resource, 'out_trade_no': order},
            'channel_amount': None, 'payment_needed_header': None,
            'payment_authorized': False, 'entitlement_created': False,
            'merchant_relationship_verified': False,
            'remaining': ['amount_convention', 'merchant_and_platform_eligibility',
                'persistent_payment_order_and_trade_binding', 'channel_signature_and_verification',
                'refund_and_commission_settlement', 'purchase_authorization']}


def recheck_order(market, prepared, merchant):
    """Host revalidates a retained preparation before any future payment integration.

    This is a point-in-time check, not a price reservation or a payment permission.
    An expired preparation cannot be used for a new purchase; reconciliation of an
    already-paid order needs its own persistent state and channel evidence.
    """
    if not isinstance(prepared, dict) or not isinstance(prepared.get('binding'), dict):
        raise MarketError('invalid_params', '需要原订单准备记录。')
    binding = prepared['binding']
    if 'quote_id' not in binding or 'release_digest' not in binding:
        raise MarketError('invalid_params', '原订单准备记录缺失。')
    current = prepare_order(market, binding['quote_id'], binding['release_digest'], merchant)
    if _canonical(current) != _canonical(prepared):
        raise MarketError('order_changed', '订单准备记录或连接关系变化；未创建替代订单。')
    return current
