"""来源：本项目原创。独立演示账本：总授权额度先分配给经用户确认的任务。"""

from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from .engine import DEFAULT_POLICY, MAX_CENTS, MERCHANTS, PRODUCTS, Wallet, WalletError, _text


MODEL = "task-allocation-v1"


class AllocationWallet(Wallet):
    """不迁移旧账本；金额仍是授权上限，未连接银行或真实付款渠道。"""

    def __init__(self, database, clock=None):
        from datetime import datetime
        from .engine import TZ

        self.database = str(database)
        self.clock = clock or (lambda: datetime.now(TZ))
        path = Path(database)
        # 检查旧文件时只读打开；不能先调用旧初始化器，否则会写入旧账本。
        if path.exists() and path.stat().st_size:
            try:
                with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                    self._check_model(db)
            except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
                raise WalletError("此文件不是任务分配账本；请使用新的独立数据库文件", "incompatible_ledger", 409) from exc
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if tables:
                self._check_model(db)
                return
            db.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE orders (id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, request_hash TEXT NOT NULL, data TEXT NOT NULL)")
            db.execute("CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, order_id TEXT, detail TEXT NOT NULL)")
            policy = dict(DEFAULT_POLICY)
            for field in ("monthly_limit_cents", "daily_limit_cents", "per_payment_limit_cents", "approval_threshold_cents"):
                policy[field] = MAX_CENTS
            db.execute("INSERT INTO settings VALUES (1, ?)", (json.dumps(policy),))
            # 旧客服工单使用 settings 3；新模式专用槽位不与已有组件共享。
            db.execute("INSERT INTO settings VALUES (10, ?)", (json.dumps({"model": MODEL, "total_cents": 0, "revision": 1}),))
            db.execute("INSERT INTO settings VALUES (11, '[]')")

    @staticmethod
    def _check_model(db):
        try:
            pool = db.execute("SELECT value FROM settings WHERE id=10").fetchone()
            tasks = db.execute("SELECT value FROM settings WHERE id=11").fetchone()
            if not pool or not tasks or json.loads(pool[0]).get("model") != MODEL:
                raise WalletError("此文件不是任务分配账本；请使用新的独立数据库文件", "incompatible_ledger", 409)
        except (sqlite3.Error, ValueError, TypeError, AttributeError) as exc:
            raise WalletError("此文件不是任务分配账本；请使用新的独立数据库文件", "incompatible_ledger", 409) from exc

    @contextmanager
    def _read_connection(self):
        """查询快照不抢占写锁，也不初始化／重建丢失的账本。"""
        with closing(sqlite3.connect(Path(self.database).resolve().as_uri() + "?mode=ro",
                                     uri=True, timeout=0.1)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            self._check_model(db)
            yield db

    @staticmethod
    def _tasks(db):
        return json.loads(db.execute("SELECT value FROM settings WHERE id=11").fetchone()[0])

    @staticmethod
    def _save_tasks(db, tasks):
        db.execute("UPDATE settings SET value=? WHERE id=11", (json.dumps(tasks, ensure_ascii=False),))

    @staticmethod
    def _pool(db):
        return json.loads(db.execute("SELECT value FROM settings WHERE id=10").fetchone()[0])

    def _bump_pool(self, db, total=None):
        pool = self._pool(db)
        if total is not None:
            pool["total_cents"] = total
        pool["revision"] += 1
        db.execute("UPDATE settings SET value=? WHERE id=10", (json.dumps(pool),))

    def _allocation(self, db):
        pool, orders = self._pool(db), self._orders(db)
        spent, _ = self._usage(orders)
        allocated = 0
        for task in self._tasks(db):
            task_spent, held = self._usage(orders, task_id=task["id"])
            if task["status"] in ("active", "paused"):
                allocated += task["budget_cents"] - task_spent
            elif task["status"] == "closed":
                allocated += held
        return {"total_cents": pool["total_cents"], "unallocated_cents": pool["total_cents"] - allocated - spent,
                "allocated_cents": allocated, "spent_cents": spent, "revision": pool["revision"]}

    def _task_view(self, db, task, orders=None):
        spent, held = self._usage(self._orders(db) if orders is None else orders, task_id=task["id"])
        available = max(0, task["budget_cents"] - spent - held) if task["status"] in ("active", "paused") else 0
        return dict(self._public_task(task), spent_cents=spent, reserved_cents=held, available_cents=available)

    @staticmethod
    def _task_unavailable_reason(task):
        return {
            "pending_budget": "任务预算尚未由用户确认，不允许新增消费",
            "paused": "任务已暂停，不允许新增消费",
            "closed": "任务已结束，不允许新增消费",
            "rejected": "任务预算已被用户拒绝，不允许新增消费",
        }.get(task["status"], "任务当前不可用，不允许新增消费")

    def _denial(self, policy, task, product, orders):
        if task["status"] != "active":
            return self._task_unavailable_reason(task)
        return super()._denial(policy, task, product, orders)

    def _require_active_task(self, db, task_id):
        task = self._task(db, task_id)
        if task["status"] != "active":
            raise WalletError(self._task_unavailable_reason(task), "task_unavailable", 409)
        return task

    @staticmethod
    def _payload(payload, required, optional=()):
        if not isinstance(payload, dict) or set(payload) - set(required) - set(optional) or set(required) - set(payload):
            raise WalletError("缺少必填字段或包含不支持的字段")

    @staticmethod
    def _revision(value):
        if type(value) is not int or value < 1:
            raise WalletError("版本必须为正整数")
        return value

    def _editable(self, db, task_id, expected):
        expected = self._revision(expected)
        tasks = self._tasks(db)
        task = next((item for item in tasks if item["id"] == task_id), None)
        if not task:
            raise WalletError("找不到该任务", "not_found", 404)
        if task["revision"] != expected:
            raise WalletError("任务已更新，请读取最新预算后重新确认", "revision_conflict", 409)
        return tasks, task

    def configure_pool(self, payload):
        self._payload(payload, {"total_cents", "expected_revision"})
        total = self._task_budget(payload["total_cents"])
        expected = self._revision(payload["expected_revision"])
        with self._connection() as db:
            self._expire(db)
            current = self._allocation(db)
            if current["revision"] != expected:
                raise WalletError("总授权额度已更新，请读取最新额度后重新确认", "revision_conflict", 409)
            if total < current["allocated_cents"] + current["spent_cents"]:
                raise WalletError("总额度不能低于已分配额度与累计支出之和", "budget_in_use", 409)
            if total != current["total_cents"]:
                self._bump_pool(db, total)
                self._audit(db, "allocation_pool_updated", f"总授权额度设为 {total} 分", actor="owner")
            return self._allocation(db)

    def create_task(self, payload):
        raise WalletError("任务预算需要先提出再由用户确认，请使用任务预算提案", "budget_confirmation_required", 409)

    def propose_task(self, payload):
        self._payload(payload, {"title", "agent_id", "budget_cents", "reason", "idempotency_key"})
        title = _text(payload["title"], "任务名称", 120)
        agent = _text(payload["agent_id"], "AI 标识", 100)
        budget = self._task_budget(payload["budget_cents"])
        reason = _text(payload["reason"], "预算理由", 500, multiline=True)
        key = _text(payload["idempotency_key"], "防重复编号", 100)
        digest = hashlib.sha256(json.dumps([title, agent, budget, reason], ensure_ascii=False).encode()).hexdigest()
        with self._connection() as db:
            tasks = self._tasks(db)
            existing = next((task for task in tasks if task.get("_creation_key") == key), None)
            if existing:
                if existing["_creation_hash"] != digest:
                    raise WalletError("同一防重复编号不能用于不同任务提案", "idempotency_conflict", 409)
                return self._task_view(db, existing)
            task = {"id": uuid.uuid4().hex[:20], "title": title, "agent_id": agent,
                    "budget_cents": 0, "status": "pending_budget", "revision": 1,
                    "proposed_budget_cents": budget, "budget_reason": reason,
                    "created_at": self._now().isoformat(), "_creation_key": key, "_creation_hash": digest}
            tasks.append(task)
            self._save_tasks(db, tasks)
            self._audit(db, "task_budget_proposed", f"任务 {task['id']} 提议预算 {budget} 分，等待用户确认", actor="simulated_agent")
            return self._task_view(db, task)

    def propose_budget(self, task_id, payload):
        self._payload(payload, {"budget_cents", "reason", "expected_revision"})
        budget = self._task_budget(payload["budget_cents"])
        reason = _text(payload["reason"], "预算理由", 500, multiline=True)
        with self._connection() as db:
            tasks, task = self._editable(db, task_id, payload["expected_revision"])
            if task["status"] not in ("active", "paused"):
                raise WalletError("只有进行中或暂停的任务可以申请追加预算", "invalid_state", 409)
            if budget <= task["budget_cents"]:
                raise WalletError("追加后的总预算必须高于当前已批准预算")
            task.update(proposed_budget_cents=budget, budget_reason=reason, revision=task["revision"] + 1)
            self._save_tasks(db, tasks)
            self._audit(db, "task_budget_proposed", f"任务 {task_id} 申请总预算 {budget} 分，原授权暂不变化", actor="simulated_agent")
            return self._task_view(db, task)

    def decide_budget(self, task_id, payload):
        self._payload(payload, {"decision", "expected_revision"}, {"budget_cents"})
        if payload["decision"] not in ("approve", "reject"):
            raise WalletError("预算决定必须为 approve 或 reject")
        approve = payload["decision"] == "approve"
        if approve != ("budget_cents" in payload):
            raise WalletError("批准时必须填写用户最终总预算；拒绝时不能填写预算")
        budget = self._task_budget(payload["budget_cents"]) if approve else None
        with self._connection() as db:
            self._expire(db)
            tasks, task = self._editable(db, task_id, payload["expected_revision"])
            if task["status"] not in ("pending_budget", "active", "paused") or task["proposed_budget_cents"] is None:
                raise WalletError("此任务没有待确认预算", "invalid_state", 409)
            if approve:
                if budget < task["budget_cents"]:
                    raise WalletError("追加确认不能降低原预算；如需释放额度，请结束任务")
                difference = budget - task["budget_cents"]
                if difference > self._allocation(db)["unallocated_cents"]:
                    raise WalletError("尚可分配的总额度不足，请调整任务预算或总授权额度", "insufficient_allocation", 409)
                task["budget_cents"] = budget
                if task["status"] == "pending_budget":
                    task["status"] = "active"
            elif task["status"] == "pending_budget":
                task["status"] = "rejected"
            task.update(proposed_budget_cents=None, revision=task["revision"] + 1)
            self._save_tasks(db, tasks)
            if approve:
                self._bump_pool(db)
            self._audit(db, "task_budget_approved" if approve else "task_budget_rejected",
                        f"任务 {task_id}；已批准总预算 {task['budget_cents']} 分", actor="owner")
            return self._task_view(db, task)

    def _cancel_unpaid(self, db, task_id, message):
        for order in self._orders(db):
            if order["task_id"] == task_id and order["status"] in ("pending_approval", "approved"):
                order.update(status="rejected", decision_reason=message)
                self._save(db, order)
                self._audit(db, "task_authorization_invalidated", message, order["id"], "owner")

    def close_task(self, task_id, payload):
        self._payload(payload, {"expected_revision"})
        with self._connection() as db:
            self._expire(db)
            tasks, task = self._editable(db, task_id, payload["expected_revision"])
            if task["status"] not in ("active", "paused", "pending_budget"):
                raise WalletError("任务已经结束，不能再次关闭", "invalid_state", 409)
            task.update(status="closed", proposed_budget_cents=None, revision=task["revision"] + 1)
            self._cancel_unpaid(db, task_id, "任务已结束，未付款申请已取消；结果未知的付款仍保留额度")
            self._save_tasks(db, tasks)
            self._bump_pool(db)
            self._audit(db, "task_closed", f"任务 {task_id} 已结束，未使用额度已归还总授权池", actor="owner")
            return self._task_view(db, task)

    def update_task(self, task_id, payload):
        self._payload(payload, {"expected_revision"}, {"title", "status"})
        if len(payload) < 2:
            raise WalletError("请提供新的任务名称或暂停/恢复状态")
        title = _text(payload["title"], "任务名称", 120) if "title" in payload else None
        if "status" in payload and payload["status"] not in ("active", "paused"):
            raise WalletError("任务更新只允许暂停或恢复；结束任务请使用关闭操作")
        with self._connection() as db:
            self._expire(db)
            tasks, task = self._editable(db, task_id, payload["expected_revision"])
            if task["status"] in ("closed", "rejected"):
                raise WalletError("已结束或拒绝的任务不能编辑", "invalid_state", 409)
            if "status" in payload and task["status"] not in ("active", "paused"):
                raise WalletError("尚未确认预算的任务不能恢复或暂停", "budget_confirmation_required", 409)
            if title is not None:
                task["title"] = title
            if "status" in payload:
                task["status"] = payload["status"]
            task["revision"] += 1
            if task["status"] == "paused":
                self._cancel_unpaid(db, task_id, "任务已暂停，未付款申请已失效；任务预算仍保留")
            self._save_tasks(db, tasks)
            self._audit(db, "task_updated", f"更新任务 {task_id}；状态 {task['status']}", actor="owner")
            return self._task_view(db, task)

    def state(self):
        # 总授权池、任务和订单必须来自同一事务，避免并发确认时显示互相矛盾的额度。
        with self._connection() as db:
            self._expire(db)
            policy, orders = self._policy(db), self._orders(db)
            ms, mr = self._usage(orders, period=7)
            ds, dr = self._usage(orders, period=10)
            return {"mode": "demo", "budget_model": MODEL, "policy": policy,
                    "allocation": self._allocation(db),
                    "summary": {"month_spent_cents": ms, "month_reserved_cents": mr,
                                "month_available_cents": max(0, policy["monthly_limit_cents"] - ms - mr),
                                "day_spent_cents": ds, "day_reserved_cents": dr,
                                "day_available_cents": max(0, policy["daily_limit_cents"] - ds - dr),
                                "pending_count": sum(order["status"] == "pending_approval" for order in orders)},
                    "tasks": [self._task_view(db, task, orders) for task in self._tasks(db)],
                    "merchants": MERCHANTS, "products": PRODUCTS, "orders": orders,
                    "audit": [dict(row) for row in db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 200")]}
