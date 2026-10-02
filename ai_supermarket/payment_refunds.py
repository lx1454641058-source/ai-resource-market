"""本项目原创。已核验退款的宿主记账、访问处理及原费率核对，不执行资金操作。"""
from copy import deepcopy

from ._commission import _fee_amounts
from .alipay_transport import GATEWAYS
from .common import MarketError, choice, fields, identifier, integer, text
from .payment_orders import _digest, _get, _hash, _put
from .payment_fulfillment import _time, validate_confirmation


ACTIONS = ('preserve_access', 'block_future_access')
MAX_REFUNDS = 1000


def _source(db, order, entry):
    """Recheck the retained query observation; never accept a caller's paid flag.

    This verifies local evidence linkage, not a new channel signature or an
    independently trusted database. Runtime query validation happens on apply.
    """
    query = _get(db, 'refund_query:' + _hash([order['order_id'], entry['refund_id']]))
    fields(query, ('binding', 'binding_sha256', 'attempts'))
    binding = query['binding']
    fields(binding, ('order_id', 'confirmation_sha256', 'merchant', 'policy',
                     'capability_reference', 'total_cents', 'refund_cents',
                     'endpoint', 'loopback_test', 'request'))
    paid = validate_confirmation(order)
    original = order['prepared']['binding']
    merchant = original['merchant']
    if (_get(db, 'payment_environment') != merchant['environment']
            or _get(db, 'payment_signed_trade:' + _hash([merchant['environment'], paid['evidence']['trade_no']]))
            != order['order_id']):
        raise MarketError('refund_record_corrupt', '原渠道交易和账本环境绑定不符。')
    expected = {'order_id': order['order_id'], 'confirmation_sha256': order['confirmation']['sha256'],
                'merchant': merchant, 'policy': paid['channel_policy'],
                'total_cents': original['price_cents'], 'refund_cents': entry['refund_cents'],
                'endpoint': GATEWAYS[merchant['environment']], 'loopback_test': False,
                'request': {'trade_no': paid['evidence']['trade_no'], 'out_trade_no': order['order_id'],
                            'out_request_no': entry['refund_id']}}
    if (any(binding[k] != value for k, value in expected.items())
            or binding['loopback_test'] is not False
            or query['binding_sha256'] != _hash(binding)
            or query['binding_sha256'] != entry['query_binding_sha256']):
        raise MarketError('refund_record_corrupt', '退款来源与原支付订单不符。')
    identifier(binding['capability_reference'], 'capability_reference')
    if not isinstance(query['attempts'], list) or not 1 <= len(query['attempts']) <= 3:
        raise MarketError('refund_record_corrupt', '退款查询来源缺失。')
    matches = [a for a in query['attempts'] if _hash(a) == entry['query_attempt_sha256']]
    if len(matches) != 1:
        raise MarketError('refund_record_corrupt', '原退款确认观察已缺失或被替换。')
    attempt = matches[0]
    fields(attempt, ('issued_at', 'wire_sha256', 'state', 'response'))
    _time(attempt['issued_at']); _digest(attempt['wire_sha256'], 'wire_sha256')
    fields(attempt['response'], ('response_sha256', 'signed_match', 'refund_success'))
    _digest(attempt['response']['response_sha256'], 'response_sha256')
    if (attempt['state'] != 'confirmed' or attempt['response']['signed_match'] is not True
            or attempt['response']['refund_success'] is not True
            or attempt['issued_at'] > entry['reconciled_at']):
        raise MarketError('refund_record_corrupt', '退款来源不是已核验的渠道成功观察。')


def _load(db, order):
    """Order anchor prevents a missing ledger from silently restoring access."""
    stored = _get(db, 'payment_refunds:' + order['order_id'])
    anchor = order.get('refund_ledger_sha256')
    if stored is None and anchor is None:
        return []
    try:
        fields(stored, ('payload', 'sha256'))
        payload = stored['payload']
        fields(payload, ('order_id', 'confirmation_sha256', 'entries'))
        if (anchor != stored['sha256'] or _hash(payload) != anchor
                or payload['order_id'] != order['order_id']
                or payload['confirmation_sha256'] != order['confirmation']['sha256']
                or validate_confirmation(order) is None):
            raise ValueError('binding')
        entries = payload['entries']
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_REFUNDS:
            raise ValueError('entries')
        seen, total, latest = set(), 0, 0
        for entry in entries:
            fields(entry, ('refund_id', 'refund_cents', 'rights_action', 'decision_reference',
                           'query_binding_sha256', 'query_attempt_sha256', 'reconciled_at'))
            refund = text(entry['refund_id'], 'refund_id', 64)
            integer(entry['refund_cents'], 1, order['prepared']['binding']['price_cents'], 'refund_cents')
            choice(entry['rights_action'], ACTIONS, 'rights_action')
            identifier(entry['decision_reference'], 'decision_reference')
            _digest(entry['query_binding_sha256'], 'query_binding_sha256')
            _digest(entry['query_attempt_sha256'], 'query_attempt_sha256')
            when = _time(entry['reconciled_at'])
            total += entry['refund_cents']
            if refund in seen or total > order['prepared']['binding']['price_cents'] or when < latest:
                raise ValueError('duplicate_or_excess')
            seen.add(refund); latest = when
            _source(db, order, entry)
        return entries
    except (MarketError, KeyError, TypeError, ValueError):
        raise MarketError('refund_record_corrupt', '退款账本或原核验来源损坏，保留记录并停止取用。') from None


