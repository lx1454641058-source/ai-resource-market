"""来源：本项目原创。宿主内部的持久提交边界；不提供支付工具或凭据隔离。"""

import uuid
import re
from urllib.parse import urlsplit

from .engine import WalletError, _text


class _OrderDispatch:
    """仅供受信宿主调用；模型不能申领执行权或自行提供付款证据。

    claim 提交事务成功后，首次调用方才可执行一次外部动作。任何重启、
    超时或重复调用均不能再次取得执行权，只能核对原订单。此约定依赖宿主
    不绕过本入口；类本身不能隔离同进程代码或其他浏览器工具。
    """

    def __init__(self, wallet, task_id):
        self.wallet = wallet
        self.task_id = _text(task_id, '任务编号', 100)

    def _order(self, db, order_id):
        order = self.wallet._get(db, _text(order_id, '订单编号', 100))
        if order['task_id'] != self.task_id:
            raise WalletError('找不到本任务订单', 'not_found', 404)
        return order

    @staticmethod
    def _checkout(value):
        """只保存宿主核验的脱敏关联字段，不接收完整URL、cookie或页面内容。"""
        keys = {'origin', 'account_ref', 'site_order_id', 'session_ref', 'amount_cents', 'currency'}
        if not isinstance(value, dict) or set(value) != keys:
            raise WalletError('结账关联字段不匹配')
        result = dict(value)
        for key in ('account_ref', 'site_order_id', 'session_ref'):
            if not isinstance(value[key], str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', value[key]):
                raise WalletError('结账关联编号必须是宿主提供的非敏感编号')
        if type(value['amount_cents']) is not int or value['amount_cents'] <= 0 or value['currency'] != 'CNY':
            raise WalletError('仅支持已核对的人民币结账金额')
        raw = value['origin']
        if not isinstance(raw, str) or len(raw) > 300 or any(c.isspace() for c in raw):
            raise WalletError('需要有效网站来源，不接受完整结账网址')
        try:
            parsed = urlsplit(raw)
            host, port = parsed.hostname, parsed.port
            if (not host or not re.fullmatch(r'[a-z0-9.-]+', host) or parsed.username is not None
                    or parsed.password is not None or parsed.path not in ('', '/')
                    or parsed.query or parsed.fragment or '?' in raw or '#' in raw
                    or parsed.scheme not in ('https', 'http')
                    or (parsed.scheme == 'http' and host not in ('127.0.0.1', 'localhost'))):
                raise ValueError('invalid origin')
            default = 443 if parsed.scheme == 'https' else 80
            result['origin'] = parsed.scheme + '://' + host + (f':{port}' if port is not None and port != default else '')
        except ValueError as exc:
            raise WalletError('网站来源必须是HTTPS，或本机HTTP实验地址；不能包含登录信息或参数') from exc
        return result

    def bind(self, order_id, checkout):
        """原订单内保存不可替换的结账关联；跨请求编号也不能绑定同一网站单。"""
        checkout = self._checkout(checkout)
        wallet = self.wallet
        duplicate = False
        with wallet._connection() as db:
            wallet._expire(db)
            order = self._order(db, order_id)
            if order.get('checkout'):
                if order['checkout'] != checkout:
                    raise WalletError('原结账关联不能替换；先核对原订单', 'checkout_conflict', 409)
                return order
            if order.get('dispatch') or order['status'] not in ('approved', 'pending_approval'):
                raise WalletError('此订单不能新增结账关联', 'invalid_state', 409)
            if checkout['amount_cents'] != order['amount_cents']:
                raise WalletError('结账金额与预留金额不符', 'amount_mismatch', 409)
            identity = ('origin', 'account_ref', 'site_order_id')
            for other in wallet._orders(db):
                previous = other.get('checkout')
                if previous and all(previous[k] == checkout[k] for k in identity):
                    # 拒绝重复候选并释放它的占用；不能抛异常回滚后遗留可执行订单。
                    order.update(status='rejected', decision_reason='此网站订单已有钱包记录；重复申请已取消，请查原单')
                    wallet._save(db, order)
                    wallet._audit(db, 'checkout_duplicate_rejected', order['decision_reason'], order_id, 'payment_host')
                    duplicate = True
                    break
            if not duplicate:
                order['checkout'] = checkout
                wallet._save(db, order)
                wallet._audit(db, 'checkout_bound', '已绑定宿主核对的网站订单与脱敏账户标识', order_id, 'payment_host')
                return order
        raise WalletError('此网站订单已有钱包记录；重复申请已取消，查询原单', 'checkout_duplicate', 409)

    def recovery(self, order_id):
        """宿主只读恢复描述；不打开网站、不重新付款、不返回任何支付凭据。"""
        with self.wallet._read_connection() as db:
            order = self._order(db, order_id)
            action = ('verify_original' if order['status'] == 'unknown' else
                      'recheck_checkout' if order['status'] == 'approved' else
                      'wait_for_user' if order['status'] == 'pending_approval' else 'stop')
            return {'order_id': order['id'], 'task_id': self.task_id, 'status': order['status'],
                    'checkout': order.get('checkout'), 'next_action': action,
                    'dispatch_id': order.get('dispatch', {}).get('id')}

    def claim(self, order_id, *, checkout=None):
        """返回一次性执行编号；None 表示不可提交，须读取原状态。

        宿主须先核对结账会话和最终金额。这里复核账本授权，不能用本方法
        替代网页金额核验。事务提交是授权先后次序的边界：后发生的暂停
        不能撤回已经申领的动作，但会保留它的未决占用。
        """
        observed = self._checkout(checkout) if checkout is not None else None
        wallet = self.wallet
        with wallet._connection() as db:
            wallet._expire(db)
            order = self._order(db, order_id)
            if order.get('dispatch') or order['status'] != 'approved':
                return None
            if order.get('checkout') != observed:
                raise WalletError('提交前的结账复核与原关联不符；未取得执行权', 'checkout_mismatch', 409)
            wallet._require_active_task(db, self.task_id)
            policy = wallet._policy(db)
            if not policy['enabled'] or policy['revision'] != order['policy_revision']:
                raise WalletError('钱包已暂停或授权发生变化', 'authorization_changed', 409)
            dispatch_id = uuid.uuid4().hex
            order.update(status='unknown', decision_reason='付款动作已申领，可能已提交；保留额度，先查原单，不再次付款',
                         dispatch={'id': dispatch_id, 'started_at': wallet._now().isoformat()})
            wallet._save(db, order)
            wallet._audit(db, 'payment_dispatch_claimed', order['decision_reason'], order_id, 'payment_host')
        # Never hand execution to the caller until the ledger commit has succeeded.
        return dispatch_id

    def record(self, order_id, dispatch_id, *, status, amount_cents, currency,
               evidence_ref, simulated):
        """受信宿主核对原支付记录后入账；不主动查询、提交或生成付款证据。

        evidence_ref 只能是宿主脱敏后的证据编号，不保存原始页面/凭据。
        当前集成只有模拟商店；此方法不证明真实来源已得到验证。
        """
        dispatch_id = _text(dispatch_id, '执行编号', 100)
        evidence_ref = _text(evidence_ref, '证据编号', 200)
        if (status not in ('paid', 'failed', 'delivery_failed') or currency != 'CNY'
                or type(amount_cents) is not int or amount_cents <= 0 or type(simulated) is not bool):
            raise WalletError('付款结果、币种或金额无效')
        observation = dict(status=status, amount_cents=amount_cents, currency=currency,
                           evidence_ref=evidence_ref, simulated=simulated)
        wallet = self.wallet
        with wallet._connection() as db:
            order = self._order(db, order_id)
            dispatch = order.get('dispatch')
            if not dispatch or dispatch['id'] != dispatch_id:
                raise WalletError('付款证据不属于此执行记录', 'dispatch_mismatch', 409)
            if amount_cents != order['amount_cents']:
                raise WalletError('实扣金额与原订单不符，保留未决占用并人工核对', 'amount_mismatch', 409)
            if dispatch.get('observation'):
                if dispatch['observation'] != observation:
                    raise WalletError('原执行结果已确认，不能用不同结果覆盖', 'result_conflict', 409)
                return order
            if order['status'] != 'unknown':
                raise WalletError('只有未决执行可以核对入账', 'invalid_state', 409)
            dispatch['observation'] = observation
            order.update(status=status, delivery=None,
                         budget_at=wallet._now().isoformat(),
                         decision_reason=('已核对原付款记录明确失败，释放占用' if status == 'failed'
                                          else '已核对原付款记录；成果交付另行确认'))
            if status != 'failed':
                order['receipt'] = dict(id=evidence_ref, amount_cents=amount_cents, currency=currency,
                                       simulated=simulated, confirmed_at=wallet._now().isoformat())
            wallet._save(db, order)
            wallet._audit(db, 'payment_dispatch_recorded', order['decision_reason'], order_id, 'payment_host')
            return order
