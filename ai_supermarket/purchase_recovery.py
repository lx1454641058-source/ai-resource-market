"""本项目原创。采购委托、账单与宿主历史校验；不恢复购买授权或发出账单。"""
import base64
from datetime import datetime
from types import SimpleNamespace

from .alipay_transport import SHANGHAI
from .budget import MAX_CENTS, reservation_amounts
from .common import MarketError, choice, fields, identifier, integer, parse_json, text
from .exchange import Exchange
from .payment_bills import PurchaseBilling, _mandate_key
from .payment_fulfillment import PaymentFulfillment
from .payment_orders import PaymentOrders, _digest, _hash
from .payment_recovery import timestamp

PREFIXES = ('purchase_mandate:', 'payment_bill:', 'payment_bill_budget:',
            'purchase_host:', 'purchase_host_order:')


def bad():
    raise MarketError('purchase_history_corrupt', '原采购、账单或预算关联不符，保留账本核查。')


def scope(value, environment):
    fields(value, ('buyer', 'root_id', 'task_id', 'budget_path_sha256', 'market_path_sha256', 'market_id', 'environment'))
    for name in ('buyer', 'root_id', 'task_id', 'market_id'): identifier(value[name], name)
    for name in ('budget_path_sha256', 'market_path_sha256'): _digest(value[name], name)
    choice(value['environment'], ('sandbox', 'production'), 'environment')
    if value['environment'] != environment: bad()


def policy(value, buyer):
    if not isinstance(value, dict) or not isinstance(value.get('merchant'), dict): bad()
    orders = PaymentOrders(SimpleNamespace(role='buyer', principal=buyer), value['merchant'])
    PaymentFulfillment(orders, value)  # Shape only; disabled and never signs or queries.


