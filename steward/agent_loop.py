"""来源：本项目原创。模型驱动的项目循环；动作仅能进入明确授权的工具目录。"""

import hashlib
import json

from .core import ContractError, checked_text, now

ASSESSMENT_FIELDS = {'quality', 'cost', 'team', 'tools', 'gaps', 'next_action'}


TOOLS = {
    'market': '调用宿主已连接的AI超市；title为market_context.tools中的工具名，content为其JSON参数。取到资源后继续制作成果，不等于任务完成。',
    'request_resource': '仅主管使用，必须附assessment。申请新的负责人API，等待人工批准，不会调用或配置服务。content为JSON：service对象含id、base_url、model、input_price、output_price（价格未知null，美元/百万Token）；purpose、alternatives、expected_benefit、data_description、cost_basis、risks为非空字符串；data_scope为task或project；tools为工具名数组；max_calls为1至100整数；estimated_cost_usd为总费用预估数字或null。task只允许save_artifact/note/ask_supervisor，project才允许读取项目历史和成果等。写明替代方案、质量/成本证据、需要发送的资料及不确定性。未知价格先申请少量试用，不能编造价格或声称货币硬上限。',
    'accept_result': '仅主管使用：用户对已提交的当前成果明确验收通过时，记录用户确认并结束当前阶段；不能把模型自检当作用户验收。content引用用户确认依据。',
    'read_history': '按title指定dialogue、user_updates、project_notes或team_history，offset为字符偏移，读取本项目完整历史的JSON文本，每次6000字符；上下文省略的历史可在此查回。',
    "assess": "主管行动前的决策记录。content必须是JSON对象：quality、cost、team、tools、gaps、next_action，值均为非空字符串；逐项说明模型质量、成本、组队、工具学习、能力缺口及下一动作。",
    "learn_tool": "title为已安装插件id。读取工具手册，保存学习记录；不授予权限，不等于实测掌握。",
    "use_plugin": "title为已学习的插件id，content为工具输入；仅执行已安装且已授权的声明式工具。",
    "reply": "主管用content回答用户当前消息，保留计划和负责人任务；无需修改工作时继续原任务。",
    "revise_work": "主管决定修改任务，content说明对用户消息的判断和调整方向，保留旧成果并开启新修订，由主管重新委派。",
    "adopt_goal": "主管根据用户对话采纳已有目标草案；content说明用户授权依据。仅在存在未处理用户消息时可用。",
    "propose_goal": "形成清晰目标草案。title为目标，items为验收标准，content说明范围与假设；等用户确认后执行。",
    "assemble_team": "组建团队。items每项是JSON字符串，字段id、role、model、tools；只能使用resource_catalog内启用的模型和工具。content说明质量/成本取舍及未知信息。",
    "answer_team": "回答项目负责人问题，content敲定方向；继续该负责人未完成的工作。",
    "ask_supervisor": "仅项目负责人使用：content向总管提出问题，不直接询问用户。",
    "report": "content向用户汇报已完成内容、证据、阻塞及下一步，保存后继续执行。",
    "export_artifact": "将artifact_id对应已保存正文导出为项目目录中的Markdown文件，返回真实文件路径；不执行代码。",
    "plan": "更新项目当前计划。items 为步骤标题；不代表步骤已执行。",
    "save_artifact": "把 content 保存为新版本成果，title 为标题；旧成果保留。",
    "read_artifact": "按 artifact_id 和 offset 读取最多8000字符的历史成果，返回总长度与下一位置。",
    "delegate": "以 title 和 content 给项目负责人布置具体任务；另消耗一次模型调用。",
    "note": "保存本项目的候选笔记；不会修改用户偏好或获得权限。",
    "ask_user": "缺少关键资料或权限时，用 content 说明具体问题并等待用户。",
    "finish": "提交当前修订的成果待用户验收。items 逐条说明验收依据，content 写限制；不等于用户已验收。",
}


