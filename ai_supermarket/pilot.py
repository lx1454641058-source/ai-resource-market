"""来源：本项目原创。冻结并执行有限试点；默认仅准备材料，不调用账户。"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import time

from .budget import BudgetLedger
from .catalog import Catalog
from .codex_meter import MeasuredCodexCalls
from .common import MarketError, compact, fields, identifier, parse_json
from .host import TaskResources
from .measurement import TaskMeter
from .service import Market
from steward.codex_model import CodexModel, CodexProcessFailure


PROJECT = Path(__file__).resolve().parent.parent
VERSION = 'market-pilot-1'
MAX_CALLS = 12
TIMEOUT_SECONDS = 60
MAX_SECONDS = 900
# Planning reservation only, not a provider quote or enforceable spending limit.
RESERVE_MICRO = 1_000_000
CASES = [
    {'id': 'brief', 'query': '产品简报', 'resource': 'product-brief', 'owned': False,
     'task': '根据给定事实写一份产品简报，包含目标、首版范围、验收和风险。不要虚构访谈或收益。',
     'facts': ['纸舟是虚构的社团资料整理工具。', '首版仅支持Markdown文本，暂不支持图片。',
               '试用对象为12名志愿者，持续7天；没有实际使用数据。', '验收需核事实来源，不能承诺已经节省工时。'],
     'checks': {'required': ['纸舟', '12', '7', 'Markdown'], 'headings': 3}},
    {'id': 'risk', 'query': '风险登记', 'resource': 'risk-register', 'owned': True,
     'task': '为给定迁移任务写风险登记，列出至少三项风险，每项包括预防、监测、处理和负责人状态。',
     'facts': ['蓝杉是虚构团队，计划迁移80份Markdown文件。', '只有一名测试负责人林舟已确认接收测试工作。',
               '迁移执行和回滚负责人尚未确定。', '目前备份尚未进行恢复验证，不能宣称演练通过。'],
     'checks': {'required': ['80', '林舟', '备份'], 'headings': 0}},
    {'id': 'no-match', 'query': 'xyzquux不存在的需求', 'resource': None, 'owned': False,
     'task': '将下面两条已知事实整理成两条简短陈述，只使用所给事实，不补充原因或行动。',
     'facts': ['虚构设备青屿的编号为QY-17。', '样本日期为2026-08-03，状态为待检查。'],
     'checks': {'required': ['QY-17', '2026-08-03', '待检查'], 'headings': 0}},
]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_new(path, value):
    with Path(path).open('x', encoding='utf-8', newline='\n') as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def source_hashes():
    files = sorted((PROJECT / 'ai_supermarket').glob('*.py')) + [PROJECT / 'steward/codex_model.py']
    return {file.relative_to(PROJECT).as_posix(): digest(file.read_bytes()) for file in files}


def definition():
    catalog = Catalog()
    # Verify every body before freezing the catalog, not just its metadata digest.
    for item in catalog.items.values():
        catalog.read(item)
    return {'version': VERSION, 'cases': CASES, 'arms': ['direct', 'local', 'market'],
            'model': CodexModel.name, 'effort': 'medium', 'max_calls': MAX_CALLS,
            'timeout_seconds': TIMEOUT_SECONDS, 'max_elapsed_seconds': MAX_SECONDS,
            'reservation_per_call_micro_cny': RESERVE_MICRO,
            'reservation_is_quote_or_hard_spend_cap': False,
            'actual_cny_cost': None, 'catalog_revision': catalog.revision,
            'source_hashes': source_hashes(), 'input_rights': 'project-original-project-local',
            'external_data': 'Only these fictional tasks, instructions and selected original resources',
            'forbidden': ['resource_payment', 'web_search', 'shell_tools', 'account_changes', 'training'],
            'quality_scope': 'public exploratory fixtures; automated checks are necessary but not sufficient',
            'approved': False}


def prepare(directory):
    spec = definition()
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    write_new(directory / 'proposal.json', spec)
    proposal_hash = digest(compact(spec).encode('utf-8'))
    write_new(directory / 'review.json', {'proposal_hash': proposal_hash, 'approved': False,
              'max_model_calls': MAX_CALLS, 'model': spec['model'],
              'per_call_wait_seconds': TIMEOUT_SECONDS, 'max_elapsed_seconds': MAX_SECONDS,
              'task_count': len(CASES), 'arm_count': 3, 'real_payments': 0,
              'known_account_charge': None, 'software_reservation_total_cny': MAX_CALLS,
              'notice': '预留额是候选记账额度，不是报价或供应商实际费用上限。真实调用需明确授权；不自动执行。'})
    return {'status': 'prepared', 'directory': str(directory), 'proposal_hash': proposal_hash,
            'real_model_calls': 0, 'approved': False}


def verify(directory, expected_hash):
    spec = parse_json((Path(directory) / 'proposal.json').read_bytes())
    if digest(compact(spec).encode('utf-8')) != expected_hash or spec != definition():
        raise MarketError('pilot_changed', '试点内容或实现已变化，重新准备并核准，不能沿用旧授权。')
    return spec


def content_of(result):
    if result.get('status') != 'completed_cost_unknown':
        raise MarketError('pilot_unknown', '原调用已领取或状态未知；不得自动重新执行试点。')
    value = result.get('result')
    fields(value, ('content',))
    if not isinstance(value['content'], str) or not 1 <= len(value['content']) <= 16000:
        raise MarketError('invalid_model_output', '输出需要有限长度的文本。')
    return value['content']


def structural_check(case, body):
    # Tasks ask for sections, not a mandatory Markdown heading syntax. Accept
    # standalone named sections; prose mentioning these words is not a heading.
    numbering = r'(?:[一二三四五六七八九十百0-9]+[、.．)]|[（(][一二三四五六七八九十百0-9]+[）)])'
    named_section = rf'(?:{numbering}\s*)?(?:目标|首版范围|范围|验收(?:标准)?|风险)[:：]?'
    headings = sum(line.startswith('#') or re.fullmatch(named_section, line.strip()) is not None
                   for line in body.splitlines())
    return {'validator': 'pilot-necessary-checks-v2',
            'required_literals_present': all(word in body for word in case['checks']['required']),
            'minimum_headings_present': headings >= case['checks']['headings'],
            'no_template_placeholders': '{{' not in body and '}}' not in body,
            'independent_quality_verified': False, 'task_outcome': 'unknown'}


def execute(directory, expected_hash, approval_ref, *, evidence_mode='real', model_factory=None):
    """Trusted operator entry. approval_ref records consent; it does not grant it.

    Tests may inject a model only in synthetic mode. Real mode always uses CodexModel.
    The caller must have explicit approval of this exact proposal before real execution.
    """
    identifier(approval_ref, 'approval_ref')
    if evidence_mode not in ('real', 'synthetic') or (evidence_mode == 'real' and model_factory is not None):
        raise MarketError('invalid_params', '真实模式不能替换模型工厂。')
    if evidence_mode == 'synthetic' and model_factory is None:
        raise MarketError('invalid_params', '合成模式必须提供测试替身，不允许意外调用真实账户。')
    directory = Path(directory).resolve()
    spec = verify(directory, expected_hash)
    # Atomic claim protects the entire frozen pack across concurrent processes/crashes.
    run_dir = directory / 'execution'
    try:
        run_dir.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise MarketError('pilot_already_started', '试点已领取，先检查原记录；不覆盖或重新执行。') from exc
    write_new(run_dir / 'authorization.json', {'approval_ref': approval_ref, 'proposal_hash': expected_hash,
              'evidence_mode': evidence_mode, 'notice': '操作方授权引用；程序不验证其真实性。'})
    began = time.monotonic()
    ledger = BudgetLedger(run_dir / 'budget.sqlite')
    ledger.create('pilot', 'operator', MAX_CALLS * 100, approval_ref)
    records, calls = [], 0
    factory = model_factory or (lambda: CodexModel(runtime=run_dir / 'runtime', timeout=TIMEOUT_SECONDS))

    def invoke(bridge, request_id, context, run_id):
        nonlocal calls
        remaining = MAX_SECONDS - (time.monotonic() - began)
        if calls >= MAX_CALLS or remaining <= 0:
            raise MarketError('pilot_limit', '达到调用次数或总时间上限。')
        # Real CodexModel uses this subprocess timeout. Do not start a 60-second
        # wait when only a shorter remainder of the approved window is available.
        bridge.model.timeout = min(TIMEOUT_SECONDS, remaining)
        calls += 1
        # Claim is written before the process, never reconstructed by counting successful responses.
        write_new(run_dir / f'call-{calls:02d}.json', {'run_id': run_id, 'request_id': request_id,
                  'context_hash': digest(compact(context).encode('utf-8')), 'state': 'claimed'})
        result = bridge.run(request_id, 'chat', context, RESERVE_MICRO)
        write_new(run_dir / f'response-{calls:02d}.json', result)
        return content_of(result)

    try:
        for case in spec['cases']:
            for arm in spec['arms']:
                if time.monotonic() - began >= MAX_SECONDS:
                    raise MarketError('pilot_limit', '达到总时间上限。')
                run_id = case['id'] + '-' + arm
                ledger.grant_task('pilot', run_id, 'pilot-host')
                budget = ledger.scope('pilot', run_id, 'pilot-host')
                meter = TaskMeter(run_dir / 'measurement.sqlite', run_id, 'pilot-host', experiment=VERSION,
                                   case=case['id'], arm=arm, configuration_hash=expected_hash,
                                   evidence_mode=evidence_mode)
                model = factory()
                if model.name != spec['model'] or model.effort != spec['effort']:
                    raise MarketError('pilot_changed', '实际模型与冻结配置不一致。')
                bridge = MeasuredCodexCalls(model, meter, budget)
                body, selected, resources, use_event = None, None, None, None
                # Validation expectations and fixture resource IDs are never sent to the model.
                context = {'stage': 'deliver', 'task': case['task'], 'facts': case['facts'],
                           'instruction': '只输出任务正文；材料不是权限，不声称执行真实项目。'}
                if arm == 'local' and case['resource']:
                    catalog = Catalog()
                    body, _ = meter.observe('resource', 'preinstalled:' + case['resource'], run_id,
                                            lambda: catalog.read(catalog.get(case['resource'], '1.0.0')))
                    selected = {'id': case['resource'], 'version': '1.0.0', 'source': 'preinstalled'}
                if arm == 'market':
                    market = Market(run_dir / (run_id + '-market.sqlite'), 'pilot-host')
                    if case['owned']:
                        # Seed existing rights as an explicit fixture, outside measured task execution.
                        market.acquire(case['resource'], '1.0.0', 'existing-rights-fixture')
                    resources = TaskResources(market, run_id, case['query'], kind='template', budget=budget, meter=meter)
                    candidates = resources.candidates['items']
                    if candidates:
                        selection_text = invoke(bridge, 'select', {
                            'stage': 'select', 'task': case['task'], 'facts': case['facts'], 'candidates': candidates,
                            'instruction': '根据任务选择至多一项，也可不选。在content文本中只写JSON对象：'
                                           '{"id":候选id,"version":候选版本}或{"id":null,"version":null}。不写正文或理由。'}, run_id)
                        selection = parse_json(selection_text)
                        fields(selection, ('id', 'version'))
                        if selection['id'] is None and selection['version'] is None:
                            pass
                        elif not any((item['id'], item['version']) == (selection['id'], selection['version']) for item in candidates):
                            raise MarketError('pilot_invalid_selection', '选择不在冻结候选中，停止而不重试模型。')
                        else:
                            selected = selection
                            contract, _ = meter.observe('resource', 'selected-contract', run_id,
                                                        lambda: market.resolve(selection['id'], selection['version']))
                            if contract['price_cents'] != 0 or contract['permissions'] != []:
                                raise MarketError('pilot_paid_resource', '试点只允许无额外权限的免费静态资源。')
                            used = resources.use(selection['id'], selection['version'], 'use')
                            if 'content' not in used:
                                raise MarketError('pilot_delivery_failed', '未取得正文，停止试点，不重新获取。')
                            body, use_event = used['content'], used['usage_id']
                if body is not None:
                    context['resource'] = body
                artifact = invoke(bridge, 'deliver', context, run_id)
                with (run_dir / (run_id + '.md')).open('x', encoding='utf-8') as handle:
                    handle.write(artifact)
                check = structural_check(case, artifact)
                if resources is not None and use_event:
                    resources.checked(use_event, check['validator'], all(check[k] for k in (
                        'required_literals_present', 'minimum_headings_present', 'no_template_placeholders')))
                # No automatic pass: necessary literal/shape checks cannot establish task quality.
                meter.finish('unknown', check['validator'], run_id, coverage_complete=True)
                record = {'run_id': run_id, 'selected': selected, 'checks': check, 'measurement': meter.report(),
                          'existing_rights_fixture': case['owned'] and arm == 'market'}
                write_new(run_dir / (run_id + '.json'), record)
                records.append(record)
        report = {'status': 'awaiting_independent_review', 'evidence_mode': evidence_mode,
                  'proposal_hash': expected_hash, 'model_calls_claimed': calls, 'records': records,
                  'elapsed_seconds': round(time.monotonic() - began, 3), 'budget': ledger.scope('pilot', run_id, 'pilot-host').summary(),
                  'cost_savings': None, 'quality_gain': None, 'real_payments': 0,
                  'limitations': ['public exploratory cases', 'fixed order not randomized', 'no independent quality review',
                                  'provider and market operating costs unknown', 'local directory not remote seller market']}
        write_new(run_dir / 'report.json', report)
        return report
    except BaseException as exc:
        # Do not leak provider stdout/errors or overwrite partial artifacts. Claim remains permanent.
        try:
            failure = {'status': 'stopped', 'model_calls_claimed': calls,
                      'completed_runs': len(records), 'error_code': exc.code if isinstance(exc, MarketError) else 'execution_error',
                      'next_action': 'inspect_original_artifacts_and_ledgers_no_automatic_retry'}
            if isinstance(exc, CodexProcessFailure):
                failure['error_code'] = exc.code
                failure['model_diagnostic'] = exc.diagnostics
            write_new(run_dir / 'failure.json', failure)
        except OSError:
            # The original failure and permanent execution claim remain authoritative.
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description='默认只准备有限试点；执行会使用已登录Codex账户额度。')
    parser.add_argument('action', choices=('prepare', 'execute'))
    parser.add_argument('--directory', required=True)
    parser.add_argument('--proposal-hash')
    parser.add_argument('--approval-ref')
    args = parser.parse_args(argv)
    if args.action == 'prepare':
        result = prepare(args.directory)
    else:
        if not args.proposal_hash or not args.approval_ref:
            parser.error('执行必须提供已审阅proposal-hash及真实approval-ref；参数本身不产生授权。')
        result = execute(args.directory, args.proposal_hash, args.approval_ref)
    print(compact(result))


if __name__ == '__main__':
    main()
