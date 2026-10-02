"""本项目原创。已授权采购组件的本地配置接线；不调用钱包或授予新购买权限。"""
import argparse
import hashlib
from pathlib import Path
import re
import sqlite3

from .alipay_proof import AlipayProofVerifier
from .alipay_transport import AlipayVerificationClient, RSA2RequestSigner
from .api_connections import _check_task, _existing_databases, _path
from .common import MarketError, choice, compact, fields, identifier, parse_json, text
from .exchange import Exchange
from .http_app import MarketHTTP
from .machine_pay import MachinePayGateway
from .origin import OriginClient, _safe_path
from .payment_bills import PurchaseBilling, _mandate_key
from .payment_fulfillment import PaymentFulfillment
from .payment_orders import PaymentOrders, _get
from .purchase_download import _read, _save
from .purchase_host import PurchasingTaskResources
from .recovery import readiness
from .task_budget_gateway import TaskBudgetGateway

FORMAT = 'market-purchase-connections-v1'
MULTI_FORMAT = 'market-purchase-connections-v2'
MAX_CONFIG = 131072


def _configuration(raw, base):
    value = parse_json(raw)
    fields(value, ('format', 'database', 'identities', 'budget', 'channels', 'origins', 'tasks'))
    if value['format'] not in (FORMAT, MULTI_FORMAT): raise MarketError('invalid_configuration', '采购配置格式不符。')
    for name in ('database', 'identities', 'budget'): value[name] = str(_path(base, value[name]))
    if len({value[n].casefold() for n in ('database', 'identities', 'budget')}) != 3:
        raise MarketError('invalid_configuration', '采购需要三个独立的原账本。')
    for name, minimum, maximum in (('channels', 1, 32), ('origins', 0, 32), ('tasks', 1, 128)):
        if not isinstance(value[name], list) or not minimum <= len(value[name]) <= maximum:
            raise MarketError('invalid_configuration', '采购连接数量或类型无效。')
    channels = set(); origins = set(); keys = set(); routes = set(); shared = {}
    for channel in value['channels']:
        fields(channel, ('id', 'policy', 'seller_name', 'signing_key_file', 'verification_key_file',
                         'billing_enabled', 'verification_enabled'))
        identifier(channel['id'], 'channel_id'); text(channel['seller_name'], 'seller_name', 100)
        if channel['id'] in channels: raise MarketError('invalid_configuration', '渠道编号重复。')
        channels.add(channel['id'])
        for name in ('billing_enabled', 'verification_enabled'):
            if type(channel[name]) is not bool: raise MarketError('invalid_configuration', '启用标记必须明确为布尔值。')
        for name in ('signing_key_file', 'verification_key_file'): channel[name] = str(_path(base, channel[name]))
        if not isinstance(channel['policy'], dict): raise MarketError('invalid_configuration', '渠道策略无效。')
        fields(channel['policy'], ('merchant', 'qualification_reference', 'signing_public_key_sha256',
            'verification_public_key_sha256', 'amount_unit', 'policy_reference', 'max_evidence_age_seconds'))
        merchant = channel['policy'].get('merchant')
        fields(merchant, ('market_id', 'market_seller', 'seller_id', 'app_id', 'service_id', 'environment'))
        for name, field in merchant.items(): identifier(field, name)
        choice(merchant['environment'], ('sandbox', 'production'), 'environment')
    for origin in value['origins']:
        fields(origin, ('provider', 'endpoint', 'binding_id', 'key_file'))
        identifier(origin['provider'], 'provider'); identifier(origin['binding_id'], 'binding_id')
        if origin['provider'] in origins: raise MarketError('invalid_configuration', '原站供应商重复。')
        origins.add(origin['provider']); origin['key_file'] = str(_path(base, origin['key_file']))
    for task in value['tasks']:
        fields(task, ('key_id', 'principal', 'root_id', 'task_id', 'purpose', 'mandate_id', 'channel_id', 'goal', 'origins'))
        if not isinstance(task['key_id'], str) or not re.fullmatch('ak_[0-9a-f]{32}', task['key_id']):
            raise MarketError('invalid_configuration', '任务需要凭据编号，不接受Token。')
        for name in ('principal', 'root_id', 'task_id', 'mandate_id', 'channel_id'): identifier(task[name], name)
        text(task['goal'], 'goal', 1000); choice(task['purpose'], ('personal_use', 'commercial_use'), 'purpose')
        if task['channel_id'] not in channels or value['format'] == FORMAT and task['key_id'] in keys:
            raise MarketError('invalid_configuration', '渠道缺失或v1重复绑定凭据；多卖家需显式v2。')
        merchant = next(c['policy']['merchant'] for c in value['channels'] if c['id'] == task['channel_id'])
        route = (task['key_id'], merchant['market_seller'])
        common = [task[n] for n in ('principal', 'root_id', 'task_id', 'purpose', 'goal')] + [merchant['market_id'], merchant['environment']]
        if route in routes or shared.setdefault(task['key_id'], common) != common:
            raise MarketError('invalid_configuration', '同一凭据的卖家不得重复，所有路线须共用原任务、目标、用途和市场。')
        routes.add(route)
        keys.add(task['key_id'])
        if not isinstance(task['origins'], list): raise MarketError('invalid_configuration', '原站列表无效。')
        for provider in task['origins']: identifier(provider, 'provider')
        if len(task['origins']) != len(set(task['origins'])) or not set(task['origins']) <= origins:
            raise MarketError('invalid_configuration', '原站列表重复或未配置。')
    return value


