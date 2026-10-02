"""来源：本项目原创。受信本地宿主的逐行 JSON 工具入口；不是 MCP 或身份沙箱。"""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .agent_interface import TaskWallet
from .agent_planner import TaskPlanner
from .allocation import AllocationWallet
from .engine import WalletError


MAX_REQUEST_BYTES = 16384
# 只公开模型所需的参数；数据库、AI主体、批准及模拟结果均不由请求指定。
METHODS = {
    'task.propose': {'title': 'string', 'budget_cents': 'integer', 'reason': 'string', 'request_id': 'string'},
    'task.status': {'task_id': 'string'},
    'task.request_increase': {'task_id': 'string', 'budget_cents': 'integer', 'reason': 'string', 'expected_revision': 'integer'},
    'purchase.context': {'task_id': 'string'},
    'purchase.submit': {'task_id': 'string', 'product_id': 'string', 'reason': 'string', 'request_id': 'string'},
    'purchase.status': {'task_id': 'string', 'order_id': 'string'},
}


def describe():
    return {'protocol': 'ai-wallet-json-lines-v1', 'mode': 'demo', 'real_payments': False,
            'methods': METHODS, 'max_request_bytes': MAX_REQUEST_BYTES,
            'notice': '宿主固定数据库和AI主体。输入每行一个method/params对象，输出每行一个结果；不提供审批。'}


def error(code, message, action='fix_request'):
    return {'ok': False, 'result': {'mode': 'demo', 'status': 'error', 'error_code': code,
                                  'next_action': action, 'message': message}}


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate field')
        value[key] = item
    return value


class AgentTools:
    def __init__(self, wallet, agent_id):
        self.wallet = wallet
        self.planner = TaskPlanner(wallet, agent_id)

    def dispatch(self, payload):
        if not isinstance(payload, dict) or set(payload) != {'method', 'params'}:
            return error('invalid_request', '请求只接受method和params两个字段。')
        method, params = payload['method'], payload['params']
        if not isinstance(method, str) or method not in METHODS:
            return error('unknown_method', '未知方法；只支持描述中列出的六个工具。')
        if not isinstance(params, dict) or set(params) != set(METHODS[method]):
            return error('invalid_params', '参数字段不匹配；请按工具描述提供全部参数，不增加权限字段。')
        for key, kind in METHODS[method].items():
            if (kind == 'string' and not isinstance(params[key], str)) or (kind == 'integer' and type(params[key]) is not int):
                return error('invalid_params', f'{key}类型不正确。')
        try:
            if method == 'task.propose':
                result = self.planner.propose(**params)
            elif method == 'task.status':
                result = self.planner.status(**params)
            elif method == 'task.request_increase':
                result = self.planner.request_increase(**params)
            else:
                # 每次重新检查固定AI主体的任务归属；错误中不泄露其他任务或订单。
                owned = self.planner.status(params['task_id'])
                if owned['status'] in ('error', 'unavailable'):
                    return {'ok': False, 'result': owned}
                task = TaskWallet(self.wallet, params['task_id'])
                if method == 'purchase.context':
                    result = task.context()
                elif method == 'purchase.submit':
                    result = task.purchase(params['product_id'], params['reason'], params['request_id'])
                else:
                    result = task.status(params['order_id'])
            return {'ok': result.get('status') not in ('error', 'unavailable'), 'result': result}
        except WalletError as exc:
            return error(exc.code, str(exc))
        except (OSError, sqlite3.Error):
            return error('unavailable', '账本暂不可读；保留原请求编号与参数，由宿主核对，勿重新采购。', 'wait_for_host')

    def handle_line(self, raw):
        if len(raw) > MAX_REQUEST_BYTES:
            return error('invalid_size', '请求超过16KB限制，未执行。')
        try:
            payload = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                                 parse_constant=lambda _: (_ for _ in ()).throw(ValueError('non-finite')))
            json.dumps(payload, ensure_ascii=False).encode('utf-8')  # 拒绝JSON转义中的孤立代理字符。
        except (ValueError, UnicodeError, RecursionError):
            return error('invalid_json', '需要有效UTF-8 JSON，不允许重复字段或非有限数字。')
        return self.dispatch(payload)


def emit(value):
    sys.stdout.buffer.write(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n')
    sys.stdout.buffer.flush()


def main():
    parser = argparse.ArgumentParser(description='本地 AI 钱包逐行 JSON 工具；宿主固定已有账本和AI主体，不是真实支付')
    parser.add_argument('--describe', action='store_true', help='输出工具契约，不打开账本')
    parser.add_argument('--database', type=Path, help='宿主指定已经初始化的任务预算账本')
    parser.add_argument('--agent-id', help='宿主固定的AI主体，不是身份凭据')
    args = parser.parse_args()
    if args.describe:
        emit(describe())
        return 0
    if not args.database or not args.agent_id:
        emit(error('setup_required', '宿主需设置--database和--agent-id；--describe可读取契约。', 'wait_for_host'))
        return 2
    if not args.database.is_file():
        emit(error('missing_ledger', '任务预算账本不存在；由用户启动任务预算界面初始化，不会自动创建新文件。', 'wait_for_host'))
        return 2
    try:
        tools = AgentTools(AllocationWallet(args.database), args.agent_id)
    except (WalletError, OSError, sqlite3.Error):
        emit(error('invalid_ledger', '无法使用此账本或AI主体；请核对新版任务预算设置。', 'wait_for_host'))
        return 2
    while True:
        line = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
        if not line:
            break
        if len(line) > MAX_REQUEST_BYTES:
            # 过长消息只拒绝一次并丢弃该行余下字节，不能把后半段当作另一笔采购。
            while not line.endswith(b'\n'):
                line = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
                if not line:
                    break
            emit(error('invalid_size', '请求超过16KB限制，未执行。'))
        else:
            emit(tools.handle_line(line))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
