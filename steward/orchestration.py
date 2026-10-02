"""来源：本项目原创。需求讨论、总管决策、团队回问与阶段汇报的持久化循环。"""

import json
from pathlib import Path

from .core import ContractError, checked_text, now
from .codex_model import ModelUnavailable
from .agent_loop import context_for, execute, validate_decision, action_signature, ASSESSMENT_FIELDS, prepare_task_resources
from .extensions import permissions, installed, run_plugin, setting


WORKER_TOOLS = ('save_artifact', 'read_artifact', 'read_history', 'note', 'export_artifact', 'ask_supervisor', 'use_plugin')
DECISIONS = {'market', 'assemble_team', 'delegate', 'answer_team', 'learn_tool', 'use_plugin',
             'save_artifact', 'export_artifact', 'finish', 'revise_work', 'adopt_goal', 'accept_result', 'request_resource'}


def say(p, role, content, kind='message'):
    p.setdefault('dialogue', []).append({'role': role, 'content': content, 'kind': kind, 'at': now()})


def catalog(app):
    evidence = {}
    for (body,) in app.db.execute("SELECT body FROM events WHERE kind='model_completed'"):
        receipt = json.loads(body)
        name, elapsed = receipt.get('model'), receipt.get('elapsed_seconds')
        if isinstance(name, str) and type(elapsed) in (int, float) and elapsed >= 0:
            evidence.setdefault(name, []).append(elapsed)
    return {
        'models': [{'id': name, 'enabled': True, 'provider': getattr(model, 'mode', 'unknown'),
                    'quality': getattr(model, 'config', {}).get('quality_note') or '未进行当前任务质量基准测试',
                    'price': {key: getattr(model, 'config', {}).get(key) for key in ('input_price', 'output_price')},
                    'price_source': '用户配置，美元/百万Token；非自动核价',
                    'observed_completed_calls': len(evidence.get(name, [])),
                    'observed_average_seconds': (round(sum(evidence[name])/len(evidence[name]), 2)
                                                 if evidence.get(name) else None),
                    'measurement_limit': '历史成功调用，任务和输入长度不同；不能据此推断质量或成功率',
                    'cost_basis': '每次派发占用1次调用名额；价格未知，不等于实际金额'}
                   for name, model in app.model_registry().items()],
        'api_candidates': [{k: c[k] for k in ('id', 'base_url', 'model', 'input_price', 'output_price', 'quality_note', 'enabled')}
                           for c in setting(app, 'api_connections', {}).values()],
        'api_policy': 'API配置或总开关不等于项目授权。须request_resource获得用户明确批准，随后在同一项目方向和工具/次数范围内派发。未接入的服务可以先申请，由用户保存配置。',
        'worker_tools': list(WORKER_TOOLS),
        'plugins': list(installed(app).values()), 'permissions': permissions(app),
        'execution': '角色独立上下文，持久化串行派发；只使用已实现的工具，不代表独立模型训练或同时并行',
        'unavailable': ['任意命令执行', '外部软件控制', '付款或发布'] +
                       ([] if installed(app).get('web-research', {}).get('kind') == 'web_research' and permissions(app)['plugins']
                        else ['网页搜索（需安装并允许联网搜索与网页核查插件）']),
    }


def submit_message(app, pid, message):
    message = checked_text(message, '消息', 4000)
    with app.transaction():
        p = app.get(pid)
        if p['status'] == 'deleted':
            raise ContractError('任务已删除，请先从回收站恢复')
        queue = p.setdefault('incoming', [])
        if len(queue) >= 20:
            raise ContractError('待处理消息已达20条，请等待总管处理后再发送')
        queue.append(message)
        say(p, 'user', message)
        # 正在派发的调用继续收尾；其旧方向结果不能跨过新消息提交。
        if p['status'] not in ('planning', 'executing'):
            p.setdefault('message_resume_status', p['status'])
            p['status'] = 'ready'
        app.save(p)
        app.event(pid, 'message_received', pending=len(queue))
    return p


def consume_messages(p):
    incoming = p.pop('incoming', [])
    if not incoming:
        return False
    p.setdefault('user_updates', []).extend({'text': text, 'source': 'user'} for text in incoming)
    p['awaiting_supervisor'] = True
    p.pop('assessment', None)
    p['status'] = 'ready'
    p['repeat_count'] = 0
    p.pop('error_message', None)
    p['error'] = None
    return True


