"""本项目原创。支付历史关联校验；无签名密钥、网络或新资金授权。"""
from types import SimpleNamespace
from urllib.parse import urlsplit

from .alipay_proof import METHOD
from .alipay_transport import GATEWAYS
from .common import MarketError, choice, compact, fields, identifier, integer, parse_json, text
from .payment_orders import PaymentOrders, _digest, _get, _hash
from .payment_fulfillment import PAID_STATES, PaymentFulfillment, _confirmation_payload, validate_confirmation

PREFIXES = ('payment_response:', 'payment_transport:', 'payment_signed_trade:',
            'paid_entitlement:', 'payment_delivery:', 'paid_acquire_request:', 'refund_query:', 'payment_refunds:')


def bad():
    raise MarketError('payment_history_corrupt', '原支付、交付或退款历史关联不符，保留记录核查。')


def timestamp(value):
    if type(value) not in (int, float) or not 0 <= value < 10**12: bad()


def endpoint(value, merchant, test):
    if type(test) is not bool: bad()
    if not test:
        if value != GATEWAYS[merchant['environment']]: bad()
        return
    try:
        url = urlsplit(value)
        if (merchant['environment'] != 'sandbox' or url.scheme != 'http' or url.hostname != '127.0.0.1'
                or not url.port or url.path != '/gateway.do' or url.query or url.fragment
                or url.username is not None or url.password is not None): bad()
    except (TypeError, ValueError): bad()


