"""来源：本项目原创。隔离临时账本的行为测试；不连接真实支付。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from ai_wallet.engine import MAX_CENTS, TZ, Wallet, WalletError


class WalletTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026, 1, 31, 12, 0, tzinfo=TZ)
        self.path = Path(self.temp.name) / "wallet.sqlite"
        self.wallet = Wallet(self.path, clock=lambda: self.now)
        self.sequence = 0

    def request(self, product="image-12", task="campaign", **extra):
        self.sequence += 1
        payload = dict(task_id=task, product_id=product, reason="完成客户项目所需",
                       idempotency_key=f"request-{self.sequence}")
        payload.update(extra)
        return self.wallet.request(payload)

    def limits(self, amount, threshold=None):
        self.wallet.update_policy(dict(monthly_limit_cents=amount,
            daily_limit_cents=amount, per_payment_limit_cents=amount,
            approval_threshold_cents=amount if threshold is None else threshold))

    def test_exact_budget_allowed_next_purchase_rejected(self):
        self.limits(1200)
        order = self.request()
        self.assertEqual(order["status"], "approved")
        self.assertEqual(self.wallet.state()["summary"]["month_available_cents"], 0)
        denied = self.request("tool-8", "documents")
        self.assertEqual(denied["status"], "rejected")
        self.wallet.execute(order["id"])
        summary = self.wallet.state()["summary"]
        self.assertEqual((summary["month_spent_cents"], summary["month_reserved_cents"]), (1200, 0))

    def test_task_budget_shared_by_paid_and_reserved_orders(self):
        self.limits(30000)
        for index in range(6):
            order = self.request("tool-8", "documents")
            if index < 3:
                self.wallet.execute(order["id"])
        denied = self.request("tool-8", "documents")
        self.assertEqual(denied["status"], "rejected")
        self.assertIn("任务", denied["decision_reason"])
        task = next(t for t in self.wallet.state()["tasks"] if t["id"] == "documents")
        self.assertEqual((task["spent_cents"], task["reserved_cents"], task["available_cents"]), (2400, 2400, 200))

    def test_daily_total_applies_across_tasks(self):
        self.wallet.update_policy(dict(daily_limit_cents=2000,
            per_payment_limit_cents=2000, approval_threshold_cents=2000))
        self.wallet.execute(self.request()["id"])
        self.request("tool-8", "documents")
        denied = self.request("tool-8", "research")
        self.assertEqual(denied["status"], "rejected")
        self.assertIn("今日", denied["decision_reason"])

    def test_month_total_spans_days_but_resets_next_month(self):
        self.now = datetime(2026, 1, 10, 12, tzinfo=TZ)
        self.limits(2000)
        self.wallet.execute(self.request()["id"])
        self.now += timedelta(days=1)
        self.wallet.execute(self.request("tool-8", "documents")["id"])
        denied = self.request("tool-8", "research")
        self.assertEqual(denied["status"], "rejected")
        self.assertIn("本月", denied["decision_reason"])
        self.now = datetime(2026, 2, 1, 12, tzinfo=TZ)
        self.assertEqual(self.request()["status"], "approved")

    def test_concurrent_requests_cannot_overreserve(self):
        self.limits(2400)
        def purchase(index):
            wallet = Wallet(self.path, clock=lambda: self.now)
            return wallet.request(dict(task_id="campaign", product_id="image-12",
                reason="并发预算检查", idempotency_key=f"parallel-{index}"))
        with ThreadPoolExecutor(max_workers=8) as pool:
            orders = list(pool.map(purchase, range(12)))
        self.assertEqual(sum(o["status"] == "approved" for o in orders), 2)
        self.assertEqual(self.wallet.state()["summary"]["month_reserved_cents"], 2400)

    def test_request_replay_preserves_single_order_and_reservation(self):
        order = self.request(idempotency_key="same")
        replay = self.request(idempotency_key="same")
        self.assertEqual(replay, order)
        state = self.wallet.state()
        self.assertEqual(len(state["orders"]), 1)
        self.assertEqual(state["summary"]["month_reserved_cents"], 1200)

    def test_concurrent_same_key_creates_one_order(self):
        payload = dict(task_id="campaign", product_id="image-12", reason="同一需求", idempotency_key="parallel-replay")
        with ThreadPoolExecutor(max_workers=6) as pool:
            orders = list(pool.map(lambda _: self.wallet.request(payload), range(6)))
        self.assertEqual(len({o["id"] for o in orders}), 1)
        self.assertEqual(self.wallet.state()["summary"]["month_reserved_cents"], 1200)

    def test_changed_replay_parameters_conflict(self):
        self.request(idempotency_key="same")
        for change in ({"product_id": "tool-8"}, {"task_id": "research"}, {"reason": "另一用途"}):
            with self.subTest(change=change), self.assertRaises(WalletError) as error:
                self.request(idempotency_key="same", **change)
            self.assertEqual(error.exception.code, "idempotency_conflict")

    def test_caller_cannot_supply_price_identity_or_status(self):
        for extra in ({"amount_cents": 1}, {"price_cents": 1}, {"agent_id": "owner"},
                      {"approved_by": "owner"}, {"status": "paid"}, {"merchant_id": "studio"}):
            with self.subTest(extra=extra), self.assertRaises(WalletError):
                self.request(**extra)
        self.assertEqual(self.wallet.state()["orders"], [])
        order = self.request()
        self.assertEqual((order["amount_cents"], order["agent_id"]), (1200, "creative-agent"))

    def test_above_threshold_requires_explicit_approval(self):
        order = self.request("report-35")
        self.assertEqual(order["status"], "pending_approval")
        self.assertEqual(self.wallet.state()["summary"]["month_reserved_cents"], 3500)
        with self.assertRaises(WalletError):
            self.wallet.execute(order["id"])
        approved = self.wallet.approve(order["id"])
        self.assertEqual((approved["status"], approved["approved_by"]), ("approved", "owner"))
        self.assertIsNone(approved["receipt"])
        paid = self.wallet.execute(order["id"])
        self.assertEqual(paid["status"], "paid")
        self.assertTrue(paid["receipt"]["simulated"])
        self.assertEqual(paid["receipt"]["currency"], "CNY")

    def test_exact_threshold_automatically_approved(self):
        self.wallet.update_policy({"approval_threshold_cents": 1200})
        self.assertEqual(self.request()["status"], "approved")

    def test_multiline_reason_is_data_while_control_characters_are_rejected(self):
        reason = "用途：宣传方案\n说明：<script>不执行，只作为文字保存</script>"
        order = self.request(reason=reason)
        self.assertEqual(order["reason"], reason)
        self.assertEqual(self.wallet.state()["orders"][0]["reason"], reason)
        with self.assertRaises(WalletError):
            self.request(reason="无效\x00字符")

    def test_hard_limit_and_total_budget_cannot_be_approved_away(self):
        denied = self.request("report-120")
        self.assertEqual(denied["status"], "rejected")
        with self.assertRaises(WalletError):
            self.wallet.approve(denied["id"])
        self.limits(3500, threshold=2000)
        pending = self.request("report-35")
        denied = self.request("tool-8", "documents")
        with self.assertRaises(WalletError):
            self.wallet.approve(denied["id"])
        self.wallet.approve(pending["id"])
        self.wallet.execute(pending["id"])
        self.assertEqual(self.wallet.state()["summary"]["month_spent_cents"], 3500)

    def test_budget_cannot_be_lowered_below_exposure(self):
        order = self.request("report-35")
        with self.assertRaises(WalletError) as error:
            self.limits(2000)
        self.assertEqual(error.exception.code, "budget_in_use")
        self.assertEqual(self.wallet.state()["orders"][0]["status"], "pending_approval")
        self.wallet.reject(order["id"])
        self.limits(2000)

    def test_pause_resume_invalidates_old_approved_and_pending_orders(self):
        orders = [self.request(), self.request("report-35")]
        self.wallet.update_policy({"enabled": False})
        self.assertEqual(self.request("tool-8")["status"], "rejected")
        self.wallet.update_policy({"enabled": True})
        for order in orders:
            with self.subTest(order=order["id"]), self.assertRaises(WalletError):
                self.wallet.execute(order["id"])
            with self.assertRaises(WalletError):
                self.wallet.approve(order["id"])
        self.assertEqual(self.wallet.state()["summary"]["month_reserved_cents"], 0)
        self.assertEqual(self.request()["status"], "approved")

    def test_scope_change_invalidates_old_requests_and_rejects_category(self):
        order = self.request("report-35")
        self.wallet.update_policy({"allowed_categories": ["image"]})
        with self.assertRaises(WalletError):
            self.wallet.approve(order["id"])
        denied = self.request("tool-8")
        self.assertEqual(denied["status"], "rejected")
        self.assertIn("类别", denied["decision_reason"])

    def test_subscriptions_unlisted_merchants_and_unknown_products_denied(self):
        for product in ("subscription-15", "outside-5"):
            with self.subTest(product=product):
                self.assertEqual(self.request(product)["status"], "rejected")
        with self.assertRaises(WalletError):
            self.request("nonexistent")
        self.wallet.update_policy({"allowed_merchant_ids": ["studio"]})
        self.assertEqual(self.request("tool-8")["status"], "rejected")
        self.assertEqual(self.wallet.state()["summary"]["month_reserved_cents"], 0)

    def test_decline_and_rejection_release_reservation(self):
        self.limits(1200)
        order = self.request()
        failed = self.wallet.execute(order["id"], "decline")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.wallet.execute(order["id"]), failed)
        second = self.request()
        self.wallet.reject(second["id"])
        self.assertEqual(self.wallet.state()["summary"]["month_available_cents"], 1200)

    def test_unknown_holds_budget_replay_does_not_charge_and_reconcile_once(self):
        self.limits(1200)
        order = self.request()
        unknown = self.wallet.execute(order["id"], "timeout")
        self.assertEqual(unknown["status"], "unknown")
        for scenario in ("success", "decline", "timeout"):
            self.assertEqual(self.wallet.execute(order["id"], scenario), unknown)
        self.assertEqual(self.request()["status"], "rejected")
        with self.assertRaises(WalletError):
            self.wallet.reject(order["id"])
        paid = self.wallet.reconcile(order["id"])
        self.assertEqual(paid["status"], "paid")
        self.assertEqual(self.wallet.reconcile(order["id"]), paid)
        self.assertEqual(self.wallet.execute(order["id"]), paid)
        state = self.wallet.state()
        self.assertEqual(state["summary"]["month_spent_cents"], 1200)
        self.assertEqual(state["summary"]["month_reserved_cents"], 0)
        self.assertEqual(sum(a["action"] == "payment_reconciled" for a in state["audit"]), 1)
        self.assertEqual(sum(a["action"] == "payment_unknown" for a in state["audit"]), 1)

    def test_unknown_crosses_day_month_and_reconcile_retains_current_exposure(self):
        self.limits(1200)
        order = self.request()
        self.wallet.execute(order["id"], "timeout")
        self.now = datetime(2026, 2, 1, 12, tzinfo=TZ)
        state = self.wallet.state()
        self.assertEqual((state["summary"]["day_reserved_cents"], state["summary"]["month_reserved_cents"]), (1200, 1200))
        self.assertEqual(self.request()["status"], "rejected")
        self.wallet.reconcile(order["id"])
        state = self.wallet.state()
        self.assertEqual((state["summary"]["day_spent_cents"], state["summary"]["month_spent_cents"]), (1200, 1200))
        self.assertEqual(self.request()["status"], "rejected")
        self.assertEqual(state["tasks"][0]["spent_cents"], 1200)

    def test_pause_does_not_release_unknown_payment(self):
        order = self.request()
        self.wallet.execute(order["id"], "timeout")
        self.wallet.update_policy({"enabled": False})
        self.assertEqual(self.wallet.state()["summary"]["month_reserved_cents"], 1200)
        self.assertEqual(self.wallet.reconcile(order["id"])["status"], "paid")

    def test_refund_is_idempotent_and_does_not_restore_cumulative_budget(self):
        self.limits(1200)
        order = self.request()
        self.wallet.execute(order["id"])
        refund = self.wallet.refund(order["id"])
        self.assertEqual(refund["status"], "refunded")
        self.assertEqual(self.wallet.refund(order["id"]), refund)
        self.assertEqual(self.wallet.state()["summary"]["month_spent_cents"], 1200)
        self.assertEqual(self.request()["status"], "rejected")

    def test_delivery_failure_retains_spend_and_allows_refund(self):
        order = self.request()
        paid = self.wallet.execute(order["id"], "delivery_failure")
        self.assertEqual(paid["status"], "delivery_failed")
        self.assertIsNotNone(paid["receipt"])
        self.assertIsNone(paid["delivery"])
        self.assertEqual(self.wallet.state()["summary"]["month_spent_cents"], 1200)
        self.assertEqual(self.wallet.refund(order["id"])["status"], "refunded")

    def test_expired_quote_cannot_execute_or_approve_and_releases_hold(self):
        first, second = self.request(), self.request("report-35")
        self.now += timedelta(minutes=15)
        with self.assertRaises(WalletError):
            self.wallet.execute(first["id"])
        with self.assertRaises(WalletError):
            self.wallet.approve(second["id"])
        state = self.wallet.state()
        self.assertEqual(state["summary"]["month_reserved_cents"], 0)
        self.assertTrue(all(o["status"] == "rejected" for o in state["orders"]))

    def test_midnight_invalidates_unpaid_quote_even_before_fifteen_minutes(self):
        self.now = datetime(2026, 1, 31, 23, 58, tzinfo=TZ)
        order = self.request()
        self.now += timedelta(minutes=3)
        with self.assertRaises(WalletError):
            self.wallet.execute(order["id"])
        self.assertEqual(self.wallet.state()["summary"]["month_reserved_cents"], 0)

    def test_database_restart_preserves_settings_orders_and_replay(self):
        self.wallet.update_policy({"approval_threshold_cents": 1000})
        order = self.request(idempotency_key="persist")
        self.wallet.approve(order["id"])
        self.wallet.execute(order["id"], "timeout")
        before = self.wallet.state()
        self.wallet = Wallet(self.path, clock=lambda: self.now)
        self.assertEqual(self.wallet.state(), before)
        self.assertEqual(self.request(idempotency_key="persist")["status"], "unknown")
        self.assertEqual(self.wallet.reconcile(order["id"])["status"], "paid")

    def test_invalid_amounts_and_policy_fields_do_not_mutate_settings(self):
        before = self.wallet.state()["policy"]
        for field in ("monthly_limit_cents", "daily_limit_cents", "per_payment_limit_cents", "approval_threshold_cents"):
            for value in (True, False, -1, 1.5, "1200", None, MAX_CENTS + 1):
                with self.subTest(field=field, value=value), self.assertRaises(WalletError):
                    self.wallet.update_policy({field: value})
        for changes in ({"revision": 999}, {"enabled": 1}, {"allowed_categories": ["image", "image"]},
                        {"allowed_merchant_ids": ["outside"]}, {}, {"unknown": 1}):
            with self.subTest(changes=changes), self.assertRaises(WalletError):
                self.wallet.update_policy(changes)
        self.assertEqual(self.wallet.state()["policy"], before)


if __name__ == "__main__":
    unittest.main()