def decision_schema():
    fields = {"action": {"type": "string", "enum": list(TOOLS)},
              "reason": {"type": "string", "maxLength": 2000},
              "title": {"type": "string", "maxLength": 160},
              "content": {"type": "string", "maxLength": 16000},
              "items": {"type": "array", "maxItems": 20, "items": {"type": "string", "maxLength": 1000}},
              "artifact_id": {"type": "integer", "minimum": 0},
              "offset": {"type": "integer", "minimum": 0}}
    fields['assessment'] = {'type': ['object', 'null'], 'additionalProperties': False,
                            'properties': {name: {'type': 'string', 'minLength': 1, 'maxLength': 2500}
                                           for name in sorted(ASSESSMENT_FIELDS)},
                            'required': sorted(ASSESSMENT_FIELDS)}
    return {"type": "object", "additionalProperties": False, "properties": fields, "required": list(fields)}


def validate_decision(value):
    expected = set(decision_schema()['properties'])-{'assessment'}
    if not isinstance(value, dict) or set(value)-{'assessment'} != expected:
        raise ContractError("总管动作字段不完整或包含未授权字段")
    assessment = value.get('assessment')
    if assessment is not None:
        if not isinstance(assessment, dict) or set(assessment) != ASSESSMENT_FIELDS:
            raise ContractError('行动评估字段不完整')
        for field in ASSESSMENT_FIELDS:
            checked_text(assessment[field], field, 2500)
    if not isinstance(value['action'], str) or value['action'] not in TOOLS:
        raise ContractError("总管请求了未授权工具")
    checked_text(value['reason'], '决策理由', 2000)
    for key, size in [('title', 160), ('content', 16000)]:
        if not isinstance(value[key], str) or len(value[key]) > size:
            raise ContractError(f'{key}格式错误')
    if not isinstance(value['items'], list) or len(value['items']) > 20:
        raise ContractError('items必须为不超过20项的列表')
    for item in value['items']:
        checked_text(item, '列表项', 1000)
    for key in ('artifact_id', 'offset'):
        if type(value[key]) is not int or value[key] < 0:
            raise ContractError(f'{key}必须为非负整数')
    if value['action'] in ('save_artifact', 'delegate'):
        checked_text(value['title'], '任务/成果标题', 160)
    if value['action'] in ('save_artifact', 'delegate', 'note', 'ask_user', 'finish'):
        checked_text(value['content'], '动作内容', 16000)
    if value['action'] in ('plan', 'finish') and not value['items']:
        raise ContractError('计划和验收必须给出具体条目')
    if value['action'] in ('propose_goal', 'assemble_team', 'answer_team', 'ask_supervisor', 'report'):
        checked_text(value['content'], '动作内容', 16000)
    return value


def action_signature(decision):
    # 仅比较执行器实际使用的参数，避免模型改变无关占位字段绕过重复保护。
    parameters = {'plan': ('items',), 'save_artifact': ('title', 'content'),
                  'read_artifact': ('artifact_id', 'offset'), 'delegate': ('title', 'content'),
                  'note': ('content',), 'ask_user': ('content',), 'finish': ('items', 'content')}
    parameters.update(propose_goal=('title', 'items', 'content'), assemble_team=('items', 'content'),
                      answer_team=('content',), ask_supervisor=('content',), report=('content',),
                      export_artifact=('artifact_id',))
    parameters.update(assess=('content',), learn_tool=('title',), use_plugin=('title', 'content'),
                      reply=('content',), revise_work=('content',), adopt_goal=('content',))
    parameters['read_history'] = ('title', 'offset')
    parameters['accept_result'] = ('content',)
    parameters['request_resource'] = ('content',)
    parameters['market'] = ('title', 'content')
    if decision['action'] == 'delegate':
        parameters['delegate'] = ('title', 'content', 'items')
    effective = {'action': decision['action']}
    effective.update({key: decision[key] for key in parameters[decision['action']]})
    return hashlib.sha256(json.dumps(effective, sort_keys=True).encode()).hexdigest()


def prepare_task_resources(app, pid, operation):
    """Persist the read-only discovery before calling a model; keep control ownership."""
    gateway = getattr(app, 'market_gateway', None)
    if gateway is None:
        return app.get(pid)
    with app.transaction():
        p = app.get(pid)
        if p['status'] != 'executing' or p.get('operation') != operation:
            return None
        if gateway and gateway.begin(p):
            app.save(p)
        return p


