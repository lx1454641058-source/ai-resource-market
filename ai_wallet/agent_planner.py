"""来源：本项目原创。受信宿主固定 AI 标识的预算提案工具，仅本地模拟。"""

import hashlib
import json
import sqlite3

from .allocation import AllocationWallet
from .engine import WalletError, _text


class TaskPlanner:
    """只映射三个固定方法给模型；此 Python 对象不是身份隔离沙箱。"""

    def __init__(self, wallet, agent_id):
        if not isinstance(wallet, AllocationWallet):
            raise ValueError('预算提案工具需要独立任务预算账本')
        self._wallet = wallet
        self._agent_id = _text(agent_id, 'AI 标识', 100)

    def propose(self, title, budget_cents, reason, request_id):
        """创建待用户确认的任务；同一需求始终复用原请求编号。"""
        def run():
            key = _text(request_id, '请求编号', 100)
            key = 'planner-v1-' + hashlib.sha256(json.dumps(
                [self._agent_id, key], ensure_ascii=False, separators=(',', ':')
            ).encode('utf-8')).hexdigest()
            task = self._wallet.propose_task({
                'title': title, 'agent_id': self._agent_id, 'budget_cents': budget_cents,
                'reason': reason, 'idempotency_key': key,
            })
            return self._read(task['id'])
        return self._run(run)

    def status(self, task_id):
        """只读自己的原任务，不确认预算或触发采购。"""
        return self._run(lambda: self._read(task_id))

    def request_increase(self, task_id, budget_cents, reason, expected_revision):
        """申请新的总预算上限；用户确认前原预算保持不变。"""
        def run():
            task_id_checked = _text(task_id, '任务编号', 100)
            self._read(task_id_checked)  # AI 标识不可变；先限制为本宿主的任务。
            task = self._wallet.propose_budget(task_id_checked, {
                'budget_cents': budget_cents, 'reason': reason,
                'expected_revision': expected_revision,
            })
            return self._read(task['id'])
        return self._run(run)

    @staticmethod
    def _run(callback):
        try:
            return callback()
        except WalletError as exc:
            return {'mode': 'demo', 'status': 'error', 'error_code': exc.code,
                    'next_action': 'fix_request', 'message': str(exc)}
        except (OSError, sqlite3.Error):
            return {'mode': 'demo', 'status': 'unavailable', 'next_action': 'wait_for_host',
                    'message': '结果暂不可读；保留原请求编号和参数，核对原任务，勿重新创建。'}

    def _read(self, task_id):
        task_id = _text(task_id, '任务编号', 100)
        wallet = self._wallet
        with wallet._read_connection() as db:
            task = wallet._task(db, task_id)
            if task['agent_id'] != self._agent_id:
                raise WalletError('找不到该任务', 'not_found', 404)
            spent, held = wallet._usage(wallet._orders(db), task_id=task_id)
            pending = task.get('proposed_budget_cents') is not None
            active = task['status'] == 'active' and wallet._policy(db)['enabled']
            action = 'wait_for_user' if pending else 'purchase_within_task' if active else 'stop'
            return {'mode': 'demo', 'task_id': task_id, 'status': task['status'],
                    'revision': task['revision'], 'budget_cents': task['budget_cents'],
                    'proposed_budget_cents': task.get('proposed_budget_cents'),
                    'available_cents': max(0, task['budget_cents'] - spent - held) if active else 0,
                    'currency': 'CNY', 'next_action': action}
