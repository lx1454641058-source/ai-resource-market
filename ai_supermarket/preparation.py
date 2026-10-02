"""来源：本项目原创。任务多资源发现与选品预检；不购买、安装或调用模型。"""

from .common import MarketError, choice, fields, identifier, integer, text
from .exchange import USES
import re


KINDS = ('skill', 'template', 'image', 'audio', 'tool')
SUPPORTED = ('skill', 'template', 'image', 'audio')


def validate_request(value, checking=False):
    fields(value, ('task_id', 'purpose', 'needs', *(['selections'] if checking else [])))
    identifier(value['task_id'], 'task_id')
    choice(value['purpose'], USES, 'purpose')
    needs = value['needs']
    if not isinstance(needs, list) or not 1 <= len(needs) <= 8:
        raise MarketError('invalid_params', 'needs需要1～8项，由调用方AI根据任务提出。')
    ids = set()
    for need in needs:
        fields(need, ('id', 'query', 'kind', 'required'))
        key = identifier(need['id'], 'need.id')
        if key in ids:
            raise MarketError('invalid_params', '需求编号不能重复。')
        ids.add(key)
        text(need['query'], 'need.query', 512)
        choice(need['kind'], KINDS, 'need.kind')
        if type(need['required']) is not bool:
            raise MarketError('invalid_params', 'required必须是布尔值。')
    if checking:
        selections = value['selections']
        if not isinstance(selections, dict) or set(selections) != ids:
            raise MarketError('invalid_params', '每项需求都需明确选择，放弃时填写null。')
        for selected in selections.values():
            if selected is not None:
                fields(selected, ('id', 'version'), ('expected_release_digest',))
                identifier(selected['id'], 'resource.id')
                identifier(selected['version'], 'resource.version')
                if 'expected_release_digest' in selected:
                    digest = selected['expected_release_digest']
                    if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
                        raise MarketError('invalid_params', '选品中的预期发行摘要需要64位小写十六进制值。')


class ResourcePreparation:
    """The caller AI proposes needs and selections; rules never infer semantic fit."""

    def __init__(self, market, budget_cents=0):
        if getattr(market, 'role', None) != 'buyer':
            raise MarketError('forbidden', '任务资源准备需要买方入口。')
        self.market = market
        self.budget_cents = integer(budget_cents, 0, 100_000_000, 'budget_cents')

    @staticmethod
    def envelope(request, status, **values):
        return {'status': status, 'task_id': request['task_id'], 'purpose': request['purpose'],
                **values, 'execution_authorized': False, 'task_completed': False}

    def discover(self, request):
        validate_request(request)
        cache, rows = {}, []
        for need in request['needs']:
            row = {'need_id': need['id'], 'required': need['required']}
            if need['kind'] not in SUPPORTED:
                row.update(status='unsupported_delivery', items=[])
            else:
                key = (need['query'], need['kind'])
                if key not in cache:
                    try:
                        cache[key] = self.market.search(need['query'], need['kind'], limit=3,
                                                       purpose=request['purpose'])
                    except MarketError as error:
                        cache[key] = {'status': 'unavailable', 'error_code': error.code, 'items': []}
                found = cache[key]
                row.update(status=found['status'], items=found['items'])
                if 'error_code' in found:
                    row['error_code'] = found['error_code']
            rows.append(row)
        return self.envelope(request, 'discovered', needs=rows, distinct_searches=len(cache),
                             next_action='ai_select_or_decline_each_need')

    def check(self, request):
        validate_request(request, checking=True)
        resolved, resources, rows = {}, {}, []
        for need in request['needs']:
            selected = request['selections'][need['id']]
            row = {'need_id': need['id'], 'required': need['required']}
            if selected is None:
                row['status'] = 'missing_required' if need['required'] else 'skipped'
            elif need['kind'] not in SUPPORTED:
                row['status'] = 'unsupported_delivery'
            else:
                key = (selected['id'], selected['version'])
                if key not in resolved:
                    try:
                        resolved[key] = self.market.resolve(*key, purpose=request['purpose'])
                    except MarketError as error:
                        resolved[key] = {'status': 'unavailable', 'error_code': error.code}
                details = resolved[key]
                row.update({name: selected[name] for name in ('id', 'version')})
                if details['status'] != 'resolved':
                    row.update(status='unavailable', error_code=details.get('error_code', 'unavailable'))
                elif ('expected_release_digest' in selected
                      and selected['expected_release_digest'] != details.get('release_digest')):
                    row.update(status='unavailable', error_code='release_changed')
                elif details['kind'] != need['kind']:
                    row['status'] = 'kind_mismatch'
                else:
                    if key not in resources:
                        compatibility = details['compatibility']
                        price = 0 if details['owned'] else details['price_cents']
                        status = compatibility['status']
                        if status == 'ready' and price:
                            status = 'payment_unavailable'
                        item = {**selected, 'status': status, 'owned': details['owned'],
                                'new_resource_cents': price,
                                'dependency_max_cents': compatibility.get('extra_max_cents'),
                                'known_dependency_cents': sum(
                                    dep['max_cost_cents'] for dep in (details['usage_contract'] or {}).get('dependencies', [])
                                    if dep['max_cost_cents'] is not None),
                                'license': details['license'], 'offer_revision': details['offer_revision'],
                                'semantic_fit_verified': False}
                        # Return the license and contract once per resource, not once per need.
                        # These conditions must remain visible for the caller AI's actual decision.
                        item['conditions'] = {name: details[name] for name in (
                            'input', 'output', 'check', 'license_text', 'source_statement',
                            'allowed_uses', 'usage_contract', 'compatibility')}
                        if 'release_digest' in details:
                            item['release_digest'] = details['release_digest']
                        if 'asset' in details:
                            item.update(asset=details['asset'], rights=details['rights'])
                        if details['owned']:
                            item['acquisition_id'] = details['acquisition_id']
                        resources[key] = item
                    row['status'] = resources[key]['status']
            rows.append(row)
        resource_cost = sum(item['new_resource_cents'] for item in resources.values())
        dependency_costs = [item['dependency_max_cents'] for item in resources.values()]
        unknown_selection = any(row['status'] in ('unavailable', 'unsupported_delivery', 'kind_mismatch',
                                                  'missing_required') for row in rows)
        total = None if unknown_selection or None in dependency_costs else resource_cost + sum(dependency_costs)
        blockers = [row['need_id'] for row in rows if row['status'] not in ('ready', 'skipped')]
        known_estimate = resource_cost + sum(item['known_dependency_cents'] for item in resources.values())
        over_budget = known_estimate > self.budget_cents
        if blockers or over_budget:
            status = 'needs_attention'
        elif not resources:
            status = 'no_resources_selected'
        else:
            status = 'prechecked'
        return self.envelope(request, status, needs=rows, resources=list(resources.values()),
                             blocked_needs=blockers, estimated_total_cents=total,
                             known_resource_cents=resource_cost, budget_cents=self.budget_cents,
                             known_estimate_cents=known_estimate,
                             over_budget=over_budget, currency='CNY',
                             estimate_scope='unique_resources_plus_per_resource_dependencies',
                             costs_excluded=['model', 'labor', 'channel_fees', 'operations'],
                             budget_reserved=False, purchases_executed=False, content_loaded=False,
                             next_action='revise_selection' if status == 'needs_attention'
                             else 'host_revalidate_before_use')
