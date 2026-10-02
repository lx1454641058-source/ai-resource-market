"""来源：本项目原创。仅监听本机的模拟 HTTP 服务，不适用于公开部署。"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import sqlite3
from urllib.parse import urlsplit

from .engine import Wallet, WalletError
from .allocation import AllocationWallet
from .access import OwnerAccess
from .support import SupportDesk


STATIC = Path(__file__).with_name("static")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WalletError("JSON 中不能出现重复字段")
        result[key] = value
    return result


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, wallet, owner_password=None):
        self.wallet = wallet
        self.access = OwnerAccess(owner_password) if owner_password is not None else None
        self.support = SupportDesk(wallet)
        self.csrf_token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    server_version = "AIWalletDemo/0.1"

    def log_message(self, format, *args):
        # Do not log request URLs, purchase reasons, request bodies or local access tokens.
        pass

    def _reply(self, status, body, content_type="application/json; charset=utf-8", cookie=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, exc):
        # Windows may reset a closing connection with unread request bytes, hiding
        # an otherwise valid 403 response. Discard only a bounded, declared body;
        # never parse or execute it, and never wait indefinitely for hostile input.
        if self.command == "POST" and not getattr(self, "_body_read", False):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if 0 < size <= 65_536:
                    self.connection.settimeout(1)
                    self.rfile.read(size)
            except (OSError, ValueError):
                pass
            finally:
                self._body_read = True
        self._reply(exc.status, {"ok": False, "error": str(exc), "code": exc.code})

    def _check_host(self):
        # Reject alternate Host headers and cross-site access, including DNS rebinding.
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
            raise WalletError("仅支持通过本机启动地址访问", "forbidden_host", 403)
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise WalletError("不允许跨站访问", "forbidden_origin", 403)

    def do_GET(self):
        try:
            self._check_host()
            path = urlsplit(self.path).path
            if path.startswith('/api/') and path not in ('/api/health', '/api/session') and self.server.access:
                self.server.access.require(self.headers.get('Cookie'), touch=False)
            if path == "/api/state":
                state = self.server.wallet.state()
                state["csrf_token"] = self.server.csrf_token
                state['owner_protected'] = self.server.access is not None
                self._reply(200, state)
            elif path == '/api/support/tickets':
                self._reply(200, {'tickets': self.server.support.list(), 'external_delivery': False})
            elif path == '/api/export':
                wallet = self.server.wallet
                # 所有部分在同一事务读取，保证订单、任务和问题记录对应同一快照。
                with wallet._connection() as db:
                    wallet._expire(db)
                    audit = [dict(row) for row in db.execute('SELECT * FROM audit ORDER BY id')]
                    exported = {'format': 'ai-wallet-local-export-v1', 'simulated': True,
                                'exported_at': wallet._now().isoformat(),
                                'orders': wallet._orders(db),
                                'tasks': [wallet._public_task(task) for task in wallet._tasks(db)],
                                'policy': wallet._policy(db),
                                'tickets': [self.server.support._public(ticket) for ticket in self.server.support._load(db)],
                                'audit': audit}
                    if isinstance(wallet, AllocationWallet):
                        exported['format'] = 'ai-wallet-task-budget-export-v1'
                        exported['allocation'] = wallet._allocation(db)
                self._reply(200, exported)
            elif path == '/api/session':
                authenticated = self.server.access is None
                if self.server.access:
                    try:
                        self.server.access.require(self.headers.get('Cookie'), touch=False)
                        authenticated = True
                    except WalletError:
                        pass
                self._reply(200, {'protected': self.server.access is not None, 'authenticated': authenticated})
            elif path == "/api/health":
                self._reply(200, {"ok": True, "mode": "demo", "real_payments": False, "real_agent": False})
            elif path in ("/", "/index.html", "/app.css", "/app.js", "/static/app.css", "/static/app.js"):
                name = "index.html" if path == "/" else path.rsplit("/", 1)[-1]
                mime = {"index.html": "text/html; charset=utf-8", "app.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8"}[name]
                self._reply(200, (STATIC / name).read_bytes(), mime)
            elif path == "/favicon.ico":
                self._reply(204, b"")
            else:
                raise WalletError("页面不存在", "not_found", 404)
        except WalletError as exc:
            self._error(exc)
        except (OSError, ValueError, sqlite3.Error):
            self._error(WalletError("本地资源暂不可用", "internal_error", 500))

    def _body(self):
        if self.headers.get_content_type() != "application/json":
            raise WalletError("请求必须使用 application/json", "invalid_content_type", 415)
        if self.headers.get("Transfer-Encoding"):
            raise WalletError("不支持分块请求")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise WalletError("无效的请求长度") from None
        if size <= 0 or size > 16_384:
            raise WalletError("请求内容为空或超过 16KB", "invalid_size", 413)
        try:
            self.connection.settimeout(5)
            raw = self.rfile.read(size)
            self._body_read = True
            data = json.loads(raw, object_pairs_hook=_unique_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError):
            raise WalletError("请求必须是有效且无重复字段的 JSON 对象") from None
        if not isinstance(data, dict):
            raise WalletError("请求内容必须是 JSON 对象")
        return data

    def do_POST(self):
        try:
            self._check_host()
            if self.headers.get("Origin") != self.server.origin:
                raise WalletError("仅允许来自本机界面的操作", "forbidden_origin", 403)
            path = urlsplit(self.path).path
            if path == '/api/session/login':
                if not self.server.access:
                    raise WalletError('普通演示模式没有管理口令', 'invalid_state', 409)
                body = self._body()
                if set(body) != {'password'}:
                    raise WalletError('登录只接受管理口令')
                token = self.server.access.login(body['password'])
                # 此服务仅允许 HTTP loopback；公网版本须 HTTPS + Secure cookie。
                self._reply(200, {'ok': True}, cookie=f'wallet_owner={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800')
                return
            if self.server.access:
                self.server.access.require(self.headers.get('Cookie'))
            if not secrets.compare_digest(self.headers.get("X-Wallet-Token", ""), self.server.csrf_token):
                raise WalletError("界面会话已失效，请刷新页面后重试", "invalid_token", 403)
            body = self._body()
            wallet = self.server.wallet
            if path == '/api/session/logout':
                if body:
                    raise WalletError('退出不接受额外字段')
                if self.server.access:
                    self.server.access.logout(self.headers.get('Cookie'))
                self._reply(200, {'ok': True}, cookie='wallet_owner=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
                return
            elif path == "/api/orders":
                result = {"order": wallet.request(body)}
            elif path.startswith('/api/allocation/'):
                if not isinstance(wallet, AllocationWallet):
                    raise WalletError('请启动任务预算版本；旧账本不会自动转换', 'allocation_unavailable', 409)
                if path == '/api/allocation/pool':
                    result = {'allocation': wallet.configure_pool(body)}
                elif path == '/api/allocation/proposals':
                    result = {'task': wallet.propose_task(body)}
                else:
                    match = re.fullmatch(r'/api/allocation/tasks/([a-zA-Z0-9_-]{1,100})/(propose|decide|close)', path)
                    if not match:
                        raise WalletError('操作不存在', 'not_found', 404)
                    task_id, action = match.groups()
                    method = {'propose': wallet.propose_budget, 'decide': wallet.decide_budget,
                              'close': wallet.close_task}[action]
                    result = {'task': method(task_id, body)}
            elif path == "/api/policy":
                result = {"policy": wallet.update_policy(body)}
            elif path == '/api/policy/commit':
                if set(body) != {'changes', 'expected_revision', 'expected_pending_ids'}:
                    raise WalletError('保存需要规则、预览版本和受影响订单列表')
                if type(body['expected_revision']) is not int or not isinstance(body['expected_pending_ids'], list):
                    raise WalletError('预览版本必须是整数，受影响订单必须是列表')
                result = {'policy': wallet.update_policy(body['changes'],
                          expected_revision=body['expected_revision'],
                          expected_pending_ids=body['expected_pending_ids'])}
            elif path in ("/api/wallet/pause", "/api/wallet/resume"):
                if body:
                    raise WalletError("此操作不接受额外参数")
                result = {"policy": wallet.update_policy({"enabled": path.endswith("resume")})}
            elif path == "/api/tasks":
                result = {"task": wallet.create_task(body)}
            elif re.fullmatch(r"/api/tasks/[a-zA-Z0-9_-]{1,100}", path):
                result = {"task": wallet.update_task(path.rsplit("/", 1)[-1], body)}
            elif path == '/api/support/tickets':
                result = {'ticket': self.server.support.create(body)}
            elif re.fullmatch(r'/api/support/tickets/[a-f0-9]{20}', path):
                result = {'ticket': self.server.support.update(path.rsplit('/', 1)[-1], body)}
            else:
                match = re.fullmatch(r"/api/orders/([a-f0-9]{20})/(approve|reject|execute|reconcile|refund)", path)
                if not match:
                    raise WalletError("操作不存在", "not_found", 404)
                order_id, action = match.groups()
                if action == "execute":
                    if set(body) - {"scenario"}:
                        raise WalletError("付款操作只接受模拟情景参数")
                    result = {"order": wallet.execute(order_id, body.get("scenario", "success"))}
                else:
                    if body:
                        raise WalletError("此操作不接受额外参数")
                    result = {"order": getattr(wallet, action)(order_id)}
            self._reply(200, dict(ok=True, **result))
        except WalletError as exc:
            self._error(exc)
        except (OSError, ValueError, TypeError, sqlite3.Error):
            self._error(WalletError("本地操作未完成，请刷新查看状态；不要重复提交未知付款", "internal_error", 500))
