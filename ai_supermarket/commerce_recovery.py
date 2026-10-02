"""本项目原创。协议、报价与已登记支付历史的只读恢复校验，不恢复购买授权。"""
import re
from types import SimpleNamespace

from .commission_agreements import AGREEMENT_POLICY, _current, _prefix, _terms, quote_fee
from .common import MarketError, fields, identifier, integer, parse_json
from .commerce import MAX_PRICE_CENTS
from .exchange import Exchange
from .payment_orders import PaymentOrders, _hash
from .payment_preparation import _prepared_record

PREFIXES = ('commission_agreement:', 'quote_fee:', 'payment_order:', 'payment_intent:',
            'payment_request:', 'payment_channel_id:')
UNCONFIRMED = ('prepared_not_payable', 'handoff_reserved', 'verification_pending',
               'reconciliation_required', 'cancelled_before_verification')


def _bad(message):
    raise MarketError('commerce_record_corrupt', message)


def validate_metadata(db, meta):
    """All pointers are checked in both directions. No live clock or signer.

    Payment evidence, rights and refunds use explicit downstream validators.
    Bills and host records use a separate validator, never broad prefix approval.
    """
    found = {}
    for key, raw in meta.items():
        if key == 'payment_environment' or key.startswith(PREFIXES):
            if not isinstance(raw, str) or len(raw.encode('utf-8')) > 32768:
                _bad('商业历史记录超过上限。')
            found[key] = parse_json(raw)
    environment = found.get('payment_environment')
    if 'payment_environment' in found and environment not in ('sandbox', 'production'):
        _bad('支付账本环境无效。')

    histories = {}; pointers = {}
    for key, value in found.items():
        if not key.startswith('commission_agreement:'):
            continue
        match = re.fullmatch(r'(commission_agreement:[0-9a-f]{64}:)(current|[1-9][0-9]{0,6})', key)
        if not match:
            _bad('协议键或修订编号无效。')
        prefix, revision = match.groups()
        if revision == 'current':
            pointers[prefix] = value
        else:
            _terms(value)
            if prefix != _prefix(value['seller']) or str(value['revision']) != revision:
                _bad('协议历史归属或修订不符。')
            histories.setdefault(prefix, []).append(value)
    if set(pointers) != set(histories):
        _bad('协议历史或当前指针缺失。')
    for prefix, records in histories.items():
        latest = max(records, key=lambda item: item['revision'])
        if latest['revision'] != len(records) or _current(db, latest['seller']) != latest:
            _bad('协议修订有缺失或当前指针回退。')

    # Quotes keep the fee revision used when issued, even after a later agreement.
    expected_fees = set()
    for row in db.execute('SELECT * FROM quotes'):
        item = db.execute('SELECT seller FROM submissions WHERE resource_id=? AND version=?',
                          (row['resource_id'], row['version'])).fetchone()
        if not item:
            _bad('报价原商品缺失。')
        quote_fee(db, row, item['seller'])
        if row['policy_version'] == AGREEMENT_POLICY:
            expected_fees.add('quote_fee:' + row['id'])
    if expected_fees != {key for key in found if key.startswith('quote_fee:')}:
        _bad('报价费率快照缺失或没有原报价。')

    orders = {}; expected_channels = {}; intent_orders = {}; referenced_orders = set()
    for key, record in found.items():
        if not key.startswith('payment_order:'):
            continue
        order_id = key[len('payment_order:'):]
        if not re.fullmatch('[0-9a-f]{32}', order_id) or not isinstance(record, dict):
            _bad('支付准备编号或记录无效。')
        prepared = record.get('prepared')
        if not isinstance(prepared, dict) or not isinstance(prepared.get('binding'), dict):
            _bad('支付准备绑定缺失。')
        binding = prepared['binding']; merchant = binding.get('merchant')
        fields(merchant, ('market_id', 'market_seller', 'seller_id', 'app_id', 'service_id', 'environment'))
        for name, value in merchant.items(): identifier(value, name)
        if merchant['environment'] not in ('sandbox', 'production') or (environment is not None and environment != merchant['environment']):
            _bad('订单与原支付环境不符。')
        identifier(binding.get('buyer'), 'buyer')
        # Reuse existing order integrity checks without opening a market or using
        # a current authority: this object supplies only the historical principal.
        view = PaymentOrders(SimpleNamespace(role='buyer', principal=binding['buyer']), merchant)
        view._order(db, order_id)
        if record['status'] in UNCONFIRMED and any(name in record for name in ('confirmation', 'entitlement_id', 'refund_ledger_sha256')):
            _bad('未确认订单不能带支付、权益或退款声明。')
        quote = db.execute('SELECT * FROM quotes WHERE id=?', (binding.get('quote_id'),)).fetchone()
        if not quote:
            _bad('订单缺少原报价。')
        item = db.execute('SELECT seller,content_hash FROM submissions WHERE resource_id=? AND version=?',
                          (quote['resource_id'], quote['version'])).fetchone()
        if not item or item['seller'] != merchant['market_seller'] or quote['content_hash'] != item['content_hash']:
            _bad('订单商户不是原商品卖方。')
        integer(quote['price_cents'], 1, MAX_PRICE_CENTS, 'price_cents')
        expected = {'format': 'machine-pay-order-preparation-v1', 'merchant': merchant,
            'buyer': quote['buyer'], 'quote_id': quote['id'], 'resource': quote['resource_id'],
            'version': quote['version'], 'purpose': quote['purpose'], 'content_hash': quote['content_hash'],
            'release_digest': Exchange._release_digest(db, quote['resource_id'], quote['version']),
            'offer_revision': quote['offer_revision'], 'price_cents': quote['price_cents'], 'currency': 'CNY',
            'expires_at': quote['expires_at'], 'fee_policy_version': quote['policy_version']}
        if quote['policy_version'] == AGREEMENT_POLICY:
            expected.update(format='machine-pay-order-preparation-v2', fee_policy=quote_fee(db, quote, item['seller']))
        if prepared != _prepared_record(expected):
            _bad('订单准备与原报价、发行或费用不一致。')
        intent = 'payment_intent:' + _hash([binding[name] for name in
            ('buyer', 'resource', 'version', 'purpose', 'content_hash', 'release_digest')])
        if record['intent_key'] != intent:
            _bad('订单防重复采购范围不符。')
        intent_orders.setdefault(intent, []).append(record)
        for name, short in prepared['channel_identifiers'].items():
            channel = 'payment_channel_id:' + name + ':' + short
            digest = prepared['binding_sha256']
            if channel in expected_channels and expected_channels[channel] != digest:
                _bad('渠道短编号冲突。')
            expected_channels[channel] = digest
        attempts = record['attempts']; seen = set()
        for i, attempt in enumerate(attempts):
            if attempt['id'] in seen:
                _bad('核验尝试编号重复。')
            seen.add(attempt['id'])
            if attempt['outcome'] == 'pending' and (i != len(attempts) - 1 or attempt['response_sha256'] is not None):
                _bad('待核验尝试的顺序或响应无效。')
            if attempt['outcome'] in ('invalid', 'unverified_response') and attempt['response_sha256'] is None:
                _bad('核验响应摘要缺失。')
        status = record['status']; handoff = record['handoff_sha256']
        if ((status in ('prepared_not_payable', 'cancelled_before_verification') and (attempts or handoff is not None))
                or (status == 'handoff_reserved' and (handoff is None or attempts))
                or (status == 'verification_pending' and (not attempts or attempts[-1]['outcome'] != 'pending'))
                or (status == 'reconciliation_required' and (not attempts or attempts[-1]['outcome'] == 'pending'))):
            _bad('订单状态与原交接/核验尝试不一致。')
        orders[order_id] = record
    if {k: v for k, v in found.items() if k.startswith('payment_channel_id:')} != expected_channels:
        _bad('渠道短编号登记缺失或存在孤立记录。')
    if {k for k in found if k.startswith('payment_intent:')} != set(intent_orders):
        _bad('采购意图登记缺失或存在孤立记录。')
    for key, records in intent_orders.items():
        selected = found[key]
        if not isinstance(selected, str) or selected not in {r['order_id'] for r in records}:
            _bad('原采购意图指向错误订单。')
        if any(r['order_id'] != selected and r['status'] != 'cancelled_before_verification' for r in records):
            _bad('同资源存在被替换的未决订单。')
    for key, value in found.items():
        if not key.startswith('payment_request:'):
            continue
        if not re.fullmatch('payment_request:[0-9a-f]{64}', key):
            _bad('准备请求摘要键无效。')
        fields(value, ('fingerprint', 'order_id'))
        if not isinstance(value['order_id'], str) or value['order_id'] not in orders:
            _bad('准备请求没有原订单。')
        binding = orders[value['order_id']]['prepared']['binding']
        if value['fingerprint'] != _hash([binding['quote_id'], binding['release_digest'], binding['merchant']]):
            _bad('准备请求与原订单范围不符。')
        referenced_orders.add(value['order_id'])
    if referenced_orders != set(orders):
        _bad('订单缺少原准备请求记录。')
    from .payment_recovery import validate_history
    from .purchase_recovery import validate_metadata as validate_purchase
    from .delivery_recovery import validate_metadata as validate_delivery
    return {**{key: meta[key] for key in found}, **validate_history(db, meta, orders),
            **validate_purchase(db, meta, orders), **validate_delivery(db, meta, orders)}
