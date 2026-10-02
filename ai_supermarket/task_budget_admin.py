"""来源：本项目原创。受信本地任务绑定管理，不接收或打印凭据秘密。"""

import argparse
from pathlib import Path
import sqlite3
import sys

from .common import MarketError, compact, error_result
from .task_budget_gateway import TaskBudgetGateway


def main(argv=None):
    parser = argparse.ArgumentParser(description='远程资料交付的凭据任务绑定；不签发钥匙或批准付款')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--identities', type=Path, required=True)
    parser.add_argument('--budget', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    bind = commands.add_parser('bind')
    for field in ('key-id', 'root-id', 'task-id', 'principal', 'authorization-ref'):
        bind.add_argument('--' + field, required=True)
    revoke = commands.add_parser('revoke')
    revoke.add_argument('--key-id', required=True)
    inspect = commands.add_parser('inspect')
    inspect.add_argument('--key-id', required=True)
    inspect.add_argument('--after-reservation-id')
    args = parser.parse_args(argv)
    try:
        gateway = TaskBudgetGateway(args.database, args.identities, args.budget)
        if args.command == 'bind':
            result = gateway.bind_key(args.key_id, args.root_id, args.task_id, args.principal, args.authorization_ref)
        elif args.command == 'inspect':
            result = gateway.inspect_binding(args.key_id, args.after_reservation_id)
        else:
            result = gateway.revoke_binding(args.key_id)
    except MarketError as exc:
        result = error_result(exc)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        result = {'status': 'error', 'error_code': 'unavailable', 'message': '任务绑定管理未完成，核对既有账本及授权记录。'}
    sys.stdout.buffer.write(compact(result).encode('utf-8') + b'\n')
    return int(result['status'] == 'error')


if __name__ == '__main__':
    raise SystemExit(main())
