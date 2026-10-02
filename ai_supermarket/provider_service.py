"""本项目原创。配置式供应商JSON服务；不生成市场身份、不上架、不支付。"""
from copy import deepcopy
from contextlib import closing
import hashlib
import math
import re
import sqlite3

from .api_product import validate_api
from .assets import version_hash
from .api_quota import canonical
from .common import MarketError, compact, fields, identifier, integer, parse_json, text
from .exchange import _release_manifest_digest, validate_listing
from .http_app import HTTPFailure, HTTPReply, MarketHTTP
from .http_client import RemoteMarket
from .origin import _safe_path
from .provider_authorization import RemoteProviderAuthorization
from .provider_json import ProviderJsonQuery
from .provider_operations import FORMAT as OPERATIONS_FORMAT, ProviderOperations
from .purchase_download import digest

FORMAT = 'market-json-provider-service-v1'
FILES = {'provider.json': 65536, 'seller-credential.json': 4096,
         'provider-key.json': 9000, 'backend-headers.json': 9000}
PRIVATE_FORMAT = 'market-json-provider-binding-v2'
PRIVATE_FILES = {**FILES, 'backend-query.json': 9000}
TYPES = {'object': (dict,), 'array': (list,), 'string': (str,), 'integer': (int,),
         'number': (int, float), 'boolean': (bool,), 'null': (type(None),)}


class ResultChecks:
    """Bounded field paths and comparisons only; no expressions or code loading."""
    def __init__(self, rules, api):
        if not isinstance(rules, list) or not 1 <= len(rules) <= 32:
            raise MarketError('invalid_configuration', '需要1至32条实际业务结果检查。')
        canonical(rules, 16384)
        names = {row['name'] for row in api['input_fields']}
        for rule in rules:
            fields(rule, ('path', 'type'), ('equals', 'input', 'minimum', 'maximum'))
            if (not isinstance(rule['path'], list) or len(rule['path']) > 8
                    or not isinstance(rule['type'], str) or rule['type'] not in TYPES
                    or any(not (type(p) is int and 0 <= p <= 1000 or type(p) is str and 1 <= len(p) <= 128)
                           for p in rule['path'])):
                raise MarketError('invalid_configuration', '结果路径或类型无效。')
            if 'input' in rule and (not isinstance(rule['input'], str) or rule['input'] not in names or 'equals' in rule):
                raise MarketError('invalid_configuration', '结果只能匹配一个已声明输入，不能同时使用equals。')
            if 'equals' in rule and type(rule['equals']) not in TYPES[rule['type']]:
                raise MarketError('invalid_configuration', '固定比较值与声明类型不符。')
            for key in ('minimum', 'maximum'):
                if key in rule and (rule['type'] not in ('integer', 'number')
                        or type(rule[key]) not in (int, float)
                        or type(rule[key]) is float and not math.isfinite(rule[key])):
                    raise MarketError('invalid_configuration', '数值范围检查无效。')
            if 'minimum' in rule and 'maximum' in rule and rule['minimum'] > rule['maximum']:
                raise MarketError('invalid_configuration', '结果范围上下限颠倒。')
        self._rules = deepcopy(rules)

    def __call__(self, result, inputs):
        for rule in self._rules:
            value = result
            for part in rule['path']:
                if type(part) is int:
                    if not isinstance(value, list) or part >= len(value): return False
                elif not isinstance(value, dict) or part not in value: return False
                value = value[part]
            if type(value) not in TYPES[rule['type']]: return False
            if 'equals' in rule and canonical(value, 65536) != canonical(rule['equals'], 65536): return False
            if 'input' in rule and (rule['input'] not in inputs
                    or canonical(value, 65536) != canonical(inputs[rule['input']], 65536)): return False
            if 'minimum' in rule and value < rule['minimum']: return False
            if 'maximum' in rule and value > rule['maximum']: return False
        return True


def _read(path, maximum):
    path = _safe_path(path)
    with path.open('rb') as handle: raw = handle.read(maximum + 1)
    if not 0 < len(raw) <= maximum:
        raise MarketError('invalid_configuration', '供应商文件为空或超过上限。')
    return raw


def _sha(raw): return hashlib.sha256(raw).hexdigest()


