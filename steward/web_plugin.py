"""来源：本项目原创。联网只读插件的权限、预算与事务外执行。"""
from .core import ContractError, checked_text, now
from .extensions import installed, permissions
from .codex_model import CodexModel, ModelUnavailable


def is_web(app, decision):
    return decision['action'] == 'use_plugin' and installed(app).get(decision['title'], {}).get('kind') == 'web_research'


def prepare(app, pid, operation, decision, delegation):
    """外发前验证真实权限；网络等待期间不持有数据库写锁。"""
    with app.transaction():
        p = app.get(pid)
        if p['status'] != 'executing' or p.get('operation') != operation or p.get('incoming'):
            return None
        plugin = installed(app).get(decision['title'])
        if not plugin or plugin != p.get('learned_tools', {}).get(decision['title']):
            raise ContractError('主管须先学习当前版本联网插件')
        if not permissions(app)['plugins']:
            raise ContractError('插件权限已关闭')
        app.before_call()
        if delegation:
            if 'use_plugin' not in delegation['tools']:
                raise ContractError('负责人未获插件工具权限')
        else:
            assessment = decision.get('assessment') or p.get('assessment')
            if p.get('phase') != 'execution' or not assessment or assessment.get('next_action') != 'use_plugin':
                raise ContractError('总管须确认目标并完成本次联网评估')
        query = checked_text(decision['content'], '联网查询', 4000)
        from .agent_loop import action_signature
        signature = ('worker:'+delegation['member'] if delegation else 'supervisor')+action_signature(decision)
        if p.get('last_signature') == signature and p.get('repeat_count', 0) >= 2:
            return {'state': 'repeat_blocked', 'content': '重复保护阻止本次查询，未占用额外额度。'}
        used = app.db.execute('SELECT used FROM limits WHERE id=1').fetchone()[0]
        if p['calls'] >= p['max_calls'] or used >= app.global_budget():
            return {'state': 'budget_exhausted', 'content': '联网插件额外需要1次调用名额；本次没有发送查询。'}
        p['calls'] += 1
        app.db.execute('UPDATE limits SET used=used+1 WHERE id=1')
        app.save(p)
        app.event(pid, 'plugin_call_reserved', plugin=plugin['id'], call=p['calls'], operation=operation)
    model = CodexModel(control=getattr(app.manager, 'control', None), runtime=app.db_path.parent/'model-runs')
    try:
        value = model.respond('web_research', {'query': query, 'date': now()[:10]})
        app.receipt(pid, model)
        if not isinstance(value, dict) or set(value) != {'content'}:
            raise ContractError('联网插件结果格式不完整')
        content = checked_text(value['content'], '调研结果', 16000)
        return {'state': 'researched', 'content': content, 'retrieved_at': now(),
                'web_tool_calls': model.last_receipt['web_tool_calls'],
                'note': '网页工具已执行；摘要由模型整理，具体结论仍须对照引用核查。'}
    except (ModelUnavailable, ContractError, OSError) as error:
        app.event(pid, 'plugin_call_failed', plugin=plugin['id'], error=type(error).__name__)
        return {'state': 'unavailable', 'content': '本轮联网未完成，未退回已占用额度；请主管调整查询或报告阻塞。',
                'error_type': type(error).__name__}
