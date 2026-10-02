"""来源：本项目原创。远程HTTP到本地stdio/MCP适配，不自动安装或配置客户端。"""

import argparse
from pathlib import Path
import sys
from types import SimpleNamespace

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_supermarket.common import MarketError, compact, error_result, parse_json
from ai_supermarket.http_client import RemoteMarket
from ai_supermarket.protocol import serve


class RemoteInterface:
    def __init__(self, client):
        self.client = client
        description = client.tool_list()
        if description.get('status') == 'error':
            raise MarketError(description.get('error_code', 'unavailable'), '远程工具列表不可用。')
        if (description.get('role') not in ('buyer', 'seller', 'reviewer')
                or not isinstance(description.get('tools'), list)
                or not isinstance(description.get('instructions'), str)):
            raise MarketError('invalid_response', '远程工具描述无效。')
        self.market = SimpleNamespace(role=description['role'])
        self.instructions = description['instructions']
        self.tools = description['tools']
        prefix = {'buyer': 'market_', 'seller': 'seller_', 'reviewer': 'review_'}[description['role']]
        if not 1 <= len(self.tools) <= 16 or any(not isinstance(t, dict) or not isinstance(t.get('name'), str)
                or not t['name'].startswith(prefix) or not isinstance(t.get('inputSchema'), dict) for t in self.tools):
            raise MarketError('invalid_response', '远程工具与角色不一致。')

    def tool_list(self):
        return self.tools

    def call(self, name, arguments):
        if not isinstance(name, str) or name not in {t['name'] for t in self.tools}:
            return {'status': 'error', 'error_code': 'unknown_tool', 'message': '此角色未提供该工具。'}
        try:
            return self.client.call(name, arguments)
        except MarketError as exc:
            return error_result(exc)


def emit(value):
    sys.stdout.buffer.write(compact(value).encode('utf-8') + b'\n')
    sys.stdout.buffer.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description='AI超市远程适配，凭据只由宿主从文件读取')
    parser.add_argument('--url', required=True)
    parser.add_argument('--credential-file', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('config', 'describe', 'mcp', 'stdio'):
        commands.add_parser(command)
    task_mcp=commands.add_parser('task-mcp')
    task_mcp.add_argument('--task-id',required=True)
    task_mcp.add_argument('--purpose',choices=['personal_use','commercial_use'],required=True)
    task_mcp.add_argument('--goal-file',type=Path,required=True)
    task_mcp.add_argument('--output-root',type=Path,required=True)
    task_mcp.add_argument('--mode',choices=['purchase','free_text','api'],default='purchase')
    call = commands.add_parser('call')
    call.add_argument('tool')
    call.add_argument('--input', type=Path)
    args = parser.parse_args(argv)
    try:
        client = RemoteMarket(args.url, args.credential_file)
        if args.command == 'config':
            emit({'mcpServers': {'ai-supermarket-remote': {'command': sys.executable,
                'args': [str(Path(__file__).resolve()), '--url', client.base_url,
                         '--credential-file', str(args.credential_file.resolve()), 'mcp']}}, 'installed': False})
            return 0
        if args.command == 'task-mcp':
            from ai_supermarket.remote_task import RemoteTaskInterface, RemoteTaskResources
            with args.goal_file.open('rb') as handle:raw=handle.read(24001)
            if len(raw)>24000:raise MarketError('invalid_size','任务目标文件超过24KB。')
            try:goal=raw.decode('utf-8').strip()
            except UnicodeError:raise MarketError('invalid_params','任务目标需要UTF-8文本。') from None
            interface=RemoteTaskInterface(RemoteTaskResources(client,args.task_id,goal,args.purpose,args.output_root,mode=args.mode))
            serve(interface.market,sys.stdin.buffer,sys.stdout.buffer,mcp=True,interface=interface,max_request_bytes=131072)
            return 0
        if args.command == 'describe':
            result = client.tool_list()
        elif args.command in ('mcp', 'stdio'):
            interface = RemoteInterface(client)
            serve(interface.market, sys.stdin.buffer, sys.stdout.buffer, mcp=args.command == 'mcp',
                  interface=interface, max_request_bytes=131072)
            return 0
        else:
            if args.input:
                with args.input.open('rb') as handle:
                    raw = handle.read(131073)
            else:
                raw = sys.stdin.buffer.read(131073)
            if len(raw) > 131072:
                raise MarketError('invalid_size', '请求超过128KB，未发送。')
            result = client.call(args.tool, parse_json(raw))
        emit(result)
        return 1 if result.get('status') in ('error', 'unavailable', 'payment_unavailable') else 0
    except MarketError as exc:
        emit(error_result(exc))
    except OSError:
        emit({'status': 'error', 'error_code': 'unavailable', 'message': '本地输入不可用；未披露路径或正文。'})
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