def context_for(app, p):
    """正文按需读取；所有成果都保留可寻址索引，预览截断有明确标识。"""
    used = app.db.execute('SELECT used FROM limits WHERE id=1').fetchone()[0]
    project_remaining = max(0, p['max_calls']-p['calls'])
    global_remaining = max(0, app.global_budget()-used)
    return {"goal": p['goal'], "acceptance": p['acceptance'], "revision": p.get('revision', 0),
            "user_updates": p.get('user_updates', [])[-3:], "feedback": p.get('feedback'),
            "history_counts": {key: len(p.get(key, [])) for key in ('dialogue', 'user_updates', 'project_notes', 'team_history')},
            "history_notice": '历史只包含近期片段，旧约束不能视为失效；需要时用read_history分段读取原始完整记录。',
            "plan": p.get('agent_plan', []), "notes": p.get('project_notes', [])[-3:],
            "memories": app.memories(),
            "tools": {k: v for k, v in TOOLS.items() if k != 'market' or getattr(app, 'market_gateway', None)},
            **({'market_context': app.market_gateway.context(p)} if getattr(app, 'market_gateway', None) else {}),
            "remaining_calls": min(project_remaining, global_remaining),
            "budget": {'project_remaining': project_remaining, 'global_remaining': global_remaining,
                       'note': '当前调用已占用名额；此处是可用于后续调用的快照，实际派发会再次核验'},
            "artifacts": [{"artifact_id": i, "title": a.get('title', f'历史成果{i}'),
                           "revision": a.get('revision', 0), "length": len(a['content']),
                           "preview": a['content'][:500], "preview_truncated": len(a['content']) > 500}
                          for i, a in enumerate(p['artifacts'])],
            "last_result": p.get('last_result'),
            "last_execution_result": p.get('last_execution_result'),
            "files": p.get('files', []),
            "last_failure": p.get('last_failure'),
            "recent_actions": p.get('action_history', [])[-6:]}


def store_artifact(p, title, content, source):
    index = len(p['artifacts'])
    p['artifacts'].append({"task": len(p['tasks']), "title": title, "content": content,
                           "revision": p.get('revision', 0), "source": source,
                           "verification": "已保存；内容与任务效果仍需用户验收"})
    p['tasks'].append({"title": title, "status": "done", "capability": "draft", "quality": 2})
    return {"tool": "save_artifact", "artifact_id": index, "saved": True, "length": len(content)}


def execute(app, p, decision):
    action = decision['action']
    if action == 'market':
        gateway = getattr(app, 'market_gateway', None)
        if gateway is None:
            raise ContractError('宿主尚未连接超市，不能由模型自行指定连接。')
        if p.get('pending_delegate') or p.get('awaiting_supervisor') or p.get('phase') == 'discovery':
            raise ContractError('须先明确当前任务；本候选仅由主管调用超市。')
        return gateway.call(p, decision['title'], decision['content'])
    if action == 'read_history':
        category, offset = decision['title'], decision['offset']
        if category not in ('dialogue', 'user_updates', 'project_notes', 'team_history'):
            raise ContractError('历史类别不支持')
        encoded = json.dumps(p.get(category, []), ensure_ascii=False)
        if offset > len(encoded):
            raise ContractError('历史偏移超过记录长度')
        end = min(len(encoded), offset+6000)
        return {'tool': action, 'category': category, 'content': encoded[offset:end],
                'length': len(encoded), 'next_offset': end if end < len(encoded) else None}
    if action == 'plan':
        p['agent_plan'] = decision['items']
        return {"tool": action, "saved": True, "items": p['agent_plan']}
    if action == 'save_artifact':
        return store_artifact(p, decision['title'], decision['content'], 'steward')
    if action == 'read_artifact':
        index, offset = decision['artifact_id'], decision['offset']
        if index >= len(p['artifacts']):
            raise ContractError('成果编号不存在')
        text = p['artifacts'][index]['content']
        if offset > len(text):
            raise ContractError('读取位置超出成果长度')
        end = min(len(text), offset+8000)
        return {"tool": action, "artifact_id": index, "content": text[offset:end],
                "total_length": len(text), "next_offset": end if end < len(text) else None}
    if action == 'delegate':
        p['pending_delegate'] = {"title": decision['title'], "instruction": decision['content']}
        return {"tool": action, "state": "queued", "note": "尚未执行，等待项目负责人调用"}
    if action == 'note':
        notes = p.setdefault('project_notes', [])
        if len(notes) >= 20:
            raise ContractError('项目笔记已达20条，请使用已有笔记，不自动覆盖')
        notes.append({"text": decision['content'], "source": "model_candidate", "at": now()})
        return {"tool": action, "saved": True, "scope": "current_project_only"}
    if action == 'ask_user':
        p['question'] = decision['content']
        p['status'] = 'waiting_input'
        return {"tool": action, "state": "waiting_input", "question": p['question']}
    if action == 'finish':
        current = [i for i, a in enumerate(p['artifacts']) if a.get('revision', 0) == p.get('revision', 0)]
        if not current:
            raise ContractError('本次修订尚无已保存成果，不能提交验收')
        if p.get('pending_delegate'):
            raise ContractError('仍有待执行委派，不能提交验收')
        p['submission'] = {"artifact_ids": current, "criteria": decision['items'],
                           "limitations": decision['content'], "kind": "model_self_review"}
        p['status'] = 'awaiting_review'
        return {"tool": action, "state": "awaiting_review", "artifact_ids": current,
                "note": "模型自检不是独立验收，也不证明外部业务完成"}
    raise ContractError('此动作需要从新需求讨论入口启动的总管工作流')