def validate_metadata(db, meta, orders):
    records = {}
    for key, raw in meta.items():
        if key.startswith(PREFIXES):
            if not isinstance(raw, str) or len(raw.encode('utf-8')) > 65536: bad()
            records[key] = parse_json(raw)
    environment = parse_json(meta['payment_environment']) if 'payment_environment' in meta else None
    for key, value in records.items():
        if not key.startswith('purchase_mandate:'): continue
        fields(value, ('mandate_id', 'terms', 'terms_sha256', 'state'))
        terms = value['terms']
        fields(terms, ('format', 'scope', 'consent_reference', 'allowed_sellers', 'allowed_purposes', 'max_order_cents', 'expires_at'))
        scope(terms['scope'], environment); identifier(value['mandate_id'], 'mandate_id')
        identifier(terms['consent_reference'], 'consent_reference')
        choice(value['state'], ('active', 'revoked'), 'mandate_state')
        integer(terms['max_order_cents'], 1, MAX_CENTS, 'max_order_cents'); timestamp(terms['expires_at'])
        if (terms['format'] != 'task-purchase-mandate-v1' or _hash(terms) != value['terms_sha256']
                or key != _mandate_key(terms['scope']['buyer'], value['mandate_id'])): bad()
        sellers, purposes = terms['allowed_sellers'], terms['allowed_purposes']
        if not isinstance(sellers, list) or not 1 <= len(sellers) <= 100: bad()
        if not isinstance(purposes, list) or not 1 <= len(purposes) <= 2: bad()
        for seller in sellers: identifier(seller, 'seller')
        for purpose in purposes: choice(purpose, ('personal_use', 'commercial_use'), 'purpose')
        if sellers != sorted(set(sellers)) or purposes != sorted(set(purposes)): bad()

    links = {}; bills = {}
    for key, binding in records.items():
        if not key.startswith('payment_bill_budget:'): continue
        order_id = key[len('payment_bill_budget:'):]
        if order_id not in orders: bad()
        fields(binding, ('scope', 'order_id', 'mandate_id', 'mandate_sha256', 'channel_policy', 'seller_name'))
        scope(binding['scope'], environment); text(binding['seller_name'], 'seller_name', 100)
        policy(binding['channel_policy'], binding['scope']['buyer'])
        order = orders[order_id]; original = order['prepared']['binding']
        mandate = records.get(_mandate_key(binding['scope']['buyer'], binding['mandate_id']))
        if mandate is None: bad()
        terms = mandate['terms']; merchant = original['merchant']
        if (binding['order_id'] != order_id or binding['scope'] != terms['scope']
                or binding['mandate_sha256'] != mandate['terms_sha256']
                or binding['channel_policy']['merchant'] != merchant
                or binding['scope']['buyer'] != original['buyer'] or binding['scope']['market_id'] != merchant['market_id']
                or merchant['market_seller'] not in terms['allowed_sellers']
                or original['purpose'] not in terms['allowed_purposes'] or original['price_cents'] > terms['max_order_cents']): bad()
        links[order_id] = binding
        bill = records.get('payment_bill:' + order_id)
        # A budget binding can survive before a bill is staged; it is not a handoff.
        if bill is None:
            if order['handoff_sha256'] is not None or 'confirmation' in order: bad()
            continue
        PurchaseBilling._read_bill(None, db, order_id)
        if bill['binding'] != binding or len(bill['header']) > 8192: bad()
        identifier(bill['reservation_id'], 'reservation_id')
        payload = bill['payload']; fields(payload, ('protocol', 'method'))
        fields(payload['protocol'], ('out_trade_no', 'amount', 'currency', 'resource_id', 'pay_before',
                                    'seller_signature', 'seller_sign_type', 'seller_unique_id'))
        signature = text(payload['protocol']['seller_signature'], 'seller_signature', 8192)
        try:
            decoded = base64.b64decode(signature, validate=True)
            if not decoded or base64.b64encode(decoded).decode('ascii') != signature: bad()
        except (ValueError, TypeError): bad()
        row = db.execute('SELECT metadata FROM submissions WHERE resource_id=? AND version=?',
                         (original['resource'], original['version'])).fetchone()
        if row is None: bad()
        goods_name = parse_json(row[0])['title']; text(goods_name, 'goods_name', 64)
        cents = original['price_cents']
        amount = f'{cents // 100}.{cents % 100:02d}' if binding['channel_policy']['amount_unit'] == 'CNY_major' else str(cents)
        deadline = int(min(original['expires_at'], terms['expires_at']))
        expected = {'protocol': {'out_trade_no': order_id, 'amount': amount, 'currency': 'CNY',
            'resource_id': order['prepared']['channel_identifiers']['resource_id'],
            'pay_before': datetime.fromtimestamp(deadline, SHANGHAI).isoformat(timespec='seconds'),
            'seller_signature': signature, 'seller_sign_type': 'RSA2', 'seller_unique_id': merchant['seller_id']},
            'method': {'seller_name': binding['seller_name'], 'seller_id': merchant['seller_id'],
                'seller_app_id': merchant['app_id'], 'goods_name': goods_name,
                'seller_unique_id_key': 'seller_id', 'service_id': merchant['service_id']}}
        if payload != expected: bad()
        if bill['state'] == 'staged':
            if order['handoff_sha256'] is not None or 'confirmation' in order: bad()
        elif bill['payload_sha256'] != order['handoff_sha256']: bad()
        if 'confirmation' in order and binding['channel_policy'] != order['confirmation']['payload']['channel_policy']: bad()
        if bill['state'] == 'budget_settled' and 'confirmation' not in order: bad()
        bills[order_id] = bill
    if {key[len('payment_bill:'):] for key in records if key.startswith('payment_bill:')} != set(bills): bad()

    expected_pointers = {}
    for key, flow in records.items():
        if not key.startswith('purchase_host:'): continue
        fields(flow, ('terms', 'terms_sha256', 'order_id')); terms = flow['terms']
        fields(terms, ('host', 'resource', 'version', 'release_digest', 'request_id'))
        host = terms['host']; fields(host, ('scope', 'mandate_id', 'purpose', 'channel_policy'))
        scope(host['scope'], environment); policy(host['channel_policy'], host['scope']['buyer'])
        for name in ('mandate_id',): identifier(host[name], name)
        choice(host['purpose'], ('personal_use', 'commercial_use'), 'purpose')
        for name in ('resource', 'version', 'request_id'): identifier(terms[name], name)
        _digest(terms['release_digest'], 'release_digest')
        if (key != 'purchase_host:' + _hash([host['scope'], terms['request_id']])
                or flow['terms_sha256'] != _hash(terms)
                or host['scope']['market_id'] != host['channel_policy']['merchant']['market_id']
                or host['scope']['environment'] != host['channel_policy']['merchant']['environment']
                or Exchange._release_digest(db, terms['resource'], terms['version']) != terms['release_digest']): bad()
        # Failed requests can precede a valid mandate; no bill then exists.
        mandate = records.get(_mandate_key(host['scope']['buyer'], host['mandate_id']))
        if mandate is not None and mandate['terms']['scope'] != host['scope']: bad()
        order_id = flow['order_id']
        if order_id is None: continue
        if not isinstance(order_id, str) or order_id not in orders or order_id in expected_pointers: bad()
        original = orders[order_id]['prepared']['binding']
        if (any(original[k] != v for k, v in {'buyer': host['scope']['buyer'], 'resource': terms['resource'],
                'version': terms['version'], 'purpose': host['purpose'], 'release_digest': terms['release_digest'],
                'merchant': host['channel_policy']['merchant']}.items())): bad()
        if orders[order_id]['handoff_sha256'] is not None and order_id not in bills: bad()
        if order_id in links and any(links[order_id][k] != host[k] for k in ('scope', 'mandate_id', 'channel_policy')): bad()
        expected_pointers[order_id] = key
    if {k[len('purchase_host_order:'):]: v for k, v in records.items() if k.startswith('purchase_host_order:')} != expected_pointers: bad()
    return {key: meta[key] for key in records}


