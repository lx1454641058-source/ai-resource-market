"""来源：本项目原创生成，未复制第三方框架实现。单用户本地原型。"""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import DemoModel


class ContractError(ValueError):
    """框架自身生成的字段错误，可以向用户展示。"""


class CallBlocked(ValueError):
    """发送前权限检查失败，可安全展示。"""


def now():
    return datetime.now(timezone.utc).isoformat()


def checked_text(value, label, limit=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ContractError(f"{label}必须为1至{limit}个字符")
    return value.strip()


def validate_plan(plan):
    if not isinstance(plan, dict) or set(plan) != {"tasks"}:
        raise ContractError("计划必须只包含 tasks 字段")
    tasks = plan["tasks"]
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 6:
        raise ContractError("任务数量必须为1至6个")
    result = []
    for task in tasks:
        if not isinstance(task, dict) or set(task) != {"title", "capability", "quality"}:
            raise ContractError("任务字段不符合契约")
        title = checked_text(task["title"], "任务标题", 160)
        if task["capability"] not in ("outline", "draft"):
            raise ContractError("工具能力未授权；首版仅允许提纲和文档草稿")
        if type(task["quality"]) is not int or task["quality"] not in (1, 2):
            raise ContractError("quality必须为整数1或2")
        if any(t["title"] == title for t in result):
            raise ContractError("计划存在重复任务")
        result.append({**task, "title": title, "status": "pending"})
    return result


class Steward:
    """事务保护本地状态；模型没有数据库、审批或任意命令执行工具。"""

    def __init__(self, db_path, manager=None, worker=None):
        self.manager = manager or DemoModel()
        self.worker = worker or self.manager
        path = Path(db_path)
        self.db_path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=55, isolation_level=None)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS memories(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY, project TEXT, at TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS limits(id INTEGER PRIMARY KEY CHECK(id=1), used INTEGER NOT NULL);
            INSERT OR IGNORE INTO limits VALUES(1,0);
        """)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def event(self, pid, kind, **data):
        # 只写事件元数据；不保存提示词、模型原始错误或凭证。
        self.db.execute("INSERT INTO events(project,at,kind,body) VALUES(?,?,?,?)",
                        (pid, now(), kind, json.dumps(data, ensure_ascii=False)))

    def get(self, pid):
        row = self.db.execute("SELECT body FROM projects WHERE id=?", (pid,)).fetchone()
        if row is None:
            raise ValueError("项目不存在")
        return json.loads(row[0])

    def save(self, project):
        project["updated"] = now()
        self.db.execute("UPDATE projects SET body=? WHERE id=?",
                        (json.dumps(project, ensure_ascii=False), project["id"]))

    def projects(self, include_deleted=False):
        values = [json.loads(r[0]) for r in self.db.execute("SELECT body FROM projects ORDER BY rowid")]
        return values if include_deleted else [p for p in values if p['status'] != 'deleted']

    def delete_project(self, pid):
        with self.transaction():
            p = self.get(pid)
            if p['status'] in ('planning', 'executing'):
                raise ContractError('任务正在运行，请先停止再删除')
            if p['status'] != 'deleted':
                p['restore_status'], p['status'] = p['status'], 'deleted'
                self.save(p)
                self.event(pid, 'deleted', reversible=True)
        return p

    def restore_project(self, pid):
        with self.transaction():
            p = self.get(pid)
            if p['status'] != 'deleted':
                raise ContractError('任务不在回收站')
            p['status'] = p.pop('restore_status')
            self.save(p)
            self.event(pid, 'restored')
        return p

    def memories(self):
        return {key: json.loads(value) for key, value in self.db.execute("SELECT key,value FROM memories")}

    def remember(self, key, value):
        key = checked_text(key, "记忆名称", 80)
        value = checked_text(value, "记忆内容", 1000)
        with self.transaction():
            count = self.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            exists = self.db.execute("SELECT 1 FROM memories WHERE key=?", (key,)).fetchone()
            if not exists and count >= 20:
                raise ValueError("首版最多保存20条用户记忆")
            body = {"value": value, "source": "explicit_user", "updated": now()}
            self.db.execute("INSERT OR REPLACE INTO memories VALUES(?,?)", (key, json.dumps(body)))
            self.event(None, "memory_updated", key=key)

    def forget(self, key):
        with self.transaction():
            self.db.execute("DELETE FROM memories WHERE key=?", (key,))
            self.event(None, "memory_forgotten", key=key)

    def create(self, goal, acceptance, max_calls=8, *, discussion=False):
        goal = checked_text(goal, "目标")
        acceptance = checked_text(acceptance, "验收条件", 2000)
        if type(max_calls) is not int or not 1 <= max_calls <= 20:
            raise ValueError("模型调用预算必须为1至20")
        pid = uuid.uuid4().hex[:12]
        project = {"id": pid, "goal": goal, "acceptance": acceptance, "status": "new",
                   "created": now(), "updated": now(), "calls": 0, "max_calls": max_calls,
                   "manager": self.manager.name, "worker": self.worker.name,
                   "tasks": [], "artifacts": [], "learning": [], "error": None,
                   "mode": getattr(self.manager, "mode", "demo" if isinstance(self.manager, DemoModel) else "local_model"),
                   "reasoning_effort": getattr(self.manager, "effort", None)}
        with self.transaction():
            self.configure_project(project)
            if discussion:
                project.update(orchestration=True, phase='discovery', original_request=goal,
                               dialogue=[{'role': 'user', 'content': goal, 'kind': 'message', 'at': now()}])
            self.db.execute("INSERT INTO projects VALUES(?,?)", (pid, json.dumps(project)))
            self.event(pid, "created", mode=project["mode"])
        return project

    def configure_project(self, project):
        """创建事务内补充运行类型；失败时不会残留只有部分字段的项目。"""

    def global_budget(self):
        row = self.db.execute("SELECT body FROM events WHERE kind='budget_config' ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(row[0])['total'] if row else 100

    def before_call(self):
        """本地演示无外发授权；个人版覆盖此钩子，每次调用前执行。"""

    def invoke_model(self, model, role, context):
        self.before_call()
        return model.respond(role, context)

    def refresh_budget(self, pid):
        with self.transaction():
            p = self.get(pid)
            used = self.db.execute('SELECT used FROM limits WHERE id=1').fetchone()[0]
            if p['status'] == 'budget_exhausted' and p['calls'] < p['max_calls'] and used < self.global_budget():
                p['status'] = 'ready' if p['tasks'] or p.get('workflow') or p.get('kind') else 'new'
                self.save(p)
                self.event(pid, 'budget_available')
        return p

    def set_budget(self, pid, total, global_total):
        if type(total) is not int or type(global_total) is not int:
            raise ValueError('调用名额必须为整数')
        with self.transaction():
            p = self.get(pid)
            used = self.db.execute('SELECT used FROM limits WHERE id=1').fetchone()[0]
            if p['status'] in ('planning', 'executing'):
                raise ValueError('请先停止当前调用再调整预算')
            if not p['calls'] <= total <= 100000 or not used <= global_total <= 100000:
                raise ValueError('总名额不能少于已用次数，且不能超过100000')
            p['max_calls'] = total
            if p['status'] == 'budget_exhausted' and total > p['calls'] and global_total > used:
                p['status'] = 'ready' if p['tasks'] or p.get('workflow') or p.get('kind') else 'new'
            self.save(p)
            self.event(None, 'budget_config', total=global_total, source='explicit_user')
            self.event(pid, 'project_budget', total=total, source='explicit_user')
        return p

    def _reserve(self, pid, stage, task_index=None, message=None):
        # 调用之前落盘扣除名额；超时/崩溃也不退回，避免重启绕过预算。
        with self.transaction():
            p = self.get(pid)
            if p["status"] not in ("new", "ready"):
                return False
            self.before_call()
            if stage == "planning" and p["status"] != "new":
                return False
            if task_index is not None and (p["status"] != "ready" or p["tasks"][task_index]["status"] != "pending"):
                return False
            used = self.db.execute("SELECT used FROM limits WHERE id=1").fetchone()[0]
            operation = uuid.uuid4().hex
            if message is not None:
                p.setdefault('messages', []).append({'role': 'user', 'content': message,
                                                     'request_id': operation, 'status': 'not_sent'})
            if p["calls"] >= p["max_calls"] or used >= self.global_budget():
                p["status"] = "budget_exhausted"
                self.save(p)
                self.event(pid, "budget_exhausted")
                return False
            p["calls"] += 1
            p["status"] = stage
            p["operation"] = operation
            if message is not None:
                p['messages'][-1]['status'] = 'pending'
            self.db.execute("UPDATE limits SET used=used+1 WHERE id=1")
            self.save(p)
            self.event(pid, "call_reserved", stage=stage, call=p["calls"])
        return p["operation"]

    def route(self, task):
        # 这是可解释的候选目录比较，不是已经实测的全球最优采购。
        offers = [
            {"tool": "outline_template", "capabilities": ["outline"], "quality": 1, "calls": 0},
            {"tool": "model_draft", "capabilities": ["outline", "draft"], "quality": 2, "calls": 1},
        ]
        eligible = [o for o in offers if task["capability"] in o["capabilities"]
                    and o["quality"] >= task["quality"]]
        if not eligible:
            raise ValueError("没有满足约束的已授权工具")
        chosen = min(eligible, key=lambda o: o["calls"])
        return {"tool": chosen["tool"], "model": self.worker.name if chosen["calls"] else None,
                "estimated_calls": chosen["calls"], "basis": "候选能力等级为假设，非实测评分",
                "reason": "先满足能力与最低质量等级，再选择模型调用次数最少的候选", "offers": offers}

    def run(self, pid):
        p = self.refresh_budget(pid)
        if p["manager"] != self.manager.name or p["worker"] != self.worker.name:
            raise ValueError("请使用创建项目时的模型；模型迁移需重新验证，不能静默替换")
        if p["status"] == "new":
            operation = self._reserve(pid, "planning")
            if not operation:
                return self.get(pid)
            try:
                plan = self.invoke_model(self.manager, "steward", {
                    "goal": p["goal"], "acceptance": p["acceptance"], "memories": self.memories()})
                tasks = validate_plan(plan)
                with self.transaction():
                    p = self.get(pid)
                    if p["status"] != "planning" or p.get("operation") != operation:
                        return p
                    p["tasks"], p["status"] = tasks, "ready"
                    self.save(p)
                    self.event(pid, "planned", tasks=len(tasks), model=self.manager.name, prompt="v1")
                    self.receipt(pid, self.manager)
            except Exception as error:
                return self._fail(pid, error, operation)
        for _ in range(6):
            p = self.get(pid)
            if p["status"] != "ready":
                return p
            control = getattr(self.worker, 'control', None)
            if control and control.cancelled.is_set():
                return self.pause(pid)
            index = next((i for i, t in enumerate(p["tasks"]) if t["status"] == "pending"), None)
            if index is None:
                with self.transaction():
                    p = self.get(pid)
                    if p["status"] == "ready":
                        p["status"] = "awaiting_review"
                        self.save(p)
                        self.event(pid, "awaiting_review")
                return self.get(pid)
            task = p["tasks"][index]
            choice = self.route(task)
            if choice["estimated_calls"]:
                operation = self._reserve(pid, "executing", index)
                if not operation:
                    return self.get(pid)
            else:
                with self.transaction():
                    p = self.get(pid)
                    if p["status"] != "ready" or p["tasks"][index]["status"] != "pending":
                        return p
                    p["status"] = "executing"
                    operation = p["operation"] = uuid.uuid4().hex
                    self.save(p)
            try:
                if choice["tool"] == "outline_template":
                    content = (f"# 目标与验收提纲\n\n目标：{p['goal']}\n\n"
                               f"用户验收条件：{p['acceptance']}\n\n"
                               "待补充：真实样本、约束、执行步骤、风险和验证证据。\n"
                               "来源：本地规则模板；不代表已完成目标。")
                else:
                    result = self.invoke_model(self.worker, "project_manager", {
                        "goal": p["goal"], "acceptance": p["acceptance"], "task": task,
                        "memories": self.memories(),
                        "prior_artifacts": [{"content": a["content"][:2000]} for a in p["artifacts"][-2:]]})
                    if not isinstance(result, dict) or set(result) != {"content"}:
                        raise ContractError("草稿输出必须仅含content")
                    content = checked_text(result["content"], "草稿", 16000)
                with self.transaction():
                    p = self.get(pid)
                    if p["status"] != "executing" or p.get("operation") != operation:
                        return p  # 用户暂停后迟到的响应不能恢复执行。
                    p["tasks"][index].update(status="done", routing=choice)
                    p["artifacts"].append({"task": index, "content": content,
                                           "verification": "仅验证非空与长度；语义待用户验收"})
                    p["status"] = "ready" if any(t["status"] == "pending" for t in p["tasks"]) else "awaiting_review"
                    self.save(p)
                    self.event(pid, "artifact_saved", task=index, tool=choice["tool"], model=choice["model"])
                    if choice["estimated_calls"]:
                        self.receipt(pid, self.worker)
            except Exception as error:
                return self._fail(pid, error, operation)
        return self.get(pid)

    def _fail(self, pid, error, operation, failure_action=None):
        with self.transaction():
            p = self.get(pid)
            if p["status"] in ("planning", "executing") and p.get("operation") == operation:
                for message in p.get('messages', []):
                    if message.get('request_id') == operation:
                        message['status'] = 'failed'
                from .runtime import ModelCancelled
                if isinstance(error, ModelCancelled):
                    p["resume_state"] = "ready" if p["tasks"] or p.get("kind") == "conversation" else "new"
                    p["status"] = "paused"
                    p["error"] = None
                    p.pop("error_message", None)
                    self.save(p)
                    self.event(pid, "paused", reason="用户停止本机调用")
                    return p
                p["status"] = "failed"
                p["error"] = type(error).__name__
                from .codex_model import ModelUnavailable
                if isinstance(error, (ModelUnavailable, ContractError, CallBlocked)):
                    p["error_message"] = str(error)
                else:
                    p['error_message'] = '内部执行未完成（'+type(error).__name__+'），已保存进度；可重试或提供错误类型给维护者。'
                if p.get('workflow') == 'agent-v1':
                    # 只反馈允许展示的错误，不能把供应商原始错误或凭证放进下一轮。
                    p['last_failure'] = {'operation': operation, 'error_type': type(error).__name__,
                                         'action': failure_action,
                                         'message': p.get('error_message', '本轮调用失败；请根据现有证据调整下一步'),
                                         'at': now()}
                self.save(p)
                self.event(pid, "failed", error=type(error).__name__)
        return self.get(pid)

    def receipt(self, pid, model):
        receipt = getattr(model, "last_receipt", None)
        if receipt:
            self.event(pid, "model_completed", **receipt)

    def chat_project(self):
        for p in self.projects():
            if p.get("kind") == "conversation":
                return p
        p = self.create("与我的个人总管持续讨论", "结合用户偏好和项目状态，提供有依据的建议", max_calls=20)
        with self.transaction():
            p["kind"], p["messages"] = "conversation", []
            self.save(p)
        return p

    def chat(self, message):
        message = checked_text(message, "消息", 4000)
        p = self.refresh_budget(self.chat_project()['id'])
        if p["manager"] != self.manager.name:
            raise ValueError("会话模型与配置不同，不能静默替换")
        if p["status"] == "paused":
            p = self.resume(p["id"])
        if p["status"] == "failed":
            # 新的用户消息明确触发下一次调用，失败本身不触发重试。
            with self.transaction():
                p = self.get(p["id"])
                p["status"], p["error"] = "ready", None
                p.pop("error_message", None)
                self.save(p)
        operation = self._reserve(p["id"], "executing", message=message)
        if not operation:
            raise ValueError("对话正在处理或额度已耗尽；请查看运行状态")
        try:
            projects = [{"id": item["id"], "goal": item["goal"], "status": item["status"],
                         "feedback": item.get("feedback")} for item in self.projects()
                        if item.get("kind") != "conversation"][-8:]
            result = self.invoke_model(self.manager, "chat", {"message": message, "history": p.get("messages", [])[-12:],
                                                   "memories": self.memories(), "projects": projects})
            if not isinstance(result, dict) or set(result) != {"content"}:
                raise ContractError("对话结果格式错误")
            content = checked_text(result["content"], "回复", 16000)
            with self.transaction():
                p = self.get(p["id"])
                if p["status"] != "executing" or p.get("operation") != operation:
                    return p
                for entry in p['messages']:
                    if entry.get('request_id') == operation:
                        entry['status'] = 'answered'
                p['messages'].append({'role': 'assistant', 'content': content, 'request_id': operation})
                p["status"] = "ready"
                self.save(p)
                self.receipt(p["id"], self.manager)
            return p
        except Exception as error:
            return self._fail(p["id"], error, operation)

    def pause(self, pid):
        with self.transaction():
            p = self.get(pid)
            if p["status"] not in ("new", "ready", "planning", "executing"):
                raise ValueError("当前状态不能暂停")
            p["resume_state"] = "ready" if p["tasks"] else "new"
            p["status"] = "paused"
            self.save(p)
            self.event(pid, "paused")
        return p

    def retry(self, pid):
        """仅由用户明确触发；当前工具全为本地草稿，无对外写入副作用。"""
        with self.transaction():
            p = self.get(pid)
            if p["status"] != "failed":
                raise ValueError("只能重试已失败的项目")
            if p["manager"] != self.manager.name or p["worker"] != self.worker.name:
                raise ValueError("重试必须使用原模型")
            p["status"] = "ready" if p["tasks"] else "new"
            p["error"] = None
            p.pop("error_message", None)
            self.save(p)
            self.event(pid, "retry_requested", reason="用户明确发起；保留已用额度与已有成果")
        return self.run(pid)

    def resume(self, pid):
        with self.transaction():
            p = self.get(pid)
            if p["status"] != "paused":
                raise ValueError("仅已暂停项目可恢复；崩溃中断先确认旧进程停止，再执行pause")
            p["status"] = p.pop("resume_state")
            self.save(p)
            self.event(pid, "resumed")
        return p

    def review(self, pid, accept, feedback):
        feedback = checked_text(feedback, "验收反馈", 2000)
        with self.transaction():
            p = self.get(pid)
            if p["status"] != "awaiting_review":
                raise ValueError("仅待验收项目可提交验收")
            p["status"] = "accepted" if accept else "rejected"
            p["feedback"] = feedback
            p["learning"].append({"kind": "user_feedback", "text": feedback,
                                  "source_project": pid, "status": "candidate",
                                  "note": "单次反馈不自动成为通用规则"})
            self.save(p)
            self.event(pid, "reviewed", accepted=accept)
        return p

    def audit(self, pid):
        return [{"at": at, "kind": kind, "data": json.loads(body)}
                for at, kind, body in self.db.execute(
                    "SELECT at,kind,body FROM events WHERE project=? ORDER BY id", (pid,))]

    def export(self, pid, directory):
        p = self.get(pid)
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"{pid}-{uuid.uuid4().hex[:8]}.md"
        report = (f"# 个人总管项目 {pid}\n\n模式：{p['mode']}\n\n状态：{p['status']}\n\n"
                  f"目标：{p['goal']}\n\n验收：{p['acceptance']}\n\n"
                  f"模型调用名额：{p['calls']}/{p['max_calls']}（不是货币账单）\n\n")
        for index, artifact in enumerate(p["artifacts"]):
            if p.get('workflow'):
                report += f"## 成果 {index} · 第 {artifact.get('revision', 0)+1} 版 · {artifact.get('title', '')}\n\n"
            report += artifact["content"] + "\n\n---\n\n"
        with target.open("x", encoding="utf-8") as stream:
            stream.write(report)
        return str(target.resolve())
