"""来源：本项目原创。项目级资源申请、人工授权及调用前持久化额度检查。"""
import hashlib
import json
import math
import re
import uuid

from .core import ContractError, now, checked_text
from .extensions import setting, permissions


TASK_TOOLS = {'save_artifact', 'note', 'ask_supervisor'}
STATES = {'pending': '待批准', 'approved': '已批准', 'rejected': '已拒绝',
          'revoked': '已撤销', 'exhausted': '次数已用完', 'uncertain': '费用或结果待核对',
          'stale': '范围或配置已改变'}


class ResourceBlocked(ContractError):
    """未发出外部请求；需要主管调整或用户处理授权。"""


def scope_stamp(p):
    # 方向改变即失效，即使尚无成果、revision 尚未增加也不能沿用授权。
    return hashlib.sha256(json.dumps([p['goal'], p['acceptance'], p.get('revision', 0),
                                     p.get('resource_epoch', 0)], ensure_ascii=False).encode()).hexdigest()


def config_stamp(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def nonnegative(value, name, nullable=True):
    if value is None and nullable:
        return
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100000:
        raise ContractError(name+'必须为空或有限非负数')


def validate_spec(spec):
    from .api_model import validate_connection
    from .orchestration import WORKER_TOOLS
    fields = {'service', 'purpose', 'alternatives', 'expected_benefit', 'data_scope',
              'data_description', 'tools', 'max_calls', 'estimated_cost_usd', 'cost_basis', 'risks'}
    if not isinstance(spec, dict) or set(spec) != fields:
        raise ContractError('资源申请字段不完整')
    service = spec['service']
    if not isinstance(service, dict) or set(service) != {'id', 'base_url', 'model', 'input_price', 'output_price'}:
        raise ContractError('申请须明确接入名称、HTTPS地址、模型及输入/输出价格（未知填null）')
    validate_connection(dict(service, key_env='RESOURCE_KEY', quality_note='', enabled=True))
    for key in ('purpose', 'alternatives', 'expected_benefit', 'data_description', 'cost_basis', 'risks'):
        checked_text(spec[key], key, 2000)
    if spec['data_scope'] not in ('task', 'project'):
        raise ContractError('数据范围只能是task或project')
    allowed = TASK_TOOLS if spec['data_scope'] == 'task' else set(WORKER_TOOLS)
    if (not isinstance(spec['tools'], list) or not spec['tools'] or
            any(not isinstance(t, str) or t not in allowed for t in spec['tools'])):
        raise ContractError('申请工具超出数据范围；task仅允许保存成果、笔记、向主管提问')
    if type(spec['max_calls']) is not int or not 1 <= spec['max_calls'] <= 100:
        raise ContractError('每份申请必须限定1至100次调用（包括失败调用）')
    nonnegative(spec['estimated_cost_usd'], '预计费用')
    return spec


def configured(app, request):
    config = setting(app, 'api_connections', {}).get(request['spec']['service']['id'])
    return config if config and all(config.get(k) == v for k, v in request['spec']['service'].items()) else None


def state_of(app, p, request):
    state = request['state']
    if state in ('pending', 'approved') and request['scope'] != scope_stamp(p):
        return 'stale'
    if state == 'approved':
        config = configured(app, request)
        if not config or config_stamp(config) != request['config_stamp']:
            return 'stale'
        if request.get('inflight'):
            return 'uncertain'
        if request['used_calls'] >= request['spec']['max_calls']:
            return 'exhausted'
    return state


def overview(app, p):
    return [{**r, 'effective_state': state_of(app, p, r), 'setup_matches': configured(app, r) is not None}
            for r in p.get('resource_requests', [])]


def describe(request):
    s, service = request['spec'], request['spec']['service']
    scope = ('仅主管为该任务整理的指令及本次负责人工作记录；指令可能包含主管摘录的项目资料。'
             if s['data_scope'] == 'task' else
             '当前项目目标、验收标准、近期用户补充、项目笔记、成果及负责人工作记录；可按授权工具读取本项目完整历史。')
    estimate = '不确定' if s['estimated_cost_usd'] is None else f"约 ${s['estimated_cost_usd']}"
    return (f"资源申请 {request['id']} · {STATES.get(request.get('effective_state', request['state']), request['state'])}\n"
            f"服务：{service['id']} / {service['model']}\n地址：{service['base_url']}\n"
            f"用途：{s['purpose']}\n质量与收益：{s['expected_benefit']}\n替代方案：{s['alternatives']}\n"
            f"发送范围：{scope}\n具体资料：{s['data_description']}\n工具：{', '.join(s['tools'])}\n"
            f"调用硬上限：{s['max_calls']}次；已占用{request['used_calls']}次（失败和中止不退回）\n"
            f"总费用预估：{estimate}；依据：{s['cost_basis']}\n"
            f"输入/输出单价：{service['input_price']} / {service['output_price']} 美元/百万Token（None表示未知）\n"
            f"已记录估算费用：${request.get('estimated_spent_usd', 0):.8f}\n风险与不确定性：{s['risks']}\n"
            '费用预估不是账单硬上限；本机严格限制调用次数。金额硬限制需在供应商账户设置。\n'
            '不自动附带全局偏好或其他项目。仅限当前项目方向；换服务、改方向或用完次数需要重新申请。')


def request_resource(app, p, content):
    from .orchestration import say
    try:
        spec = validate_spec(json.loads(content))
    except (ValueError, TypeError) as error:
        if isinstance(error, ContractError):
            raise
        raise ContractError('资源申请content必须为有效JSON对象') from None
    requests = p.setdefault('resource_requests', [])
    if len(requests) >= 100:
        raise ContractError('本项目资源申请已达100份，请整理为后续项目')
    pending = next((r for r in requests if state_of(app, p, r) == 'pending'), None)
    if pending:
        p.update(status='waiting_input', waiting_resource=pending['id'])
        return {'state': 'pending', 'request_id': pending['id'], 'note': '已有申请待决定，不能悄悄替换'}
    r = {'id': uuid.uuid4().hex[:12], 'spec': spec, 'scope': scope_stamp(p),
         'state': 'pending', 'created_at': now(), 'used_calls': 0, 'estimated_spent_usd': 0}
    requests.append(r)
    p.update(status='waiting_input', waiting_resource=r['id'])
    p.pop('awaiting_supervisor', None)
    p.pop('message_resume_status', None)
    say(p, 'steward', describe(r)+f"\n\n可打开“资源申请”，或回复：批准资源 {r['id']} / 拒绝资源 {r['id']}。", 'resource_request')
    app.event(p['id'], 'resource_requested', request_id=r['id'], model='api:'+spec['service']['id'])
    return {'state': 'pending', 'request_id': r['id'], 'executed': False}


def decide(app, pid, request_id, choice):
    """只由用户界面/明确命令进入；模型工具目录中没有审批动作。"""
    from .orchestration import say
    if choice not in ('approve', 'reject', 'revoke'):
        raise ContractError('不支持的资源决定')
    with app.transaction():
        p = app.get(pid)
        if p['status'] == 'deleted':
            raise ContractError('请先恢复任务')
        r = next((r for r in p.get('resource_requests', []) if r['id'] == request_id), None)
        if not r:
            raise ContractError('资源申请不存在，请使用对话中显示的完整编号')
        if choice == 'revoke':
            if r['state'] not in ('approved', 'uncertain'):
                raise ContractError('该申请没有可撤销的授权')
            r['state'] = 'revoked'
        else:
            if r['state'] != 'pending':
                raise ContractError('该申请已处理，不能重复批准或退回次数')
            if p['status'] in ('planning', 'executing'):
                raise ContractError('请等待当前调用完成或停止后再处理申请')
            if choice == 'approve':
                if state_of(app, p, r) != 'pending' or p.get('incoming'):
                    raise ContractError('方向已变或还有消息待主管处理，请让主管重新确认申请')
                config = configured(app, r)
                if not config or not config['enabled']:
                    raise ContractError('请先在API接入保存并启用与申请相同的名称、地址、模型和价格；保存配置不等于批准')
                if not permissions(app)['api']:
                    raise ContractError('请先在权限设置中打开API开关；开关不代替项目授权')
                app.require_context_sharing()
                r['config_stamp'] = config_stamp(config)
                r['state'] = 'approved'
            else:
                r['state'] = 'rejected'
        r['decided_at'] = now()
        app.event(pid, 'resource_decided', request_id=r['id'], choice=choice, source='explicit_user')
        label = {'approve': '批准', 'reject': '拒绝', 'revoke': '撤销'}[choice]
        say(p, 'user', f"{label}资源 {r['id']}。", 'resource_decision')
        if p.get('waiting_resource') == r['id']:
            p.pop('waiting_resource', None)
            p['status'] = 'ready'
        # 让主管基于决定重新判断，保留原负责人工作，不直接启动收费调用。
        p['awaiting_supervisor'] = True
        p.pop('assessment', None)
        p['last_result'] = {'resource_decision': choice, 'request_id': r['id'],
                            'note': '由主管继续评估；拒绝后优先使用现有资源，不重复催批。'}
        app.save(p)
    return p


def handle_command(app, pid, message):
    match = re.fullmatch(r'(批准|拒绝|撤销)资源\s+([a-f0-9]{12})[。.!！]?', message.strip())
    if not match:
        return None
    return decide(app, pid, match[2], {'批准': 'approve', '拒绝': 'reject', '撤销': 'revoke'}[match[1]])


def grant_for(app, p, model_name, tools):
    for r in reversed(p.get('resource_requests', [])):
        if ('api:'+r['spec']['service']['id'] == model_name and state_of(app, p, r) == 'approved'
                and set(tools) <= set(r['spec']['tools'])):
            return r
    raise ResourceBlocked('该模型在当前项目方向、工具范围或剩余次数内没有有效授权；请主管调整方案或提交资源申请')


def reserve(app, pid, model, role, context):
    from .agent_loop import TOOLS
    with app.transaction():
        p = app.get(pid)
        task = p.get('pending_delegate')
        if (role != 'team_member' or p['status'] != 'executing' or not task
                or task['model'] != model.name or p.get('awaiting_supervisor') or p.get('incoming')):
            raise ResourceBlocked('本轮不是有效的项目负责人委派，不能发送API请求')
        if not permissions(app)['api']:
            raise ResourceBlocked('API总开关已关闭')
        r = grant_for(app, p, model.name, task['tools'])
        if config_stamp(model.config) != r['config_stamp']:
            raise ResourceBlocked('本轮模型配置与批准配置不一致')
        r['used_calls'] += 1
        r['inflight'] = p['operation']
        app.save(p)
        app.event(pid, 'resource_call_reserved', request_id=r['id'], operation=p['operation'], used=r['used_calls'])
        # 白名单构造外发包，避免整个总管上下文（偏好、目录、其他角色信息）顺带发送。
        safe = {'task': {k: task[k] for k in ('role', 'title', 'instruction', 'tools', 'dialogue')},
                'tools': {name: TOOLS[name] for name in task['tools']},
                'authorized_purpose': r['spec']['purpose']}
        if r['spec']['data_scope'] == 'project':
            for key in ('goal', 'acceptance', 'revision', 'user_updates', 'feedback', 'plan', 'notes', 'artifacts'):
                if key in context:
                    safe[key] = context[key]
        return r['id'], p['operation'], safe


def settle(app, pid, request_id, operation, receipt):
    with app.transaction():
        p = app.get(pid)
        r = next(r for r in p['resource_requests'] if r['id'] == request_id)
        if r.get('inflight') != operation:
            return
        r.pop('inflight')
        cost = (receipt or {}).get('estimated_cost_usd')
        known = type(cost) in (int, float) and math.isfinite(cost) and cost >= 0
        if known:
            r['estimated_spent_usd'] = round(r['estimated_spent_usd']+cost, 8)
        # 不退回失败/取消名额；缺回执不能假装免费，也不能无声反复付费重试。
        uncertain = not receipt or not receipt.get('completed') or not known
        estimate = r['spec']['estimated_cost_usd']
        over_estimate = estimate is not None and r['estimated_spent_usd'] >= estimate
        if r['state'] == 'approved' and (uncertain or over_estimate):
            r['state'] = 'uncertain'
        app.event(pid, 'resource_call_settled', request_id=r['id'], operation=operation,
                  estimated_cost_usd=cost if known else None, uncertain=uncertain, estimate_reached=over_estimate)
        app.save(p)