def configure(remote, seller, resource_id, version, deployment_file, output):
    """Read one authenticated approved release; write only a new local candidate.

    No market mutation, provider credential copying, backend call or startup.
    The server's state is an observation, not a continuing authorization.
    """
    if type(remote) is not RemoteMarket:
        raise MarketError('invalid_client', '需要固定市场连接。')
    identifier(seller, 'seller'); identifier(resource_id, 'id'); identifier(version, 'version')
    if not resource_id.startswith(seller + ':'):
        raise MarketError('seller_mismatch', '只能为自己的指定商品生成配置。')
    output = _safe_path(output)
    if output.exists(): raise MarketError('state_conflict', '配置输出目录已存在，不覆盖或重新取最新发行。')
    deployment = parse_json(_read(deployment_file, 32768))
    fields(deployment, ('binding_id', 'listen_port', 'backend', 'result_checks'))
    identifier(deployment['binding_id'], 'binding_id'); integer(deployment['listen_port'], 1024, 65535, 'listen_port')
    original_key = remote.credential().split('.')[0]
    original_url = remote.base_url
    def unchanged():
        if remote.base_url != original_url or remote.credential().split('.')[0] != original_key:
            raise MarketError('connection_changed', '生成期间的市场连接或卖方身份发生变化。')
    discovery = remote.tool_list(); unchanged()
    if (discovery.get('role') != 'seller' or discovery.get('seller_principal') != seller
            or discovery.get('provider_authorization') != {'version': 1, 'route': '/v1/provider/authorize', 'cache_allowed': False}):
        raise MarketError('provider_connection_unverified', '当前卖方身份或原操作授权路由不符。')
    observed = remote.call('seller_status', {'id': resource_id, 'version': version}); unchanged()
    if observed.get('status') == 'error':
        raise MarketError('provider_release_unverified', '指定商品状态未核实，未生成配置。')
    fields(observed, ('status', 'id', 'version', 'revision', 'review_note', 'metadata', 'content_hash', 'content', 'offer'))
    if observed['id'] != resource_id or observed['version'] != version or observed['status'] != 'approved':
        raise MarketError('provider_release_unverified', '指定商品版本尚未审核通过。')
    integer(observed['revision'], 1, 1000000, 'revision')
    validate_listing(observed['metadata'], observed['content']); digest(observed['content_hash'])
    if (observed['metadata']['kind'] != 'api'
            or version_hash(observed['metadata'], observed['content']) != observed['content_hash']):
        raise MarketError('provider_release_unverified', '需要API商品及一致的原版本摘要。')
    offer = observed['offer']; fields(offer, ('price_cents', 'revision', 'availability'))
    integer(offer['price_cents'], 0, 100000000, 'price_cents'); integer(offer['revision'], 1, 1000000, 'offer_revision')
    if offer['availability'] != 'active':
        raise MarketError('provider_release_unverified', '指定商品当前不可售。')
    release = _release_manifest_digest(resource_id, version, observed['content_hash'], observed['metadata'])
    config = {'format': FORMAT, 'market_url': original_url, 'seller': seller, 'key_id': original_key,
              'provider': observed['metadata']['api']['provider'],
              'release': {'id': resource_id, 'version': version, 'release_digest': release},
              'api': observed['metadata']['api'], **deployment}
    _configuration(canonical(config, 65536).encode(), {})
    raw = canonical(config, 65536).encode()
    canonical(observed, 131072)  # Bound/validate without reordering the saved metadata.
    # Existing API version_hash includes metadata key order. Preserve the server's
    # order so this observation can be rechecked using the original algorithm.
    observation = compact(observed).encode()
    receipt = {'status': 'configured_not_started', 'id': resource_id, 'version': version,
               'release_digest': release, 'config_sha256': _sha(raw), 'observation_sha256': _sha(observation),
               'market_url': original_url, 'seller': seller, 'key_id': original_key,
               'network_requests': 2, 'market_mutations': 0, 'backend_called': False,
               'server_started': False, 'current_state_rechecked': False}
    # Finish all validation before mkdir. Partial filesystem failure is left in
    # place for inspection; never overwrite or silently refresh a saved release.
    output.mkdir(parents=True, exist_ok=False)
    for name, value in (('provider.json', raw), ('observation.json', observation),
                        ('receipt.json', canonical(receipt, 4096).encode())):
        with (output / name).open('xb') as handle: handle.write(value)
    return receipt


