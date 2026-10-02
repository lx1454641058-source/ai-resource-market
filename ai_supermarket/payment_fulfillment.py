"""来源：本项目原创。可信宿主的支付确认、原版本权益与交付记账。

不接收模型的paid=true断言。默认禁用；只消费匹配的HTTPS渠道和验签证据。
确认后交付失败保留已付状态，不删除证据、不重新付款。
"""
from copy import deepcopy
import math

from .alipay_transport import GATEWAYS
from .alipay_proof import METHOD
from .assets import get_asset
from .commission_agreements import AGREEMENT_POLICY, quote_fee
from .common import MarketError, choice, compact, fields, identifier, integer
from .contracts import preflight
from .payment_orders import _digest, _get, _hash, _put


PAID_STATES = ('paid_entitlement_pending', 'paid_entitlement_granted', 'paid_delivered_to_host')


def bind_payment_environment(store, environment):
    """Trusted ledger setup before any payment order; immutable environment binding."""
    choice(environment, ('sandbox','production'), 'environment')
    with store.connect(write=True) as db:
        current = _get(db, 'payment_environment')
        if current is not None:
            if current != environment:
                raise MarketError('payment_environment_mismatch', '支付账本不能切换沙箱与生产环境。')
            return {'environment':current,'replayed':True}
        if (db.execute("SELECT 1 FROM meta WHERE key LIKE 'payment_order:%' LIMIT 1").fetchone()
                or db.execute("SELECT 1 FROM acquisitions WHERE id LIKE 'ap_pay_%' LIMIT 1").fetchone()):
            raise MarketError('payment_environment_unbound', '已有未绑定环境的支付记录，请先核查并隔离原账本。')
        _put(db,'payment_environment',environment)
    return {'environment':environment,'replayed':False}


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise MarketError('invalid_clock', '支付证据时间无效。')
    return value


def validate_confirmation(record):
    paid = record['status'] in PAID_STATES
    confirmation = record.get('confirmation')
    if not paid:
        if confirmation is not None or 'entitlement_id' in record:
            raise MarketError('payment_record_corrupt', '支付确认与订单状态不符。')
        return None
    if not isinstance(confirmation, dict):
        raise MarketError('payment_record_corrupt', '支付确认记录缺失。')
    fields(confirmation, ('payload', 'sha256'))
    payload = confirmation['payload']
    if (not isinstance(payload, dict) or _hash(payload) != confirmation['sha256']
            or payload.get('order_id') != record['order_id']
            or payload.get('prepared_sha256') != record['prepared_sha256']
            or payload.get('binding_sha256') != record['prepared']['binding_sha256']):
        raise MarketError('payment_record_corrupt', '支付确认摘要或归属不符。')
    has_right = record['status'] != 'paid_entitlement_pending'
    if has_right != ('entitlement_id' in record):
        raise MarketError('payment_record_corrupt', '支付权益与订单状态不符。')
    return payload


def _paid_order(db, acquired):
    """Validate original payment lineage independently of present access rights."""
    if not acquired['id'].startswith('ap_pay_'):
        return
    link = _get(db, 'paid_entitlement:'+acquired['id'])
    if not isinstance(link, dict):
        raise MarketError('integrity_failed', '付费权益来源记录缺失。')
    order = _get(db, 'payment_order:'+str(link.get('order_id', '')))
    if not isinstance(order, dict):
        raise MarketError('integrity_failed', '付费权益原订单缺失。')
    try:
        confirmation = validate_confirmation(order)
    except MarketError:
        raise MarketError('integrity_failed', '付费权益支付确认记录损坏。') from None
    if (confirmation is None or order.get('entitlement_id') != acquired['id']
            or order['confirmation']['sha256'] != link.get('confirmation_sha256')
            or _hash(order['prepared']) != order['prepared_sha256']):
        raise MarketError('integrity_failed', '付费权益原订单绑定无效。')
    binding = order['prepared']['binding']
    if _get(db,'payment_environment') != binding['merchant']['environment']:
        raise MarketError('integrity_failed', '付费权益与账本环境不符。')
    expected = (binding['buyer'], binding['resource'], binding['version'], binding['purpose'], binding['content_hash'])
    actual = tuple(acquired[k] for k in ('principal','resource_id','version','purpose','content_hash'))
    if actual != expected:
        raise MarketError('integrity_failed', '付费权益内容与原订单不符。')
    return order


