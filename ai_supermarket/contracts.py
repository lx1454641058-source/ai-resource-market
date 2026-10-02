"""来源：本项目原创。静态商品使用契约和宿主预检；声明不授予权限或执行依赖。"""

from dataclasses import dataclass
import re

from .common import MarketError, choice, fields, identifier, integer, text


PERMISSIONS = ('file_read', 'file_write', 'network', 'code_execution')


def strings(limit=16, enum=None):
    item = {'type': 'string', 'minLength': 1, 'maxLength': 128}
    if enum:
        item['enum'] = list(enum)
    return {'type': 'array', 'minItems': 0, 'maxItems': limit, 'uniqueItems': True, 'items': item}


CONTRACT_SCHEMA = {'type': 'object', 'additionalProperties': False,
    'required': ['version', 'delivery', 'capabilities', 'permissions', 'dependencies', 'sample'],
    'properties': {
        'version': {'type': 'integer', 'minimum': 1, 'maximum': 4},
        'delivery': {'type': 'string', 'minLength': 1, 'maxLength': 32, 'enum': ['static_markdown', 'origin_file', 'provider_api']},
        'capabilities': strings(), 'permissions': strings(enum=PERMISSIONS),
        'dependencies': {'type': 'array', 'minItems': 0, 'maxItems': 8, 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['id', 'license_name', 'max_cost_cents'], 'properties': {
                'id': {'type': 'string', 'minLength': 1, 'maxLength': 128},
                'license_name': {'type': 'string', 'minLength': 1, 'maxLength': 100},
                'max_cost_cents': {'type': ['integer', 'null'], 'minimum': 0, 'maximum': 100_000_000}}}},
        'sample': {'type': 'object', 'additionalProperties': False, 'required': ['input', 'expected', 'check'],
                   'properties': {key: {'type': 'string', 'minLength': 1, 'maxLength': 1000}
                                  for key in ('input', 'expected', 'check')}}}}


def names(values, name, maximum=16, enum=None):
    if not isinstance(values, (list, tuple)) or len(values) > maximum:
        raise MarketError('invalid_params', f'{name}列表大小无效。')
    for value in values:
        identifier(value, name)
        if enum:
            choice(value, enum, name)
    if len(values) != len(set(values)):
        raise MarketError('invalid_params', f'{name}不能重复。')
    return frozenset(values)


def validate_contract(value):
    fields(value, CONTRACT_SCHEMA['required'])
    integer(value['version'], 1, 4, 'contract.version')
    choice(value['delivery'], {1: ('static_markdown',), 2: ('static_asset',), 3: ('origin_file',), 4: ('provider_api',)}[value['version']], 'contract.delivery')
    capabilities = names(value['capabilities'], 'capabilities')
    required = {1: 'read_markdown', 2: 'receive_asset', 3: 'receive_origin', 4: 'invoke_api'}[value['version']]
    permissions = names(value['permissions'], 'permissions', enum=PERMISSIONS)
    if value['version'] == 3 and not {'network', 'file_write'} <= permissions:
        raise MarketError('invalid_params', '原站交付需声明network与file_write。')
    if value['version'] == 4 and (not {'network', 'file_write'} <= permissions or value['dependencies']):
        raise MarketError('invalid_params', 'API需声明network与file_write；本版套餐不支持额外付费依赖。')
    if required not in capabilities:
        raise MarketError('invalid_params', '契约缺少对应交付能力。')
    dependencies = value['dependencies']
    if not isinstance(dependencies, list) or len(dependencies) > 8:
        raise MarketError('invalid_params', 'dependencies需要0～8项。')
    ids = []
    for dependency in dependencies:
        fields(dependency, ('id', 'license_name', 'max_cost_cents'))
        ids.append(identifier(dependency['id'], 'dependency.id'))
        text(dependency['license_name'], 'dependency.license_name', 100)
        if re.search(r'\b(?:a?gpl)(?:\b|[-v0-9])', dependency['license_name'], re.I):
            raise MarketError('unsupported_license', '本项目不引入GPL/AGPL依赖。')
        if dependency['max_cost_cents'] is not None:
            integer(dependency['max_cost_cents'], 0, 100_000_000, 'dependency.max_cost_cents')
    names(ids, 'dependency.id', 8)
    fields(value['sample'], ('input', 'expected', 'check'))
    for key, sample in value['sample'].items():
        text(sample, 'sample.' + key, 1000)


@dataclass(frozen=True)
class HostPolicy:
    """Configured by the trusted host, never by model tool arguments or seller content."""
    capabilities: frozenset = frozenset({'read_markdown'})
    permissions: frozenset = frozenset()
    dependencies: frozenset = frozenset()
    extra_budget_cents: int = 0

    @classmethod
    def from_json(cls, value):
        fields(value, ('capabilities', 'permissions', 'dependencies', 'extra_budget_cents'))
        return cls(names(value['capabilities'], 'capabilities'),
                   names(value['permissions'], 'permissions', enum=PERMISSIONS),
                   names(value['dependencies'], 'dependencies', 64),
                   integer(value['extra_budget_cents'], 0, 100_000_000, 'extra_budget_cents'))


def preflight(contract, policy):
    if contract is None:
        return {'status': 'contract_missing', 'next_action': 'choose_declared_resource', 'execution_authorized': False}
    validate_contract(contract)
    missing_capabilities = sorted(set(contract['capabilities']) - policy.capabilities)
    missing_permissions = sorted(set(contract['permissions']) - policy.permissions)
    missing_dependencies = sorted({d['id'] for d in contract['dependencies']} - policy.dependencies)
    costs = [d['max_cost_cents'] for d in contract['dependencies']]
    extra = None if None in costs else sum(costs)
    if missing_capabilities:
        status = 'incompatible'
    elif missing_permissions:
        status = 'permission_required'
    elif missing_dependencies:
        status = 'dependency_missing'
    elif extra is None:
        status = 'cost_unknown'
    elif extra > policy.extra_budget_cents:
        status = 'extra_budget_exceeded'
    else:
        status = 'ready'
    return {'status': status, 'missing_capabilities': missing_capabilities,
            'missing_permissions': missing_permissions, 'missing_dependencies': missing_dependencies,
            'extra_max_cents': extra, 'currency': 'CNY', 'cost_scope': 'dependencies_per_task',
            'evidence': 'reviewed_declaration_only', 'execution_authorized': False,
            'next_action': 'continue_resource_flow' if status == 'ready' else 'stop_or_choose_other_resource'}