def run_project(app, pid):
    p = app.refresh_budget(pid)
    if p['manager'] != app.manager.name or p['worker'] != app.worker.name:
        raise ValueError('项目模型与当前配置不一致')
    while True:
        p = app.get(pid)
        if p['status'] not in ('new', 'ready'):
            return p
        control = getattr(app.manager, 'control', None)
        if control and control.cancelled.is_set():
            return app.pause(pid)
        operation = app._reserve(pid, 'executing')
        if not operation:
            return app.get(pid)
        p = app.get(pid)
        delegation = p.get('pending_delegate')
        attempted_action = 'delegate' if delegation else None
        try:
            p = prepare_task_resources(app, pid, operation)
            if p is None:
                return app.get(pid)
            context = context_for(app, p)
            if delegation:
                context['task'] = {'title': delegation['title'], 'instruction': delegation['instruction']}
                result = app.invoke_model(app.worker, 'project_manager', context)
                if not isinstance(result, dict) or set(result) != {'content'}:
                    raise ContractError('项目负责人输出必须只包含content')
                content = checked_text(result['content'], '项目负责人成果', 16000)
            else:
                decision = validate_decision(app.invoke_model(app.manager, 'agent', context))
                attempted_action = decision['action']
            # 同一执行令牌只有一次提交；迟到响应不能覆盖暂停或已恢复的新执行。
            with app.transaction():
                p = app.get(pid)
                if p['status'] != 'executing' or p.get('operation') != operation:
                    return p
                if control:
                    control.check()
                p['status'] = 'ready'
                p.pop('last_failure', None)
                if delegation:
                    p['last_result'] = store_artifact(p, delegation['title'], content, 'project_manager')
                    p.pop('pending_delegate', None)
                    app.receipt(pid, app.worker)
                    app.event(pid, 'agent_action', action='delegate_completed')
                else:
                    # 忽略理由变化，识别重复工具参数；连续三次重复时保存并暂停。
                    signature = action_signature(decision)
                    repeated = p.get('repeat_count', 0)+1 if p.get('last_signature') == signature else 1
                    p['repeat_count'], p['last_signature'] = repeated, signature
                    if repeated >= 3:
                        p['status'], p['resume_state'] = 'paused', 'ready'
                        p['last_result'] = {'state': 'paused', 'reason': '连续重复相同动作，等待用户检查'}
                        app.event(pid, 'loop_paused')
                    else:
                        p['last_result'] = execute(app, p, decision)
                    p.setdefault('action_history', []).append({"action": decision['action'],
                                                               "reason": decision['reason'], "at": now()})
                    app.receipt(pid, app.manager)
                    app.event(pid, 'agent_action', action=decision['action'], state=p['status'])
                app.save(p)
        except Exception as error:
            return app._fail(pid, error, operation, failure_action=attempted_action)