def _fingerprints(config, raw):
    result = {'connections.json': hashlib.sha256(raw).hexdigest()}
    for channel in config['channels']:
        for field in ('signing_key_file', 'verification_key_file'):
            result['channel:' + channel['id'] + ':' + field] = hashlib.sha256(_read(Path(channel[field]), 32768)).hexdigest()
    for origin in config['origins']:
        result['origin:' + origin['provider']] = hashlib.sha256(_read(Path(origin['key_file']), 10000)).hexdigest()
    return result


def _origin(row):
    value = parse_json(_read(Path(row['key_file']), 10000)); fields(value, ('key_hex',))
    key = value['key_hex']
    if not isinstance(key, str) or not re.fullmatch('[0-9a-fA-F]{64,8192}', key) or len(key) % 2:
        raise MarketError('invalid_configuration', '原站凭据文件无效。')
    return OriginClient(row['provider'], row['endpoint'], bytes.fromhex(key), row['binding_id'])


def _app(config):
    _existing_databases(config)
    if readiness(config['database'], config['identities'], config['budget']) != {'status': 'ready'}:
        raise MarketError('not_ready', '原采购账本尚未就绪，不初始化或放行。')
    return MarketHTTP(config['database'], config['identities'], allowed_hosts=['127.0.0.1:8765'],
                      budget_database=config['budget'], enforce_task_budget=True)


def _components(app, config):
    origins = {row['provider']: _origin(row) for row in config['origins']}
    channels = {}
    for row in config['channels']:
        signer = RSA2RequestSigner(_read(Path(row['signing_key_file']), 32768))
        verifier = AlipayProofVerifier(_read(Path(row['verification_key_file']), 32768), row['policy']['merchant'],
            amount_unit=row['policy']['amount_unit'], policy_reference=row['policy']['policy_reference'])
        if verifier.key_sha256 != row['policy']['verification_public_key_sha256']:
            raise MarketError('payment_policy_mismatch', '验签文件与原渠道策略不符。')
        channels[row['id']] = row, signer, verifier
    prepared = []
    for task in config['tasks']:
        identity = _check_task(app, task); row, signer, verifier = channels[task['channel_id']]
        market = Exchange(config['database'], task['principal'], host_policy=identity['policy'], clock=app.gateway.clock)
        orders = PaymentOrders(market, row['policy']['merchant'])
        budget = app.gateway.budgets.scope(task['root_id'], task['task_id'], task['principal'])
        billing = PurchaseBilling(orders, budget, signer, row['policy'], seller_name=row['seller_name'], enabled=row['billing_enabled'])
        fulfillment = PaymentFulfillment(orders, row['policy'], enabled=row['billing_enabled'])
        verification = AlipayVerificationClient(orders, verifier, signer, network_enabled=row['verification_enabled'], clock=market.clock)
        with market.store.connect() as db:
            if _get(db, 'payment_environment') != row['policy']['merchant']['environment']:
                raise MarketError('payment_environment_mismatch', '配置与原支付环境不符，不自动绑定。')
            mandate = _get(db, _mandate_key(task['principal'], task['mandate_id']))
            if (not isinstance(mandate, dict) or mandate.get('state') != 'active'
                    or mandate['terms']['scope'] != billing._scope
                    or mandate['terms']['expires_at'] <= market.clock()
                    or task['purpose'] not in mandate['terms']['allowed_purposes']
                    or row['policy']['merchant']['market_seller'] not in mandate['terms']['allowed_sellers']):
                raise MarketError('purchase_not_authorized', '需要同一原任务、卖家、用途且有效的购买委托；配置不授予权限。')
        prepared.append((task, billing, fulfillment, verification,
                         {provider: origins[provider] for provider in task['origins']}))
    return prepared