def validate_history(db, meta, orders):
    records = {}
    for key, raw in meta.items():
        if key.startswith(PREFIXES):
            limit = 1048576 if key.startswith('payment_refunds:') else 65536
            if not isinstance(raw, str) or len(raw.encode('utf-8')) > limit: bad()
            records[key] = parse_json(raw)
    trades = {}
    for key, value in records.items():
        if not key.startswith(('payment_response:', 'payment_transport:')): continue
        parts = key.split(':', 2)
        if len(parts) != 3 or parts[1] not in orders: bad()
        _, order_id, attempt_id = parts
        order = orders[order_id]; binding = order['prepared']['binding']; merchant = binding['merchant']
        attempt = next((a for a in order['attempts'] if a['id'] == attempt_id), None)
        if attempt is None: bad()
        if key.startswith('payment_response:'):
            fields(value, ('status', 'order_id', 'trade_no', 'resource_id', 'amount_cents', 'currency', 'environment',
                'binding_sha256', 'request_sha256', 'response_sha256', 'public_key_sha256', 'amount_unit', 'policy_reference',
                'response_freshness_verified', 'merchant_relationship_verified', 'paid_verified', 'entitlement_created'))
            expected = {'status': 'signature_and_order_match', 'order_id': order_id,
                'resource_id': order['prepared']['channel_identifiers']['resource_id'], 'amount_cents': binding['price_cents'],
                'currency': 'CNY', 'environment': merchant['environment'], 'binding_sha256': order['prepared']['binding_sha256'],
                'request_sha256': attempt['request_sha256']}
            if any(value[k] != v for k, v in expected.items()): bad()
            integer(value['amount_cents'], 1, 100000000, 'amount_cents')
            for name in ('response_freshness_verified', 'merchant_relationship_verified', 'paid_verified', 'entitlement_created'):
                if value[name] is not False: bad()
            for name in ('response_sha256', 'public_key_sha256'): _digest(value[name], name)
            identifier(value['trade_no'], 'trade_no'); identifier(value['policy_reference'], 'policy_reference')
            choice(value['amount_unit'], ('CNY_major', 'CNY_minor'), 'amount_unit')
            if attempt['outcome'] == 'signed_response_match' and attempt['response_sha256'] != value['response_sha256']: bad()
            trade = 'payment_signed_trade:' + _hash([merchant['environment'], value['trade_no']])
            if trade in trades and trades[trade] != order_id: bad()
            trades[trade] = order_id
        else:
            fields(value, ('endpoint', 'method', 'request_sha256', 'wire_sha256', 'issued_at',
                'signing_public_key_sha256', 'verification_public_key_sha256', 'amount_unit', 'policy_reference',
                'loopback_test', 'outcome', 'https_response_received', 'response_sha256'),
                ('request_timestamp', 'received_at', 'elapsed_ms', 'error'))
            endpoint(value['endpoint'], merchant, value['loopback_test'])
            if value['method'] != METHOD or value['request_sha256'] != attempt['request_sha256']: bad()
            for name in ('wire_sha256', 'signing_public_key_sha256', 'verification_public_key_sha256'): _digest(value[name], name)
            identifier(value['policy_reference'], 'policy_reference'); timestamp(value['issued_at'])
            choice(value['amount_unit'], ('CNY_major', 'CNY_minor'), 'amount_unit')
            choice(value['outcome'], ('reserved', 'unknown', 'rejected', 'signature_and_order_match'), 'outcome')
            if type(value['https_response_received']) is not bool: bad()
            if 'elapsed_ms' in value: integer(value['elapsed_ms'], 0, 10**12, 'elapsed_ms')
            if 'request_timestamp' in value: text(value['request_timestamp'], 'request_timestamp', 40)
            expected_error = {'unknown': 'channel_result_unknown', 'rejected': 'response_not_verified'}.get(value['outcome'])
            if value.get('error') != expected_error: bad()
            if value['outcome'] in ('reserved', 'unknown'):
                if value['https_response_received'] or value['response_sha256'] is not None: bad()
            else:
                _digest(value['response_sha256'], 'response_sha256'); timestamp(value.get('received_at'))
                if value['received_at'] < value['issued_at'] or value['https_response_received'] != (not value['loopback_test']): bad()
            if value['outcome'] == 'signature_and_order_match':
                proof = records.get('payment_response:' + order_id + ':' + attempt_id)
                if not isinstance(proof, dict) or proof.get('response_sha256') != value['response_sha256']: bad()
    if {k: v for k, v in records.items() if k.startswith('payment_signed_trade:')} != trades: bad()

    for order_id, order in orders.items():
        for attempt in order['attempts']:
            if attempt['outcome'] == 'signed_response_match' and 'payment_response:' + order_id + ':' + attempt['id'] not in records: bad()
        if order['status'] == 'signature_checked_reconciliation_required' and not any(
                k.startswith('payment_response:' + order_id + ':') for k in records): bad()
        if order['status'] not in PAID_STATES: continue
        payload = validate_confirmation(order)
        fields(payload, ('format', 'order_id', 'attempt_id', 'prepared_sha256', 'binding_sha256', 'channel_policy',
                         'evidence', 'transport', 'fees', 'handoff_sha256', 'confirmed_at'))
        timestamp(payload['confirmed_at']); identifier(payload['attempt_id'], 'attempt_id')
        merchant = order['prepared']['binding']['merchant']
        context = PaymentOrders(SimpleNamespace(role='buyer', principal=order['prepared']['binding']['buyer']), merchant)
        PaymentFulfillment(context, payload['channel_policy'])  # Validate shape; stays disabled.
        expected = _confirmation_payload(db, order, payload['attempt_id'], payload['channel_policy'], payload['confirmed_at'])
        if payload != expected: bad()

    # Preserve paid lineage, without applying current access checks to refunded history.
    expected_rights = {}; expected_deliveries = {}
    for order_id, order in orders.items():
        if 'entitlement_id' not in order: continue
        binding = order['prepared']['binding']; acquisition_id = order['entitlement_id']
        if acquisition_id != 'ap_pay_' + _hash([binding['buyer'], order_id])[:32]: bad()
        acquired = db.execute('SELECT * FROM acquisitions WHERE id=?', (acquisition_id,)).fetchone()
        if not acquired or any(acquired[column] != binding[name] for column, name in
                (('principal', 'buyer'), ('resource_id', 'resource'), ('version', 'version'), ('purpose', 'purpose'), ('content_hash', 'content_hash'))): bad()
        if acquired['request_id'] != 'paid:' + order_id: bad()
        audit = db.execute('SELECT * FROM exchange_audit WHERE id=?', ('e_' + acquisition_id,)).fetchone()
        expected = {'acquisition_id': acquisition_id, 'purpose': binding['purpose'], 'reused': False,
            'release_digest': binding['release_digest'], 'payment_order_id': order_id,
            'payment_confirmation_sha256': order['confirmation']['sha256']}
        if (not audit or audit['actor'] != binding['buyer'] or audit['action'] != 'acquire'
                or audit['resource_id'] != binding['resource'] or audit['version'] != binding['version']
                or parse_json(audit['detail']) != expected): bad()
        expected_rights['paid_entitlement:' + acquisition_id] = {'order_id': order_id, 'confirmation_sha256': order['confirmation']['sha256']}
        if order['status'] == 'paid_delivered_to_host':
            value = records.get('payment_delivery:' + order_id)
            fields(value, ('order_id', 'acquisition_id', 'usage_id', 'task_id', 'request_id', 'delivered_at',
                           'channel_fulfillment_confirmed', 'task_completed'))
            usage = db.execute('SELECT * FROM usage_events WHERE id=?', (value['usage_id'],)).fetchone()
            if (not usage or usage['principal'] != binding['buyer'] or usage['acquisition_id'] != acquisition_id
                    or value != {'order_id': order_id, 'acquisition_id': acquisition_id, 'usage_id': usage['id'],
                        'task_id': usage['task_id'], 'request_id': usage['request_id'], 'delivered_at': usage['created_at'],
                        'channel_fulfillment_confirmed': False, 'task_completed': False}): bad()
            expected_deliveries['payment_delivery:' + order_id] = value
    if {k: v for k, v in records.items() if k.startswith('paid_entitlement:')} != expected_rights: bad()
    if {row[0] for row in db.execute("SELECT id FROM acquisitions WHERE id LIKE 'ap_pay_%'")} != {k[len('paid_entitlement:'):] for k in expected_rights}: bad()
    if {k: v for k, v in records.items() if k.startswith('payment_delivery:')} != expected_deliveries: bad()
    for key, value in records.items():
        if key.startswith('paid_acquire_request:'):
            _digest(key[len('paid_acquire_request:'):], 'request_key'); fields(value, ('fingerprint', 'acquisition_id'))
            _digest(value['fingerprint'], 'fingerprint'); identifier(value['acquisition_id'], 'acquisition_id')
            acquired = db.execute('SELECT * FROM acquisitions WHERE id=?', (value['acquisition_id'],)).fetchone()
            if not acquired: bad()
            # API reuse also uses this key, with or without an explicit release digest.
            from .exchange import Exchange
            from .api_quota import checksum
            release = Exchange._release_digest(db, acquired['resource_id'], acquired['version'])
            if value['fingerprint'] not in {checksum(compact([acquired['resource_id'], acquired['version'], acquired['purpose'], d])) for d in (None, release)}: bad()
    from .refund_recovery import validate_refunds
    validate_refunds(db, records, orders)
    return {key: meta[key] for key in records}
