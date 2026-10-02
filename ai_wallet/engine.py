"""来源：本项目原创。确定性授权、预算预留和模拟付款；不连接真实支付。"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid


TZ = timezone(timedelta(hours=8))
HELD = ("pending_approval", "approved", "unknown")
SPENT = ("paid", "delivery_failed", "refunded")
MAX_CENTS = 100_000_000
DEFAULT_POLICY = {
    "enabled": True,
    "monthly_limit_cents": 30_000,
    "daily_limit_cents": 10_000,
    "per_payment_limit_cents": 10_000,
    "approval_threshold_cents": 2_000,
    "allowed_categories": ["image", "data", "tool"],
    "allowed_merchant_ids": ["studio", "insight", "compute"],
    "revision": 1,
}
MERCHANTS = [
    {"id": "studio", "name": "像素素材 · 模拟商家", "category": "image"},
    {"id": "insight", "name": "知数资料 · 模拟商家", "category": "data"},
    {"id": "compute", "name": "轻舟工具 · 模拟商家", "category": "tool"},
    {"id": "outside", "name": "未授权商店 · 模拟商家", "category": "other"},
]
PRODUCTS = [
    {"id": "image-12", "merchant_id": "studio", "title": "宣传配图 · 单张", "price_cents": 1200,
     "category": "image", "license": "本地原创演示内容；不代表实际商业图片授权", "is_subscription": False},
    {"id": "report-35", "merchant_id": "insight", "title": "行业概览 · 数据摘要", "price_cents": 3500,
     "category": "data", "license": "本地原创模拟数据；不可作为真实研究依据", "is_subscription": False},
    {"id": "tool-8", "merchant_id": "compute", "title": "文档转换 · 单次", "price_cents": 800,
     "category": "tool", "license": "模拟转换结果；未调用外部服务", "is_subscription": False},
    {"id": "report-120", "merchant_id": "insight", "title": "专题资料包 · 超限演示", "price_cents": 12000,
     "category": "data", "license": "本地原创模拟数据", "is_subscription": False},
    {"id": "subscription-15", "merchant_id": "compute", "title": "工具月订阅 · 禁止续费演示", "price_cents": 1500,
     "category": "tool", "license": "演示项目，不提供真实订阅", "is_subscription": True},
    {"id": "outside-5", "merchant_id": "outside", "title": "未授权商品 · 拦截演示", "price_cents": 500,
     "category": "other", "license": "演示项目", "is_subscription": False},
]
TASKS = [
    {"id": "campaign", "title": "新品宣传方案", "agent_id": "creative-agent", "budget_cents": 10000},
    {"id": "research", "title": "行业资料整理", "agent_id": "research-agent", "budget_cents": 8000},
    {"id": "documents", "title": "客户文档处理", "agent_id": "document-agent", "budget_cents": 5000},
]


class WalletError(ValueError):
    def __init__(self, message, code="invalid_request", status=400):
        super().__init__(message)
        self.code = code
        self.status = status


def _text(value, name, limit=500, multiline=False):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise WalletError(f"{name}需要是 1～{limit} 个字符的文本")
    if any(ord(char) < 32 and not (multiline and char in "\n\r\t") for char in value):
        raise WalletError(f"{name}不能包含控制字符")
    return value.strip()


class Wallet:
    """账本金额均为整数分。每个写操作在独立事务内重新核验共享预算。"""

    def __init__(self, database, clock=None):
        self.database = str(database)
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.now(TZ))
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
                    request_hash TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
                    actor TEXT NOT NULL, action TEXT NOT NULL,
                    order_id TEXT, detail TEXT NOT NULL
                );
            """)
            db.execute("INSERT OR IGNORE INTO settings VALUES (1, ?)", (json.dumps(DEFAULT_POLICY),))
            # Seed once: later starts must preserve user tasks and the existing ledger.
            seeded_tasks = [dict(task, status="active", revision=1) for task in TASKS]
            db.execute("INSERT OR IGNORE INTO settings VALUES (2, ?)", (json.dumps(seeded_tasks),))

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            # BEGIN IMMEDIATE prevents concurrent readers from both reserving the same budget.
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _now(self):
        return self.clock().astimezone(TZ)

    def _audit(self, db, action, detail, order_id=None, actor="system"):
        db.execute("INSERT INTO audit (at, actor, action, order_id, detail) VALUES (?, ?, ?, ?, ?)",
                   (self._now().isoformat(), actor, action, order_id, detail))

    @staticmethod
    def _policy(db):
        return json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])

    @staticmethod
    def _tasks(db):
        return json.loads(db.execute("SELECT value FROM settings WHERE id=2").fetchone()[0])

    @staticmethod
    def _public_task(task):
        return {key: value for key, value in task.items() if not key.startswith("_")}

    @staticmethod
    def _save_tasks(db, tasks):
        db.execute("UPDATE settings SET value=? WHERE id=2", (json.dumps(tasks, ensure_ascii=False),))

    def _task(self, db, task_id):
        task = next((task for task in self._tasks(db) if task["id"] == task_id), None)
        if not task:
            raise WalletError("找不到该任务", "not_found", 404)
        return task

    def _require_active_task(self, db, task_id):
        task = self._task(db, task_id)
        if task["status"] != "active":
            raise WalletError("任务已暂停或归档，不能批准或执行新增付款", "task_unavailable", 409)
        return task

    @staticmethod
    def _task_budget(value):
        if type(value) is not int or not 0 <= value <= MAX_CENTS:
            raise WalletError("任务预算必须是范围内的整数分，不能为负数、小数或布尔值")
        return value

    def create_task(self, payload):
        required = {"title", "agent_id", "budget_cents", "idempotency_key"}
        if not isinstance(payload, dict) or set(payload) != required:
            raise WalletError("新建任务只接受名称、AI 标识、预算和防重复编号")
        title = _text(payload["title"], "任务名称", 120)
        agent_id = _text(payload["agent_id"], "AI 标识", 100)
        budget = self._task_budget(payload["budget_cents"])
        key = _text(payload["idempotency_key"], "防重复编号", 100)
        digest = hashlib.sha256(json.dumps([title, agent_id, budget], ensure_ascii=False).encode()).hexdigest()
        with self._connection() as db:
            tasks = self._tasks(db)
            existing = next((task for task in tasks if task.get("_creation_key") == key), None)
            if existing:
                if existing["_creation_hash"] != digest:
                    raise WalletError("同一防重复编号不能用于不同任务", "idempotency_conflict", 409)
                return self._public_task(existing)
            task = {"id": uuid.uuid4().hex[:20], "title": title, "agent_id": agent_id,
                    "budget_cents": budget, "status": "active", "revision": 1,
                    "created_at": self._now().isoformat(), "_creation_key": key, "_creation_hash": digest}
            tasks.append(task)
            self._save_tasks(db, tasks)
            self._audit(db, "task_created", f"创建任务 {task['id']}：{title}；预算 {budget} 分", actor="owner")
            return self._public_task(task)

    def update_task(self, task_id, payload):
        task_id = _text(task_id, "任务编号", 100)
        allowed = {"title", "budget_cents", "status", "expected_revision"}
        if (not isinstance(payload, dict) or set(payload) - allowed
                or "expected_revision" not in payload or len(payload) < 2):
            raise WalletError("任务更新需要当前版本与名称、预算或状态；AI 标识不能更改")
        expected = payload["expected_revision"]
        if type(expected) is not int or expected < 1:
            raise WalletError("任务版本必须是正整数")
        changes = {key: value for key, value in payload.items() if key != "expected_revision"}
        if "title" in changes:
            changes["title"] = _text(changes["title"], "任务名称", 120)
        if "budget_cents" in changes:
            changes["budget_cents"] = self._task_budget(changes["budget_cents"])
        if "status" in changes and changes["status"] not in ("active", "paused", "archived"):
            raise WalletError("任务状态必须是 active、paused 或 archived")
        with self._connection() as db:
            self._expire(db)
            tasks = self._tasks(db)
            task = next((task for task in tasks if task["id"] == task_id), None)
            if not task:
                raise WalletError("找不到该任务", "not_found", 404)
            if task["revision"] != expected:
                raise WalletError("任务已在其他页面更新，请读取最新版本后重试", "revision_conflict", 409)
            if all(task[key] == value for key, value in changes.items()):
                return self._public_task(task)
            if task["status"] == "archived":
                raise WalletError("已归档任务不能恢复或编辑；请创建新任务", "task_archived", 409)
            orders = self._orders(db)
            spent, held = self._usage(orders, task_id=task_id)
            if changes.get("budget_cents", task["budget_cents"]) < spent + held:
                raise WalletError("任务预算不能低于累计支出与预留额度；可先取消未付款申请", "budget_in_use", 409)
            task.update(changes, revision=task["revision"] + 1)
            self._save_tasks(db, tasks)
            if task["status"] in ("paused", "archived"):
                for order in orders:
                    if order["task_id"] == task_id and order["status"] in ("pending_approval", "approved"):
                        order.update(status="rejected", decision_reason="任务已暂停或归档，此申请失效；已释放预留额度")
                        self._save(db, order)
                        self._audit(db, "task_authorization_invalidated", order["decision_reason"], order["id"], "owner")
            self._audit(db, "task_updated", f"更新任务 {task_id}；状态 {task['status']}；版本 {task['revision']}", actor="owner")
            return self._public_task(task)

    @staticmethod
    def _orders(db):
        return [json.loads(row[0]) for row in db.execute("SELECT data FROM orders ORDER BY rowid DESC")]

    @staticmethod
    def _save(db, order):
        db.execute("UPDATE orders SET data=? WHERE id=?", (json.dumps(order, ensure_ascii=False), order["id"]))

    @staticmethod
    def _get(db, order_id):
        row = db.execute("SELECT data FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row:
            raise WalletError("找不到这笔申请", "not_found", 404)
        return json.loads(row[0])

    def _expire(self, db):
        now = self._now()
        for order in self._orders(db):
            if order["status"] in ("pending_approval", "approved") and (
                datetime.fromisoformat(order["expires_at"]) <= now or order["created_at"][:10] != now.date().isoformat()
            ):
                order.update(status="rejected", decision_reason="报价已过期或已跨日，请重新申请")
                self._save(db, order)
                self._audit(db, "quote_expired", order["decision_reason"], order["id"])

    def _usage(self, orders, task_id=None, period=None):
        now = self._now().isoformat()
        spent = reserved = 0
        for order in orders:
            if task_id and order["task_id"] != task_id:
                continue
            # Unknown payments carry their full reservation forward across date boundaries.
            in_period = not period or order.get("budget_at", order["created_at"])[:period] == now[:period]
            if order["status"] in SPENT and in_period:
                spent += order["amount_cents"]
            elif order["status"] in HELD and (in_period or order["status"] == "unknown"):
                reserved += order["amount_cents"]
        return spent, reserved

    def state(self):
        with self._connection() as db:
            self._expire(db)
            policy, orders = self._policy(db), self._orders(db)
            ms, mr = self._usage(orders, period=7)
            ds, dr = self._usage(orders, period=10)
            tasks = []
            for task in self._tasks(db):
                spent, held = self._usage(orders, task_id=task["id"])
                tasks.append(dict(self._public_task(task), spent_cents=spent, reserved_cents=held,
                                  available_cents=max(0, task["budget_cents"] - spent - held)))
            return {
                "mode": "demo", "policy": policy,
                "summary": {"month_spent_cents": ms, "month_reserved_cents": mr,
                            "month_available_cents": max(0, policy["monthly_limit_cents"] - ms - mr),
                            "day_spent_cents": ds, "day_reserved_cents": dr,
                            "day_available_cents": max(0, policy["daily_limit_cents"] - ds - dr),
                            "pending_count": sum(o["status"] == "pending_approval" for o in orders)},
                "tasks": tasks, "merchants": MERCHANTS, "products": PRODUCTS,
                "orders": orders, "audit": [dict(r) for r in db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 200")],
            }

    def _denial(self, policy, task, product, orders):
        if not policy["enabled"]:
            return "钱包已暂停，不允许新增消费"
        if task["status"] != "active":
            return "任务已暂停或归档，不允许新增消费"
        if product["is_subscription"]:
            return "第一版不允许订阅或自动续费"
        if product["merchant_id"] not in policy["allowed_merchant_ids"]:
            return "该商家不在授权名单内"
        if product["category"] not in policy["allowed_categories"]:
            return "该商品类别不在授权范围内"
        amount = product["price_cents"]
        if amount > policy["per_payment_limit_cents"]:
            return "超过单笔硬上限，审批也不能放行"
        for budget, spent, held, label in (
            (task["budget_cents"], *self._usage(orders, task_id=task["id"]), "任务"),
            (policy["monthly_limit_cents"], *self._usage(orders, period=7), "本月"),
            (policy["daily_limit_cents"], *self._usage(orders, period=10), "今日"),
        ):
            if spent + held + amount > budget:
                return f"{label}剩余预算不足，包含已预留的申请"
        return None

    def _resolve_product(self, product_id):
        """默认演示目录；受信宿主可替换来源，不向模型开放金额参数。"""
        product = next((p for p in PRODUCTS if p["id"] == product_id), None)
        if not product:
            raise WalletError("未知商品", "not_found", 404)
        merchant_name = next(m["name"] for m in MERCHANTS if m["id"] == product["merchant_id"])
        return product, merchant_name

    def request(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"task_id", "product_id", "reason", "idempotency_key"}:
            raise WalletError("申请只接受任务、商品、购买理由和防重复编号；金额与身份由系统确定")
        key = _text(payload["idempotency_key"], "防重复编号", 100)
        reason = _text(payload["reason"], "购买理由", 500, multiline=True)
        task_id = _text(payload["task_id"], "任务编号", 100)
        product_id = _text(payload["product_id"], "商品编号", 100)
        digest = hashlib.sha256(json.dumps([task_id, product_id, reason], ensure_ascii=False).encode()).hexdigest()
        with self._connection() as db:
            self._expire(db)
            existing = db.execute("SELECT request_hash, data FROM orders WHERE request_key=?", (key,)).fetchone()
            if existing:
                if existing["request_hash"] != digest:
                    raise WalletError("同一防重复编号不能用于不同购买请求", "idempotency_conflict", 409)
                return json.loads(existing["data"])
            task = self._task(db, task_id)
            product, merchant_name = self._resolve_product(product_id)
            policy = self._policy(db)
            denial = self._denial(policy, task, product, self._orders(db))
            status = "rejected" if denial else (
                "pending_approval" if product["price_cents"] > policy["approval_threshold_cents"] else "approved"
            )
            now = self._now()
            order = {
                "id": uuid.uuid4().hex[:20], "task_id": task_id, "agent_id": task["agent_id"],
                "task_title": task["title"],
                "product_id": product_id, "product_title": product["title"],
                "merchant_name": merchant_name,
                "amount_cents": product["price_cents"], "status": status, "reason": reason,
                "decision_reason": denial or ("超过自动付款门槛，等待用户批准" if status == "pending_approval" else "符合预授权规则"),
                "created_at": now.isoformat(), "budget_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=15)).isoformat(),
                "receipt": None, "delivery": None, "policy_revision": policy["revision"],
                "approved_by": "policy" if status == "approved" else None,
            }
            db.execute("INSERT INTO orders VALUES (?, ?, ?, ?)",
                       (order["id"], key, digest, json.dumps(order, ensure_ascii=False)))
            self._audit(db, "purchase_requested", order["decision_reason"], order["id"], "simulated_agent")
            return order

    def approve(self, order_id):
        with self._connection() as db:
            self._expire(db)
            order, policy = self._get(db, order_id), self._policy(db)
            self._require_active_task(db, order["task_id"])
            if order["status"] == "approved":
                return order
            if order["status"] != "pending_approval":
                raise WalletError("只有待审批申请可以批准", "invalid_state", 409)
            if not policy["enabled"] or policy["revision"] != order["policy_revision"]:
                raise WalletError("授权已变化，请重新申请", "authorization_changed", 409)
            order.update(status="approved", approved_by="owner", decision_reason="用户批准此笔订单，尚未付款")
            self._save(db, order)
            self._audit(db, "purchase_approved", order["decision_reason"], order_id, "owner")
            return order

    def reject(self, order_id):
        with self._connection() as db:
            order = self._get(db, order_id)
            if order["status"] == "rejected":
                return order
            if order["status"] not in ("pending_approval", "approved"):
                raise WalletError("此订单已进入付款流程，不能直接撤销；请先核验状态", "invalid_state", 409)
            order.update(status="rejected", decision_reason="用户拒绝或取消申请，已释放预留额度")
            self._save(db, order)
            self._audit(db, "purchase_rejected", order["decision_reason"], order_id, "owner")
            return order

    def execute(self, order_id, scenario="success"):
        if scenario not in ("success", "decline", "timeout", "delivery_failure"):
            raise WalletError("未知模拟情景")
        with self._connection() as db:
            self._expire(db)
            order, policy = self._get(db, order_id), self._policy(db)
            # A replay returns the existing result. UNKNOWN cannot trigger another payment.
            if order.get("checkout") and order["status"] == "approved":
                raise WalletError("已绑定网站结账，须由宿主复核后提交，不能模拟执行", "host_dispatch_required", 409)
            if order["status"] in (*SPENT, "failed", "unknown"):
                return order
            self._require_active_task(db, order["task_id"])
            if order["status"] != "approved":
                raise WalletError("此申请尚未批准或已失效，不能付款", "invalid_state", 409)
            if not policy["enabled"] or policy["revision"] != order["policy_revision"]:
                raise WalletError("钱包已暂停或授权发生变化", "authorization_changed", 409)
            if scenario == "decline":
                order.update(status="failed", decision_reason="模拟渠道明确拒付，已释放预留额度")
            elif scenario == "timeout":
                # Demo fixture: the provider succeeded, but the response was lost. Never retry.
                order.update(status="unknown", decision_reason="模拟付款响应超时；保留额度，先核验结果，不重复付款")
            else:
                self._settle(order, delivery_failed=scenario == "delivery_failure")
            self._save(db, order)
            self._audit(db, "payment_" + order["status"], order["decision_reason"], order_id, "simulated_provider")
            return order

    def _settle(self, order, delivery_failed=False):
        order["status"] = "delivery_failed" if delivery_failed else "paid"
        order["receipt"] = {"id": "DEMO-" + order["id"], "amount_cents": order["amount_cents"],
                            "currency": "CNY", "simulated": True, "confirmed_at": self._now().isoformat()}
        order["decision_reason"] = "模拟付款成功，但资源未交付，可模拟退款" if delivery_failed else "模拟付款已确认，演示资源已交付"
        order["delivery"] = None if delivery_failed else {
            "title": order["product_title"], "content": "这是本地生成的交付凭据示例，不含真实购买的资料、图片或工具。",
            "simulated": True,
        }

    def reconcile(self, order_id):
        with self._connection() as db:
            order = self._get(db, order_id)
            if order.get("dispatch"):
                raise WalletError("此订单须由付款宿主核对原记录，不能使用模拟核验", "host_reconciliation_required", 409)
            if order["status"] in SPENT:
                return order
            if order["status"] != "unknown":
                raise WalletError("只有结果未知的付款需要核验", "invalid_state", 409)
            self._settle(order)
            # If confirmation crosses a budget period, retain exposure in the current period.
            order["budget_at"] = self._now().isoformat()
            self._save(db, order)
            self._audit(db, "payment_reconciled", "模拟渠道查询确认此前已付款；没有再次扣款", order_id, "simulated_provider")
            return order

    def refund(self, order_id):
        with self._connection() as db:
            order = self._get(db, order_id)
            if order.get("dispatch"):
                raise WalletError("此订单须核对实际退款记录，不能使用模拟退款", "host_reconciliation_required", 409)
            if order["status"] == "refunded":
                return order
            if order["status"] not in ("paid", "delivery_failed"):
                raise WalletError("仅已确认付款的订单可以模拟退款", "invalid_state", 409)
            order.update(status="refunded", decision_reason="模拟渠道已确认原路退款；消费限额按累计支出计算，不自动恢复")
            order["refund_receipt"] = {"id": "DEMO-REFUND-" + order_id, "simulated": True,
                                       "amount_cents": order["amount_cents"], "confirmed_at": self._now().isoformat()}
            self._save(db, order)
            self._audit(db, "payment_refunded", order["decision_reason"], order_id, "owner")
            return order

    def update_policy(self, changes, expected_revision=None, expected_pending_ids=None):
        allowed = set(DEFAULT_POLICY) - {"revision"}
        if not isinstance(changes, dict) or not changes or set(changes) - allowed:
            raise WalletError("未知或缺失的授权规则字段")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
            raise WalletError("授权版本必须是正整数，不能为布尔值")
        if expected_pending_ids is not None:
            if (not isinstance(expected_pending_ids, list)
                    or any(not isinstance(order_id, str) or not order_id or len(order_id) > 100
                           for order_id in expected_pending_ids)
                    or len(set(expected_pending_ids)) != len(expected_pending_ids)):
                raise WalletError("待付款订单清单必须是不含重复项的订单编号列表")
        with self._connection() as db:
            self._expire(db)
            old = self._policy(db)
            if expected_revision is not None and expected_revision != old["revision"]:
                raise WalletError("确认后授权规则已变化，本次未保存，请重新核对", "revision_conflict", 409)
            orders = self._orders(db)
            if expected_pending_ids is not None:
                current_ids = sorted(order["id"] for order in orders
                                     if order["status"] in ("pending_approval", "approved"))
                if sorted(expected_pending_ids) != current_ids:
                    raise WalletError("确认后待付款订单已变化，本次未保存，请重新核对影响清单", "pending_orders_conflict", 409)
            policy = dict(old, **changes)
            if type(policy["enabled"]) is not bool:
                raise WalletError("启用状态必须是真或假")
            for field in ("monthly_limit_cents", "daily_limit_cents", "per_payment_limit_cents", "approval_threshold_cents"):
                if type(policy[field]) is not int or not 0 <= policy[field] <= MAX_CENTS:
                    raise WalletError("额度必须是范围内的整数分，不能为负数、小数或布尔值")
            if not policy["approval_threshold_cents"] <= policy["per_payment_limit_cents"] <= policy["daily_limit_cents"] <= policy["monthly_limit_cents"]:
                raise WalletError("请满足：审批门槛 ≤ 单笔上限 ≤ 每日上限 ≤ 每月上限")
            for field, values in (
                ("allowed_categories", {"image", "data", "tool"}),
                ("allowed_merchant_ids", {"studio", "insight", "compute"}),
            ):
                supplied = policy[field]
                if not isinstance(supplied, list) or any(not isinstance(v, str) for v in supplied):
                    raise WalletError("授权范围必须是列表")
                if len(supplied) != len(set(supplied)) or set(supplied) - values:
                    raise WalletError("授权范围包含重复或不支持的项目")
            for field, period in (("monthly_limit_cents", 7), ("daily_limit_cents", 10)):
                spent, held = self._usage(orders, period=period)
                if policy[field] < spent + held:
                    raise WalletError("新预算不能低于已支出和已预留额度；可先拒绝待处理申请", "budget_in_use", 409)
            if policy == old:
                return policy
            policy["revision"] = old["revision"] + 1
            db.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(policy),))
            for order in orders:
                if order["status"] in ("pending_approval", "approved"):
                    order.update(status="rejected", decision_reason="用户修改或暂停授权，旧申请失效；如需购买请重新申请")
                    self._save(db, order)
                    self._audit(db, "authorization_invalidated", order["decision_reason"], order["id"])
            self._audit(db, "policy_updated", "用户更新授权，版本 " + str(policy["revision"]), actor="owner")
            return policy
