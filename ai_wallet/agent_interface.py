"""来源：本项目原创。受信本地宿主使用的单任务模拟工具，不是身份安全边界。"""

from datetime import datetime
import hashlib
import json
import sqlite3

from .engine import PRODUCTS, WalletError, _text


class TaskWallet:
    """宿主固定任务和模拟情景；只把三个方法的 JSON 参数/结果交给模型。"""

    def __init__(self, wallet, task_id, *, simulation="success"):
        self._wallet = wallet
        self._task_id = _text(task_id, "任务编号", 100)
        if simulation not in ("success", "decline", "timeout", "delivery_failure"):
            raise WalletError("未知模拟情景")
        self._simulation = simulation
        with wallet._connection() as db:
            wallet._task(db, self._task_id)

    def context(self):
        """每个任务开始时读取一次；额度只是快照，付款前仍由账本重新检查。"""
        wallet = self._wallet
        with wallet._connection() as db:
            task, policy, orders = wallet._task(db, self._task_id), wallet._policy(db), wallet._orders(db)
            spent, held = wallet._usage(orders, task_id=self._task_id)
            month_spent, month_held = wallet._usage(orders, period=7)
            day_spent, day_held = wallet._usage(orders, period=10)
            available = max(0, min(task["budget_cents"] - spent - held,
                                   policy["monthly_limit_cents"] - month_spent - month_held,
                                   policy["daily_limit_cents"] - day_spent - day_held))
            active = policy["enabled"] and task["status"] == "active"
            return {
                "mode": "demo", "currency": "CNY", "task_id": self._task_id,
                "task_title": task["title"], "can_purchase": active,
                "available_cents": available if active else 0,
                "per_payment_limit_cents": policy["per_payment_limit_cents"],
                "approval_threshold_cents": policy["approval_threshold_cents"],
                "products": [
                    {"id": product["id"], "title": product["title"], "price_cents": product["price_cents"],
                     "scope_allowed": not product["is_subscription"]
                     and product["merchant_id"] in policy["allowed_merchant_ids"]
                     and product["category"] in policy["allowed_categories"]}
                    for product in PRODUCTS
                ],
                "notice": "仅本地模拟；scope_allowed 仅表示钱包范围，不承诺预算通过或真实素材许可。",
            }

    def purchase(self, product_id, reason, request_id):
        """同一业务请求一直复用原编号与参数；自动执行已获授权的模拟付款。"""
        order = None
        try:
            request_id = _text(request_id, "请求编号", 100)
            # Only namespace the key, never hash the purchase payload into it:
            # changed payloads with the same original key must still conflict.
            key = "agent-v1-" + hashlib.sha256(json.dumps(
                [self._task_id, request_id], ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")).hexdigest()
            order = self._wallet.request({"task_id": self._task_id, "product_id": product_id,
                                          "reason": reason, "idempotency_key": key})
            if order["status"] == "approved":
                order = self._wallet.execute(order["id"], self._simulation)
            return self._result(order)
        except WalletError as exc:
            if order is not None:
                # Authorization can change between request and execute. Read the
                # same order, without approving, reconciling or paying again.
                result = self.status(order["id"])
                result["error_code"] = exc.code
                return result
            return self._error(exc)
        except (OSError, sqlite3.Error):
            # The host may have lost the response after a committed transaction.
            # Never report that payment failed or recommend a new request key.
            result = {"mode": "demo", "status": "unavailable", "next_action": "wait_for_host",
                      "message": "本地结果暂不可读；保存原请求编号和参数，由宿主排查，勿换编号重购。"}
            if order is not None:
                result["order_id"] = order["id"]
            return result

    def status(self, order_id):
        """纯读取本任务订单；不触发报价过期写入、付款或模拟渠道核验。"""
        try:
            order_id = _text(order_id, "订单编号", 100)
            with self._wallet._connection() as db:
                order = self._wallet._get(db, order_id)
                if order["task_id"] != self._task_id:
                    raise WalletError("找不到这笔申请", "not_found", 404)
                return self._result(order)
        except WalletError as exc:
            return self._error(exc)
        except (OSError, sqlite3.Error):
            return {"mode": "demo", "status": "unavailable", "next_action": "wait_for_host",
                    "message": "本地订单暂不可读；由宿主排查，勿换编号重购。"}

    @staticmethod
    def _error(exc):
        return {"mode": "demo", "status": "error", "error_code": exc.code,
                "next_action": "fix_request", "message": str(exc)}

    def _result(self, order):
        status = order["status"]
        actions = {"paid": "use_demo_delivery", "pending_approval": "wait_for_user",
                   "approved": "resume_same_request", "unknown": "wait_for_user_reconciliation",
                   "failed": "wait_for_user", "delivery_failed": "wait_for_user_support",
                   "rejected": "stop", "refunded": "stop"}
        result = {"mode": "demo", "order_id": order["id"], "status": status,
                  "amount_cents": order["amount_cents"], "currency": "CNY",
                  "next_action": actions.get(status, "wait_for_host"), "message": order["decision_reason"]}
        if status == "paid" and not order.get("delivery"):
            result.update(next_action="wait_for_host", message="已记录付款，尚未确认成果交付；由宿主核对原订单，不重新购买。")
        if order.get("checkout") and status in ("approved", "unknown"):
            result.update(next_action="wait_for_host", message=(
                "由宿主重新核对原结账会话后处理；不要新建购买请求。" if status == "approved" else
                "由宿主核对原网站订单及支付记录；保留占用，不再次付款。"))
        if status in ("approved", "pending_approval"):
            result["expires_at"] = order["expires_at"]
            now = self._wallet._now()
            if datetime.fromisoformat(order["expires_at"]) <= now or order["created_at"][:10] != now.date().isoformat():
                # Display expiry without modifying the ledger in this read path.
                result.update(next_action="stop", message="原报价已过期；不要自动重购，由用户确认是否发起新需求。")
        if status == "paid" and order.get("delivery"):
            result["delivery"] = {key: order["delivery"][key] for key in ("title", "content", "simulated")}
        return result