def confirm_goal(app, pid):
    with app.transaction():
        p = app.get(pid)
        if p['status'] != 'waiting_input' or not p.get('goal_proposal') or p.get('incoming'):
            raise ContractError('当前没有可确认的目标草案，请先与总管讨论')
        proposal = p.pop('goal_proposal')
        if p.get('artifacts'):
            p['revision'] = p.get('revision', 0)+1
        p.update(goal=proposal['goal'], acceptance='\n'.join(proposal['criteria']), phase='execution', status='ready')
        p.pop('submission', None)
        p.pop('question', None)
        p.pop('awaiting_supervisor', None)
        say(p, 'user', '确认目标，授权总管在现有工具与调用预算内安排推进。', 'goal_confirmed')
        app.save(p)
        app.event(pid, 'goal_confirmed', revision=p.get('revision', 0))
    return p


def assemble(app, p, decision):
    if p.get('pending_delegate'):
        raise ContractError('先处理正在进行的委派，再调整团队')
    members = []
    for encoded in decision['items']:
        try:
            member = json.loads(encoded)
        except (ValueError, TypeError):
            raise ContractError('团队成员必须为JSON对象字符串') from None
        if not isinstance(member, dict) or set(member) != {'id', 'role', 'model', 'tools'}:
            raise ContractError('团队成员需要id、role、model、tools')
        for key in ('id', 'role', 'model'):
            checked_text(member[key], key, 160)
        if member['model'] not in app.model_registry():
            raise ContractError('所选模型尚未启用')
        if not isinstance(member['tools'], list) or not member['tools'] or any(
                not isinstance(t, str) or t not in WORKER_TOOLS for t in member['tools']):
            raise ContractError('团队请求了未接入的工具')
        if any(m['id'] == member['id'] for m in members):
            raise ContractError('团队成员编号重复')
        if member['model'].startswith('api:'):
            from .resources import grant_for
            grant_for(app, p, member['model'], member['tools'])
        members.append({**member, 'status': 'idle'})
    if not 1 <= len(members) <= 8:
        raise ContractError('团队需要1至8位成员')
    if p.get('worker_question'):
        p.setdefault('team_history', []).append({'kind': 'reassigned', 'issue': p.pop('worker_question'),
                                                'reason': decision['content'], 'at': now()})
    p['team'] = members
    p['selection_reason'] = decision['content']
    say(p, 'steward', '团队安排：\n'+'\n'.join(f"{m['role']} · {m['model']}" for m in members)+
        '\n\n选择依据：'+decision['content'], 'team')
    return {'team': members, 'state': 'configured'}


def export_artifact(app, p, index):
    if not permissions(app)['export']:
        raise ContractError('文件导出权限已关闭')
    if index >= len(p['artifacts']):
        raise ContractError('成果编号不存在')
    base = app.db_path.parent.resolve()
    folder = base / 'deliverables' / p['id']
    # 不接受模型提供路径，不跟随已存在的目录链接。
    for directory in (base / 'deliverables', folder):
        if directory.is_symlink() or directory.resolve().is_relative_to(base) is False:
            raise ContractError('成果目录不能指向项目数据目录之外')
        directory.mkdir(exist_ok=True)
    path = folder / f'artifact-{index:04d}.md'
    if path.is_symlink():
        raise ContractError('成果路径不能为链接')
    content = p['artifacts'][index]['content']
    try:
        with path.open('x', encoding='utf-8') as stream:
            stream.write(content)
    except FileExistsError:
        if path.read_text(encoding='utf-8') != content:
            raise ContractError('已有导出文件内容不同，保留原文件，不覆盖') from None
    receipt = {'artifact_id': index, 'path': str(path), 'state': 'written'}
    if receipt not in p.setdefault('files', []):
        p['files'].append(receipt)
    return receipt


