"""本项目原创。原客户端收货与渠道履约历史校验，不取用资源或发送回执。"""
import hashlib
import re

from .alipay_fulfillment import METHOD
from .common import MarketError, choice, compact, fields, identifier, parse_json
from .exchange import Exchange
from .payment_orders import _digest, _hash
from .payment_recovery import endpoint, timestamp
from .resource_bundle import SUFFIXES

PREFIXES = ('client_delivery:', 'remote_file_delivery:', 'order_client_receipt:', 'channel_fulfillment:')


def bad():
    raise MarketError('delivery_history_corrupt', '原客户端收货、渠道交付或账户关联不符，保留记录核查。')


def validate_metadata(db, meta, orders):
    records = {}
    for key, raw in meta.items():
        if key.startswith(PREFIXES):
            if not isinstance(raw, str) or len(raw.encode('utf-8')) > 65536: bad()
            records[key] = parse_json(raw)
    offers = {}
    for key, record in records.items():
        if not key.startswith('client_delivery:'): continue
        fields(record, ('scope', 'request_id', 'offer', 'offer_sha256', 'acknowledgement'))
        scope, offer = record['scope'], record['offer']
        fields(scope, ('principal', 'root_id', 'task_id', 'key_id'))
        for name in ('principal', 'root_id', 'task_id'): identifier(scope[name], name)
        if not isinstance(scope['key_id'], str) or not re.fullmatch('ak_[0-9a-f]{32}', scope['key_id']): bad()
        identifier(record['request_id'], 'request_id')
        if key != 'client_delivery:' + _hash([scope, record['request_id']]): bad()
        fields(offer, ('id', 'version', 'acquisition_id', 'usage_id', 'delivery_request_id', 'release_digest',
                       'bundle_sha256', 'manifest_sha256', 'resource_sha256', 'license_sha256', 'order_id'))
        for name in ('id', 'version', 'acquisition_id', 'usage_id', 'delivery_request_id'): identifier(offer[name], name)
        for name in ('release_digest', 'bundle_sha256', 'manifest_sha256', 'resource_sha256', 'license_sha256'): _digest(offer[name], name)
        if record['offer_sha256'] != _hash(offer): bad()
        acquired = db.execute('SELECT * FROM acquisitions WHERE id=?', (offer['acquisition_id'],)).fetchone()
        usage = db.execute('SELECT * FROM usage_events WHERE id=?', (offer['usage_id'],)).fetchone()
        if (not acquired or not usage or acquired['principal'] != scope['principal']
                or [acquired['resource_id'], acquired['version']] != [offer['id'], offer['version']]
                or usage['acquisition_id'] != acquired['id'] or usage['principal'] != scope['principal']
                or usage['task_id'] != scope['task_id'] or usage['request_id'] != offer['delivery_request_id']): bad()
        release = Exchange._release_digest(db, offer['id'], offer['version'])
        audit = db.execute('SELECT * FROM exchange_audit WHERE id=?', ('e_' + acquired['id'],)).fetchone()
        if (not acquired['id'].startswith('ap_') or not audit or audit['actor'] != scope['principal']
                or audit['action'] != 'acquire' or audit['resource_id'] != offer['id'] or audit['version'] != offer['version']): bad()
        detail = parse_json(audit['detail'])
        if (not isinstance(detail, dict) or detail.get('acquisition_id') != acquired['id']
                or detail.get('purpose') != acquired['purpose'] or detail.get('release_digest') != release
                or offer['release_digest'] != release): bad()
        if acquired['id'].startswith('ap_pay_'):
            order_id = offer['order_id']
            if not isinstance(order_id, str) or order_id not in orders or orders[order_id].get('entitlement_id') != acquired['id']: bad()
        elif offer['order_id'] is not None: bad()
        row = db.execute('SELECT metadata,content_hash FROM submissions WHERE resource_id=? AND version=?',
                         (offer['id'], offer['version'])).fetchone()
        if not row or row['content_hash'] != acquired['content_hash']: bad()
        item = parse_json(row['metadata'])
        kind = 'origin' if 'origin' in item else 'asset' if 'asset' in item else None
        if kind is None: bad()
        descriptor = item[kind]; license_raw = item['license_text'].encode('utf-8')
        if (offer['resource_sha256'] != descriptor['sha256']
                or offer['license_sha256'] != hashlib.sha256(license_raw).hexdigest()): bad()
        manifest = {'format': 'market-resource-zip-v1', 'id': offer['id'], 'version': offer['version'],
            'acquisition_id': acquired['id'], 'task_id': scope['task_id'], 'request_id': record['request_id'],
            'usage_id': usage['id'], 'purpose': acquired['purpose'], 'release_digest': release,
            'resource': {'path': 'resource' + SUFFIXES[descriptor['media_type']], 'media_type': descriptor['media_type'],
                         'sha256': descriptor['sha256'], 'size': descriptor['bytes'] if kind == 'asset' else descriptor['size']},
            'license': {'path': 'LICENSE.txt', 'name': item['license_name'], 'sha256': offer['license_sha256'], 'size': len(license_raw)},
            'source_statement': item['source_statement'], 'task_completed': False,
            'execution_authorized': False, 'client_receipt_confirmed': False}
        if 'rights' in item: manifest['rights'] = item['rights']
        if hashlib.sha256(compact(manifest).encode('utf-8')).hexdigest() != offer['manifest_sha256']: bad()
        ack = record['acknowledgement']
        if ack is not None:
            fields(ack, ('receipt_id', 'offer_sha256', 'bundle_sha256', 'acknowledged_at', 'evidence'))
            timestamp(ack['acknowledged_at'])
            if (ack['receipt_id'] != 'cr_' + _hash([key, record['offer_sha256']])
                    or ack['offer_sha256'] != record['offer_sha256'] or ack['bundle_sha256'] != offer['bundle_sha256']
                    or ack['evidence'] != 'authenticated_client_statement'): bad()
        offers[key] = record

    for key, marker in records.items():
        if not key.startswith('remote_file_delivery:'): continue
        order_id = key[len('remote_file_delivery:'):]
        delivery = parse_json(meta['payment_delivery:' + order_id]) if 'payment_delivery:' + order_id in meta else None
        if not delivery or marker != {'acquisition_id': delivery['acquisition_id'], 'usage_id': delivery['usage_id'],
                                     'delivery_sha256': _hash(delivery)}: bad()
        matches = {k: v for k, v in offers.items() if v['offer']['order_id'] == order_id and v['offer']['usage_id'] == delivery['usage_id']}
        if not matches: bad()
        pointer = records.get('order_client_receipt:' + order_id)
        if pointer is not None:
            if not isinstance(pointer, str) or pointer not in matches or matches[pointer]['acknowledgement'] is None: bad()
        elif any(v['acknowledgement'] is not None for v in matches.values()): bad()
    for key in records:
        if key.startswith('order_client_receipt:') and 'remote_file_delivery:' + key[len('order_client_receipt:'):] not in records: bad()

    for record in offers.values():
        offer = record['offer']; order_id = offer['order_id']
        if order_id is None or 'payment_delivery:' + order_id not in meta: continue
        delivery = parse_json(meta['payment_delivery:' + order_id])
        channel = records.get('channel_fulfillment:' + order_id)
        if channel is not None and not isinstance(channel, dict): bad()
        # A previously started local-channel action is not retroactively gated
        # by a later HTTP download; preserve the runtime's original distinction.
        if (offer['usage_id'] == delivery['usage_id'] and 'remote_file_delivery:' + order_id not in records
                and (channel is None or channel.get('state') == 'pending' and not channel.get('attempts'))): bad()

    for key, value in records.items():
        if not key.startswith('channel_fulfillment:'): continue
        order_id = key[len('channel_fulfillment:'):]
        if order_id not in orders or orders[order_id]['status'] != 'paid_delivered_to_host': bad()
        order = orders[order_id]; confirmation = order['confirmation']['payload']
        delivery = parse_json(meta['payment_delivery:' + order_id])
        fields(value, ('binding', 'binding_sha256', 'state', 'transport', 'response', 'attempts'))
        expected = {'order_id': order_id, 'confirmation_sha256': order['confirmation']['sha256'],
            'delivery_sha256': _hash(delivery), 'merchant': order['prepared']['binding']['merchant'],
            'channel_policy': confirmation['channel_policy'], 'request': {'trade_no': confirmation['evidence']['trade_no']}}
        if value['binding'] != expected or value['binding_sha256'] != _hash(expected): bad()
        if 'remote_file_delivery:' + order_id in records and 'order_client_receipt:' + order_id not in records: bad()
        attempts = value['attempts']
        if not isinstance(attempts, list) or len(attempts) > 3: bad()
        previous = None; previous_endpoint = None
        for attempt in attempts:
            fields(attempt, ('state', 'transport', 'response'))
            if previous is not None and previous != 'retryable': bad()
            _attempt(attempt, expected)
            address = (attempt['transport']['endpoint'], attempt['transport']['loopback_test'])
            if previous_endpoint is not None and address != previous_endpoint: bad()
            previous, previous_endpoint = attempt['state'], address
        if value['state'] == 'pending':
            if value['transport'] is not None or value['response'] is not None or attempts and previous != 'retryable': bad()
        elif not attempts or attempts[-1] != {k: value[k] for k in ('state', 'transport', 'response')}: bad()
    return {key: meta[key] for key in records}


