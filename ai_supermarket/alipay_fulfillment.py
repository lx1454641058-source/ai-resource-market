"""来源：本项目原创。交付后的支付宝履约回执；默认关闭，不扣款、不发权益。

持久化待发送记录与发送意图。未知结果保留待核查，仅已验签SYSTEM_ERROR可显式重试。
不能用签名响应或本机测试替代真实渠道确认。
"""
from datetime import datetime
from copy import deepcopy
import http.client
from urllib.parse import urlencode

from .alipay_proof import _signed_response
from .alipay_transport import AlipayVerificationClient, GATEWAYS, SHANGHAI, _now, _sha
from .common import MarketError, choice, fields, identifier, text
from .payment_fulfillment import PaymentFulfillment, check_paid_history, validate_confirmation
from .payment_orders import _digest, _get, _hash, _put
from .payment_preparation import _canonical


METHOD = 'alipay.aipay.agent.fulfillment.confirm'
RESPONSE_KEY = 'alipay_aipay_agent_fulfillment_confirm_response'


class AlipayFulfillmentClient:
    def __init__(self, fulfillment, transport, *, network_enabled=False):
        if (type(fulfillment) is not PaymentFulfillment or type(transport) is not AlipayVerificationClient
                or fulfillment.orders is not transport.orders):
            raise MarketError('invalid_adapter', '履约回执需绑定同一可信付款与传输宿主。')
        if type(network_enabled) is not bool:
            raise MarketError('invalid_params', 'network_enabled必须为布尔值。')
        self.fulfillment, self.transport = fulfillment, transport
        self.orders = fulfillment.orders
        self._enabled = network_enabled
        self._endpoint, self._test = transport._endpoint, transport._test
        self._configured()

    def _configured(self):
        policy = self.fulfillment._policy
        channel = self.transport
        if (channel._endpoint != self._endpoint or channel._test != self._test
                or self.orders.merchant != policy['merchant'] or channel._merchant != policy['merchant']
                or channel._signer.public_key_sha256 != policy['signing_public_key_sha256']
                or channel._verifier.key_sha256 != policy['verification_public_key_sha256']
                or channel._verifier._merchant != policy['merchant']
                or channel._verifier.amount_unit != policy['amount_unit']
                or channel._verifier.policy_reference != policy['policy_reference']):
            raise MarketError('payment_policy_mismatch', '履约回执的商户、密钥或策略与付款确认不符。')

    @staticmethod
    def _key(order_id):
        identifier(order_id, 'order_id')
        return 'channel_fulfillment:'+order_id

    def _binding(self, db, order_id):
        self._configured()
        self.fulfillment._environment(db)
        record = self.orders._order(db, order_id)
        confirmed = validate_confirmation(record)
        if confirmed is None or record['status'] != 'paid_delivered_to_host':
            raise MarketError('delivery_required', '付款和实际交付尚未完成，不能发送履约回执。')
        if confirmed['channel_policy'] != self.fulfillment._policy:
            raise MarketError('payment_policy_mismatch', '履约需沿用原收款策略。')
        receipt = _get(db, 'payment_delivery:'+order_id)
        if not isinstance(receipt, dict):
            raise MarketError('delivery_required', '原订单缺少持久化交付记录。')
        fields(receipt, ('order_id','acquisition_id','usage_id','task_id','request_id','delivered_at',
                         'channel_fulfillment_confirmed','task_completed'))
        usage = db.execute('SELECT * FROM usage_events WHERE id=?', (receipt['usage_id'],)).fetchone()
        acquired = db.execute('SELECT * FROM acquisitions WHERE id=?', (record['entitlement_id'],)).fetchone()
        if (not usage or not acquired or receipt['order_id'] != order_id
                or receipt['acquisition_id'] != record['entitlement_id']
                or usage['acquisition_id'] != record['entitlement_id']
                or usage['principal'] != self.orders.market.principal
                or any(usage[a] != receipt[b] for a,b in (('task_id','task_id'),('request_id','request_id'),('created_at','delivered_at')))):
            raise MarketError('delivery_binding_mismatch', '交付回执不属于原买方、权益或实际使用事件。')
        check_paid_history(db, acquired)
        from .client_receipts import require_client_receipt
        require_client_receipt(self.orders.market,db,order_id,receipt)
        trade_no = confirmed['evidence']['trade_no']
        text(trade_no, 'trade_no', 32)
        if _get(db, 'payment_signed_trade:'+_hash([self.orders.merchant['environment'],trade_no])) != order_id:
            raise MarketError('trade_conflict', '原交易号未唯一绑定订单。')
        return {'order_id':order_id, 'confirmation_sha256':record['confirmation']['sha256'],
                'delivery_sha256':_hash(receipt), 'merchant':self.orders.merchant,
                'channel_policy':self.fulfillment._policy, 'request':{'trade_no':trade_no}}

    def _load(self, db, order_id):
        binding = self._binding(db, order_id)
        value = _get(db, self._key(order_id))
        if value is None:
            return None, binding
        fields(value, ('binding','binding_sha256','state','transport','response','attempts'))
        if value['binding'] != binding or value['binding_sha256'] != _hash(binding):
            raise MarketError('fulfillment_binding_mismatch', '原履约请求、付款或交付证据已变化。')
        choice(value['state'], ('pending','reserved','unknown','rejected','confirmed','test_response','retryable'), 'fulfillment_state')
        attempts = value['attempts']
        if not isinstance(attempts,list) or len(attempts)>3:
            raise MarketError('fulfillment_record_corrupt', '履约发送历史无效或超出次数。')
        for attempt in attempts:
            fields(attempt, ('state','transport','response'))
            choice(attempt['state'], ('reserved','unknown','rejected','confirmed','test_response','retryable'), 'attempt_state')
        if value['state']=='pending':
            if attempts and attempts[-1]['state']!='retryable':
                raise MarketError('fulfillment_record_corrupt', '未决或已完成请求不能重新等待发送。')
        elif not attempts or attempts[-1] != {k:value[k] for k in ('state','transport','response')}:
            raise MarketError('fulfillment_record_corrupt', '当前状态与原发送历史不一致。')
        metadata, response = value['transport'], value['response']
        if value['state'] == 'pending':
            if metadata is not None or response is not None:
                raise MarketError('fulfillment_record_corrupt', '待发送回执不能已有发送观察。')
        else:
            if not isinstance(metadata,dict):
                raise MarketError('fulfillment_record_corrupt', '缺少原发送意图。')
            fields(metadata, ('endpoint','loopback_test','issued_at','method','wire_sha256'))
            if metadata['method'] != METHOD or metadata['endpoint'] != self._endpoint or metadata['loopback_test'] != self._test:
                raise MarketError('fulfillment_binding_mismatch', '原发送端点或模式变化，不能切换渠道重试。')
            _digest(metadata['wire_sha256'], 'wire_sha256')
            if value['state'] in ('reserved','unknown') and response is not None:
                raise MarketError('fulfillment_record_corrupt', '未决发送不应已有确认响应。')
            if value['state'] in ('rejected','confirmed','test_response','retryable') and not isinstance(response,dict):
                raise MarketError('fulfillment_record_corrupt', '响应观察记录缺失。')
        if value['state']=='retryable' and (response.get('sub_code')!='SYSTEM_ERROR'
                or response.get('public_key_sha256')!=self.fulfillment._policy['verification_public_key_sha256']):
            raise MarketError('fulfillment_record_corrupt', '缺少允许原请求重试的已验签错误。')
        if value['state'] == 'confirmed':
            if (not isinstance(metadata,dict) or not isinstance(response,dict)
                    or metadata.get('loopback_test') is not False
                    or metadata.get('endpoint') != GATEWAYS[binding['merchant']['environment']]
                    or response.get('trade_no') != binding['request']['trade_no']
                    or response.get('https_response_received') is not True
                    or response.get('public_key_sha256') != self.fulfillment._policy['verification_public_key_sha256']):
                raise MarketError('fulfillment_record_corrupt', '缺少匹配的可信渠道回执。')
            _digest(response.get('response_sha256'), 'response_sha256')
        return value, binding

    @staticmethod
    def _view(value):
        return {'order_id':value['binding']['order_id'], 'status':value['state'],
                'binding_sha256':value['binding_sha256'],
                'channel_fulfillment_confirmed':value['state']=='confirmed',
                'response_sha256':value['response'].get('response_sha256') if value['response'] else None,
                'attempt_count':len(value['attempts']),
                'next_action':('none' if value['state']=='confirmed' else
                               'host_send_receipt' if value['state']=='pending' else
                               'host_explicit_retry' if value['state']=='retryable' and len(value['attempts'])<3
                               else 'reconcile_original_receipt'),
                'payment_sent':False, 'entitlement_created':False, 'task_completed':False}

    def prepare(self, order_id):
        """Queue only after delivery. Safe for an asynchronous host worker; no network."""
        with self.orders.market.store.connect(write=True) as db:
            value, binding = self._load(db, order_id)
            if value is None:
                value = {'binding':binding, 'binding_sha256':_hash(binding),
                         'state':'pending', 'transport':None, 'response':None, 'attempts':[]}
                _put(db, self._key(order_id), value)
        return self._view(value)

    def history(self, order_id):
        with self.orders.market.store.connect() as db:
            value, _ = self._load(db, order_id)
        if value is None:
            raise MarketError('fulfillment_not_found', '原订单尚未准备渠道履约回执。')
        return self._view(value)

    def _request(self, binding, timestamp):
        params = {'app_id':binding['merchant']['app_id'], 'method':METHOD, 'format':'json',
                  'charset':'utf-8', 'sign_type':'RSA2', 'timestamp':timestamp, 'version':'1.0',
                  'biz_content':_canonical(binding['request'])}
        params['sign'] = self.transport._signer._sign_fields(params)
        body = urlencode({'biz_content':params.pop('biz_content')}).encode('ascii')
        return urlencode(params), body

    def _response(self, raw, binding):
        body, signed, signature = _signed_response(raw, RESPONSE_KEY)
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        try:
            self.transport._verifier._key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        except (InvalidSignature, ValueError):
            raise MarketError('invalid_signature', '履约回执原始响应验签失败。') from None
        if body.get('code') != '10000' or body.get('sub_code'):
            # The official endpoint documents same-request retry for SYSTEM_ERROR only.
            if body.get('code') != '10000' and body.get('sub_code')=='SYSTEM_ERROR':
                if 'trade_no' in body and body['trade_no']!=binding['request']['trade_no']:
                    raise MarketError('trade_conflict', '错误响应携带另一交易号。')
                return {'result':'retryable','sub_code':'SYSTEM_ERROR','response_sha256':_sha(raw),
                        'public_key_sha256':self.transport._verifier.key_sha256,
                        'https_response_received':not self._test}
            raise MarketError('fulfillment_rejected', '渠道尚未确认履约。')
        if body.get('trade_no') != binding['request']['trade_no']:
            raise MarketError('trade_conflict', '渠道履约回执不是原交易。')
        for key, expected in {'app_id':binding['merchant']['app_id'],
                              'seller_id':binding['merchant']['seller_id'], 'out_trade_no':binding['order_id']}.items():
            if key in body and body[key] != expected:
                raise MarketError('fulfillment_binding_mismatch', '履约响应的商户或订单不符。')
        return {'trade_no':body['trade_no'], 'response_sha256':_sha(raw),
                'public_key_sha256':self.transport._verifier.key_sha256,
                'https_response_received':not self._test}

    def _finish(self, order_id, binding_digest, state, response):
        with self.orders.market.store.connect(write=True) as db:
            value, _ = self._load(db, order_id)
            if value is None or value['state'] != 'reserved' or value['binding_sha256'] != binding_digest:
                raise MarketError('fulfillment_conflict', '原发送状态已变化，须核查该履约请求。')
            value.update(state=state, response=response)
            value['attempts'][-1] = {k:deepcopy(value[k]) for k in ('state','transport','response')}
            _put(db, self._key(order_id), value)
            self._load(db, order_id)
        return self._view(value)

    def send(self, order_id):
        """At most one send per admitted attempt. Unknown is never reset to pending."""
        if not self._enabled:
            raise MarketError('channel_disabled', '宿主未启用履约回执发送。')
        self.prepare(order_id)
        with self.orders.market.store.connect(write=True) as db:
            value, binding = self._load(db, order_id)
            if value['state'] != 'pending':
                return {**self._view(value), 'request_sent_this_call':False}
            if len(value['attempts'])>=3:
                raise MarketError('attempt_limit', '履约发送已达本地上限，须核查原交易。')
            issued = _now(self.transport._clock)
            timestamp = datetime.fromtimestamp(issued, SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')
            query, body = self._request(binding, timestamp)
            value['transport'] = {'endpoint':self.transport._endpoint, 'loopback_test':self.transport._test,
                                  'issued_at':issued, 'method':METHOD,
                                  'wire_sha256':_sha((self.transport._endpoint+'?'+query+'\n').encode()+body)}
            value['state'] = 'reserved'
            value['attempts'].append({k:deepcopy(value[k]) for k in ('state','transport','response')})
            _put(db, self._key(order_id), value)
        # Commit before I/O. A crash here leaves reserved, never an inferred failed send.
        try:
            raw = self.transport._post(query, body)
        except (OSError, http.client.HTTPException, MarketError):
            result = self._finish(order_id, value['binding_sha256'], 'unknown', None)
            return {**result, 'request_sent_this_call':None}
        try:
            self._configured()
            response = self._response(raw, binding)
        except MarketError:
            state, response = 'rejected', {'response_sha256':_sha(raw)}
        else:
            state = ('retryable' if response.get('result')=='retryable' else
                     'test_response' if self._test else 'confirmed')
            response['received_at'] = _now(self.transport._clock)
        result = self._finish(order_id, value['binding_sha256'], state, response)
        return {**result, 'request_sent_this_call':True}

    def retry(self, order_id):
        """Explicit host retry of a signed SYSTEM_ERROR; same trade/binding, max 3 sends."""
        if not self._enabled:
            raise MarketError('channel_disabled', '宿主未启用履约回执发送。')
        with self.orders.market.store.connect(write=True) as db:
            value, _ = self._load(db, order_id)
            if value is None or value['state']!='retryable':
                raise MarketError('reconciliation_required', '仅已验签SYSTEM_ERROR允许显式原单重试。')
            if len(value['attempts'])>=3:
                raise MarketError('attempt_limit', '履约发送已达本地上限，须核查原交易。')
            value.update(state='pending', transport=None, response=None)
            _put(db, self._key(order_id), value)
        return self.send(order_id)