def supervise(app, p, decision):
    action = decision['action']
    if action != 'assess' and decision.get('assessment') is not None:
        if decision['assessment'].get('next_action') != action:
            raise ContractError('评估的下一动作必须与本次指令一致')
        # 同一响应先校验并保存评估，再执行指令；仍在同一数据库事务内。
        # 兼容旧版单独assess动作，但无需为传递指令重复调用一次模型。
        supervise(app, p, {**decision, 'action': 'assess', 'assessment': None,
                           'content': json.dumps(decision['assessment'], ensure_ascii=False)})
    if action == 'assess':
        try:
            assessment = json.loads(decision['content'])
        except ValueError:
            raise ContractError('决策摘要必须为JSON对象') from None
        if not isinstance(assessment, dict) or set(assessment) != ASSESSMENT_FIELDS:
            raise ContractError('决策需要质量、成本、组队、工具、缺口和下一动作六项摘要')
        for key in ASSESSMENT_FIELDS:
            checked_text(assessment[key], key, 2500)
        p['assessment'] = assessment
        p.setdefault('assessments', []).append({**assessment, 'at': now()})
        return {'state': 'assessed', 'decision': assessment}
    if action in DECISIONS or (action == 'ask_user' and p.get('phase') == 'execution'):
        assessment = p.pop('assessment', None)
        if not assessment or assessment['next_action'] != action:
            raise ContractError('主管须先完成针对该动作的选型、成本、组队和能力缺口评估')
    if p.get('phase') == 'discovery' and action not in ('ask_user', 'propose_goal', 'report', 'note', 'reply', 'adopt_goal', 'read_history'):
        raise ContractError('需求尚在讨论，先形成目标并由用户确认，不能提前执行')
    if action == 'ask_supervisor':
        raise ContractError('总管不能向自己提问，应回答负责人或询问用户')
    if action == 'request_resource':
        from .resources import request_resource
        return request_resource(app, p, decision['content'])
    if action == 'accept_result':
        if not p.get('awaiting_supervisor') or p.get('message_resume_status') != 'awaiting_review' or not p.get('submission'):
            raise ContractError('只有用户正在回复已提交成果时才能记录验收')
        if p.get('pending_delegate') or p.get('worker_question'):
            raise ContractError('团队仍有未完成工作，不能结束阶段')
        p.update(status='accepted', feedback=p.get('user_updates', [])[-1]['text'])
        p.pop('awaiting_supervisor', None)
        p.pop('message_resume_status', None)
        say(p, 'steward', '已记录你对本阶段成果的验收。'+decision['content'], 'accepted')
        app.event(p['id'], 'accepted_by_conversation', revision=p.get('revision', 0))
        return {'state': 'accepted', 'basis': '用户对已提交成果的当前回复'}
    if action == 'reply':
        checked_text(decision['content'], '回复', 16000)
        say(p, 'steward', decision['content'])
        p.pop('awaiting_supervisor', None)
        prior = p.pop('message_resume_status', 'ready')
        if p.get('phase') == 'discovery' or prior in ('awaiting_review', 'accepted', 'rejected', 'waiting_input'):
            p['status'] = 'waiting_input' if p.get('phase') == 'discovery' else prior
        result = {'state': 'replied', 'work_preserved': True}
        buffered = p.get('deferred_worker_result')
        if buffered and p.get('pending_delegate') == buffered['delegation'] and p['status'] == 'ready':
            p.pop('deferred_worker_result', None)
            # 普通讨论不浪费已返回的负责人结果；主管决定保留原任务后才应用。
            result['worker_result'] = worker_step(app, p, buffered['decision'], p['pending_delegate'])
            p.setdefault('action_history', []).append({'action': buffered['decision']['action'],
                                                       'reason': buffered['decision']['reason'], 'at': now()})
        return result
    if action == 'revise_work':
        p['resource_epoch'] = p.get('resource_epoch', 0)+1
        if p.get('artifacts'):
            p['revision'] = p.get('revision', 0)+1
        for key in ('pending_delegate', 'worker_question', 'submission', 'goal_proposal', 'awaiting_supervisor', 'message_resume_status', 'deferred_worker_result'):
            p.pop(key, None)
        for member in p.get('team', []):
            member['status'] = 'needs_replanning'
        say(p, 'steward', '已决定调整任务：'+decision['content']+'\n接下来重新向负责人下达指令。', 'decision')
        return {'state': 'revision_opened', 'revision': p.get('revision', 0)}
    if action == 'adopt_goal':
        proposal = p.get('goal_proposal')
        if not proposal or not p.get('awaiting_supervisor'):
            raise ContractError('采纳目标需要已有草案和新的用户对话依据')
        p.update(goal=proposal['goal'], acceptance='\n'.join(proposal['criteria']), phase='execution')
        p.pop('goal_proposal', None)
        p.pop('awaiting_supervisor', None)
        p.pop('message_resume_status', None)
        say(p, 'steward', '根据我们的讨论，接下来按这个目标推进：'+p['goal']+'\n'+decision['content'], 'goal_adopted')
        return {'state': 'goal_adopted'}
    if action == 'learn_tool':
        plugin = installed(app).get(decision['title'])
        if not plugin:
            raise ContractError('该工具尚未安装，需说明缺口并由用户通过插件市场安装')
        p.setdefault('learned_tools', {})[plugin['id']] = plugin
        return {'state': 'manual_read', 'manual': plugin['manual'], 'note': '已读取用法，效果仍需实际工具验证'}
    if action == 'use_plugin':
        return plugin_step(app, p, decision)
    if action == 'propose_goal':
        checked_text(decision['title'], '目标', 160)
        if not decision['items'] or len('\n'.join(decision['items'])) > 2000:
            raise ContractError('目标需要不超过2000字符的明确验收标准')
        p['goal_proposal'] = {'goal': decision['title'], 'criteria': decision['items'], 'scope': decision['content']}
        p['status'] = 'waiting_input'
        p['phase'] = 'discovery'
        p.pop('awaiting_supervisor', None)
        p.pop('message_resume_status', None)
        say(p, 'steward', '建议目标：'+decision['title']+'\n完成标准：\n'+'\n'.join(decision['items'])+
            '\n'+decision['content']+'\n\n你可以直接回复意见，我们继续讨论或据此推进。', 'goal_proposal')
        return p['goal_proposal']
    if action == 'assemble_team':
        return assemble(app, p, decision)
    if action == 'delegate':
        if p.get('awaiting_supervisor'):
            raise ContractError('请先回复用户消息或明确决定调整任务，再下达委派')
        if p.get('worker_question'):
            raise ContractError('请先回答负责人的问题')
        if len(decision['items']) != 1:
            raise ContractError('委派items必须只包含一位已组建的成员编号')
        member = next((m for m in p.get('team', []) if m['id'] == decision['items'][0]), None)
        if not member:
            raise ContractError('请先组建包含所选成员的团队')
        member['status'] = 'working'
        p.pop('deferred_worker_result', None)
        p['pending_delegate'] = {'member': member['id'], 'model': member['model'], 'role': member['role'],
                                 'tools': member['tools'], 'title': decision['title'],
                                 'instruction': decision['content'], 'dialogue': []}
        return {'state': 'queued', 'member': member['id']}
    if action == 'answer_team':
        question = p.pop('worker_question', None)
        if not question:
            raise ContractError('当前没有等待总管回答的负责人问题')
        delegation = question['delegation']
        delegation['dialogue'].extend([{'role': 'worker', 'content': question['question']},
                                       {'role': 'steward', 'content': decision['content']}])
        p['pending_delegate'] = delegation
        p.pop('awaiting_supervisor', None)
        p.pop('message_resume_status', None)
        for member in p['team']:
            if member['id'] == delegation['member']:
                member['status'] = 'working'
        say(p, 'steward', '已为负责人敲定方向：'+decision['content'], 'decision')
        return {'state': 'answered', 'member': delegation['member']}
    if action == 'report':
        say(p, 'steward', decision['content'], 'progress')
        p['steps_since_report'] = 0
        return {'state': 'reported', 'continues': True}
    if action == 'export_artifact':
        return export_artifact(app, p, decision['artifact_id'])
    result = execute(app, p, decision)
    if action == 'ask_user':
        p.pop('awaiting_supervisor', None)
        p.pop('message_resume_status', None)
        say(p, 'steward', decision['content'], 'question')
    if action == 'finish':
        if p.get('worker_question'):
            raise ContractError('负责人仍在等待方向决策，不能提交验收')
        say(p, 'steward', '本阶段成果已保存，等待你验收。\n'+decision['content']+
            '\n验收依据：\n'+'\n'.join(decision['items']), 'submission')
    return result


