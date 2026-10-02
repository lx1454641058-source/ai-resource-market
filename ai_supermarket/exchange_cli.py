"""来源：本项目原创。开放市场本地角色入口，未实现公网身份认证，不修改客户端配置。"""

import argparse
from pathlib import Path
import sqlite3
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_supermarket.common import MarketError, compact, error_result, parse_json
from ai_supermarket.exchange import Exchange, ROLES
from ai_supermarket.exchange_tools import ExchangeInterface
from ai_supermarket.protocol import serve


MAX_INPUT = 131072
ROOT = Path(__file__).resolve().parents[1]


def emit(value):
    sys.stdout.buffer.write(compact(value).encode('utf-8') + b'\n')
    sys.stdout.buffer.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description='AI超市开放市场：本地卖方/买方/审核入口，无真实支付')
    parser.add_argument('--database', type=Path, default=ROOT/'work'/'ai-supermarket'/'exchange.sqlite')
    parser.add_argument('--principal', required=True, help='由受信宿主固定；不是身份认证凭据')
    parser.add_argument('--role', choices=ROLES, default='buyer', help='由受信宿主固定；勿向不可信用户开放启动参数')
    parser.add_argument('--host-policy', type=Path, help='受信宿主能力/权限/依赖声明文件；不允许模型自行设置')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('describe')
    commands.add_parser('config')
    commands.add_parser('mcp')
    commands.add_parser('stdio')
    commands.add_parser('diagnose')
    call = commands.add_parser('call')
    call.add_argument('tool')
    call.add_argument('--input', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'config':
            policy_args = ['--host-policy', str(args.host_policy.resolve())] if args.host_policy else []
            emit({'mcpServers': {'ai-supermarket-' + args.role: {
                'command': sys.executable, 'args': [str(Path(__file__).resolve()), '--database', str(args.database.resolve()),
                                                   '--principal', args.principal, '--role', args.role, *policy_args, 'mcp']}},
                  'authentication': 'trusted_local_host_only', 'installed': False})
            return 0
        policy = None
        if args.host_policy:
            with args.host_policy.open('rb') as handle:
                raw = handle.read(16385)
            if len(raw) > 16384:
                raise MarketError('invalid_size', '宿主策略超过16KB。')
            policy = parse_json(raw)
        market = Exchange(args.database, args.principal, args.role, host_policy=policy)
        interface = ExchangeInterface(market)
        if args.command == 'describe':
            emit({'role': args.role, 'tools': interface.tool_list(), 'instructions': interface.instructions})
            return 0
        if args.command == 'diagnose':
            with market.store.connect() as db:
                integrity = db.execute('PRAGMA quick_check').fetchone()[0]
                count = db.execute("SELECT COUNT(*) FROM offers WHERE availability='active'").fetchone()[0]
            emit({'status': 'ok' if integrity == 'ok' else 'error', 'database_check': integrity,
                  'active_offers': count, 'python_version': sys.version.split()[0], 'role': args.role,
                  'tools': [t['name'] for t in interface.tool_list()], 'real_payments': False,
                  'public_authentication': False, 'model_connection_tested': False})
            return 0 if integrity == 'ok' else 1
        if args.command in ('mcp', 'stdio'):
            serve(market, sys.stdin.buffer, sys.stdout.buffer, mcp=args.command == 'mcp',
                  interface=interface, max_request_bytes=MAX_INPUT)
            return 0
        if args.input:
            with args.input.open('rb') as handle:
                raw = handle.read(MAX_INPUT + 1)
        else:
            raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise MarketError('invalid_size', '请求超过128KB；正文单独限制24KB。')
        result = interface.call(args.tool, parse_json(raw))
        emit(result)
        return 1 if result['status'] in ('error', 'unavailable', 'payment_unavailable') else 0
    except MarketError as exc:
        emit(error_result(exc))
        return 1
    except (OSError, sqlite3.Error):
        emit({'status': 'error', 'error_code': 'unavailable', 'message': '市场账本或输入不可用。'})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