def _configuration(raw, headers, private_query=None):
    config = parse_json(raw)
    fields(config, ('format', 'market_url', 'provider', 'binding_id', 'seller', 'key_id',
                    'listen_port', 'release', 'api', 'backend', 'result_checks'))
    if config['format'] != FORMAT: raise MarketError('invalid_configuration', '供应商配置版本不符。')
    text(config['market_url'], 'market_url', 2048)
    for name in ('provider', 'binding_id', 'seller'): identifier(config[name], name)
    if not isinstance(config['key_id'], str) or not re.fullmatch(r'ak_[0-9a-f]{32}', config['key_id']):
        raise MarketError('invalid_configuration', '需要原卖方凭据编号。')
    integer(config['listen_port'], 1024, 65535, 'listen_port')
    release = config['release']; fields(release, ('id', 'version', 'release_digest'))
    identifier(release['id'], 'id'); identifier(release['version'], 'version'); digest(release['release_digest'])
    if not release['id'].startswith(config['seller'] + ':'):
        raise MarketError('invalid_configuration', '商品必须属于配置的原卖方。')
    validate_api(config['api'])
    if config['api']['provider'] != config['provider']:
        raise MarketError('invalid_configuration', '商品API提供方不符。')
    backend = config['backend']; fields(backend, ('endpoint', 'query_map', 'fixed_query', 'timeout'))
    check = ResultChecks(config['result_checks'], config['api'])
    adapter = ProviderJsonQuery(config['api'], backend['endpoint'], backend['query_map'],
        result_check=check, fixed_query=backend['fixed_query'], headers=headers,
        timeout=backend['timeout'], enabled=True, private_query=private_query)
    return config, adapter


def _components(directory, raw):
    headers = parse_json(raw['backend-headers.json'])
    private_query = parse_json(raw['backend-query.json']) if 'backend-query.json' in raw else None
    config, backend = _configuration(raw['provider.json'], headers, private_query)
    private = parse_json(raw['provider-key.json']); fields(private, ('key_hex',))
    if not isinstance(private['key_hex'], str) or not re.fullmatch(r'[0-9a-fA-F]{64,8192}', private['key_hex']):
        raise MarketError('invalid_configuration', '供应商连接凭据格式无效。')
    try: key = bytes.fromhex(private['key_hex'])
    except ValueError: raise MarketError('invalid_configuration', '供应商连接凭据格式无效。') from None
    remote = RemoteMarket(config['market_url'], directory / 'seller-credential.json')
    credential = parse_json(raw['seller-credential.json']); fields(credential, ('token',))
    token = credential['token']
    if (not isinstance(token, str) or not re.fullmatch(r'ak_[0-9a-f]{32}\.[A-Za-z0-9_-]{43}', token)
            or token.split('.')[0] != config['key_id']):
        raise MarketError('connection_changed', '配置与原卖方凭据编号不符。')
    return config, backend, remote, key


def prepare(directory, config_file, credential_file, key_file, headers_file=None, *, query_file=None):
    directory = _safe_path(directory)
    raw = {'provider.json': _read(config_file, FILES['provider.json']),
           'seller-credential.json': _read(credential_file, FILES['seller-credential.json']),
           'provider-key.json': _read(key_file, FILES['provider-key.json']),
           'backend-headers.json': b'{}' if headers_file is None else _read(headers_file, FILES['backend-headers.json'])}
    if query_file is not None:
        raw['backend-query.json'] = _read(query_file, PRIVATE_FILES['backend-query.json'])
    config, backend, _, key = _components(directory, raw)
    # Validate every source before creating a new private directory. Never reset
    # an existing operation ledger or generate a replacement market identity.
    directory.mkdir(parents=True, exist_ok=False)
    for name, value in raw.items():
        with (directory / name).open('xb') as handle: handle.write(value)
    ProviderOperations(directory / 'operations.sqlite', config['provider'], config['binding_id'], key,
                       lambda request: False, backend.execute)
    marker = {'format': PRIVATE_FORMAT if query_file is not None else FORMAT,
              'files': {name: _sha(value) for name, value in raw.items()}}
    with (directory / 'binding.json').open('xb') as handle: handle.write(canonical(marker, 4096).encode())
    return {'status': 'prepared', 'network_requests': 0, 'server_started': False,
            'existing_identity_reused': True, 'listing_created': False}


