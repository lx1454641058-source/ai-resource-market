"""来源：本项目原创。任务购买授权、共享额度与Machine Pay签名账单。

可信宿主入口，默认禁用；不调用钱包、不发送账单、不扣款。
两个账本按市场→预算的顺序处理，失败保留占用，可用原请求恢复。
"""
import base64
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from .alipay_transport import RSA2RequestSigner, SHANGHAI
from .budget import TaskBudget, MAX_CENTS
from .common import MarketError, choice, fields, identifier, integer, text
from .payment_fulfillment import PaymentFulfillment, _time, validate_confirmation
from .payment_orders import _get, _hash, _put
from .payment_preparation import _canonical, recheck_order


def _scope(orders, budget):
    if type(budget) is not TaskBudget or budget.principal != orders.market.principal:
        raise MarketError('budget_scope_mismatch', '购买预算必须绑定当前买方任务。')
    return {'buyer':budget.principal, 'root_id':budget.root_id, 'task_id':budget.task_id,
            'budget_path_sha256':_hash(str(Path(budget.ledger.store.path).resolve()).casefold()),
            'market_path_sha256':_hash(str(Path(orders.market.store.path).resolve()).casefold()),
            'market_id':orders.merchant['market_id'], 'environment':orders.merchant['environment']}


def _mandate_key(buyer, mandate_id):
    identifier(mandate_id,'mandate_id')
    return 'purchase_mandate:'+_hash([buyer,mandate_id])


def authorize_purchase_task(orders, budget, mandate_id, *, consent_reference,
                            allowed_sellers, allowed_purposes, max_order_cents, expires_at):
    """Operator records actual user consent; a budget authorization alone is insufficient."""
    scope = _scope(orders,budget)
    identifier(consent_reference,'consent_reference')
    integer(max_order_cents,1,MAX_CENTS,'max_order_cents')
    expires_at = _time(expires_at)
    if not isinstance(allowed_sellers,list) or not 1 <= len(allowed_sellers) <= 100:
        raise MarketError('invalid_params','购买授权需要明确卖家范围。')
    for seller in allowed_sellers:identifier(seller,'seller')
    if not isinstance(allowed_purposes,list) or not 1 <= len(allowed_purposes) <= 2:
        raise MarketError('invalid_params','购买授权需要明确用途范围。')
    for purpose in allowed_purposes:choice(purpose,('personal_use','commercial_use'),'purpose')
    if len(set(allowed_sellers)) != len(allowed_sellers) or len(set(allowed_purposes)) != len(allowed_purposes):
        raise MarketError('invalid_params','授权范围不能重复。')
    terms = {'format':'task-purchase-mandate-v1','scope':scope,'consent_reference':consent_reference,
             'allowed_sellers':sorted(allowed_sellers),'allowed_purposes':sorted(allowed_purposes),
             'max_order_cents':max_order_cents,'expires_at':expires_at}
    key = _mandate_key(budget.principal,mandate_id)
    with orders.market.store.connect(write=True) as db:
        if _get(db,'payment_environment') != scope['environment']:
            raise MarketError('payment_environment_mismatch','先为支付账本绑定正确环境。')
        old = _get(db,key)
        if old is not None:
            if old['terms'] != terms:
                raise MarketError('idempotency_conflict','原购买授权不能更改范围或额度。')
            return {**old,'replayed':True}
        if expires_at <= _time(orders.market.clock()):
            raise MarketError('purchase_authorization_expired','购买授权已过期。')
        with budget.ledger.store.connect() as bdb:
            budget._bound(bdb,active=True)
        record = {'mandate_id':mandate_id,'terms':terms,'terms_sha256':_hash(terms),'state':'active'}
        _put(db,key,record)
    return {**record,'replayed':False}


def revoke_purchase_task(orders, mandate_id):
    """Stops future bill handoff; previously issued signed bills cannot be unsent."""
    key = _mandate_key(orders.market.principal,mandate_id)
    with orders.market.store.connect(write=True) as db:
        record = _get(db,key)
        if record is None:raise MarketError('purchase_authorization_missing','找不到本买方的购买授权。')
        record['state']='revoked';_put(db,key,record)
    return {'mandate_id':mandate_id,'state':'revoked','prior_bills_cancelled':False}