def validate_budget_links(market, budget):
    """Cross-check retained task and expense records, ignoring current activation.

    Scope path hashes stay historical in copies. They bind the original request,
    never authorize a host on the new path. Orphan pre-stage holds remain held.
    """
    rows = market.execute('SELECT key,value FROM meta').fetchall()
    records = {k: parse_json(v) for k, v in rows if k.startswith(PREFIXES)}
    if not records: return
    if budget is None: bad()
    scopes = [v['terms']['scope'] for k, v in records.items() if k.startswith('purchase_mandate:')]
    scopes += [v['terms']['host']['scope'] for k, v in records.items() if k.startswith('purchase_host:')]
    for binding in scopes:
        task = budget.execute('SELECT principal FROM budget_tasks WHERE root_id=? AND id=?',
                              (binding['root_id'], binding['task_id'])).fetchone()
        if not task or task['principal'] != binding['buyer']: bad()
    for key, binding in records.items():
        if not key.startswith('payment_bill_budget:'): continue
        order_id = binding['order_id']; bound = binding['scope']
        request = 'purchase:' + _hash([bound['market_path_sha256'], order_id])
        row = budget.execute('SELECT * FROM reservations WHERE root_id=? AND task_id=? AND request_id=?',
                             (bound['root_id'], bound['task_id'], request)).fetchone()
        bill = records.get('payment_bill:' + order_id)
        if row is None:
            if bill is not None: bad()
            continue  # No bill exists; no financial success is claimed.
        order = parse_json(market.execute('SELECT value FROM meta WHERE key=?', ('payment_order:' + order_id,)).fetchone()[0])
        original = order['prepared']['binding']
        if (row['principal'] != bound['buyer'] or parse_json(row['spec']) !=
                ['resource', 'order:' + order['prepared']['binding_sha256'], original['price_cents']]): bad()
        reservation_amounts(budget, row)
        if bill is not None and row['id'] != bill['reservation_id']: bad()
        if row['state'] == 'settled':
            if ('confirmation' not in order or row['actual_cents'] != original['price_cents']
                    or row['evidence_ref'] != 'payment-confirmation:' + order['confirmation']['sha256']): bad()
        if bill is not None:
            if bill['state'] == 'budget_settled' and row['state'] != 'settled': bad()
            if bill['state'] != 'staged' and row['state'] == 'reserved': bad()
        # handed_off + settled is legitimate if the market commit failed after
        # the budget commit; retain it for explicit original-order reconciliation.
    # An interrupted pre-stage reservation may have no market bill. Once the
    # expense started, removing both bill and budget-link cannot hide its lineage.
    by_binding = {}
    for key, raw in rows:
        if key.startswith('payment_order:'):
            order = parse_json(raw); by_binding[order['prepared']['binding_sha256']] = order
    for row in budget.execute("SELECT * FROM reservations WHERE request_id LIKE 'purchase:%'"):
        spec = parse_json(row['spec'])
        if not isinstance(spec, list) or len(spec) != 3 or spec[0] != 'resource' or not isinstance(spec[1], str): continue
        order = by_binding.get(spec[1].removeprefix('order:')) if spec[1].startswith('order:') else None
        if order is None: continue
        for bound in scopes:
            if (row['root_id'], row['task_id'], row['principal']) != (bound['root_id'], bound['task_id'], bound['buyer']): continue
            if row['request_id'] != 'purchase:' + _hash([bound['market_path_sha256'], order['order_id']]): continue
            if 'payment_bill:' + order['order_id'] not in records and (row['state'] not in ('reserved', 'cancelled') or 'confirmation' in order): bad()