def plugin_step(app, p, decision):
    plugin_id = decision['title']
    if p.get('learned_tools', {}).get(plugin_id) != installed(app).get(plugin_id) or plugin_id not in installed(app):
        raise ContractError('主管须先学习当前版本的插件用法')
    if installed(app)[plugin_id]['kind'] == 'web_research':
        if not permissions(app)['plugins']:
            raise ContractError('插件权限已关闭')
        result = p.pop('_prepared_web_result', None)
        if result is None:
            raise ContractError('联网插件必须由项目循环在事务外执行')
        return result
    return run_plugin(app, plugin_id, decision['content'])


def worker_step(app, p, decision, delegation):
    if decision.get('assessment') is not None:
        raise ContractError('负责人不能提交主管权限的行动评估')
    action = decision['action']
    if action not in delegation['tools']:
        raise ContractError('项目负责人请求了分工之外的工具')
    if action == 'ask_supervisor':
        p.setdefault('team_history', []).append({'kind': 'question', 'member': delegation['member'],
                                                'content': decision['content'], 'at': now()})
        p['worker_question'] = {'delegation': delegation, 'question': decision['content']}
        p.pop('pending_delegate', None)
        for member in p['team']:
            if member['id'] == delegation['member']:
                member['status'] = 'blocked'
        return {'state': 'needs_supervisor', 'member': delegation['member'], 'question': decision['content']}
    if action == 'use_plugin':
        result = plugin_step(app, p, decision)
    else:
        result = export_artifact(app, p, decision['artifact_id']) if action == 'export_artifact' else execute(app, p, decision)
    if action == 'save_artifact':
        p['artifacts'][-1]['source'] = delegation['member']
        p.pop('pending_delegate', None)
        say(p, 'steward', f"{delegation['role']}已交付“{decision['title']}”，成果已保存，接下来由总管检查。", 'delivery')
        for member in p['team']:
            if member['id'] == delegation['member']:
                member['status'] = 'delivered'
    else:
        delegation['dialogue'].append({'role': 'tool', 'content': json.dumps(result, ensure_ascii=False)})
    return result


