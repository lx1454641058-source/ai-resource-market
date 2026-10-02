"""来源：本项目原创。本机CLI/stdio入口；不安装或修改AI客户端设置。"""

import argparse
from pathlib import Path
import sqlite3
import sys

from .catalog import Catalog, DEFAULT_CATALOG
from .common import MarketError, compact, error_result, parse_json
from .protocol import serve
from .service import Market
from .tools import dispatch, tool_list


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / 'work' / 'ai-supermarket' / 'market.sqlite'


def emit(value):
    sys.stdout.buffer.write(compact(value).encode('utf-8') + b'\n')
    sys.stdout.buffer.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description='AI超市本地首版：面向机器、免费资源、无模型或支付调用')
    parser.add_argument('--database', type=Path, default=DEFAULT_DB)
    parser.add_argument('--principal', default='local-agent', help='由受信宿主固定的主体，不是生产身份凭据')
    parser.add_argument('--catalog', type=Path, default=DEFAULT_CATALOG)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('describe', help='显示工具契约，不创建账本')
    commands.add_parser('check-catalog', help='检查目录、来源与实际文件校验值')
    commands.add_parser('config', help='打印MCP客户端配置，不修改客户端')
    commands.add_parser('mcp', help='运行本地MCP标准输入输出服务')
    commands.add_parser('stdio', help='运行逐行JSON工具服务')
    demo_parser = commands.add_parser('demo', help='运行普通程序宿主演示，生成新目录与检查报告，无模型调用')
    demo_parser.add_argument('--output-root', type=Path, default=ROOT / 'work' / 'ai-supermarket-demo')
    commands.add_parser('benchmark', help='运行公开开发样例的检索检查，不测量模型token')
    search = commands.add_parser('search', help='查找最多几个相关资源')
    search.add_argument('q')
    search.add_argument('--kind', choices=('skill', 'template'))
    search.add_argument('--limit', type=int, default=3)
    call = commands.add_parser('call', help='调用一个工具；参数从JSON文件或标准输入读取')
    call.add_argument('tool')
    call.add_argument('--input', type=Path)
    begin = commands.add_parser('begin', help='宿主任务前检查；记录次数，不保存任务原文')
    begin.add_argument('task_id')
    begin.add_argument('q')
    begin.add_argument('--kind', choices=('skill', 'template'))
    args = parser.parse_args(argv)
    try:
        if args.command == 'demo':
            from .evaluation import demo
            result = demo(args.output_root)
            emit(result)
            return 0 if result['status'] == 'complete' else 1
        if args.command == 'describe':
            emit({'version': '0.1.0', 'real_payments': False, 'tools': tool_list()})
            return 0
        if args.command == 'config':
            emit({'mcpServers': {'ai-supermarket-local': {'command': sys.executable,
                 'args': [str(Path(__file__).with_name('entry.py').resolve()), '--database', str(args.database.resolve()),
                          '--principal', args.principal, '--catalog', str(args.catalog.resolve()), 'mcp']}}})
            return 0
        if args.command == 'check-catalog':
            catalog = Catalog(args.catalog)
            for item in catalog.items.values():
                catalog.read(item)
            emit({'status': 'valid', 'resources': len(catalog.items), 'catalog_revision': catalog.revision,
                  'checks': ['schema', 'original_source', 'local_use', 'confined_paths', 'sha256', 'utf8'],
                  'task_quality_verified': False})
            return 0
        market = Market(args.database, args.principal, args.catalog)
        if args.command == 'benchmark':
            from .evaluation import benchmark
            report = benchmark(market)
            emit(report)
            return 0 if report['passed'] == report['cases'] else 1
        if args.command in ('mcp', 'stdio'):
            serve(market, sys.stdin.buffer, sys.stdout.buffer, mcp=args.command == 'mcp')
            return 0
        if args.command == 'search':
            result = market.search(args.q, args.kind, args.limit)
        elif args.command == 'begin':
            result = market.begin_task(args.task_id, args.q, args.kind)
        else:
            if args.input:
                with args.input.open('rb') as handle:
                    raw = handle.read(16385)
            else:
                raw = sys.stdin.buffer.read(16385)
            if len(raw) > 16384:
                raise MarketError('invalid_size', '输入超过16KB。')
            result = dispatch(market, args.tool, parse_json(raw))
        emit(result)
        return 1 if result['status'] in ('error', 'unavailable', 'payment_unavailable') else 0
    except MarketError as exc:
        emit(error_result(exc))
        return 1
    except (OSError, sqlite3.Error):
        emit({'status': 'error', 'error_code': 'unavailable', 'message': '目录或账本不可用；核对本地路径与文件。'})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
