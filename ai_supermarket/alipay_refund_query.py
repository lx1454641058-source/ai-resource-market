"""本项目原创。原退款的有界状态查询；不发起退款、不修改权益或结算。"""
from copy import deepcopy
from datetime import datetime
import http.client
import re
from urllib.parse import urlencode

from .alipay_fulfillment import AlipayFulfillmentClient
from .alipay_proof import _signed_response
from .alipay_transport import SHANGHAI, _now, _sha
from .common import MarketError, choice, fields, identifier, integer, text
from .payment_fulfillment import validate_confirmation
from .payment_orders import _digest, _get, _hash, _put
from .payment_preparation import _canonical

METHOD = 'alipay.trade.fastpay.refund.query'
RESPONSE_KEY = 'alipay_trade_fastpay_refund_query_response'


def _cents(value):
    # This query API explicitly specifies major CNY, independently of the
    # original Machine Pay billing convention. Never use float arithmetic.
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9][0-9]{0,7})(\.[0-9]{1,2})?', value):
        raise MarketError('refund_amount_invalid', '退款查询金额需要精确人民币元字符串。')
    whole, _, fraction = value.partition('.')
    return int(whole) * 100 + int((fraction + '00')[:2])


class AlipayRefundQueryClient:
    """Host-only query adapter. capability_reference is an operator evidence
    reference, not proof that a particular Machine Pay merchant is eligible.
    No refund execution method, generic signing tool or model tool is exposed.
    """
    def __init__(self, fulfillment, transport, *, capability_reference, network_enabled=False):
        identifier(capability_reference, 'capability_reference')
        if type(network_enabled) is not bool:
            raise MarketError('invalid_params', 'network_enabled必须为布尔值。')
        # Reuse the existing fixed merchant/key/endpoint policy validation only.
        # This internal guard never prepares or sends a fulfillment receipt.
        self._guard = AlipayFulfillmentClient(fulfillment, transport)
        self.fulfillment, self.transport, self.orders = fulfillment, transport, fulfillment.orders
        self._reference, self._enabled = capability_reference, network_enabled

    @staticmethod
    def _key(order_id, refund_id):
        identifier(order_id, 'order_id'); text(refund_id, 'refund_id', 64)
        return 'refund_query:' + _hash([order_id, refund_id])

    def _binding(self, db, order_id, refund_id, amount):
        self._guard._configured()
        self.fulfillment._require(); self.fulfillment._environment(db)
        order = self.orders._order(db, order_id)
        paid = validate_confirmation(order)
        if paid is None or paid['channel_policy'] != self.fulfillment._policy:
            raise MarketError('payment_not_confirmed', '需要原订单及匹配收款策略的付款确认。')
        total = order['prepared']['binding']['price_cents']
        integer(amount, 1, total, 'refund_cents')
        trade = paid['evidence']['trade_no']
        if _get(db, 'payment_signed_trade:' + _hash([self.orders.merchant['environment'], trade])) != order_id:
            raise MarketError('trade_conflict', '原渠道交易未唯一绑定当前订单。')
        return {'order_id': order_id, 'confirmation_sha256': order['confirmation']['sha256'],
                'merchant': deepcopy(self.orders.merchant), 'policy': deepcopy(self.fulfillment._policy),
                'capability_reference': self._reference, 'total_cents': total, 'refund_cents': amount,
                'endpoint': self.transport._endpoint, 'loopback_test': self.transport._test,
                'request': {'trade_no': trade, 'out_trade_no': order_id, 'out_request_no': refund_id}}

    def _load(self, db, order_id, refund_id, amount):
        key = self._key(order_id, refund_id)
        binding = self._binding(db, order_id, refund_id, amount)
        value = _get(db, key)
        if value is None:
            return key, {'binding': binding, 'binding_sha256': _hash(binding), 'attempts': []}
        fields(value, ('binding', 'binding_sha256', 'attempts'))
        if value['binding'] != binding or value['binding_sha256'] != _hash(binding):
            raise MarketError('refund_binding_mismatch', '原退款编号、金额、订单或渠道配置发生变化。')
        if not isinstance(value['attempts'], list) or not 1 <= len(value['attempts']) <= 3:
            raise MarketError('refund_record_corrupt', '退款查询历史无效。')
        for attempt in value['attempts']:
            fields(attempt, ('issued_at', 'wire_sha256', 'state', 'response'))
            _now(lambda: attempt['issued_at']); _digest(attempt['wire_sha256'], 'wire_sha256')
            choice(attempt['state'], ('reserved', 'unknown', 'unconfirmed', 'invalid_response', 'confirmed', 'test_response'), 'query_state')
            response = attempt['response']
            if attempt['state'] in ('reserved', 'unknown'):
                if response is not None:
                    raise MarketError('refund_record_corrupt', '未知查询不能已有确认响应。')
            else:
                fields(response, ('response_sha256', 'signed_match', 'refund_success'))
                _digest(response['response_sha256'], 'response_sha256')
                if type(response['signed_match']) is not bool or type(response['refund_success']) is not bool:
                    raise MarketError('refund_record_corrupt', '退款查询验签标记无效。')
                if (attempt['state'] in ('confirmed', 'test_response') and not (response['signed_match'] and response['refund_success'])
                        or attempt['state'] in ('unconfirmed', 'invalid_response') and response['refund_success']
                        or attempt['state'] == 'confirmed' and binding['loopback_test']
                        or attempt['state'] == 'test_response' and not binding['loopback_test']):
                    raise MarketError('refund_record_corrupt', '退款确认与原测试/渠道证据不符。')
        return key, value

    @staticmethod
    def _view(value):
        attempts, binding = value['attempts'], value['binding']
        confirmed = any(a['state'] == 'confirmed' for a in attempts)
        state = 'confirmed' if confirmed else attempts[-1]['state'] if attempts else 'not_queried'
        return {'order_id': binding['order_id'], 'refund_id': binding['request']['out_request_no'],
                'status': state, 'refund_confirmed': confirmed, 'attempt_count': len(attempts),
                'refund_cents': binding['refund_cents'], 'original_paid_cents': binding['total_cents'],
                'refund_executed_by_this_client': False, 'entitlements_changed': False,
                'commission_settled': False, 'cumulative_refund_cents': None,
                'next_action': 'host_reconcile_rights_and_settlement' if confirmed else 'check_original_refund',
                'response_sha256': next((a['response']['response_sha256'] for a in reversed(attempts)
                                         if a['state'] == 'confirmed'), None)}

    def history(self, order_id, refund_id, refund_cents):
        with self.orders.market.store.connect() as db:
            db.execute('BEGIN')
            _, value = self._load(db, order_id, refund_id, refund_cents)
        return self._view(value)

    def _response(self, raw, binding):
        body, signed, signature = _signed_response(raw, RESPONSE_KEY)
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        try:
            self.transport._verifier._key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        except (InvalidSignature, ValueError):
            raise MarketError('invalid_signature', '退款查询原始响应验签失败。') from None
        for name, expected in {**binding['request'], 'app_id': binding['merchant']['app_id'],
                               'seller_id': binding['merchant']['seller_id'], 'currency': 'CNY'}.items():
            if name in body and body[name] != expected:
                raise MarketError('refund_binding_mismatch', '退款响应与原订单或主体不符。')
        if body.get('code') != '10000' or body.get('sub_code') or body.get('refund_status') != 'REFUND_SUCCESS':
            return False
        if (any(body.get(k) != v for k, v in binding['request'].items())
                or _cents(body.get('total_amount')) != binding['total_cents']
                or _cents(body.get('refund_amount')) != binding['refund_cents']):
            raise MarketError('refund_binding_mismatch', '成功退款须匹配原交易、退款编号及精确金额。')
        return True

    def query(self, order_id, refund_id, refund_cents):
        if not self._enabled:
            raise MarketError('channel_disabled', '宿主尚未启用退款查询。')
        with self.orders.market.store.connect(write=True) as db:
            key, value = self._load(db, order_id, refund_id, refund_cents)
            if any(a['state'] == 'confirmed' for a in value['attempts']):
                return {**self._view(value), 'request_sent_this_call': False}
            if len(value['attempts']) >= 3:
                raise MarketError('attempt_limit', '原退款查询已达三次上限，保留记录核账。')
            issued = _now(self.transport._clock)
            if value['attempts'] and issued - value['attempts'][-1]['issued_at'] < 10:
                raise MarketError('query_too_soon', '原退款的再次查询至少间隔十秒。')
            binding = value['binding']
            params = {'app_id': binding['merchant']['app_id'], 'method': METHOD, 'format': 'json',
                      'charset': 'utf-8', 'sign_type': 'RSA2', 'version': '1.0',
                      'timestamp': datetime.fromtimestamp(issued, SHANGHAI).strftime('%Y-%m-%d %H:%M:%S'),
                      'biz_content': _canonical(binding['request'])}
            params['sign'] = self.transport._signer._sign_fields(params)
            body = urlencode({'biz_content': params.pop('biz_content')}).encode('ascii')
            query = urlencode(params)
            wire = _sha((binding['endpoint'] + '?' + query + '\n').encode() + body)
            index = len(value['attempts'])
            value['attempts'].append({'issued_at': issued, 'wire_sha256': wire, 'state': 'reserved', 'response': None})
            _put(db, key, value)
        # Commit the attempt before I/O. Lost replies consume a slot; the only
        # repeat operation allowed here is an explicit read-only status query.
        sent, state, response = None, 'unknown', None
        try:
            raw = self.transport._post(query, body)
        except (OSError, http.client.HTTPException, MarketError):
            pass
        else:
            sent = True
            response = {'response_sha256': _sha(raw), 'signed_match': False, 'refund_success': False}
            try:
                self._guard._configured()
                matched = self._response(raw, binding)
            except MarketError:
                state = 'invalid_response'
            else:
                response['signed_match'] = True
                response['refund_success'] = matched
                state = ('test_response' if binding['loopback_test'] else 'confirmed') if matched else 'unconfirmed'
        with self.orders.market.store.connect(write=True) as db:
            key, current = self._load(db, order_id, refund_id, refund_cents)
            attempt = current['attempts'][index]
            if attempt['state'] != 'reserved' or attempt['wire_sha256'] != wire:
                raise MarketError('refund_query_conflict', '原查询意图发生变化，保留记录核对。')
            attempt.update(state=state, response=response)
            _put(db, key, current)
            self._load(db, order_id, refund_id, refund_cents)
        return {**self._view(current), 'request_sent_this_call': sent}
