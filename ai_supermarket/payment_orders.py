"""来源：本项目原创。可信宿主的持久化支付准备及核验尝试记录。

仅存订单条件、状态和摘要，不存付款凭证或密钥，不联网。
付款确认和权益发放由可信宿主payment_fulfillment入口验证并提交。
"""
from copy import deepcopy
import hashlib
import re

from .common import MarketError, choice, fields, identifier, parse_json
from .payment_preparation import _canonical, prepare_order, recheck_order


def _hash(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _digest(value, name):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise MarketError('invalid_params', name + '需要SHA256摘要。')


def _get(db, key):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return parse_json(row[0]) if row else None


def _put(db, key, value):
    db.execute('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               (key, _canonical(value)))


class PaymentOrders:
    """Trusted local host only; merchant mapping is not supplied by model tools."""
    def __init__(self, market, merchant):
        if market.role != 'buyer':
            raise MarketError('forbidden', '支付准备记录需要买方宿主。')
        self.market, self.merchant = market, deepcopy(merchant)

    def _order(self, db, order_id):
        identifier(order_id, 'order_id')
        environment = _get(db, 'payment_environment')
        if environment is not None and environment != self.merchant.get('environment'):
            raise MarketError('payment_environment_mismatch', '支付订单与账本环境不符。')
        record = _get(db, 'payment_order:' + order_id)
        if record is None:
            raise MarketError('order_not_found', '当前买方没有此支付准备记录。')
        fields(record, ('order_id', 'prepared', 'prepared_sha256', 'intent_key', 'status', 'attempts', 'handoff_sha256'),
               ('confirmation', 'entitlement_id', 'refund_ledger_sha256'))
        if (not isinstance(record['prepared'], dict) or not isinstance(record['prepared'].get('binding'), dict)
                or not isinstance(record['attempts'], list) or len(record['attempts']) > 4):
            raise MarketError('order_corrupt', '订单准备记录结构损坏。')
        prepared = record['prepared']
        if prepared['binding'].get('buyer') != self.market.principal:
            raise MarketError('order_not_found', '当前买方没有此支付准备记录。')
        digest = _hash(prepared['binding'])
        if (prepared['binding'].get('merchant') != self.merchant or prepared.get('binding_sha256') != digest
                or prepared.get('channel_identifiers', {}).get('out_trade_no') != order_id
                or record['order_id'] != order_id
                or record['prepared_sha256'] != _hash(prepared)):
            raise MarketError('order_changed', '原订单或商户绑定已变化。')
        choice(record['status'], ('prepared_not_payable', 'handoff_reserved', 'verification_pending', 'signature_checked_reconciliation_required',
               'reconciliation_required', 'cancelled_before_verification', 'paid_entitlement_pending',
               'paid_entitlement_granted', 'paid_delivered_to_host'), 'stored_status')
        from .payment_fulfillment import validate_confirmation
        validate_confirmation(record)
        if record['handoff_sha256'] is not None:
            _digest(record['handoff_sha256'], 'handoff_sha256')
        for attempt in record['attempts']:
            fields(attempt, ('id', 'request_sha256', 'outcome', 'response_sha256'))
            identifier(attempt['id'], 'attempt_id')
            _digest(attempt['request_sha256'], 'request_sha256')
            choice(attempt['outcome'], ('pending', 'unknown', 'invalid', 'unverified_response', 'signed_response_match'), 'stored_outcome')
            if attempt['response_sha256'] is not None:
                _digest(attempt['response_sha256'], 'response_sha256')
        return record

    @staticmethod
    def _view(record):
        return {**deepcopy(record), 'payment_authorized': False, 'paid_verified': 'confirmation' in record,
                'entitlement_created': 'entitlement_id' in record, 'channel_request_sent_by_this_module': False}

    def get(self, order_id):
        """Read retained history even if a quote expired; never create a new order."""
        with self.market.store.connect() as db:
            db.execute('BEGIN')
            return self._view(self._order(db, order_id))

    def prepare(self, quote_id, release_digest, request_id):
        identifier(request_id, 'request_id')
        identifier(quote_id, 'quote_id')
        _digest(release_digest, 'release_digest')
        request_key = 'payment_request:' + _hash([self.market.principal, request_id])
        fingerprint = _hash([quote_id, release_digest, self.merchant])
        # Replay does not require a still-valid quote and never changes its data.
        with self.market.store.connect() as db:
            environment = _get(db, 'payment_environment')
            if environment is not None and environment != self.merchant.get('environment'):
                raise MarketError('payment_environment_mismatch', '不能在另一环境账本创建支付准备。')
            old = _get(db, request_key)
            if old:
                if old['fingerprint'] != fingerprint:
                    raise MarketError('idempotency_conflict', '原准备请求不能替换报价或商户。')
                return {**self._view(self._order(db, old['order_id'])), 'replayed': True}
        prepared = prepare_order(self.market, quote_id, release_digest, self.merchant)
        binding = prepared['binding']
        order_id = prepared['channel_identifiers']['out_trade_no']
        # One unresolved purchase per entitlement across new quote/request IDs.
        # Repricing or changing the channel merchant cannot evade this local guard.
        intent = [binding[k] for k in ('buyer', 'resource', 'version', 'purpose', 'content_hash', 'release_digest')]
        intent_key = 'payment_intent:' + _hash(intent)
        record = {'order_id': order_id, 'prepared': prepared, 'prepared_sha256': _hash(prepared),
                  'intent_key': intent_key, 'status': 'prepared_not_payable', 'attempts': [],
                  'handoff_sha256': None}
        with self.market.store.connect(write=True) as db:
            environment = _get(db, 'payment_environment')
            if environment is not None and environment != self.merchant.get('environment'):
                raise MarketError('payment_environment_mismatch', '支付准备期间账本环境变化。')
            old = _get(db, request_key)
            if old and old['fingerprint'] != fingerprint:
                raise MarketError('idempotency_conflict', '原准备请求不能替换报价或商户。')
            if old:
                return {**self._view(self._order(db, old['order_id'])), 'replayed': True}
            # Bind BOTH short IDs to the full digest. Never accept a collision.
            for name, short in prepared['channel_identifiers'].items():
                key = 'payment_channel_id:' + name + ':' + short
                existing = _get(db, key)
                if existing is not None and existing != prepared['binding_sha256']:
                    raise MarketError('channel_id_conflict', '渠道短编号已绑定另一订单。')
            existing = _get(db, 'payment_order:' + order_id)
            if existing is not None:
                existing = self._order(db, order_id)
                if existing['prepared_sha256'] != record['prepared_sha256']:
                    raise MarketError('channel_id_conflict', '原订单绑定冲突。')
                if existing['status'] == 'cancelled_before_verification':
                    raise MarketError('order_closed', '已取消订单不能用新请求重开，请重新报价。')
                record = existing
            pointer = _get(db, intent_key)
            if pointer is not None and pointer != order_id:
                prior = _get(db, 'payment_order:' + pointer)
                if prior is None or prior['status'] != 'cancelled_before_verification':
                    raise MarketError('pending_payment', '同资源已有未解决的支付准备；核查原订单，不另建订单。')
            _put(db, 'payment_order:' + order_id, record)
            _put(db, intent_key, order_id)
            for name, short in prepared['channel_identifiers'].items():
                _put(db, 'payment_channel_id:' + name + ':' + short, prepared['binding_sha256'])
            _put(db, request_key, {'fingerprint': fingerprint, 'order_id': order_id})
        return self._view(record)

    def cancel_unstarted(self, order_id):
        """Close only locally unstarted preparation, never infer a channel refund."""
        with self.market.store.connect(write=True) as db:
            record = self._order(db, order_id)
            if record['status'] == 'cancelled_before_verification':
                return self._view(record)
            if 'confirmation' in record:
                raise MarketError('order_closed', '已确认支付的订单不能按未付款取消。')
            if record['attempts'] or record['handoff_sha256'] is not None:
                raise MarketError('reconciliation_required', '已有核验尝试，不能按未付款取消。')
            record['status'] = 'cancelled_before_verification'
            _put(db, 'payment_order:' + order_id, record)
        return self._view(record)

    def reserve_handoff(self, order_id, payload_digest):
        """Persist intent BEFORE future bill publication; no bill is generated here.

        Once reserved, even a lost response cannot justify local cancellation or
        replacement. A future adapter must independently check payment authority,
        amount, signature and merchant eligibility; this is only a journal guard.
        """
        _digest(payload_digest, 'payload_digest')
        with self.market.store.connect(write=True) as db:
            record = self._order(db, order_id)
            if record['handoff_sha256'] is not None:
                if record['handoff_sha256'] != payload_digest:
                    raise MarketError('idempotency_conflict', '原账单交接意图不能替换内容。')
                return {**self._view(record), 'handoff_replayed': True}
            if record['status'] != 'prepared_not_payable':
                raise MarketError('order_closed', '此订单不能开始新的账单交接。')
            recheck_order(self.market, record['prepared'], self.merchant)
            record.update(status='handoff_reserved', handoff_sha256=payload_digest)
            _put(db, 'payment_order:' + order_id, record)
        return self._view(record)

    def begin_verification(self, order_id, attempt_id, request_digest):
        """Persist query intent BEFORE a future adapter call; this sends nothing.

        For Alipay, use alipay_proof.request_digest: it binds the method,
        merchant mapping and business request, not future transport parameters.
        Raw proof, session and keys stay outside the ledger. Unknown attempts require an
        explicit observation before another query; no automatic retry is provided.
        """
        identifier(attempt_id, 'attempt_id')
        _digest(request_digest, 'request_digest')
        with self.market.store.connect(write=True) as db:
            record = self._order(db, order_id)
            if record['status'] == 'cancelled_before_verification':
                raise MarketError('order_closed', '已取消的准备不能开始核验。')
            for attempt in record['attempts']:
                if attempt['id'] == attempt_id:
                    if attempt['request_sha256'] != request_digest:
                        raise MarketError('idempotency_conflict', '原核验编号不能替换请求。')
                    return {**self._view(record), 'attempt_replayed': True}
            if 'confirmation' in record:
                raise MarketError('order_closed', '支付已确认，不启动新的凭证核验。')
            if record['attempts'] and record['attempts'][-1]['outcome'] == 'pending':
                raise MarketError('reconciliation_required', '上次核验未有结论，先记录原状态。')
            if len(record['attempts']) >= 4:
                raise MarketError('attempt_limit', '核验尝试已达本地上限，请人工核查原订单。')
            # A previously handed-off bill can need reconciliation after expiry.
            # Hold the write reservation while checking first-use current terms.
            if not record['attempts'] and record['handoff_sha256'] is None:
                recheck_order(self.market, record['prepared'], self.merchant)
            record['attempts'].append({'id': attempt_id, 'request_sha256': request_digest,
                                       'outcome': 'pending', 'response_sha256': None})
            record['status'] = 'verification_pending'
            _put(db, 'payment_order:' + order_id, record)
        return self._view(record)

    def observe(self, order_id, attempt_id, outcome, response_digest=None):
        """Record unverified observations only. No value can mark paid or grant rights.

        Invalid proof is not proof that the customer has never paid. All outcomes
        retain the purchase guard until actual channel reconciliation is added.
        """
        identifier(attempt_id, 'attempt_id')
        choice(outcome, ('unknown', 'invalid', 'unverified_response'), 'outcome')
        if response_digest is not None:
            _digest(response_digest, 'response_digest')
        if outcome != 'unknown' and response_digest is None:
            raise MarketError('invalid_params', '有响应的核验记录需要原始响应摘要。')
        with self.market.store.connect(write=True) as db:
            record = self._order(db, order_id)
            if 'confirmation' in record:
                raise MarketError('order_closed', '支付确认后不能改写核验观察。')
            attempt = next((a for a in record['attempts'] if a['id'] == attempt_id), None)
            if attempt is None:
                raise MarketError('attempt_not_found', '必须先保存原核验请求。')
            if attempt['outcome'] != 'pending':
                if (attempt['outcome'], attempt['response_sha256']) != (outcome, response_digest):
                    raise MarketError('idempotency_conflict', '不能改写原核验观察结果。')
                return {**self._view(record), 'observation_replayed': True}
            attempt.update(outcome=outcome, response_sha256=response_digest)
            record['status'] = 'reconciliation_required'
            _put(db, 'payment_order:' + order_id, record)
        return self._view(record)

    def record_signed_response(self, order_id, attempt_id, raw_response, request, verifier):
        """Verify original bytes using host-pinned key; persist match, never grant rights.

        A signed response is not a fresh authenticated transport exchange. That
        and actual merchant onboarding still need the future channel adapter.
        """
        from .alipay_proof import AlipayProofVerifier
        if type(verifier) is not AlipayProofVerifier:
            raise MarketError('invalid_verifier', '需要可信宿主配置的RSA2核验器。')
        identifier(attempt_id, 'attempt_id')
        retained = self.get(order_id)
        checked = verifier.verify(raw_response, retained['prepared'], request)
        evidence_key = 'payment_response:' + order_id + ':' + attempt_id
        trade_key = 'payment_signed_trade:' + _hash([checked['environment'], checked['trade_no']])
        with self.market.store.connect(write=True) as db:
            record = self._order(db, order_id)
            if record['prepared_sha256'] != retained['prepared_sha256']:
                raise MarketError('order_changed', '核验期间原订单变化。')
            attempt = next((a for a in record['attempts'] if a['id'] == attempt_id), None)
            if attempt is None or attempt['request_sha256'] != checked['request_sha256']:
                raise MarketError('verification_request_mismatch', '回执不属于已记录的原核验请求。')
            existing = _get(db, evidence_key)
            if existing is not None:
                if _canonical(existing) != _canonical(checked):
                    raise MarketError('idempotency_conflict', '不能替换已留存的验签证据。')
                return {**self._view(record), 'verification': checked, 'verification_replayed': True}
            if 'confirmation' in record:
                raise MarketError('order_closed', '支付确认后不能追加替代证据。')
            owner = _get(db, trade_key)
            if owner is not None and owner != order_id:
                raise MarketError('trade_conflict', '同一环境的交易号已绑定其他订单。')
            if attempt['outcome'] == 'pending':
                attempt.update(outcome='signed_response_match', response_sha256=checked['response_sha256'])
            # Earlier timeout/invalid observations remain intact if later evidence
            # arrives. Do not rewrite history into a fictitious earlier success.
            _put(db, evidence_key, checked)
            _put(db, trade_key, order_id)
            record['status'] = 'signature_checked_reconciliation_required'
            _put(db, 'payment_order:' + order_id, record)
        return {**self._view(record), 'verification': checked}

    def response_evidence(self, order_id, attempt_id):
        identifier(attempt_id, 'attempt_id')
        with self.market.store.connect() as db:
            db.execute('BEGIN')
            self._order(db, order_id)
            evidence = _get(db, 'payment_response:' + order_id + ':' + attempt_id)
            if evidence is None:
                raise MarketError('verification_not_found', '尚无该请求的验签匹配记录。')
            return evidence
