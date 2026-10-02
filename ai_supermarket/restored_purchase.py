"""本项目原创。恢复副本只读查回原任务采购，不改绑旧委托或重发账单。"""
from .common import MarketError, fields, identifier, parse_json
from .payment_orders import PaymentOrders, _get, _hash


def find(host, request_id):
    identifier(request_id, 'request_id')
    current = host.billing._scope
    with host.market.store.connect() as db:
        gate = db.execute("SELECT value FROM meta WHERE key='recovery_state'").fetchone()
        if gate is None or gate[0] != 'released': return None
        matches = []
        # Recovery-only lookup. Normal requests still use the indexed exact key.
        for row in db.execute("SELECT key,value FROM meta WHERE key GLOB 'purchase_host:*'"):
            value = parse_json(row['value']); fields(value, ('terms', 'terms_sha256', 'order_id'))
            terms = value['terms']; fields(terms, ('host', 'resource', 'version', 'release_digest', 'request_id'))
            binding = terms['host']; fields(binding, ('scope', 'mandate_id', 'purpose', 'channel_policy'))
            scope = binding['scope']
            if terms['request_id'] != request_id or any(scope.get(n) != current[n] for n in ('buyer', 'root_id', 'task_id')):
                continue
            if scope == current: continue
            if (_hash(terms) != value['terms_sha256'] or row['key'] != 'purchase_host:' + _hash([scope, request_id])):
                raise MarketError('purchase_history_corrupt', '原采购记录摘要不符。')
            if (any(scope.get(n) != current[n] for n in ('market_id', 'environment'))
                    or binding['purpose'] != host.purpose):
                raise MarketError('purchase_scope_mismatch', '恢复后的原请求不能更换市场、环境或用途。')
            order = None
            if value['order_id'] is not None:
                orders = PaymentOrders(host.market, binding['channel_policy']['merchant'])
                order = orders._view(orders._order(db, value['order_id']))
                b = order['prepared']['binding']
                if (_get(db, 'purchase_host_order:' + value['order_id']) != row['key']
                        or [b['resource'], b['version'], b['purpose'], b['release_digest']]
                        != [terms['resource'], terms['version'], host.purpose, terms['release_digest']]):
                    raise MarketError('purchase_binding_mismatch', '恢复后的请求没有绑定原订单。')
            matches.append((value, order))
        if len(matches) > 1:
            raise MarketError('purchase_restore_ambiguous', '多个历史采购使用相同任务请求；保留记录供有权宿主核对。')
        return matches[0] if matches else None


def status(history):
    flow, order = history
    return {'status': 'purchase_pending', 'request_id': flow['terms']['request_id'],
            'order_id': flow['order_id'], 'order_status': order['status'] if order else 'preparation_pending',
            'paid_verified': bool(order and order['paid_verified']),
            'entitlement_created': bool(order and order['entitlement_created']),
            'next_action': 'host_reconcile_restored_purchase', 'payment_sent': False, 'task_completed': False}