def check_paid_acquisition(db, acquired):
    """Fail closed on lost paid lineage or blocked access, including reuse."""
    order = _paid_order(db, acquired)
    if order is None:
        return
    from .payment_refunds import check_refund_access
    check_refund_access(db, order)


def check_paid_history(db, acquired):
    """Bookkeeping only: a refund cannot undo an already evidenced delivery."""
    order = _paid_order(db, acquired)
    if order is not None:
        from .payment_refunds import _load
        _load(db, order)


def _confirmation_payload(db, record, attempt_id, policy, confirmed_at):
    """Check retained source linkage at original confirmation time, no writes or network."""
    order_id = record['order_id']
    if record['handoff_sha256'] is None or _get(db, 'payment_environment') != policy['merchant']['environment']:
        raise MarketError('payment_environment_mismatch', '原交接或支付环境不符。')
    evidence = _get(db, 'payment_response:'+order_id+':'+attempt_id)
    transport = _get(db, 'payment_transport:'+order_id+':'+attempt_id)
    attempt = next((a for a in record['attempts'] if a['id'] == attempt_id), None)
    if not isinstance(evidence, dict) or not isinstance(transport, dict) or attempt is None:
        raise MarketError('payment_evidence_missing', '需要原核验请求、验签及传输证据。')
    binding = record['prepared']['binding']; merchant = policy['merchant']
    if (transport.get('loopback_test') is not False or transport.get('https_response_received') is not True
            or transport.get('outcome') != 'signature_and_order_match'
            or transport.get('method') != METHOD
            or transport.get('endpoint') != GATEWAYS[merchant['environment']]
            or evidence.get('status') != 'signature_and_order_match'):
        raise MarketError('untrusted_payment_evidence', '测试、未知或非固定HTTPS核验不能确认支付。')
    for name in ('signing_public_key_sha256','verification_public_key_sha256','amount_unit','policy_reference'):
        if transport.get(name) != policy[name]:
            raise MarketError('payment_policy_mismatch', '渠道密钥或金额依据与配置不符。')
    expected = {'binding_sha256':record['prepared']['binding_sha256'], 'order_id':order_id,
                'resource_id':record['prepared']['channel_identifiers']['resource_id'],
                'amount_cents':binding['price_cents'], 'currency':'CNY', 'environment':merchant['environment'],
                'request_sha256':attempt['request_sha256'], 'public_key_sha256':policy['verification_public_key_sha256'],
                'amount_unit':policy['amount_unit'], 'policy_reference':policy['policy_reference']}
    if (any(evidence.get(k) != v for k,v in expected.items())
            or transport.get('request_sha256') != attempt['request_sha256']
            or transport.get('response_sha256') != evidence.get('response_sha256')
            or attempt['outcome'] != 'signed_response_match'
            or attempt['response_sha256'] != evidence.get('response_sha256')):
        raise MarketError('payment_binding_mismatch', '支付证据未完整绑定原请求和订单。')
    _digest(transport.get('wire_sha256'), 'wire_sha256')
    _digest(evidence.get('response_sha256'), 'response_sha256')
    identifier(evidence.get('trade_no'), 'trade_no')
    now = _time(confirmed_at)
    issued, received = _time(transport.get('issued_at')), _time(transport.get('received_at'))
    if not issued <= received <= now or now-issued > policy['max_evidence_age_seconds']:
        raise MarketError('payment_evidence_expired', '首次确认需要近期完整的渠道核验。')
    trade_key = 'payment_signed_trade:'+_hash([merchant['environment'],evidence['trade_no']])
    if _get(db, trade_key) != order_id:
        raise MarketError('trade_conflict', '交易号未唯一绑定此订单。')
    row = db.execute('SELECT * FROM quotes WHERE id=? AND buyer=?',
                     (binding['quote_id'], binding['buyer'])).fetchone()
    pairs = {'resource_id':'resource','version':'version','purpose':'purpose','content_hash':'content_hash',
             'price_cents':'price_cents','offer_revision':'offer_revision','expires_at':'expires_at',
             'policy_version':'fee_policy_version'}
    if not row or any(row[k] != binding[v] for k,v in pairs.items()):
        raise MarketError('order_changed', '原报价与已付订单条件不符。')
    # Historical fees remain fixed after payment, even after expiry/reprice.
    fees = quote_fee(db, row, merchant['market_seller'])
    if row['policy_version'] == AGREEMENT_POLICY and fees != binding.get('fee_policy'):
        raise MarketError('order_changed', '原费率快照不符。')
    payload = {'format':'payment-confirmation-v1', 'order_id':order_id, 'attempt_id':attempt_id,
               'prepared_sha256':record['prepared_sha256'], 'binding_sha256':record['prepared']['binding_sha256'],
               'channel_policy':policy, 'evidence':evidence, 'transport':transport,
               'fees':fees, 'handoff_sha256':record['handoff_sha256'], 'confirmed_at':now}
    return payload


