"""本项目原创。按项目保存远程超市配置；凭据只引用私有文件，配置检查不联网。"""
from copy import deepcopy
from pathlib import Path

from ai_supermarket.common import MarketError, compact, fields, parse_json
from ai_supermarket.http_client import RemoteMarket
from ai_supermarket.purchase_download import _read, sha
from steward.core import ContractError
from steward.extensions import setting, save_setting
from steward.remote_market_gateway import RemoteMarketGateway, project_scope
from steward.run_lock import project_lock

CONFIG_FIELDS = ('url', 'credential_file', 'task_id', 'output_root', 'purpose', 'mode',
                 'max_calls', 'allow_api_result_read')


def load_import(path):
    try:
        value = parse_json(_read(Path(path), 16384))
        fields(value, CONFIG_FIELDS)
        return value
    except (MarketError, OSError, ValueError):
        raise ContractError('配置文件字段无效；只接受连接、凭据路径、任务、目录、用途、模式和上限，不接收Token。') from None


def saved(app, pid):
    value = setting(app, 'project_market_' + pid, None)
    if value is None:
        return None
    try:
        fields(value, ('format', 'scope', 'enabled', 'key_id', 'config', 'config_sha256'))
        fields(value['config'], CONFIG_FIELDS)
        if (value['format'] != 'steward-market-settings-v1' or type(value['enabled']) is not bool or
                sha(compact(value['config']).encode()) != value['config_sha256']):
            raise ValueError('binding')
    except (MarketError, ValueError, TypeError):
        raise ContractError('原超市配置记录损坏，保留记录核对，不自动回退到无连接执行。') from None
    return deepcopy(value)


def prepare(project, config):
    """Validate only existing local inputs; returned metadata never contains credential contents."""
    try:
        fields(config, CONFIG_FIELDS)
        normalized = deepcopy(config)
        for name in ('credential_file', 'output_root'):
            if not isinstance(config[name], str) or not Path(config[name]).is_absolute():
                raise ValueError('absolute_path_required')
            normalized[name] = str(Path(config[name]))
        remote = RemoteMarket(normalized['url'], normalized['credential_file'])
        normalized['url'] = remote.base_url
        gateway = RemoteMarketGateway()
        gateway.bind(project, remote, normalized['task_id'], normalized['output_root'],
                     **{key: normalized[key] for key in ('purpose', 'mode', 'max_calls', 'allow_api_result_read')})
        scope = project_scope(project)
        marker = gateway.bindings[scope]['marker']
        # Catch reuse of a persisted remote task before saving unusable GUI settings.
        marker_name = 'steward-' + sha(compact([marker['endpoint'], marker['key_id'], marker['task_id']]).encode()) + '.json'
        previous = Path(normalized['output_root']) / marker_name
        if previous.exists() and parse_json(_read(previous, 16384)) != marker:
            raise ValueError('existing_remote_task_binding')
        record = {'format': 'steward-market-settings-v1', 'scope': scope, 'enabled': True,
                  'key_id': marker['key_id'], 'config': normalized,
                  'config_sha256': sha(compact(normalized).encode())}
        return record, gateway
    except (MarketError, OSError, ValueError, TypeError, KeyError):
        raise ContractError('超市配置未通过本地检查；核对绝对路径、原身份、远程任务编号、模式及已存在绑定。') from None


def check(app, pid, config, expected_scope):
    project = app.get(pid)
    if project_scope(project) != expected_scope:
        raise ContractError('窗口打开后任务目标或范围已改变，请重新打开配置。')
    record, _ = prepare(project, config)
    return {'status': 'local_configuration_valid', 'config_sha256': record['config_sha256'],
            'server_contacted': False, 'model_calls': 0, 'payment_executed': False}


def save(app, pid, config, expected_scope):
    with project_lock(app.db_path, pid), app.transaction():
        project = app.get(pid)
        if project_scope(project) != expected_scope:
            raise ContractError('任务范围已改变，请重新打开配置。')
        if project['status'] in ('planning', 'executing', 'deleted') or (project.get('orchestration') and project.get('phase') != 'execution'):
            raise ContractError('先确认任务目标并停止执行，再保存当前任务连接。')
        record, _ = prepare(project, config)
        original = saved(app, pid)
        if (original and original['scope'] == expected_scope and
                (original['config_sha256'], original['key_id']) != (record['config_sha256'], record['key_id'])):
            if expected_scope in project.get('market_runs', {}):
                raise ContractError('本修订已使用原连接，不能换配置重置任务；保留原记录。')
        save_setting(app, 'project_market_' + pid, record)
    return {'status': 'saved', 'server_contacted': False, 'model_calls': 0, 'payment_executed': False}


def disable(app, pid):
    with project_lock(app.db_path, pid), app.transaction():
        project = app.get(pid)
        if project['status'] in ('planning', 'executing'):
            raise ContractError('先停止执行再停用超市连接。')
        record = saved(app, pid)
        if record is not None:
            record['enabled'] = False
            save_setting(app, 'project_market_' + pid, record)
    return {'status': 'disabled', 'records_preserved': True}


def attach_for_run(app, project):
    """Called while PersonalSteward owns its existing project run lock, before model use."""
    managed = getattr(app, '_settings_market_gateway', None)
    current = getattr(app, 'market_gateway', None)
    record = saved(app, project['id'])
    if record is None or not record['enabled'] or (project.get('orchestration') and project.get('phase') != 'execution'):
        if managed is not None and current is managed:
            app.market_gateway = None
            app._settings_market_gateway = None
        return
    if current is not None and current is not managed:
        raise ContractError('已有代码注入的超市连接，不能用保存配置静默替换。')
    if record['scope'] != project_scope(project):
        raise ContractError('当前目标或权限范围与保存的超市连接不符，请核对新的任务绑定。')
    validated, gateway = prepare(project, record['config'])
    if validated != record:
        raise ContractError('原超市身份或配置已变化，保留记录并停止加载。')
    app.market_gateway = gateway
    app._settings_market_gateway = gateway
