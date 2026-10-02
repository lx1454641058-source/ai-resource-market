"""来源：本项目原创。用户已明确表达的偏好；不包含推测的人格资料。"""

from pathlib import Path

from .codex_model import CodexModel
from .core import CallBlocked, Steward


from .paths import ROOT, data_directory
DATABASE = data_directory() / "personal-steward.sqlite"


class PersonalSteward(Steward):
    def model_registry(self):
        models = {self.worker.name: self.worker}
        models.update(getattr(self, 'extra_workers', {}))
        from .extensions import setting, permissions
        from .api_model import ApiModel
        if permissions(self)['api']:
            for config in setting(self, 'api_connections', {}).values():
                if config['enabled']:
                    model = ApiModel(config, getattr(self.manager, 'control', None), self.db_path.parent / 'credentials')
                    models[model.name] = model
        return models

    def save_api_connection(self, config):
        from .api_model import validate_connection
        from .extensions import setting, save_setting
        validate_connection(config)
        with self.transaction():
            configs = setting(self, 'api_connections', {})
            configs[config['id']] = config
            save_setting(self, 'api_connections', configs)

    def credential_vault(self):
        from .credentials import CredentialVault
        return CredentialVault(self.db_path.parent / 'credentials')

    def test_connection(self, config=None, control=None):
        """用户主动点击才测试；仅发送固定测试语句，计入全局调用名额。"""
        from .api_model import ApiModel
        from .extensions import permissions, save_setting
        from .credentials import fingerprint
        from .core import ContractError, now
        from .agent_loop import validate_decision
        model = (ApiModel(config, control, self.db_path.parent / 'credentials') if config
                 else CodexModel(control=control, runtime=self.db_path.parent / 'model-runs'))
        if config and not permissions(self)['api']:
            raise ContractError('请先在权限设置中允许API调用，然后测试连接')
        with self.transaction():
            used = self.db.execute('SELECT used FROM limits WHERE id=1').fetchone()[0]
            if used >= self.global_budget():
                raise ContractError('全局调用额度已用完，请先调整额度')
            self.db.execute('UPDATE limits SET used=used+1 WHERE id=1')
            save_setting(self, 'connection_test_'+model.name, {})
            self.event(None, 'connection_test_started', model=model.name)
        # 真实验证负责人动作协议，避免仅返回任意文本也标为接入成功。
        context = {'task': {'role': '连接验收员', 'instruction': '仅调用note记录：连接成功。',
                           'tools': ['note'], 'dialogue': []}}
        result = validate_decision(model.respond('team_member', context))
        if result['action'] != 'note' or not result['content'].strip():
            raise ContractError('连接收到响应，但模型未通过动作协议测试，请检查兼容性')
        receipt = dict(model.last_receipt or {}, checked_at=now(), protocol_ok=True,
                       fingerprint=fingerprint(config) if config else 'codex-supervisor')
        with self.transaction():
            save_setting(self, 'connection_test_'+model.name, receipt)
            self.receipt(None, model)
        return receipt

    def invoke_model(self, model, role, context):
        if getattr(model, 'mode', None) == 'api':
            raise CallBlocked('API必须通过有项目授权的负责人委派调用')
        return super().invoke_model(model, role, context)

    def invoke_project_model(self, pid, model, role, context):
        if getattr(model, 'mode', None) != 'api':
            return self.invoke_model(model, role, context)
        from .resources import reserve, settle
        self.before_call()
        request_id, operation, safe_context = reserve(self, pid, model, role, context)
        model.last_receipt = None
        try:
            return super().invoke_model(model, role, safe_context)
        finally:
            settle(self, pid, request_id, operation, model.last_receipt)

    def decide_resource(self, pid, request_id, choice):
        from .resources import decide
        return decide(self, pid, request_id, choice)

    def begin_request(self, message):
        self.require_context_sharing()
        return self.create(checked_project_message(message), '与用户讨论后确定', max_calls=20, discussion=True)

    def enable_worker_models(self, names):
        if not isinstance(names, list) or len(names) > 8:
            raise ValueError('最多启用8个负责人模型')
        models = {name: CodexModel(model=name, control=getattr(self.manager, 'control', None),
                                  runtime=self.db_path.parent / 'model-runs') for name in names}
        self.event(None, 'worker_models_enabled', names=list(models), source='explicit_user')
        self.extra_workers = models

    def confirm_goal(self, pid):
        from .orchestration import confirm_goal
        return confirm_goal(self, pid)

    def configure_project(self, project):
        project['workflow'] = 'agent-v1'
        project['revision'] = 0

    def context_sharing_allowed(self):
        row = self.db.execute("SELECT body FROM events WHERE kind='context_sharing_consent' ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return False
        import json
        return json.loads(row[0]).get("allowed") is True

    def set_context_sharing(self, allowed):
        if type(allowed) is not bool:
            raise ValueError("授权必须是明确的允许或拒绝")
        self.event(None, "context_sharing_consent", allowed=allowed,
                   destination="OpenAI / Codex / gpt-6-astra", scope="偏好、近期对话、相关项目目标及状态")

    def require_context_sharing(self):
        if not self.context_sharing_allowed():
            raise CallBlocked("尚未授权发送个人上下文，请先打开‘权限设置’启用。")

    def before_call(self):
        self.require_context_sharing()

    def run(self, pid):
        self.require_context_sharing()
        from .run_lock import project_lock, recover
        with project_lock(self.db_path, pid):
            recover(self, pid)
            with self.transaction():
                p = self.get(pid)
                p['run_lock_version'] = 1
                self.save(p)
            return self._run_owned(pid)

    def _run_owned(self, pid):
        # Historical portable packages contain only steward. Keep their ordinary
        # tasks usable; a saved market connection must never silently disappear.
        from .extensions import setting
        if setting(self, 'project_market_' + pid, None) is not None or getattr(self, '_settings_market_gateway', None) is not None:
            try:
                from .market_settings import attach_for_run
            except ModuleNotFoundError as exc:
                if exc.name != 'ai_supermarket' and not exc.name.startswith('ai_supermarket.'):
                    raise
                from .core import ContractError
                raise ContractError('当前程序缺少超市模块，无法加载已保存连接；请使用配套完整源码。') from None
            attach_for_run(self, self.get(pid))
        if self.get(pid).get('orchestration'):
            from .orchestration import run
            return run(self, pid)
        if self.get(pid).get('workflow') == 'agent-v1':
            from .agent_loop import run_project
            return run_project(self, pid)
        return super().run(pid)

    def answer_project(self, pid, message):
        message = checked_project_message(message)
        if self.get(pid).get('orchestration'):
            from .resources import handle_command
            decided = handle_command(self, pid, message)
            if decided is not None:
                return decided
            from .orchestration import submit_message
            return submit_message(self, pid, message)
        with self.transaction():
            p = self.get(pid)
            if p.get('kind') == 'conversation' or p['status'] not in (
                    'new', 'ready', 'paused', 'failed', 'budget_exhausted',
                    'waiting_input', 'awaiting_review', 'rejected', 'accepted'):
                raise ValueError('项目正在执行，请先在运行窗口停止，再提交新要求')
            if len(p.get('user_updates', [])) >= 20:
                raise ValueError('本项目补充记录已达20条；请创建后续项目并提供相关依据')
            # 用户明确提交新要求才接续旧版项目；保留旧任务和成果供回查。
            if p.get('workflow') != 'agent-v1':
                p['workflow'] = 'agent-v1'
                p.setdefault('revision', 0)
            if p.get('artifacts') or p['status'] in ('awaiting_review', 'rejected', 'accepted'):
                p['revision'] = p.get('revision', 0)+1
            p.pop('submission', None)
            # 旧委派可能与新指令相冲突，由总管基于新要求重新决定。
            p.pop('pending_delegate', None)
            p.pop('resume_state', None)
            p.pop('last_failure', None)
            p.setdefault('user_updates', []).append({'text': message, 'source': 'user'})
            p['status'] = 'ready'
            p.pop('question', None)
            p['repeat_count'] = 0
            p['error'] = None
            p.pop('error_message', None)
            self.save(p)
            self.event(pid, 'user_update', revision=p.get('revision', 0))
        return p

    def chat(self, message):
        self.require_context_sharing()
        return super().chat(message)

    def retry(self, pid):
        self.require_context_sharing()
        return super().retry(pid)


def open_personal(db_path=None, control=None):
    path = Path(db_path or DATABASE)
    app = PersonalSteward(path, CodexModel(control=control, runtime=path.parent / 'model-runs'))
    import json
    row = app.db.execute("SELECT body FROM events WHERE kind='worker_models_enabled' ORDER BY id DESC LIMIT 1").fetchone()
    if row:
        app.extra_workers = {name: CodexModel(model=name, control=control, runtime=path.parent / 'model-runs') for name in json.loads(row[0])['names']}
    initialized = app.db.execute("SELECT 1 FROM events WHERE kind='profile_initialized' LIMIT 1").fetchone()
    if not initialized:
        # 已有档案保留；新学员不继承创作者的个人偏好。
        app.event(None, "profile_initialized", source="空白档案；偏好由当前用户自行保存")
    from .run_lock import project_lock, recover, RunBusy
    for project in app.projects():
        if project.get('run_lock_version') == 1 and project['status'] in ('planning', 'executing'):
            try:
                with project_lock(app.db_path, project['id']):
                    recover(app, project['id'])
            except RunBusy:
                pass
    return app


def checked_project_message(message):
    from .core import checked_text
    return checked_text(message, '补充/修改要求', 4000)


def main():
    from .gui import main as gui_main
    gui_main(personal=True)


if __name__ == "__main__":
    main()