def check_refund_access(db, order):
    if any(entry['rights_action'] == 'block_future_access' for entry in _load(db, order)):
        raise MarketError('refunded_access_blocked', '原订单退款处理已停止后续资源访问。')


def _view(order, entries):
    paid = validate_confirmation(order)
    total = order['prepared']['binding']['price_cents']
    fees = paid['fees']
    rate = fees['commission_bps']
    _, original_platform, original_supplier = _fee_amounts(total, 0, rate)
    if (fees['currency'] != 'CNY' or fees['net_cents'] != total
            or fees['platform_cents'] != original_platform
            or fees['seller_before_other_fees_cents'] != original_supplier):
        raise MarketError('payment_record_corrupt', '原成交费率与佣金金额不符。')
    refunded = sum(entry['refund_cents'] for entry in entries)
    net, platform, supplier = _fee_amounts(total, refunded, rate)
    return {'order_id': order['order_id'], 'currency': 'CNY', 'original_paid_cents': total,
            'recorded_refund_count': len(entries), 'recorded_refund_cents': refunded,
            'channel_refund_total_known': False,
            'future_access_blocked': any(e['rights_action'] == 'block_future_access' for e in entries),
            'accounting_proposal': {'commission_bps': rate, 'remaining_net_cents': net,
                'remaining_platform_cents': platform, 'remaining_seller_before_other_fees_cents': supplier,
                'cumulative_platform_reversal_cents': original_platform - platform,
                'cumulative_seller_reversal_before_other_fees_cents': original_supplier - supplier,
                'channel_fee_cents': None},
            'funds_moved': False, 'commission_settled': False, 'budget_restored': False}


class PaymentRefunds:
    """Host-only consumer of a fixed AlipayRefundQueryClient, disabled by default.

    rights_action and decision_reference must come from the operator's actual
    refund terms/decision. This class neither makes that decision nor refunds.
    """
    def __init__(self, query_client, *, enabled=False):
        from .alipay_refund_query import AlipayRefundQueryClient
        if type(query_client) is not AlipayRefundQueryClient or type(enabled) is not bool:
            raise MarketError('invalid_configuration', '需要固定退款查询器和布尔启用配置。')
        self.query, self.orders, self._enabled = query_client, query_client.orders, enabled

    def _order(self, db, order_id):
        self.query.fulfillment._require(); self.query.fulfillment._environment(db)
        order = self.orders._order(db, order_id)
        paid = validate_confirmation(order)
        if paid is None or paid['channel_policy'] != self.query.fulfillment._policy:
            raise MarketError('payment_not_confirmed', '需要原订单的匹配支付确认。')
        return order

    def status(self, order_id):
        with self.orders.market.store.connect() as db:
            db.execute('BEGIN')
            order = self._order(db, order_id)
            return _view(order, _load(db, order))

    def reconcile(self, order_id, refund_id, refund_cents, *, rights_action, decision_reference):
        if not self._enabled:
            raise MarketError('refund_reconciliation_disabled', '宿主尚未启用退款处理。')
        choice(rights_action, ACTIONS, 'rights_action')
        identifier(decision_reference, 'decision_reference')
        with self.orders.market.store.connect(write=True) as db:
            order = self._order(db, order_id)
            entries = _load(db, order)
            # Pure local load: no status query, signer or network call is made.
            _, query = self.query._load(db, order_id, refund_id, refund_cents)
            old = next((e for e in entries if e['refund_id'] == refund_id), None)
            # An earlier in-flight query can reply after reconciliation. Replays
            # must retain the exact observation already used, not pick a new one.
            confirmed = next((a for a in query['attempts'] if a['state'] == 'confirmed'
                              and (old is None or _hash(a) == old['query_attempt_sha256'])), None)
            if confirmed is None:
                raise MarketError('refund_not_confirmed', '只有已验签匹配的渠道退款成功观察才能记账。')
            new = {'refund_id': refund_id, 'refund_cents': refund_cents, 'rights_action': rights_action,
                   'decision_reference': decision_reference, 'query_binding_sha256': query['binding_sha256'],
                   'query_attempt_sha256': _hash(confirmed)}
            if old is not None:
                if any(old[k] != value for k, value in new.items()):
                    raise MarketError('idempotency_conflict', '原退款金额、来源和处理决定不可替换。')
                return {**_view(order, entries), 'replayed': True}
            if len(entries) >= MAX_REFUNDS:
                raise MarketError('refund_limit', '原订单退款记录达到上限，请保留账本核账。')
            if sum(e['refund_cents'] for e in entries) + refund_cents > order['prepared']['binding']['price_cents']:
                raise MarketError('refund_exceeds_payment', '累计已记退款不能超过原付款金额。')
            new['reconciled_at'] = _time(self.orders.market.clock())
            payload = {'order_id': order_id, 'confirmation_sha256': order['confirmation']['sha256'],
                       'entries': [*deepcopy(entries), new]}
            anchor = _hash(payload)
            _put(db, 'payment_refunds:' + order_id, {'payload': payload, 'sha256': anchor})
            order['refund_ledger_sha256'] = anchor
            _put(db, 'payment_order:' + order_id, order)
            # Validate the final state within the same transaction; failure rolls back both writes.
            return {**_view(order, _load(db, order)), 'replayed': False}