class PurchaseBilling:
    def __init__(self, orders, budget, signer, channel_policy, *, seller_name, enabled=False):
        self._scope = _scope(orders,budget)
        PaymentFulfillment(orders,channel_policy)  # Shared trusted channel policy validation.
        if type(signer) is not RSA2RequestSigner or signer.public_key_sha256 != channel_policy['signing_public_key_sha256']:
            raise MarketError('payment_policy_mismatch','账单签名密钥与收款配置不符。')
        text(seller_name,'seller_name',100)
        if type(enabled) is not bool:raise MarketError('invalid_params','enabled必须为布尔值。')
        self.orders,self.budget,self.signer = orders,budget,signer
        self._policy,self._seller_name,self._enabled = deepcopy(channel_policy),seller_name,enabled

    def _require(self, db):
        if not self._enabled:raise MarketError('billing_disabled','可信宿主尚未启用账单交接。')
        if _scope(self.orders,self.budget) != self._scope or self.orders.merchant != self._policy['merchant']:
            raise MarketError('budget_scope_mismatch','购买任务或商户绑定已变化。')
        if _get(db,'payment_environment') != self._scope['environment']:
            raise MarketError('payment_environment_mismatch','账单与账本环境不符。')

    def _mandate(self, db, mandate_id, record):
        value = _get(db,_mandate_key(self.budget.principal,mandate_id))
        if not isinstance(value,dict):raise MarketError('purchase_authorization_missing','缺少明确任务购买授权。')
        fields(value,('mandate_id','terms','terms_sha256','state'))
        terms = value['terms'];binding=record['prepared']['binding']
        if not isinstance(terms,dict) or _hash(terms) != value['terms_sha256'] or terms.get('scope') != self._scope:
            raise MarketError('purchase_authorization_mismatch','购买授权记录或任务范围不符。')
        if value['state'] != 'active':raise MarketError('purchase_authorization_revoked','购买授权已撤销。')
        if _time(self.orders.market.clock()) >= terms['expires_at']:
            raise MarketError('purchase_authorization_expired','购买授权已过期。')
        if (binding['merchant']['market_seller'] not in terms['allowed_sellers']
                or binding['purpose'] not in terms['allowed_purposes']
                or binding['price_cents'] > terms['max_order_cents']):
            raise MarketError('purchase_not_authorized','卖家、用途或单笔金额超出购买授权。')
        return value

    def _binding(self, order_id, mandate):
        return {'scope':self._scope,'order_id':order_id,'mandate_id':mandate['mandate_id'],
                'mandate_sha256':mandate['terms_sha256'],'channel_policy':self._policy,'seller_name':self._seller_name}

    def _read_bill(self, db, order_id):
        value = _get(db,'payment_bill:'+order_id)
        if value is None:return None
        fields(value,('binding','binding_sha256','reservation_id','payload','payload_sha256','header','state'))
        if (_hash(value['binding']) != value['binding_sha256'] or _hash(value['payload']) != value['payload_sha256']
                or base64.urlsafe_b64encode(_canonical(value['payload']).encode('utf-8')).decode('ascii') != value['header']):
            raise MarketError('bill_corrupt','原支付账单摘要或编码不符。')
        choice(value['state'],('staged','handed_off','budget_settled'),'bill_state')
        return value

    def _build(self, db, record, mandate):
        binding=record['prepared']['binding'];merchant=binding['merchant']
        row=db.execute('SELECT metadata FROM submissions WHERE resource_id=? AND version=?',
                       (binding['resource'],binding['version'])).fetchone()
        from .common import parse_json
        if row is None:raise MarketError('not_found','原账单商品版本缺失。')
        goods_name=parse_json(row[0])['title'];text(goods_name,'goods_name',64)
        cents=binding['price_cents']
        amount=f'{cents//100}.{cents%100:02d}' if self._policy['amount_unit']=='CNY_major' else str(cents)
        deadline=int(min(binding['expires_at'],mandate['terms']['expires_at']))
        if deadline<=_time(self.orders.market.clock()):
            raise MarketError('quote_expired','账单秒级有效期不足，未生成签名账单。')
        pay_before=datetime.fromtimestamp(deadline,SHANGHAI).isoformat(timespec='seconds')
        signed={'amount':amount,'currency':'CNY','goods_name':goods_name,'out_trade_no':record['order_id'],
                'pay_before':pay_before,'resource_id':record['prepared']['channel_identifiers']['resource_id'],
                'seller_id':merchant['seller_id'],'service_id':merchant['service_id']}
        payload={'protocol':{'out_trade_no':signed['out_trade_no'],'amount':amount,'currency':'CNY',
                    'resource_id':signed['resource_id'],'pay_before':pay_before,
                    'seller_signature':self.signer._sign_fields(signed),'seller_sign_type':'RSA2',
                    'seller_unique_id':merchant['seller_id']},
                 'method':{'seller_name':self._seller_name,'seller_id':merchant['seller_id'],
                    'seller_app_id':merchant['app_id'],'goods_name':goods_name,
                    'seller_unique_id_key':'seller_id','service_id':merchant['service_id']}}
        header=base64.urlsafe_b64encode(_canonical(payload).encode('utf-8')).decode('ascii')
        if len(header)>8192:raise MarketError('bill_too_large','账单超出Header大小限制。')
        return payload,header

    def _check_reservation(self, saved, record):
        from .common import parse_json
        with self.budget.ledger.store.connect() as db:
            row=self.budget._get(db,saved['reservation_id'])
            request='purchase:'+_hash([self._scope['market_path_sha256'],record['order_id']])
            spec=['resource','order:'+record['prepared']['binding_sha256'],record['prepared']['binding']['price_cents']]
            if row['request_id']!=request or parse_json(row['spec'])!=spec:
                raise MarketError('bill_corrupt','预算占用不属于原账单或金额。')

    def issue(self, order_id, mandate_id):
        """Return a fixed 402 bill to the host; does not serve HTTP or invoke a wallet."""
        with self.orders.market.store.connect(write=True) as db:
            self._require(db)
            record=self.orders._order(db,order_id)
            if 'confirmation' in record:raise MarketError('already_paid','支付已确认，应恢复原权益。')
            if record['status'] not in ('prepared_not_payable','handoff_reserved'):
                raise MarketError('order_closed','原订单不允许准备支付账单。')
            mandate=self._mandate(db,mandate_id,record)
            binding=self._binding(order_id,mandate)
            saved=self._read_bill(db,order_id)
            if saved and saved['binding'] != binding:
                raise MarketError('idempotency_conflict','原账单不能更换任务、购买授权或收款配置。')
            # Do not expose an old bill after revocation, pause, expiration or terms change.
            recheck_order(self.orders.market,record['prepared'],self.orders.merchant)
            with self.budget.ledger.store.connect() as bdb:self.budget._bound(bdb,active=True)
            budget_link=_get(db,'payment_bill_budget:'+order_id)
            if budget_link is not None and budget_link != binding:
                raise MarketError('idempotency_conflict','同一订单已绑定另一购买任务。')
            if record['attempts']:
                raise MarketError('reconciliation_required','已有支付核验，不能再次交出支付账单。')
            if record['handoff_sha256'] is not None and saved is None:
                raise MarketError('reconciliation_required','已有其他账单交接，保留原记录核查。')
            _put(db,'payment_bill_budget:'+order_id,binding)
            # Global/root exposure is shared with models, dependencies and other tasks.
            request='purchase:'+_hash([self._scope['market_path_sha256'],order_id])
            reservation=self.budget.reserve(request,'resource','order:'+record['prepared']['binding_sha256'],
                                            record['prepared']['binding']['price_cents'])
            if reservation['state'] not in ('reserved','in_flight'):
                raise MarketError('reconciliation_required','预算占用已取消、未知或核销，不能重复交出账单。')
            if saved is None:
                if reservation['state'] != 'reserved':
                    raise MarketError('reconciliation_required','发现没有账单的已开始预算，须核查原操作。')
                payload,header=self._build(db,record,mandate)
                saved={'binding':binding,'binding_sha256':_hash(binding),'reservation_id':reservation['reservation_id'],
                       'payload':payload,'payload_sha256':_hash(payload),'header':header,'state':'staged'}
                _put(db,'payment_bill:'+order_id,saved)
            elif saved['reservation_id']!=reservation['reservation_id']:
                raise MarketError('bill_corrupt','原账单预算占用编号不符。')
        # Stage is committed before crossing into a started/possibly handed-off budget.
        with self.orders.market.store.connect(write=True) as db:
            self._require(db);record=self.orders._order(db,order_id)
            self._mandate(db,mandate_id,record)
            saved=self._read_bill(db,order_id)
            if saved is None or saved['binding']!=binding:
                raise MarketError('bill_corrupt','原已准备账单不完整。')
            recheck_order(self.orders.market,record['prepared'],self.orders.merchant)
            if record['status'] not in ('prepared_not_payable','handoff_reserved'):
                raise MarketError('order_closed','账单准备期间原订单状态变化。')
            if record['attempts'] or 'confirmation' in record:
                raise MarketError('reconciliation_required','已开始核验或确认，停止账单交接。')
            old_handoff=record['handoff_sha256']
            if old_handoff is not None and old_handoff!=saved['payload_sha256']:
                raise MarketError('idempotency_conflict','原交接账单内容不同。')
            with self.budget.ledger.store.connect() as bdb:self.budget._bound(bdb,active=True)
            self._check_reservation(saved,record)
            begun=self.budget.begin(saved['reservation_id'])
            if begun['state']!='in_flight':
                raise MarketError('reconciliation_required','原预算状态不允许账单交接。')
            record.update(status='handoff_reserved',handoff_sha256=saved['payload_sha256'])
            _put(db,'payment_order:'+order_id,record)
            saved['state']='handed_off';_put(db,'payment_bill:'+order_id,saved)
        return {'status':'payment_needed','http_status':402,'headers':{'Payment-Needed':saved['header']},
                'order_id':order_id,'reservation_id':saved['reservation_id'],'replayed':old_handoff is not None,
                'purchase_authorized_by_host':True,'wallet_payment_authorized':False,'payment_sent':False,
                'paid_verified':False,'next_action':'trusted_host_payment_handoff'}

    def history(self, order_id):
        with self.orders.market.store.connect() as db:
            self.orders._order(db,order_id);saved=self._read_bill(db,order_id)
            if saved is None:raise MarketError('bill_not_found','尚无原支付账单。')
            if saved['binding']['scope']!=self._scope:
                raise MarketError('budget_scope_mismatch','不是当前任务的支付账单。')
        return {'order_id':order_id,'state':saved['state'],'payload_sha256':saved['payload_sha256'],
                'reservation':self.budget.status(saved['reservation_id']),'new_payment_authorized':False}

    def settle_budget(self, order_id):
        """Record confirmed original price once; this is expense accounting, not a debit."""
        with self.orders.market.store.connect(write=True) as db:
            self._require(db);record=self.orders._order(db,order_id)
            confirmed=validate_confirmation(record);saved=self._read_bill(db,order_id)
            if confirmed is None:raise MarketError('payment_not_confirmed','原订单尚未确认付款。')
            if (saved is None or saved['state'] not in ('handed_off','budget_settled')
                    or saved['binding']['scope']!=self._scope or saved['binding']['channel_policy']!=confirmed['channel_policy']
                    or saved['payload_sha256']!=record['handoff_sha256']):
                raise MarketError('payment_binding_mismatch','已付原单与预算账单不匹配。')
            receipt='payment-confirmation:'+record['confirmation']['sha256']
            self._check_reservation(saved,record)
            result=self.budget.settle(saved['reservation_id'],record['prepared']['binding']['price_cents'],receipt)
            saved['state']='budget_settled';_put(db,'payment_bill:'+order_id,saved)
        return {'status':'budget_settled','order_id':order_id,'reservation':result,'payment_sent':False}
