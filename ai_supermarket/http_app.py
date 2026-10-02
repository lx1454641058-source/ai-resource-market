"""来源：本项目原创。ASGI机器接口；只通过身份网关调用市场，不公开管理方法。"""

import asyncio
from collections import Counter
from dataclasses import dataclass
import ipaddress
import math
from pathlib import Path
import sqlite3
import time
import uuid

from .common import MarketError, compact, error_result, fields, integer, parse_json
from .gateway import AuthenticatedGateway


@dataclass(frozen=True)
class Limits:
    body_bytes: int = 131072
    asset_body_bytes: int = 2228238
    body_seconds: float = 10.0
    concurrent: int = 16
    global_per_minute: int = 600
    peer_per_minute: int = 120
    principal_per_minute: int = 120
    tracked_keys: int = 4096
    enrollment_per_minute: int = 30
    enrollment_peer_per_minute: int = 5

    def __post_init__(self):
        for name in ('body_bytes', 'asset_body_bytes', 'concurrent', 'global_per_minute', 'peer_per_minute',
                     'principal_per_minute', 'tracked_keys', 'enrollment_per_minute', 'enrollment_peer_per_minute'):
            integer(getattr(self, name), 1, 10_000_000, name)
        if type(self.body_seconds) not in (float, int) or not 0 < self.body_seconds <= 60:
            raise ValueError('body_seconds需要0～60秒。')


class WindowLimiter:
    """Bounded process-local 60-second windows. Call only from the ASGI event loop."""
    def __init__(self, capacity, clock):
        self.capacity, self.clock, self.windows = capacity, clock, {}

    def take(self, key, limit):
        now = self.clock()
        entry = self.windows.get(key)
        if entry is None or entry[0] <= now:
            if entry is None and len(self.windows) >= self.capacity:
                self.windows = {k: v for k, v in self.windows.items() if v[0] > now}
                if len(self.windows) >= self.capacity:
                    return max(1, math.ceil(min(v[0] for v in self.windows.values()) - now))
            self.windows[key] = [now + 60, 1]
            return 0
        if entry[1] >= limit:
            return max(1, math.ceil(entry[0] - now))
        entry[1] += 1
        return 0


class HTTPFailure(Exception):
    def __init__(self, status, code, message, headers=()):
        self.status, self.code, self.message, self.headers = status, code, message, headers


@dataclass(frozen=True)
class HTTPReply:
    status: int
    body: dict | bytes
    headers: list


def response_status(result):
    code = result.get('error_code', result.get('status'))
    if code == 'unauthenticated':
        return 401
    if code in ('unavailable', 'payment_unavailable', 'upgrade_required', 'wrong_database',
                'recovery_required', 'snapshot_read_only', 'not_ready', 'enrollment_recovery_required',
                'enrollment_not_initialized', 'measurement_required', 'invalid_database'):
        return 503
    if code in ('forbidden', 'license_denied', 'self_review', 'enrollment_denied', 'enrollment_inactive',
                'task_binding_denied', 'task_budget_required', 'budget_scope_mismatch', 'budget_inactive'):
        return 403
    if code in ('not_found', 'unknown_tool'):
        return 404
    if code in ('idempotency_conflict', 'revision_conflict', 'feedback_conflict', 'invalid_state',
                'invalid_quote', 'quote_changed', 'quote_expired', 'version_exists', 'version_changed',
                'enrollment_conflict', 'task_binding_conflict', 'budget_exceeded',
                'budget_operation_cancelled', 'budget_operation_in_flight', 'budget_operation_unknown'):
        return 409
    return 422 if result.get('status') == 'error' else 200


