"""本项目原创。退款查询及记账的历史恢复校验，不退款或恢复消费预算。"""
from .common import fields, identifier, integer, text
from .payment_fulfillment import validate_confirmation
from .payment_orders import _digest, _hash
from .payment_refunds import _load


def validate_refunds(db, records, orders):
    from .payment_recovery import bad, endpoint, timestamp
    for key, value in records.items():
        if key.startswith('payment_refunds:'):
            if key[len('payment_refunds:'):] not in orders: bad()
        if not key.startswith('refund_query:'): continue
        fields(value, ('binding', 'binding_sha256', 'attempts')); binding = value['binding']
        fields(binding, ('order_id', 'confirmation_sha256', 'merchant', 'policy', 'capability_reference',
                        'total_cents', 'refund_cents', 'endpoint', 'loopback_test', 'request'))
        if not isinstance(binding['order_id'], str) or binding['order_id'] not in orders: bad()
        order = orders[binding['order_id']]; paid = validate_confirmation(order)
        if paid is None: bad()
        original = order['prepared']['binding']; request = binding['request']
        fields(request, ('trade_no', 'out_trade_no', 'out_request_no'))
        refund_id = text(request['out_request_no'], 'refund_id', 64)
        identifier(binding['capability_reference'], 'capability_reference')
        integer(binding['refund_cents'], 1, original['price_cents'], 'refund_cents')
        integer(binding['total_cents'], 1, 100000000, 'total_cents')
        if (key != 'refund_query:' + _hash([order['order_id'], refund_id]) or value['binding_sha256'] != _hash(binding)
                or binding['confirmation_sha256'] != order['confirmation']['sha256']
                or binding['merchant'] != original['merchant'] or binding['policy'] != paid['channel_policy']
                or binding['total_cents'] != original['price_cents']
                or request['trade_no'] != paid['evidence']['trade_no'] or request['out_trade_no'] != order['order_id']): bad()
        endpoint(binding['endpoint'], binding['merchant'], binding['loopback_test'])
        attempts = value['attempts']
        if not isinstance(attempts, list) or not 1 <= len(attempts) <= 3: bad()
        for attempt in attempts:
            fields(attempt, ('issued_at', 'wire_sha256', 'state', 'response'))
            timestamp(attempt['issued_at']); _digest(attempt['wire_sha256'], 'wire_sha256')
            if attempt['state'] not in ('reserved', 'unknown', 'unconfirmed', 'invalid_response', 'confirmed', 'test_response'): bad()
            response = attempt['response']
            if attempt['state'] in ('reserved', 'unknown'):
                if response is not None: bad()
                continue
            fields(response, ('response_sha256', 'signed_match', 'refund_success'))
            _digest(response['response_sha256'], 'response_sha256')
            if type(response['signed_match']) is not bool or type(response['refund_success']) is not bool: bad()
            if (attempt['state'] in ('confirmed', 'test_response') and not (response['signed_match'] and response['refund_success'])
                    or attempt['state'] in ('unconfirmed', 'invalid_response') and response['refund_success']
                    or attempt['state'] == 'confirmed' and binding['loopback_test']
                    or attempt['state'] == 'test_response' and not binding['loopback_test']): bad()
    # Missing anchors/ledgers, excess totals and missing original query evidence
    # use the exact same validation as the runtime access and accounting path.
    for order in orders.values():
        _load(db, order)