def _attempt(value, binding):
    state, transport, response = value['state'], value['transport'], value['response']
    choice(state, ('reserved', 'unknown', 'rejected', 'confirmed', 'test_response', 'retryable'), 'channel_state')
    fields(transport, ('endpoint', 'loopback_test', 'issued_at', 'method', 'wire_sha256'))
    endpoint(transport['endpoint'], binding['merchant'], transport['loopback_test'])
    timestamp(transport['issued_at']); _digest(transport['wire_sha256'], 'wire_sha256')
    if transport['method'] != METHOD: bad()
    if state in ('reserved', 'unknown'):
        if response is not None: bad()
        return
    if state == 'rejected':
        fields(response, ('response_sha256',)); _digest(response['response_sha256'], 'response_sha256'); return
    required = ('response_sha256', 'public_key_sha256', 'https_response_received', 'received_at')
    fields(response, required + (('result', 'sub_code') if state == 'retryable' else ('trade_no',)))
    _digest(response['response_sha256'], 'response_sha256'); timestamp(response['received_at'])
    if (response['public_key_sha256'] != binding['channel_policy']['verification_public_key_sha256']
            or response['https_response_received'] is not (not transport['loopback_test'])
            or response['received_at'] < transport['issued_at']): bad()
    if state == 'retryable':
        if response['result'] != 'retryable' or response['sub_code'] != 'SYSTEM_ERROR': bad()
    elif (response['trade_no'] != binding['request']['trade_no'] or (state == 'test_response') != transport['loopback_test']): bad()


def validate_account_links(market, identities, budget):
    """Historical identity/task existence, never reactivates expired or revoked keys."""
    for key, raw in market.execute("SELECT key,value FROM meta WHERE key LIKE 'client_delivery:%'"):
        record = parse_json(raw); scope = record['scope']
        if identities is None or budget is None: bad()
        account = identities.execute('SELECT principal FROM api_keys WHERE id=?', (scope['key_id'],)).fetchone()
        task = budget.execute('SELECT principal FROM budget_tasks WHERE root_id=? AND id=?',
                              (scope['root_id'], scope['task_id'])).fetchone()
        if not account or not task or account['principal'] != scope['principal'] or task['principal'] != scope['principal']: bad()
