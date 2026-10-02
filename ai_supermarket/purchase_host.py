"""来源：本项目原创。任务查货到受限采购、付款核验和交付的可信宿主入口。

不调用钱包；账单仅首次主动交出，恢复查询不会自动再发账单或重试渠道。
原始付款凭证只传给宿主固定核验器，不保存在流程记录中。
"""
from copy import deepcopy
import hashlib

from .alipay_transport import AlipayVerificationClient
from .common import MarketError, compact, fields, identifier
from .host import TaskResources
from .origin import OriginClient, _safe_path, deliver_origin
from .payment_bills import PurchaseBilling
from .payment_fulfillment import PaymentFulfillment
from .payment_orders import _get, _hash, _put


class PurchasingTaskResources(TaskResources):
    """Explicit host injection only; user/model cannot choose keys or payment policy."""
    def __init__(self, billing, fulfillment, verification, goal, *, mandate_id,
                 purpose='personal_use', kind=None, origin_clients=None, output_root=None):
        if (type(billing) is not PurchaseBilling or type(fulfillment) is not PaymentFulfillment
                or type(verification) is not AlipayVerificationClient
                or billing.orders is not fulfillment.orders or billing.orders is not verification.orders
                or billing._policy != fulfillment._policy):
            raise MarketError('purchase_host_mismatch', '采购组件必须属于同一可信订单宿主及渠道配置。')
        policy = billing._policy
        if (verification._merchant != policy['merchant']
                or verification._signer.public_key_sha256 != policy['signing_public_key_sha256']
                or verification._verifier.key_sha256 != policy['verification_public_key_sha256']
                or verification._verifier.amount_unit != policy['amount_unit']
                or verification._verifier.policy_reference != policy['policy_reference']):
            raise MarketError('purchase_host_mismatch', '账单、付款核验和权益的配置不一致。')
        identifier(mandate_id, 'mandate_id')
        self.billing, self.fulfillment, self.verification = billing, fulfillment, verification
        self.orders, self.mandate_id = billing.orders, mandate_id
        self.clients = dict(origin_clients or {})
        if any(type(c) is not OriginClient or name != c.provider for name, c in self.clients.items()):
            raise MarketError('purchase_host_mismatch', '原站连接必须由宿主固定到供应商。')
        self.output_root = _safe_path(output_root) if output_root is not None else None
        if self.output_root is not None and not self.output_root.is_dir():
            raise MarketError('invalid_path', '原站交付根目录必须预先准备。')
        # Persist the task/search using the existing entry hook, with no model call.
        super().__init__(self.orders.market, billing.budget.task_id, goal, kind, purpose, billing.budget)
        self._binding = {'scope':deepcopy(billing._scope), 'mandate_id':mandate_id,
                         'purpose':purpose, 'channel_policy':deepcopy(policy)}

    def _key(self, request_id):
        identifier(request_id, 'request_id')
        # Deliberately excludes mandate/purpose: changing either cannot hide an old request.
        return 'purchase_host:'+_hash([self.billing._scope, request_id])

    def _load(self, db, request_id):
        key = self._key(request_id)
        value = _get(db, key)
        if value is None:
            return None
        fields(value, ('terms', 'terms_sha256', 'order_id'))
        terms = value['terms']
        if (_hash(terms) != value['terms_sha256'] or terms['host'] != self._binding
                or terms['request_id'] != request_id):
            raise MarketError('purchase_scope_mismatch', '原采购请求属于另一任务条件或记录已变化。')
        if value['order_id'] is not None:
            order = self.orders._order(db, value['order_id'])
            b = order['prepared']['binding']
            if (_get(db, 'purchase_host_order:'+value['order_id']) != key
                    or [b['resource'], b['version'], b['purpose'], b['release_digest']]
                    != [terms['resource'], terms['version'], self.purpose, terms['release_digest']]):
                raise MarketError('purchase_binding_mismatch', '采购流程没有绑定原订单条件。')
        return value

    def _required(self, request_id):
        with self.market.store.connect() as db:
            value = self._load(db, request_id)
        if value is None:
            from .restored_purchase import find
            if find(self, request_id) is not None:
                raise MarketError('purchase_recovery_required', '原订单已恢复；核查原付款与预算，不重新交接账单或凭证。')
            raise MarketError('purchase_not_found', '当前任务没有该采购请求。')
        return value

    def _use(self, resource_id, version, request_id):
        key = self._key(request_id)
        with self.market.store.connect() as db:
            old = self._load(db, request_id)
        if old is not None:
            if [old['terms']['resource'], old['terms']['version']] != [resource_id, version]:
                raise MarketError('idempotency_conflict', '原任务请求不能更换商品。')
            return self.resume(request_id)
        from .restored_purchase import find, status
        history = find(self, request_id)
        if history is not None:
            flow, order = history
            if [flow['terms']['resource'], flow['terms']['version']] != [resource_id, version]:
                raise MarketError('idempotency_conflict', '恢复后的原任务请求不能更换商品。')
            if order and order['paid_verified'] and order['entitlement_created']:
                # Reuse already granted rights through current access/refund policy.
                # No old authorization, budget settlement or payment is replayed.
                return super()._use(resource_id, version, request_id)
            return status(history)
        # Existing paid rights and free goods use the same ordinary acquisition path.
        result = super()._use(resource_id, version, request_id)
        if result['status'] != 'payment_unavailable':
            return result
        detail = self.market.resolve(resource_id, version, self.purpose)
        terms = {'host':self._binding, 'resource':resource_id, 'version':version,
                 'release_digest':detail['release_digest'], 'request_id':request_id}
        with self.market.store.connect(write=True) as db:
            old = self._load(db, request_id)
            if old is not None:
                if old['terms'] != terms:
                    raise MarketError('idempotency_conflict', '并发采购条件已变化。')
            else:
                _put(db, key, {'terms':terms, 'terms_sha256':_hash(terms), 'order_id':None})
        # Only the creator calls handoff automatically; a concurrent/repeated use reads state.
        return self.handoff(request_id) if old is None else self.resume(request_id)

    def handoff(self, request_id):
        """Explicitly return/recover the same bill, never execute a wallet payment."""
        from .restored_purchase import find, status
        history = find(self, request_id)
        if history is not None: return status(history)
        flow = self._required(request_id)
        terms = flow['terms']; key = self._key(request_id)
        if flow['order_id'] is None:
            quote = self.market.quote(terms['resource'], terms['version'], self.purpose, _hash([key,'quote']))
            order = self.orders.prepare(quote['quote_id'], terms['release_digest'], _hash([key,'prepare']))
            with self.market.store.connect(write=True) as db:
                current = self._load(db, request_id)
                pointer = _get(db, 'purchase_host_order:'+order['order_id'])
                if pointer is not None and pointer != key:
                    raise MarketError('purchase_scope_mismatch', '原订单已经绑定另一采购流程。')
                if current['order_id'] not in (None, order['order_id']):
                    raise MarketError('purchase_binding_mismatch', '原流程订单不一致。')
                current['order_id'] = order['order_id']
                _put(db, 'purchase_host_order:'+order['order_id'], key)
                _put(db, key, current)
            flow = current
        # Purchase authority also precedes dependency holds; a spending cap alone is not consent.
        with self.market.store.connect() as db:
            self.billing._require(db)
            self.billing._mandate(db, self.mandate_id, self.orders._order(db, flow['order_id']))
        # Reserve declared downstream costs before handing out a bill; shared cap includes both.
        compatibility = self.market.check_compatibility(terms['resource'], terms['version'], self.purpose)
        if compatibility['status'] != 'ready':
            raise MarketError(compatibility['status'], '当前宿主不能使用此资源。')
        extra = compatibility['extra_max_cents']
        if extra:
            reference = hashlib.sha256(compact([terms['resource'],terms['version'],self.purpose]).encode()).hexdigest()
            reserved = self.budget.reserve('dependency:'+reference, 'dependency', reference, extra)
            if reserved['state'] not in ('reserved', 'settled'):
                raise MarketError('dependency_reconciliation_required', '先核对原依赖成本，停止账单交接。')
        return {**self.billing.issue(flow['order_id'], self.mandate_id), 'request_id':request_id}

    def status(self, request_id):
        from .restored_purchase import find, status
        history = find(self, request_id)
        if history is not None: return status(history)
        flow = self._required(request_id)
        order = self.orders.get(flow['order_id']) if flow['order_id'] else None
        if order is None:
            action = 'host_handoff_original_request'
        elif order['paid_verified']:
            action = 'host_resume_delivery'
        elif order['attempts']:
            action = 'host_check_original_verification'
        else:
            action = 'host_payment_handoff' if order['handoff_sha256'] else 'host_handoff_original_request'
        return {'status':'delivered_to_host' if order and order['status']=='paid_delivered_to_host' else 'purchase_pending',
                'request_id':request_id, 'order_id':flow['order_id'],
                'order_status':order['status'] if order else 'preparation_pending',
                'paid_verified':bool(order and order['paid_verified']),
                'entitlement_created':bool(order and order['entitlement_created']),
                'next_action':action, 'payment_sent':False, 'task_completed':False}

    def verify_payment(self, request_id, attempt_id, proof_request):
        """Host receives proof from its wallet path; never accept a paid=true assertion."""
        flow = self._required(request_id)
        if flow['order_id'] is None:
            raise MarketError('handoff_required', '原任务尚未准备支付账单。')
        order = self.orders.get(flow['order_id'])
        if order['handoff_sha256'] is None:
            raise MarketError('handoff_required', '原账单尚未交接。')
        self.verification.verify(flow['order_id'], attempt_id, proof_request)
        return self.resume(request_id)

    def resume(self, request_id):
        """No wallet/query retry. Confirm saved evidence, account once and resume delivery."""
        from .restored_purchase import find
        history = find(self, request_id)
        if history is not None:
            terms = history[0]['terms']
            return self._use(terms['resource'], terms['version'], request_id)
        flow = self._required(request_id)
        if flow['order_id'] is None:
            return self.status(request_id)
        order = self.orders.get(flow['order_id'])
        if not order['paid_verified']:
            attempts = order['attempts']
            if not attempts or attempts[-1]['outcome'] != 'signed_response_match':
                return self.status(request_id)
            self.fulfillment.confirm(flow['order_id'], attempts[-1]['id'])
        self.billing.settle_budget(flow['order_id'])
        # Accounting survives a paused task; further delivery requires an active task.
        with self.budget.ledger.store.connect() as db:
            self.budget._bound(db, active=True)
        granted = self.fulfillment.grant(flow['order_id'])
        delivery_request = _hash([self._key(request_id), 'delivery'])
        delivery = self.market.read(granted['acquisition_id'], self.task_id, delivery_request)
        if delivery['status'] == 'host_transfer_required':
            origin = delivery.get('origin')
            client = self.clients.get(origin['provider']) if origin else None
            if client is None or self.output_root is None:
                return {**delivery, 'order_id':flow['order_id'], 'request_id':request_id,
                        'delivery_request_id':delivery_request, 'paid_verified':True, 'task_completed':False}
            directory = self.output_root/_hash([self._key(request_id), granted['acquisition_id']])
            delivery = deliver_origin(self.market, granted['acquisition_id'], self.task_id,
                                      delivery_request, client, directory)
            # The transfer module itself never pays; do not turn that flag into a claim
            # that this confirmed purchase involved no real payment.
            delivery.pop('real_payments', None)
        receipt = self.fulfillment.record_delivery(flow['order_id'], self.task_id, delivery_request)
        return {**delivery, 'order_id':flow['order_id'], 'acquisition_id':granted['acquisition_id'],
                'request_id':request_id, 'paid_verified':True, 'delivery_receipt':receipt,
                'task_completed':False}

    def acknowledge_delivery(self, request_id):
        """For host asset transfer: require the exact existing usage record, no invented receipt."""
        flow = self._required(request_id)
        return self.fulfillment.record_delivery(flow['order_id'], self.task_id,
                                               _hash([self._key(request_id), 'delivery']))

    def prepare_channel_receipt(self, request_id, client):
        """Call after consuming delivery; only queues, never sends an external message."""
        from .alipay_fulfillment import AlipayFulfillmentClient
        if type(client) is not AlipayFulfillmentClient or client.fulfillment is not self.fulfillment:
            raise MarketError('purchase_host_mismatch', '渠道回执需绑定当前采购宿主。')
        flow = self._required(request_id)
        if flow['order_id'] is None:
            raise MarketError('delivery_required', '原采购尚未交付。')
        return client.prepare(flow['order_id'])