def prepare(directory, config_file):
    directory = _safe_path(directory); config_file = _safe_path(config_file)
    if directory.exists(): raise MarketError('output_exists', '采购连接目录必须是新目录，不覆盖原配置或交付。')
    config = _configuration(_read(config_file, MAX_CONFIG), config_file.parent)
    raw = compact(config).encode('utf-8'); fingerprints = _fingerprints(config, raw)
    _components(_app(config), config)  # No host construction, task search, bill, or network.
    if fingerprints != _fingerprints(config, raw): raise MarketError('connection_changed', '准备期间文件变化。')
    directory.mkdir(parents=True, exist_ok=False)
    for task in config['tasks']: (directory / 'results' / task['key_id']).mkdir(parents=True, exist_ok=True)
    _save(directory / 'connections.json', raw)
    _save(directory / 'binding.json', compact({'format': config['format'], 'files': fingerprints}).encode('utf-8'))
    return {'status': 'prepared', 'tasks': len({t['key_id'] for t in config['tasks']}), 'channels': len(config['channels']),
            'credentials_copied': False, 'new_authorizations': 0, 'bills_created': 0, 'network_calls': 0}


def load(directory):
    directory = _safe_path(directory); raw = _read(directory / 'connections.json', MAX_CONFIG)
    config = _configuration(raw, directory)
    binding = parse_json(_read(directory / 'binding.json', MAX_CONFIG))
    if binding != {'format': config['format'], 'files': _fingerprints(config, raw)}:
        raise MarketError('connection_changed', '原采购配置或密钥文件变化，停止并核对。')
    for task in config['tasks']:
        path = _safe_path(directory / 'results' / task['key_id'])
        if not path.is_dir(): raise MarketError('not_found', '缺少原交付目录，不重新初始化。')
    return config, binding


def check(directory):
    config, _ = load(directory); _components(_app(config), config)
    return {'status': 'checked', 'scope': 'local_configuration_only', 'tasks': len({t['key_id'] for t in config['tasks']}),
            'new_authorizations': 0, 'bills_created': 0, 'network_calls': 0, 'wallet_execution_available': False}


def attach(app, directory):
    if type(app) is not MarketHTTP or type(app.gateway) is not TaskBudgetGateway or app.purchases is not None:
        raise MarketError('purchase_host_mismatch', '采购连接需要尚未接线的任务预算市场。')
    directory = _safe_path(directory); config, binding = load(directory)
    for name, actual in (('database', app.gateway.database), ('identities', app.gateway.identities.store.path),
                         ('budget', app.gateway.budgets.store.path)):
        if Path(config[name]).resolve() != Path(actual).resolve():
            raise MarketError('purchase_host_mismatch', '连接属于其他原账本，不改绑。')
    _existing_databases(config)
    if readiness(config['database'], config['identities'], config['budget']) != {'status': 'ready'}:
        raise MarketError('not_ready', '原采购账本未就绪。')
    prepared = _components(app, config)
    if app.apis is not None:
        for task in config['tasks']:
            for host in app.apis.hosts.get(task['key_id'], {}).values():
                if host.purpose != task['purpose']:
                    raise MarketError('purchase_host_mismatch', '同一任务的采购与API用途不一致。')
    def unchanged():
        # Compare to the attached snapshot even if both disk files are replaced.
        current, current_binding = load(directory)
        if current != config or current_binding != binding:
            raise MarketError('connection_changed', '运行中的采购连接不能替换授权或密钥。')
    unchanged(); groups = {}
    for task, billing, fulfillment, verification, origins in prepared:
        group = groups.setdefault(task['key_id'], {})
        group[billing.orders.merchant['market_seller']] = PurchasingTaskResources(billing, fulfillment, verification, task['goal'],
            mandate_id=task['mandate_id'], purpose=task['purpose'], origin_clients=origins,
            output_root=directory / 'results' / task['key_id'])
    hosts = {key: next(iter(group.values())) if len(group) == 1 else group for key, group in groups.items()}
    unchanged()
    app.purchases = MachinePayGateway(app.gateway, hosts, configuration_check=unchanged)
    return {'status': 'attached', 'tasks': len(hosts), 'wallet_execution_available': False, 'network_calls': 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description='已授权采购宿主的本地配置；不发授权、不付款')
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('prepare'); p.add_argument('--config', type=Path, required=True); p.add_argument('--directory', type=Path, required=True)
    p = commands.add_parser('check'); p.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = prepare(args.directory, args.config) if args.command == 'prepare' else check(args.directory)
        print(compact(result)); return 0
    except MarketError as exc: code = exc.code
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError, ImportError): code = 'purchase_connection_unavailable'
    print(compact({'status': 'error', 'error_code': code, 'message': '核对原授权、账本、配置与本地支付依赖；未发送账单或请求渠道。'})); return 1


if __name__ == '__main__': raise SystemExit(main())