def load(directory):
    directory = _safe_path(directory)
    marker = parse_json(_read(directory / 'binding.json', 4096)); fields(marker, ('format', 'files'))
    if marker['format'] == FORMAT: selected = FILES
    elif marker['format'] == PRIVATE_FORMAT: selected = PRIVATE_FILES
    else: raise MarketError('connection_changed', '供应商绑定格式不可识别。')
    if not isinstance(marker['files'], dict) or set(marker['files']) != set(selected):
        raise MarketError('connection_changed', '供应商绑定文件清单不符。')
    # Never treat a lost private file as anonymous access or upgrade a running
    # v1 directory merely because a new file appeared beside it.
    if selected is FILES and (directory / 'backend-query.json').exists():
        raise MarketError('connection_changed', '旧绑定旁出现未绑定的私有认证文件。')
    raw = {name: _read(directory / name, maximum) for name, maximum in selected.items()}
    expected = {'format': marker['format'], 'files': {name: _sha(value) for name, value in raw.items()}}
    if marker != expected: raise MarketError('connection_changed', '原配置或凭据文件发生变化，保留原操作库核查。')
    config, backend, remote, key = _components(directory, raw)
    path = _safe_path(directory / 'operations.sqlite')
    if not path.is_file(): raise MarketError('provider_incomplete', '原操作库缺失，不能重新初始化。')
    with path.open('rb') as handle:
        if handle.read(16) != b'SQLite format 3\x00':
            raise MarketError('provider_incomplete', '原操作库为空或不可识别。')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        if ({row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                != {'configuration', 'operations'}
                or list(db.execute('SELECT format,provider,binding_id FROM configuration'))
                != [(OPERATIONS_FORMAT, config['provider'], config['binding_id'])]):
            raise MarketError('ledger_mismatch', '原操作库与供应商配置不符。')
    return config, backend, remote, key, expected


def probe(directory):
    config, _, remote, _, _ = load(directory)
    reply = remote.tool_list()
    if (reply.get('role') != 'seller' or reply.get('seller_principal') != config['seller']
            or reply.get('provider_authorization') != {'version': 1, 'route': '/v1/provider/authorize', 'cache_allowed': False}):
        raise MarketError('provider_connection_unverified', '原卖方身份或市场授权接口未核验。')
    return {'status': 'connected', 'scope': 'seller_identity_and_route_only', 'operation_authorized': False,
            'backend_called': False, 'listing_created': False}


def application(directory):
    directory = _safe_path(directory)
    config, backend, remote, key, expected = load(directory)
    release = config['release']
    original = RemoteProviderAuthorization(remote, config['provider'], config['binding_id'],
        {(release['id'], release['version']): release['release_digest']})
    def authorize(request):
        # Check the frozen files during running service too. Cached results must
        # not bypass changed credentials, endpoint, checks or release scope.
        if (expected['format'] == FORMAT and (directory / 'backend-query.json').exists()
                or any(_sha(_read(directory / name, PRIVATE_FILES[name])) != value
                       for name, value in expected['files'].items())):
            raise MarketError('connection_changed', '在用供应商配置或凭据已变化。')
        return original(request)
    operations = ProviderOperations(directory / 'operations.sqlite', config['provider'], config['binding_id'],
                                    key, authorize, backend.execute, require_existing=True)
    return ProviderApp(operations, config['listen_port']), config


class ProviderApp(MarketHTTP):
    """Loopback ASGI ingress for the existing signed provider protocol."""
    def __init__(self, operations, port):
        self._configure_transport([f'127.0.0.1:{port}'])
        self.operations = operations

    async def __call__(self, scope, receive, send):
        async def signed_json(message):
            if (message['type'] == 'http.response.start'
                    and any(key == b'x-market-response-signature' for key, _ in message['headers'])):
                message = {**message, 'headers': [(key, b'application/json' if key == b'content-type' else value)
                                                for key, value in message['headers']]}
            await send(message)
        await super().__call__(scope, receive, signed_json)

    async def route(self, scope, receive):
        headers = scope.get('headers', [])
        if sum(len(k) + len(v) for k, v in headers) > 16384: raise HTTPFailure(431, 'invalid_headers', '请求头超限。')
        if self.header(headers, b'host', True).decode('latin-1') not in self.hosts:
            raise HTTPFailure(400, 'invalid_host', 'Host不符。')
        if (scope.get('client') or ('',))[0] != '127.0.0.1' or self.header(headers, b'origin') is not None:
            raise HTTPFailure(403, 'forbidden', '仅允许本机机器入口。')
        if scope.get('query_string') or scope.get('path') != '/api': raise HTTPFailure(404, 'not_found', '入口不存在。')
        if scope.get('method') != 'POST': raise HTTPFailure(405, 'method_not_allowed', '需要POST。')
        self.rate('global', self.limits.global_per_minute); self.json_headers(headers)
        signature = self.header(headers, b'x-market-signature', True).decode('ascii')
        raw = await self.read_body(receive, headers, maximum=20480)
        body, signed = await self.worker(self.operations.handle, raw, signature)
        return HTTPReply(200, body, [(b'x-market-response-signature', signed.encode('ascii'))])
