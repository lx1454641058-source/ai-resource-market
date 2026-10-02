"""本项目原创。可信运营配置到市场API宿主；不发凭据、不授权任务、不请求后端。"""
import argparse
from contextlib import closing
import hashlib
from pathlib import Path
import re
import sqlite3
import sys

from .api_delivery import ApiDeliveryHost
from .api_gateway import ApiGateway
from .api_provider import ApiProviderClient
from .api_quota import ProviderQuota, canonical
from .common import MarketError, fields, identifier, parse_json
from .contracts import HostPolicy
from .exchange import Exchange
from .http_app import MarketHTTP
from .origin import _safe_path
from .provider_authorization import _active_identity
from .purchase_download import _read, _save
from .recovery import connect_existing, validate_database
from .task_budget_gateway import TaskBudgetGateway

FORMAT = 'market-api-connections-v1'
MAX_CONFIG = 65536


def _path(base, value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise MarketError('invalid_configuration', '连接文件路径无效。')
    return _safe_path(base / value)


def _configuration(raw, base):
    value = parse_json(raw)
    fields(value, ('format', 'database', 'identities', 'budget', 'providers'))
    if value['format'] != FORMAT or not isinstance(value['providers'], list) or not 1 <= len(value['providers']) <= 32:
        raise MarketError('invalid_configuration', '需要有界的市场API连接配置。')
    for name in ('database', 'identities', 'budget'):
        value[name] = str(_path(base, value[name]))
    if len({value[name] for name in ('database', 'identities', 'budget')}) != 3:
        raise MarketError('invalid_configuration', '市场、身份及预算需要独立原账本。')
    providers = set(); task_count = 0; purposes = {}
    for row in value['providers']:
        fields(row, ('provider', 'binding_id', 'endpoint', 'connection_key_file', 'operations', 'tasks'))
        provider = identifier(row['provider'], 'provider'); identifier(row['binding_id'], 'binding_id')
        if provider in providers:
            raise MarketError('invalid_configuration', '同一提供方必须共用一份连接与次数账本。')
        providers.add(provider)
        row['connection_key_file'] = str(_path(base, row['connection_key_file']))
        if not isinstance(row['tasks'], list) or not row['tasks']:
            raise MarketError('invalid_configuration', '连接缺少明确任务范围。')
        task_count += len(row['tasks'])
        if task_count > 128:
            raise MarketError('invalid_configuration', '任务连接总数超过128项。')
        keys = set()
        for task in row['tasks']:
            fields(task, ('key_id', 'principal', 'root_id', 'task_id', 'purpose'))
            if not isinstance(task['key_id'], str) or not re.fullmatch('ak_[0-9a-f]{32}', task['key_id']):
                raise MarketError('invalid_configuration', '需要凭据编号，不接受Token。')
            for name in ('principal', 'root_id', 'task_id'):
                identifier(task[name], name)
            if task['key_id'] in keys or task['purpose'] not in ('personal_use', 'commercial_use'):
                raise MarketError('invalid_configuration', '任务连接重复或用途无效。')
            keys.add(task['key_id'])
            if purposes.setdefault(task['key_id'], task['purpose']) != task['purpose']:
                raise MarketError('invalid_configuration', '同一任务的提供方必须使用相同用途。')
    return value


def _client(row, raw=None):
    private = parse_json(_read(Path(row['connection_key_file']), 10000) if raw is None else raw)
    fields(private, ('key_hex',))
    if (not isinstance(private['key_hex'], str) or not re.fullmatch('[0-9a-fA-F]{64,8192}', private['key_hex'])
            or len(private['key_hex']) % 2):
        raise MarketError('invalid_configuration', '连接密钥文件格式无效。')
    return ApiProviderClient(row['provider'], row['endpoint'], bytes.fromhex(private['key_hex']),
                             row['binding_id'], row['operations'])


def _check_task(app, task):
    # This is trusted local configuration validation, not authentication by key id.
    identity = _active_identity(app, task['key_id'])
    with app.gateway.budgets.store.connect() as db:
        binding = app.gateway._binding(db, identity)
        if any(binding[k] != task[k] for k in ('root_id', 'task_id', 'principal')):
            raise MarketError('api_host_mismatch', '配置与原任务授权不符，不改绑。')
        app.gateway.budgets._task(db, binding['root_id'], binding['task_id'], identity['principal'], active=True)
    return identity


def _existing_databases(config):
    # Avoid implicitly creating any missing/empty original dependency during prepare/check.
    for name, kind in (('database', 'market'), ('identities', 'identities'), ('budget', 'budget')):
        with closing(connect_existing(config[name])) as db:
            validate_database(db, kind, deep=False)


def _app(config):
    _existing_databases(config)
    return MarketHTTP(config['database'], config['identities'], allowed_hosts=['127.0.0.1:8765'],
                      budget_database=config['budget'], enforce_task_budget=True)


def _fingerprints(directory, count):
    names = ['connections.json'] + [f'provider-{i}-key.json' for i in range(count)]
    return {name: hashlib.sha256(_read(directory / name, MAX_CONFIG)).hexdigest() for name in names}


def prepare(directory, config_file):
    directory = _safe_path(directory)
    if directory.exists():
        raise MarketError('output_exists', '准备目录已存在，不刷新连接或重建账本。')
    config_file = _safe_path(config_file)
    config = _configuration(_read(config_file, MAX_CONFIG), config_file.parent)
    app = _app(config)
    # Validate the complete plan before creating any output. No provider IO here.
    private_files = []
    for row in config['providers']:
        raw = _read(Path(row['connection_key_file']), 10000)
        _client(row, raw)
        private_files.append(raw)
        for task in row['tasks']:
            _check_task(app, task)
    directory.mkdir(parents=True, exist_ok=False)
    for i, row in enumerate(config['providers']):
        raw = private_files[i]
        row['connection_key_file'] = f'provider-{i}-key.json'
        _save(directory / row['connection_key_file'], raw)
        ProviderQuota(directory / f'provider-{i}-quota.sqlite', row['provider'], lambda principal, request: False)
    _save(directory / 'connections.json', canonical(config, MAX_CONFIG).encode())
    _save(directory / 'binding.json', canonical({'format': FORMAT, 'files': _fingerprints(directory, len(config['providers']))}, MAX_CONFIG).encode())
    return {'status': 'prepared', 'providers': len(config['providers']),
            'task_connections': sum(len(row['tasks']) for row in config['providers']),
            'server_started': False, 'backend_calls': 0, 'payment_enabled': False}


def load(directory):
    directory = _safe_path(directory)
    config = _configuration(_read(directory / 'connections.json', MAX_CONFIG), directory)
    binding = parse_json(_read(directory / 'binding.json', MAX_CONFIG))
    fields(binding, ('format', 'files'))
    if binding != {'format': FORMAT, 'files': _fingerprints(directory, len(config['providers']))}:
        raise MarketError('connection_changed', '连接配置或密钥变化，停止调用并核查原文件。')
    for i, row in enumerate(config['providers']):
        if Path(row['connection_key_file']) != directory / f'provider-{i}-key.json':
            raise MarketError('connection_changed', '准备后的连接凭据必须位于原目录。')
    return config, binding


def attach(app, directory):
    """Attach all hosts atomically before serving. Existing private ledgers only."""
    if type(app) is not MarketHTTP or type(app.gateway) is not TaskBudgetGateway or app.apis is not None:
        raise MarketError('api_host_mismatch', 'API连接需未接线的任务预算市场。')
    directory = _safe_path(directory)
    config, binding = load(directory)
    for name, actual in (('database', app.gateway.database), ('identities', app.gateway.identities.store.path),
                         ('budget', app.gateway.budgets.store.path)):
        if Path(config[name]).resolve() != Path(actual).resolve():
            raise MarketError('api_host_mismatch', '连接属于其他原市场、身份或预算。')
    _existing_databases(config)
    hosts = {}
    for i, row in enumerate(config['providers']):
        client = _client(row)
        for task in row['tasks']:
            identity = _check_task(app, task)
            policy = HostPolicy.from_json(identity['policy'])

            def task_check(principal, task_id, provider, *, task=task, row=row, policy=policy):
                if (principal, task_id, provider) != (task['principal'], task['task_id'], row['provider']):
                    return False
                # Retain the original binding snapshot, not a replacement file's assertions.
                if _fingerprints(directory, len(config['providers'])) != binding['files']:
                    raise MarketError('connection_changed', '运行中连接文件变化，停止新调用。')
                return HostPolicy.from_json(_check_task(app, task)['policy']) == policy

            market = Exchange(config['database'], task['principal'], host_policy=identity['policy'], clock=app.gateway.clock)
            host = ApiDeliveryHost(market, row['provider'], task['task_id'], task['purpose'],
                directory / f'provider-{i}-quota.sqlite', directory / 'results' / task['key_id'] / row['provider'],
                row['binding_id'], task_check, client.execute, client.lookup, require_existing_ledger=True)
            hosts.setdefault(task['key_id'], {})[row['provider']] = host
    apis = ApiGateway(app.gateway, hosts)
    # Validate every host before making any route available.
    for row in config['providers']:
        for task in row['tasks']:
            apis._bound_host(_check_task(app, task), row['provider'])
    app.apis = apis
    return {'status': 'checked', 'scope': 'local_configuration_only', 'providers': len(config['providers']),
            'task_connections': sum(len(row['tasks']) for row in config['providers']),
            'backend_calls': 0, 'payment_enabled': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare_parser = commands.add_parser('prepare')
    prepare_parser.add_argument('--config', type=Path, required=True)
    prepare_parser.add_argument('--directory', type=Path, required=True)
    check_parser = commands.add_parser('check')
    check_parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            result = prepare(args.directory, args.config)
        else:
            config, _ = load(args.directory)
            result = attach(_app(config), args.directory)
        print(canonical(result, 4096)); return 0
    except MarketError as exc:
        code = exc.code
    except (ValueError, OSError, sqlite3.Error):
        code = 'connection_unavailable'
    print(canonical({'status': 'error', 'error_code': code, 'message': '连接未启用，核对原配置及账本；未请求后端。'}, 4096))
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
