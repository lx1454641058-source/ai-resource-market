"""本项目原创。依商品卖家或原请求选择已有采购宿主，不创建订单或授权。"""
from .common import MarketError, identifier
from .payment_orders import _get, _hash


def select_host(hosts, key_id, request_id, resource=None):
    """All hosts share one task/scope: the existing purchase_host key is the lock.

    A paid creator persists that key before handoff. Concurrent different seller
    requests therefore conflict in the original host transaction, not new state.
    """
    identifier(request_id, 'request_id')
    first = next(iter(hosts.values()))
    with first.market.store.connect() as db:
        flow = _get(db, first._key(request_id))
        if flow is not None:
            if not isinstance(flow, dict) or not isinstance(flow.get('terms'), dict):
                raise MarketError('purchase_binding_mismatch', '原采购记录无效。')
            terms = flow['terms']
            matches = [host for host in hosts.values() if host._binding == terms.get('host')]
            if len(matches) != 1:
                raise MarketError('purchase_route_unavailable', '原请求的授权渠道未配置，不能换卖家或渠道重购。')
            host = matches[0]; host._load(db, request_id)
            if resource is not None and list(resource) != [terms['resource'], terms['version']]:
                raise MarketError('idempotency_conflict', '原任务请求不能更换商品或卖家。')
            return host
        from .restored_purchase import find
        history = find(first, request_id)
        if history is not None:
            terms = history[0]['terms']
            if resource is not None and list(resource) != [terms['resource'], terms['version']]:
                raise MarketError('idempotency_conflict', '恢复后的原任务请求不能更换商品或卖家。')
            merchant = terms['host']['channel_policy']['merchant']
            host = hosts.get(merchant['market_seller'])
            if host is None or host.orders.merchant != merchant:
                raise MarketError('purchase_route_unavailable', '原卖家渠道未配置，保留恢复的订单等待核对。')
            return host
        if resource is None:
            # Free file deliveries have a client offer but no purchase order.
            scope = {'principal': first.market.principal, 'root_id': first.budget.root_id,
                     'task_id': first.task_id, 'key_id': key_id}
            receipt = _get(db, 'client_delivery:' + _hash([scope, request_id]))
            if receipt is None:
                raise MarketError('purchase_not_found', '当前任务没有该原采购或文件交付请求。')
            if not isinstance(receipt, dict) or not isinstance(receipt.get('offer'), dict):
                raise MarketError('receipt_corrupt', '原文件交付记录无效。')
            resource = (receipt['offer']['id'], receipt['offer']['version'])
        for name, value in zip(('resource', 'version'), resource): identifier(value, name)
        row = db.execute('''SELECT s.seller,s.content_hash,o.price_cents FROM submissions s
            JOIN offers o USING(resource_id,version) WHERE s.resource_id=? AND s.version=?''', tuple(resource)).fetchone()
        if row is None: raise MarketError('not_found', '目标资源不存在。')
        selected = hosts.get(row['seller'])
        if selected is not None: return selected
        owned = db.execute('''SELECT 1 FROM acquisitions WHERE principal=? AND resource_id=? AND version=?
            AND purpose=? AND content_hash=?''', (first.market.principal, *resource, first.purpose, row['content_hash'])).fetchone()
        if row['price_cents'] == 0 or owned:
            return first  # No new paid order; normal access/contract checks still run.
        raise MarketError('purchase_not_authorized', '此卖家没有配置获准采购渠道；不新建付费订单。')