def run(app, pid):
    app.refresh_budget(pid)
    while True:
        with app.transaction():
            p = app.get(pid)
            if p['status'] not in ('new', 'ready'):
                return p
            if consume_messages(p):
                app.save(p)
        control = getattr(app.manager, 'control', None)
        if control and control.cancelled.is_set():
            return app.pause(pid)
        operation = app._reserve(pid, 'executing')
        if not operation:
            with app.transaction():
                p = app.get(pid)
                if p['status'] == 'budget_exhausted' and (not p.get('dialogue') or p['dialogue'][-1].get('kind') != 'budget'):
                    say(p, 'steward', '当前调用预算已用完，已保存进展与待执行工作。调整调用预算后可继续。', 'budget')
                    app.save(p)
            return p
        attempted = None
        model = app.manager
        delegation = None
        try:
            p = prepare_task_resources(app, pid, operation)
            if p is None:
                return app.get(pid)
            delegation = None if p.get('awaiting_supervisor') else p.get('pending_delegate')
            context = context_for(app, p)
            recent_dialogue = [{**entry, 'content': entry['content'][:1400],
                                'content_truncated': len(entry['content']) > 1400}
                               for entry in p.get('dialogue', [])[-8:]]
            context.update(phase=p.get('phase'), dialogue=recent_dialogue,
                           awaiting_supervisor=p.get('awaiting_supervisor', False), assessment=p.get('assessment'),
                           pending_task=p.get('pending_delegate'),
                           deferred_worker_action=p.get('deferred_worker_result', {}).get('decision', {}).get('action'),
                           learned_tools=p.get('learned_tools', {}), goal_proposal=p.get('goal_proposal'),
                           resource_catalog=catalog(app), team=p.get('team', []),
                           team_history=p.get('team_history', [])[-8:],
                           worker_question=p.get('worker_question'), selection_reason=p.get('selection_reason'),
                           report_due=p.get('steps_since_report', 0) >= 3)
            from .resources import overview
            context['resource_requests'] = overview(app, p)
            if delegation:
                model = app.model_registry().get(delegation['model'])
                if model is None:
                    raise ContractError('负责人成员模型已不可用，请让总管重新分配')
                context['task'] = delegation
            role = 'team_member' if delegation else 'supervisor'
            response = (app.invoke_project_model(pid, model, role, context) if hasattr(app, 'invoke_project_model')
                        else app.invoke_model(model, role, context))
            # 模型已完成与指令可执行是两件事。即使后续校验拒绝指令，
            # 仍保留真实用量凭据，避免把失败任务中的有效模型开销漏掉。
            app.receipt(pid, model)
            decision = validate_decision(response)
            attempted = decision['action']
            from .web_plugin import is_web, prepare
            if control:
                control.check()
            web_result = prepare(app, pid, operation, decision, delegation) if is_web(app, decision) else None
            with app.transaction():
                p = app.get(pid)
                if p['status'] != 'executing' or p.get('operation') != operation:
                    return p
                if control:
                    control.check()
                if p.get('incoming'):
                    consume_messages(p)
                    if delegation:
                        p['deferred_worker_result'] = {'decision': decision, 'delegation': delegation}
                    app.event(pid, 'worker_result_deferred' if delegation else 'outdated_result_discarded', operation=operation)
                    app.save(p)
                    continue
                p['status'] = 'ready'
                p.pop('last_failure', None)
                signature = ('worker:'+delegation['member'] if delegation else 'supervisor')+action_signature(decision)
                # 评估和执行各自计数，插入assess不能绕过相同执行动作的重复保护。
                sig_key, count_key = ('assessment_signature', 'assessment_repeat') if attempted == 'assess' else ('last_signature', 'repeat_count')
                repeated = p.get(count_key, 0)+1 if signature == p.get(sig_key) else 1
                p[sig_key], p[count_key] = signature, repeated
                if attempted != 'assess':
                    p['assessment_repeat'] = 0
                if repeated >= 3:
                    p.update(status='paused', resume_state='ready', last_result={'reason': '连续重复相同动作，等待检查'})
                    say(p, 'steward', '连续动作未发生变化，已暂停，保留已有成果。', 'blocked')
                else:
                    if web_result is not None:
                        p['_prepared_web_result'] = web_result
                    p['last_result'] = (worker_step(app, p, decision, p['pending_delegate']) if delegation
                                        else supervise(app, p, decision))
                    if attempted not in ('assess', 'report', 'reply'):
                        p['last_execution_result'] = p['last_result']
                    p.setdefault('action_history', []).append({'action': attempted, 'reason': decision['reason'], 'at': now()})
                    if attempted not in ('assess', 'report'):
                        p['steps_since_report'] = p.get('steps_since_report', 0)+1
                    # 固定间隔的事实摘要不另花模型调用，不能伪造完成百分比。
                    if p.get('steps_since_report', 0) >= 3 and p['status'] == 'ready':
                        say(p, 'steward', f"阶段进度：已保存 {len(p['artifacts'])} 份成果，已用 {p['calls']} 次调用。\n"
                            f"最近动作：{attempted}。下一步判断依据：{decision['reason']}", 'progress')
                        p['steps_since_report'] = 0
                app.event(pid, 'orchestration_action', action=attempted, role='worker' if delegation else 'supervisor')
                app.save(p)
        except Exception as error:
            # 负责人接入故障先交总管决定如何补救；同一委派最多两次自动交回，
            # 不直接循环重试供应商，也不丢弃已经保存的工具凭据。
            if delegation and isinstance(error, (ModelUnavailable, TimeoutError, OSError, ContractError)):
                with app.transaction():
                    p = app.get(pid)
                    if p['status'] == 'executing' and p.get('operation') == operation:
                        failures = delegation.get('failures', 0)+1
                        if failures <= 2:
                            delegation['failures'] = failures
                            p['worker_question'] = {'delegation': delegation,
                                'question': '本次工作未完成：'+(str(error) if isinstance(error, (ModelUnavailable, ContractError)) else type(error).__name__)+
                                            '。请决定如何调整；不能声称本次已完成。'}
                            p.pop('pending_delegate', None)
                            p['status'] = 'ready'
                            for member in p['team']:
                                if member['id'] == delegation['member']:
                                    member['status'] = 'blocked'
                            say(p, 'steward', '负责人本次接入未完成，已交回总管判断补救方向。', 'progress')
                            app.event(pid, 'worker_issue', error=type(error).__name__, attempt=failures)
                            app.save(p)
                            continue
            return app._fail(pid, error, operation, failure_action=attempted)