class PaymentFulfillment:
    def __init__(self, orders, channel_policy, *, enabled=False):
        # Policy is an operator-held assertion backed by real onboarding evidence,
        # not a new qualification issued by this module or accepted from a model.
        fields(channel_policy, ('merchant', 'qualification_reference', 'signing_public_key_sha256',
               'verification_public_key_sha256', 'amount_unit', 'policy_reference', 'max_evidence_age_seconds'))
        if channel_policy['merchant'] != orders.merchant:
            raise MarketError('order_changed', '收款关系配置与原订单宿主不符。')
        identifier(channel_policy['qualification_reference'], 'qualification_reference')
        identifier(channel_policy['policy_reference'], 'policy_reference')
        for name in ('signing_public_key_sha256','verification_public_key_sha256'):
            _digest(channel_policy[name], name)
        choice(channel_policy['amount_unit'], ('CNY_major','CNY_minor'), 'amount_unit')
        integer(channel_policy['max_evidence_age_seconds'], 1, 300, 'max_evidence_age_seconds')
        if type(enabled) is not bool:
            raise MarketError('invalid_params', 'enabled必须为布尔值。')
        self.orders, self._policy, self._enabled = orders, deepcopy(channel_policy), enabled

    def _require(self):
        if not self._enabled:
            raise MarketError('fulfillment_disabled', '可信宿主尚未启用付费权益。')
        if self.orders.merchant != self._policy['merchant']:
            raise MarketError('order_changed', '运行期间商户配置变化。')

    def _environment(self, db):
        if _get(db,'payment_environment') != self._policy['merchant']['environment']:
            raise MarketError('payment_environment_mismatch', '需预先绑定独立的渠道环境账本。')

    def confirm(self, order_id, attempt_id):
        self._require(); identifier(attempt_id, 'attempt_id')
        market = self.orders.market
        with market.store.connect(write=True) as db:
            self._environment(db)
            record = self.orders._order(db, order_id)
            old = validate_confirmation(record)
            if old is not None:
                if old['attempt_id'] != attempt_id or old['channel_policy'] != self._policy:
                    raise MarketError('idempotency_conflict', '不能替换原支付确认或收款关系。')
                return {**self.orders._view(record), 'confirmation_replayed':True}
            if record['handoff_sha256'] is None:
                raise MarketError('handoff_required', '原账单交接意图尚未保存。')
            payload = _confirmation_payload(db, record, attempt_id, self._policy, market.clock())
            record.update(status='paid_entitlement_pending', confirmation={'payload':payload,'sha256':_hash(payload)})
            _put(db, 'payment_order:'+order_id, record)
        return {**self.orders._view(record), 'confirmation_replayed':False}

    def grant(self, order_id):
        self._require()
        market = self.orders.market
        with market.store.connect(write=True) as db:
            self._environment(db)
            record = self.orders._order(db, order_id)
            confirmed = validate_confirmation(record)
            if confirmed is None:
                raise MarketError('payment_not_confirmed', '原订单尚未确认支付。')
            if confirmed['channel_policy'] != self._policy:
                raise MarketError('payment_policy_mismatch', '须使用原收款关系处理该已付订单。')
            from .payment_refunds import check_refund_access
            check_refund_access(db, record)
            if 'entitlement_id' in record:
                acquired = market._owned(db, record['entitlement_id'])
                return {**market._release_delivery(db, acquired), 'order_id':order_id, 'replayed':True}
            from .exchange import PublishedCatalog
            binding = record['prepared']['binding']
            catalog = PublishedCatalog(db, allow_existing=True)
            item = catalog.get(binding['resource'], binding['version'], binding['purpose'])
            catalog.read(item)
            if (item['sha256'] != binding['content_hash'] or item['seller'] != self._policy['merchant']['market_seller']
                    or market._release_digest(db, binding['resource'], binding['version']) != binding['release_digest']):
                raise MarketError('release_changed', '已付原版本或许可发生变化，保留支付记录。')
            compatible = preflight(item.get('usage_contract'), market.host_policy)
            if compatible['status'] != 'ready':
                raise MarketError(compatible['status'], '宿主不能接收，保留已付订单等待恢复。')
            if 'asset' in item: get_asset(db, binding['resource'], binding['version'], item)
            owned = db.execute('''SELECT id FROM acquisitions WHERE principal=? AND resource_id=?
                AND version=? AND purpose=? AND content_hash=? LIMIT 1''',
                tuple(binding[k] for k in ('buyer','resource','version','purpose','content_hash'))).fetchone()
            if owned:
                raise MarketError('already_owned_reconciliation', '已有其他获取记录，保留支付并核查，不能重复发放。')
            acquired_id = 'ap_pay_'+_hash([binding['buyer'],order_id])[:32]
            now = _time(market.clock())
            db.execute('INSERT INTO acquisitions VALUES (?,?,?,?,?,?,?,?)',
                       (acquired_id,binding['buyer'],'paid:'+order_id,binding['resource'],binding['version'],
                        binding['purpose'],binding['content_hash'],now))
            detail = {'acquisition_id':acquired_id,'purpose':binding['purpose'], 'reused':False,
                      'release_digest':binding['release_digest'], 'payment_order_id':order_id,
                      'payment_confirmation_sha256':record['confirmation']['sha256']}
            db.execute('INSERT INTO exchange_audit VALUES (?,?,?,?,?,?,?)',
                       ('e_'+acquired_id,binding['buyer'],'acquire',binding['resource'],binding['version'],compact(detail),now))
            _put(db, 'paid_entitlement:'+acquired_id, {'order_id':order_id,'confirmation_sha256':record['confirmation']['sha256']})
            record.update(status='paid_entitlement_granted', entitlement_id=acquired_id)
            _put(db, 'payment_order:'+order_id, record)
            acquired = market._owned(db, acquired_id)
            return {**market._release_delivery(db, acquired), 'order_id':order_id, 'replayed':False}

    def record_delivery(self, order_id, task_id, request_id):
        """Only an existing successful host usage event can acknowledge delivery."""
        self._require();identifier(task_id,'task_id');identifier(request_id,'request_id')
        market = self.orders.market
        with market.store.connect(write=True) as db:
            self._environment(db)
            record = self.orders._order(db, order_id)
            if 'entitlement_id' not in record:
                raise MarketError('entitlement_missing', '原订单尚无付费权益。')
            market._owned(db, record['entitlement_id'])
            usage = db.execute('SELECT * FROM usage_events WHERE principal=? AND task_id=? AND request_id=?',
                               (market.principal,task_id,request_id)).fetchone()
            if not usage or usage['acquisition_id'] != record['entitlement_id']:
                raise MarketError('delivery_not_recorded', '缺少本订单实际交付的使用记录。')
            receipt = {'order_id':order_id, 'acquisition_id':record['entitlement_id'], 'usage_id':usage['id'],
                       'task_id':task_id, 'request_id':request_id, 'delivered_at':usage['created_at'],
                       'channel_fulfillment_confirmed':False,'task_completed':False}
            old = _get(db,'payment_delivery:'+order_id)
            if old is not None and old != receipt:
                raise MarketError('idempotency_conflict', '原首笔交付证据不可替换。')
            _put(db,'payment_delivery:'+order_id,receipt)
            record['status'] = 'paid_delivered_to_host'; _put(db,'payment_order:'+order_id,record)
        return {'status':'delivered_to_host', **receipt, 'replayed':old is not None}