class MarketHTTP:
    """One process/event loop. TLS terminates at the server; proxy headers are not trusted."""
    def __init__(self, market_database, identity_database, *, allowed_hosts, limits=None, clock=None,
                 budget_database=None, enrollment_database=None, measurement_database=None, enforce_task_budget=False,
                 purchase_hosts=None, api_hosts=None, asset_uploads=False):
        self._configure_transport(allowed_hosts,limits,clock)
        if type(asset_uploads) is not bool:
            raise ValueError('asset_uploads需要布尔开关。')
        self.asset_uploads = asset_uploads
        if asset_uploads:
            from contextlib import closing
            from .recovery import connect_existing, validate_database
            with closing(connect_existing(market_database)) as db:
                meta = validate_database(db, 'market')
                if meta.get('exchange_schema_version') != '2':
                    raise ValueError('远程素材提交必须显式使用已有素材库。')
        if not Path(identity_database).is_file():
            raise ValueError('身份账本必须已由受信运营程序初始化。')
        if enforce_task_budget and (budget_database is None or not Path(budget_database).is_file()):
            raise ValueError('强制任务预算需要既有预算账本。')
        self.readiness_paths = (market_database, identity_database, budget_database, measurement_database)
        self.enrollments = None
        if enrollment_database is not None:
            from .enrollment import Enrollments
            self.enrollments = Enrollments(enrollment_database, identity_database)
        if enforce_task_budget:
            from .task_budget_gateway import TaskBudgetGateway
            self.gateway = TaskBudgetGateway(market_database, identity_database, budget_database)
        else:
            self.gateway = AuthenticatedGateway(market_database, identity_database)
        self.purchases = None
        if purchase_hosts is not None:
            from .machine_pay import MachinePayGateway
            self.purchases = MachinePayGateway(self.gateway,purchase_hosts)
        self.apis = None
        if api_hosts is not None:
            from .api_gateway import ApiGateway
            self.apis = ApiGateway(self.gateway,api_hosts)
    def _configure_transport(self,allowed_hosts,limits=None,clock=None):
        """Bounded HTTP framing without constructing marketplace identities."""
        if not allowed_hosts or any(not isinstance(h,str) or not h or '*' in h or '/' in h
                                    or any(c.isspace() for c in h) for h in allowed_hosts):
            raise ValueError('allowed_hosts需要明确的Host值，不允许通配符。')
        self.hosts = frozenset(h.lower() for h in allowed_hosts)
        self.limits = limits or Limits()
        self.clock = clock or time.monotonic
        self.limiter = WindowLimiter(self.limits.tracked_keys, self.clock)
        self.active = 0
        self.status_counts = Counter()

    async def worker(self, function, *args):
        # A disconnected caller does not cancel a SQLite write or free its concurrency slot early.
        job = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            try:
                await job
            except Exception:
                pass
            raise

    @staticmethod
    def header(headers, key, required=False):
        values = [v for k, v in headers if k.lower() == key]
        if len(values) > 1 or (required and not values):
            raise HTTPFailure(400, 'invalid_headers', '必要请求头缺失或重复。')
        return values[0] if values else None

    def rate(self, key, limit):
        retry = self.limiter.take(key, limit)
        if retry:
            raise HTTPFailure(429, 'rate_limited', '请求过于频繁；保留原操作编号，等待后核验状态。',
                              [(b'retry-after', str(retry).encode('ascii'))])

    async def read_body(self, receive, headers, maximum=None, *, binary_asset=False):
        limit = self.limits.asset_body_bytes if binary_asset else self.limits.body_bytes
        maximum = min(limit, maximum) if maximum is not None else limit
        length = self.header(headers, b'content-length')
        if length is not None:
            if not length.isdigit() or len(length) > 10:
                raise HTTPFailure(400, 'invalid_length', '请求长度无效。')
            if int(length) > maximum:
                raise HTTPFailure(413, 'invalid_size', '请求超过大小限制，未执行。')
        chunks, size = [], 0
        try:
            async with asyncio.timeout(self.limits.body_seconds):
                while True:
                    message = await receive()
                    if message['type'] == 'http.disconnect':
                        raise HTTPFailure(400, 'disconnected', '请求未完整接收，未执行。')
                    if message['type'] != 'http.request':
                        raise HTTPFailure(400, 'invalid_request', '需要HTTP请求。')
                    chunk = message.get('body', b'')
                    size += len(chunk)
                    if size > maximum:
                        raise HTTPFailure(413, 'invalid_size', '请求超过大小限制，未执行。')
                    if chunk:
                        chunks.append(chunk)
                    if not message.get('more_body', False):
                        break
        except TimeoutError:
            raise HTTPFailure(408, 'request_timeout', '请求正文接收超时，未执行。') from None
        if length is not None and size != int(length):
            raise HTTPFailure(400, 'invalid_length', '请求正文与声明长度不符。')
        return b''.join(chunks)

    def json_headers(self, headers):
        content_type = self.header(headers, b'content-type', required=True)
        if content_type.lower().replace(b' ', b'') not in (b'application/json', b'application/json;charset=utf-8'):
            raise HTTPFailure(415, 'unsupported_media_type', '需要application/json UTF-8正文。')
        if self.header(headers, b'content-encoding') is not None:
            raise HTTPFailure(415, 'unsupported_encoding', '不接受压缩正文。')

    def ready(self):
        from .recovery import readiness
        result = readiness(*self.readiness_paths)
        if result['status'] == 'ready' and self.enrollments is not None:
            try:
                self.enrollments.ready()
            except (MarketError, OSError, sqlite3.Error):
                return {'status': 'not_ready'}
        return result

    def enroll(self, value):
        # A restored/partial business batch cannot admit new accounts before release.
        if self.ready()['status'] != 'ready':
            raise MarketError('not_ready', '入驻服务尚未就绪，未开户。')
        return self.enrollments.claim(value)

    async def route(self, scope, receive):
        headers = scope.get('headers', [])
        if sum(len(k) + len(v) for k, v in headers) > 16384:
            raise HTTPFailure(431, 'invalid_headers', '请求头过大。')
        host = self.header(headers, b'host', required=True)
        if host.decode('latin-1').lower() not in self.hosts:
            raise HTTPFailure(400, 'invalid_host', 'Host不在服务允许范围。')
        if scope.get('query_string'):
            raise HTTPFailure(400, 'query_not_allowed', '此机器接口不接受URL查询参数。')
        if self.header(headers, b'origin') is not None:
            raise HTTPFailure(403, 'browser_origin_denied', '此入口仅接受机器调用，不接受浏览器Origin。')
        peer = (scope.get('client') or ('unknown', 0))[0]
        try:
            loopback = ipaddress.ip_address(peer).is_loopback
        except ValueError:
            loopback = False
        if scope.get('scheme') != 'https' and not loopback:
            raise HTTPFailure(403, 'tls_required', '非本机请求必须使用HTTPS。')
        self.rate('global', self.limits.global_per_minute)
        self.rate('peer:' + peer, self.limits.peer_per_minute)
        path, method = scope.get('path'), scope.get('method')
        allowed = {'/health/live': 'GET', '/health/ready': 'GET', '/v1/tools': 'GET', '/v1/tools/call': 'POST'}
        if self.asset_uploads:
            from .asset_upload import ROUTE
            allowed[ROUTE] = 'POST'
        if self.enrollments is not None:
            allowed['/v1/enroll'] = 'POST'
        if self.purchases is not None:
            allowed.update({('/v1/purchases/'+action):'POST' for action in ('resource','status','handoff','receipt','receipt_status')})
        if self.apis is not None:
            from .api_gateway import ACTIONS
            allowed.update({('/v1/api/'+action):'POST' for action in ACTIONS})
            from .provider_authorization import ROUTE
            allowed[ROUTE]='POST'
        if path not in allowed:
            raise HTTPFailure(404, 'not_found', '入口不存在。')
        if method != allowed[path]:
            raise HTTPFailure(405, 'method_not_allowed', '此入口不支持该方法。',
                              [(b'allow', allowed[path].encode('ascii'))])
        if method == 'GET' and (self.header(headers, b'content-length') not in (None, b'0')
                                or self.header(headers, b'transfer-encoding') is not None):
            raise HTTPFailure(400, 'unexpected_body', 'GET入口不接受正文。')
        if path == '/health/live':
            return {'status': 'alive', 'api_version': '1'}
        if path == '/health/ready':
            return await self.worker(self.ready)
        if path == '/v1/enroll':
            self.rate('enrollment', self.limits.enrollment_per_minute)
            self.rate('enrollment-peer:' + peer, self.limits.enrollment_peer_per_minute)
            self.json_headers(headers)
            value = parse_json(await self.read_body(receive, headers, maximum=4096))
            return await self.worker(self.enroll, value)
        auth = self.header(headers, b'authorization')
        if auth is None or auth[:7].lower() != b'bearer ':
            raise MarketError('unauthenticated', '需要有效凭据。')
        try:
            token = auth[7:].decode('ascii')
        except UnicodeError:
            raise MarketError('unauthenticated', '需要有效凭据。') from None
        identity = await self.worker(self.gateway.identities.authenticate, token)
        self.rate('principal:' + identity['principal'], self.limits.principal_per_minute)
        if path == '/v1/tools':
            # Re-authentication immediately before the action observes revocation during body/queue waits.
            result = await self.worker(self.gateway.tool_list, token)
            if self.asset_uploads and result.get('role') == 'seller':
                from .asset_upload import describe
                result['asset_upload'] = describe()
            if self.apis is not None:
                api_description = await self.worker(self.apis.describe,token)
                if api_description is not None:
                    result['resource_api'] = api_description
                    result['instructions'] += ' API调用见resource_api；查询与取结果不会重发调用，未知状态保留原编号。'
                elif result.get('role')=='seller':
                    from .provider_authorization import ROUTE
                    result['provider_authorization']={'version':1,'route':ROUTE,'cache_allowed':False}
            if self.purchases is not None:
                purchase_api = await self.worker(self.purchases.describe,token)
                if purchase_api is not None:
                    result['purchase_api'] = purchase_api
                    result['instructions'] += (' 绑定的采购HTTP入口见purchase_api；402账单需交由获准钱包处理，'
                        '再携Payment-Proof请求原资源。202表示待处理，不能据此重新付款。')
            return result
        if self.asset_uploads and path == '/v1/seller/assets':
            from .asset_upload import MEDIA_TYPE, MAX_BODY, receive as receive_asset
            if identity['role'] != 'seller':
                raise HTTPFailure(403, 'forbidden', '仅卖方可以提交素材。')
            if self.header(headers, b'content-type', required=True).lower() != MEDIA_TYPE.encode('ascii'):
                raise HTTPFailure(415, 'unsupported_media_type', '需要素材二进制提交格式。')
            if self.header(headers, b'content-encoding') is not None:
                raise HTTPFailure(415, 'unsupported_encoding', '不接受压缩正文。')
            if self.header(headers, b'payment-proof') is not None:
                raise HTTPFailure(400, 'unexpected_payment_proof', '素材提交不接受付款凭证。')
            body = await self.read_body(receive, headers, maximum=MAX_BODY, binary_asset=True)
            return await self.worker(receive_asset, self.gateway, token, body)
        self.json_headers(headers)
        value = parse_json(await self.read_body(receive, headers))
        if path=='/v1/provider/authorize':
            if self.header(headers,b'payment-proof') is not None:
                raise HTTPFailure(400,'unexpected_payment_proof','供应商授权查询不接收付款凭证。')
            from .provider_authorization import authorize
            result=await self.worker(authorize,self.apis,token,value)
            return HTTPReply(200,result,[])
        if path.startswith('/v1/api/'):
            if self.header(headers,b'payment-proof') is not None:
                raise HTTPFailure(400,'unexpected_payment_proof','API调用入口不接收付款凭证。')
            result = await self.worker(self.apis.call,token,path.rsplit('/',1)[1],value)
            return HTTPReply(202 if result.get('status')=='reserved' else response_status(result),result,[])
        if path.startswith('/v1/purchases/'):
            proof = self.header(headers,b'payment-proof')
            accept = self.header(headers,b'accept')
            if accept not in (None,b'*/*',b'application/json',b'application/zip'):
                raise HTTPFailure(406,'unsupported_response','支持application/json或application/zip。')
            status,body,reply_headers = await self.worker(self.purchases.call,token,path.rsplit('/',1)[1],value,proof,
                                                         accept==b'application/zip')
            return HTTPReply(status,body,reply_headers)
        fields(value, ('tool', 'arguments'))
        return await self.worker(self.gateway.call, token, value['tool'], value['arguments'])

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self._handle(scope, receive, send)
        entered = self.active < self.limits.concurrent
        if entered:
            self.active += 1
        try:
            return await self._handle(scope, receive, send, admitted=entered)
        finally:
            if entered:
                self.active -= 1

    async def _handle(self, scope, receive, send, admitted=True):
        if scope['type'] == 'lifespan':
            while True:
                event = await receive()
                if event['type'] == 'lifespan.startup':
                    await send({'type': 'lifespan.startup.complete'})
                elif event['type'] == 'lifespan.shutdown':
                    await send({'type': 'lifespan.shutdown.complete'})
                    return
        if scope['type'] != 'http':
            raise RuntimeError('Only HTTP is supported')
        trace_id, extra_headers = uuid.uuid4().hex, []
        try:
            if not admitted:
                raise HTTPFailure(503, 'busy', '服务繁忙；本次未执行。', [(b'retry-after', b'1')])
            result = await self.route(scope, receive)
            if isinstance(result,HTTPReply):
                status,extra_headers,result = result.status,list(result.headers),result.body
            else:
                status = response_status(result)
        except HTTPFailure as exc:
            status, extra_headers = exc.status, list(exc.headers)
            result = {'status': 'error', 'error_code': exc.code, 'message': exc.message}
        except MarketError as exc:
            result = error_result(exc)
            status = 400 if exc.code in ('invalid_json', 'invalid_params','invalid_payment_header') else response_status(result)
        except (OSError, sqlite3.Error):
            status, result = 503, {'status': 'error', 'error_code': 'unavailable', 'message': '服务暂不可用；保留原操作编号核验。'}
        except Exception:
            # No exception text, request body, credentials or URL enters the response or logs.
            status, result = 500, {'status': 'error', 'error_code': 'internal_error', 'message': '服务异常；操作结果可能未知，请保留原编号核验。'}
        if status == 401:
            extra_headers.append((b'www-authenticate', b'Bearer realm="ai-supermarket"'))
        if status >= 400:
            extra_headers.append((b'connection', b'close'))
        payload = result if isinstance(result,bytes) else compact(result).encode('utf-8')
        response_headers = [(b'content-type', b'application/zip' if isinstance(result,bytes) else b'application/json; charset=utf-8'),
                            (b'content-length', str(len(payload)).encode('ascii')),
                            (b'cache-control', b'no-store'), (b'x-content-type-options', b'nosniff'),
                            (b'x-request-id', trace_id.encode('ascii')), *extra_headers]
        self.status_counts[status] += 1
        try:
            # Retain the admission slot until bounded transmission ends: binary payloads
            # must not accumulate behind arbitrarily many slow/disconnected clients.
            async with asyncio.timeout(15):
                await send({'type': 'http.response.start', 'status': status, 'headers': response_headers})
                await send({'type': 'http.response.body', 'body': payload})
        except (OSError, TimeoutError):
            return
