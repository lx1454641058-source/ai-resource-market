"""来源：本项目原创。MCP 2025-11-25 本地预算管理入口；模拟采购需显式启用。"""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .agent_stdio import AgentTools, METHODS, MAX_REQUEST_BYTES, _unique
from .allocation import AllocationWallet
from .engine import WalletError, _text


PROTOCOL = '2025-11-25'
TOOL_NAMES = {'wallet_' + method.replace('.', '_'): method for method in METHODS}
DESCRIPTIONS = {
    'task.propose': '提出任务与建议预算；等待用户确认，不自行分配额度。仅本地模拟。',
    'task.status': '读取本AI的原任务状态；等待时不反复调用，不能自行批准预算。',
    'task.request_increase': '申请增加原任务预算，仍需用户确认；不能转移其他任务额度。',
    'purchase.context': '读取本任务预算和本地模拟商品目录；不是真实商家支持名单。',
    'purchase.submit': '预算内模拟采购。相同购买始终复用request_id；结果未知先查原单，禁止换编号重购。',
    'purchase.status': '只读原订单，不再次付款；按next_action等待用户或宿主处理。',
}


def tool_list(*, enable_demo_purchases=False):
    return [{'name': name, 'description': DESCRIPTIONS[method],
             'inputSchema': {'type': 'object', 'properties': {key: {'type': kind} for key, kind in METHODS[method].items()},
                             'required': list(METHODS[method]), 'additionalProperties': False},
             'annotations': {'readOnlyHint': method in ('task.status', 'purchase.context', 'purchase.status'),
                             'openWorldHint': False}}
            for name, method in TOOL_NAMES.items()
            if method.startswith('task.') or enable_demo_purchases]


def rpc_error(request_id, code, message):
    return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': code, 'message': message}}


class MCPServer:
    """单stdio会话；工具权限继续由已有AgentTools与宿主固定身份决定。"""

    def __init__(self, tools, *, enable_demo_purchases=False):
        self.tools = tools
        self.phase = 'new'
        # 工具清单和调用检查共用同一集合；不能只隐藏按钮却允许猜名称采购。
        self.exposed_tools = tool_list(enable_demo_purchases=enable_demo_purchases)
        self.allowed_names = {tool['name'] for tool in self.exposed_tools}
        self.enable_demo_purchases = enable_demo_purchases

    def handle(self, message):
        if (not isinstance(message, dict) or message.get('jsonrpc') != '2.0'
                or not isinstance(message.get('method'), str)
                or set(message) - {'jsonrpc', 'id', 'method', 'params'}):
            return rpc_error(None, -32600, 'Invalid JSON-RPC request')
        has_id = 'id' in message
        request_id = message.get('id')
        if has_id and (type(request_id) not in (str, int) or
                       (isinstance(request_id, str) and len(request_id) > 128)):
            return rpc_error(None, -32600, 'Invalid request ID')
        method, params = message['method'], message.get('params', {})
        if not isinstance(params, dict):
            return rpc_error(request_id, -32602, 'Parameters must be an object') if has_id else None
        if not has_id:
            # Notifications never execute tools, even if named tools/call.
            if method == 'notifications/initialized' and self.phase == 'initializing':
                self.phase = 'ready'
            return None
        result = None
        if method == 'ping':
            result = {}
        elif method == 'initialize':
            info = params.get('clientInfo')
            if self.phase != 'new':
                return rpc_error(request_id, -32600, 'Already initialized')
            if (not isinstance(params.get('protocolVersion'), str)
                    or not isinstance(params.get('capabilities'), dict)
                    or not isinstance(info, dict) or not isinstance(info.get('name'), str)
                    or not isinstance(info.get('version'), str)):
                return rpc_error(request_id, -32602, 'Invalid initialization parameters')
            self.phase = 'initializing'
            result = {'protocolVersion': PROTOCOL, 'capabilities': {'tools': {'listChanged': False}},
                      'serverInfo': {'name': 'yuxing-ai-wallet-demo', 'version': '0.1.0'},
                      'instructions': ('开发测试模式：允许固定目录模拟采购，不会真实扣款。' if self.enable_demo_purchases else
                                       '预算管理模式：仅提出任务预算、查询原任务、申请追加；不提供采购或付款工具。') +
                                      '预算由用户确认，不能自行批准或访问其他AI任务。支付工具尚未接通。'}
        elif self.phase != 'ready':
            return rpc_error(request_id, -32000, 'Initialize and notify initialized before using tools')
        elif method == 'tools/list':
            if set(params) - {'_meta'}:
                return rpc_error(request_id, -32602, 'No pagination cursor is supported')
            result = {'tools': self.exposed_tools}
        elif method == 'tools/call':
            name, arguments = params.get('name'), params.get('arguments', {})
            if (not isinstance(name, str) or name not in self.allowed_names or not isinstance(arguments, dict)
                    or set(params) - {'name', 'arguments', '_meta'}):
                return rpc_error(request_id, -32602, 'Unknown tool or unsupported parameters')
            reply = self.tools.dispatch({'method': TOOL_NAMES[name], 'params': arguments})
            result = {'content': [{'type': 'text', 'text': json.dumps(reply['result'], ensure_ascii=False, separators=(',', ':'))}],
                      'structuredContent': reply['result'], 'isError': not reply['ok']}
        else:
            return rpc_error(request_id, -32601, 'Method not found')
        return {'jsonrpc': '2.0', 'id': request_id, 'result': result}

    def handle_line(self, raw):
        try:
            value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError('non-finite')))
            json.dumps(value, ensure_ascii=False).encode('utf-8')
        except (ValueError, UnicodeError, RecursionError):
            return rpc_error(None, -32700, 'Invalid UTF-8 JSON')
        return self.handle(value)


def emit(value):
    if value is not None:
        sys.stdout.buffer.write(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n')
        sys.stdout.buffer.flush()


def main():
    parser = argparse.ArgumentParser(description='予行MCP预算管理；支付工具尚未接通')
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--agent-id', required=True)
    parser.add_argument('--print-config', action='store_true', help='仅输出可审阅的客户端配置，不修改任何AI软件设置')
    parser.add_argument('--enable-demo-purchases', action='store_true', help='仅开发测试：额外开放固定目录模拟采购工具，不提供真实付款')
    args = parser.parse_args()
    try:
        agent_id = _text(args.agent_id, 'AI标识', 100)
        if not args.database.is_file():
            raise WalletError('先启动任务预算页面初始化账本；此入口不创建账本。')
        if args.print_config:
            config = {'mcpServers': {'yuxing-wallet-demo': {'command': sys.executable,
                'args': [str(Path(__file__).with_name('mcp_entry.py').resolve()),
                         '--database', str(args.database.resolve()), '--agent-id', agent_id]}}}
            if args.enable_demo_purchases:
                config['mcpServers']['yuxing-wallet-demo']['args'].append('--enable-demo-purchases')
            print(json.dumps(config, ensure_ascii=False, indent=2))
            return 0
        server = MCPServer(AgentTools(AllocationWallet(args.database), agent_id),
                           enable_demo_purchases=args.enable_demo_purchases)
    except (WalletError, OSError, sqlite3.Error):
        print('钱包MCP启动失败：请核对已有任务预算账本及固定AI标识。', file=sys.stderr)
        return 2
    try:
        while True:
            line = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_REQUEST_BYTES:
                while not line.endswith(b'\n'):
                    line = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
                    if not line:
                        break
                emit(rpc_error(None, -32700, 'Message exceeds 16KB; discarded without execution'))
            else:
                emit(server.handle_line(line))
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
