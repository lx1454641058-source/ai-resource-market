"""来源：本项目原创。任务预算独立账本的额度守恒和权限边界回归。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from ai_wallet.allocation import AllocationWallet, MODEL
from ai_wallet.engine import MAX_CENTS, TZ, Wallet, WalletError


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "allocation.sqlite"
        self.now = datetime(2026, 9, 10, 12, tzinfo=TZ)
        self.wallet = AllocationWallet(self.path, clock=lambda: self.now)
        self.wallet.configure_pool({"total_cents": 100_000, "expected_revision": 1})
        self.sequence = 0

    def proposal(self, budget=30_000, key=None):
        self.sequence += 1
        return self.wallet.propose_task({"title": "创作任务", "agent_id": "creator",
                                         "budget_cents": budget, "reason": "采购创作素材",
                                         "idempotency_key": key or str(self.sequence)})

    def approve(self, task, budget=None):
        return self.wallet.decide_budget(task["id"], {"decision": "approve",
                        "budget_cents": task["proposed_budget_cents"] if budget is None else budget,
                        "expected_revision": task["revision"]})

    def purchase(self, task, product="image-12", scenario="success"):
        self.sequence += 1
        order = self.wallet.request({"task_id": task["id"], "product_id": product,
                                    "reason": "任务素材", "idempotency_key": str(self.sequence)})
        if scenario is not None and order["status"] == "approved":
            order = self.wallet.execute(order["id"], scenario)
        return order

    def assert_pool(self, unallocated, allocated, spent):
        allocation = self.wallet.state()["allocation"]
        self.assertEqual((allocation["unallocated_cents"], allocation["allocated_cents"], allocation["spent_cents"]),
                         (unallocated, allocated, spent))
        self.assertEqual(allocation["total_cents"], unallocated + allocated + spent)

    def test_new_ledger_is_empty_and_restarts_preserve_model(self):
        state = self.wallet.state()
        self.assertEqual(state["tasks"], [])
        self.assertEqual(state["budget_model"], MODEL)
        self.assertEqual(state["policy"]["approval_threshold_cents"], MAX_CENTS)
        self.assertEqual(AllocationWallet(self.path).state()["allocation"], state["allocation"])

    def test_existing_legacy_ledger_rejected_without_modifying_file(self):
        path = Path(self.temp.name) / "legacy.sqlite"
        Wallet(path)
        before = path.read_bytes()
        with self.assertRaises(WalletError) as result:
            AllocationWallet(path)
        self.assertEqual(result.exception.code, "incompatible_ledger")
        self.assertEqual(path.read_bytes(), before)

    def test_unrelated_non_database_is_not_overwritten(self):
        path = Path(self.temp.name) / "unrelated.sqlite"
        path.write_bytes(b"unrelated local document")
        with self.assertRaises(WalletError):
            AllocationWallet(path)
        self.assertEqual(path.read_bytes(), b"unrelated local document")

    def test_proposal_never_grants_spending_and_same_key_is_idempotent(self):
        task = self.proposal(key="stable")
        self.assertEqual(task["status"], "pending_budget")
        self.assertEqual(task["budget_cents"], 0)
        self.assertEqual(self.proposal(key="stable")["id"], task["id"])
        order = self.purchase(task)
        self.assertEqual(order["status"], "rejected")
        self.assertIn("预算尚未由用户确认", order["decision_reason"])
        with self.assertRaisesRegex(WalletError, "预算尚未由用户确认"):
            self.wallet.execute(order["id"])
        self.assert_pool(100_000, 0, 0)
        with self.assertRaises(WalletError) as result:
            self.proposal(budget=40_000, key="stable")
        self.assertEqual(result.exception.code, "idempotency_conflict")

    def test_user_final_budget_allocated_before_purchase_and_released_on_close(self):
        task = self.approve(self.proposal(40_000), budget=30_000)
        self.assert_pool(70_000, 30_000, 0)
        self.purchase(task, "tool-8")
        self.assert_pool(70_000, 29_200, 800)
        closed = self.wallet.close_task(task["id"], {"expected_revision": task["revision"]})
        self.assertEqual(closed["available_cents"], 0)
        self.assert_pool(99_200, 0, 800)

    def test_normal_purchase_does_not_require_second_owner_approval(self):
        task = self.approve(self.proposal())
        self.assertEqual(self.purchase(task, "report-120")["status"], "paid")
        self.assert_pool(70_000, 18_000, 12_000)

    def test_budget_addition_proposes_without_consuming_then_allocates_difference(self):
        task = self.approve(self.proposal())
        task = self.wallet.propose_budget(task["id"], {"budget_cents": 50_000, "reason": "补充视频素材",
                                                      "expected_revision": task["revision"]})
        self.assertEqual(task["budget_cents"], 30_000)
        self.assert_pool(70_000, 30_000, 0)
        task = self.approve(task, 45_000)
        self.assertEqual(task["budget_cents"], 45_000)
        self.assert_pool(55_000, 45_000, 0)

    def test_reject_initial_or_additional_proposal_does_not_allocate(self):
        proposal = self.proposal()
        rejected = self.wallet.decide_budget(proposal["id"], {"decision": "reject", "expected_revision": 1})
        self.assertEqual(rejected["status"], "rejected")
        self.assert_pool(100_000, 0, 0)
        task = self.approve(self.proposal())
        proposed = self.wallet.propose_budget(task["id"], {"budget_cents": 50_000, "reason": "补充",
                                                          "expected_revision": task["revision"]})
        task = self.wallet.decide_budget(task["id"], {"decision": "reject", "expected_revision": proposed["revision"]})
        self.assertEqual(task["status"], "active")
        self.assertEqual(task["budget_cents"], 30_000)
        self.assertIsNone(task["proposed_budget_cents"])
        self.assert_pool(70_000, 30_000, 0)

    def test_duplicate_or_stale_decision_cannot_allocate_twice(self):
        task = self.proposal()
        self.approve(task)
        with self.assertRaises(WalletError) as result:
            self.approve(task)
        self.assertEqual(result.exception.code, "revision_conflict")
        self.assert_pool(70_000, 30_000, 0)

    def test_concurrent_tasks_cannot_overallocate_shared_pool(self):
        first, second = self.proposal(60_000), self.proposal(60_000)
        def attempt(task):
            try:
                return self.approve(task)["status"]
            except WalletError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as executor:
            result = list(executor.map(attempt, (first, second)))
        self.assertCountEqual(result, ["active", "insufficient_allocation"])
        self.assert_pool(40_000, 60_000, 0)

    def test_old_creation_budget_edit_and_pending_activation_are_blocked(self):
        task = self.proposal()
        for callback in (
            lambda: self.wallet.create_task({}),
            lambda: self.wallet.update_task(task["id"], {"budget_cents": 20_000, "expected_revision": 1}),
            lambda: self.wallet.update_task(task["id"], {"status": "active", "expected_revision": 1}),
        ):
            with self.assertRaises(WalletError):
                callback()
        self.assert_pool(100_000, 0, 0)

    def test_pause_retains_task_allocation_and_cancels_unpaid_orders(self):
        task = self.approve(self.proposal())
        order = self.purchase(task, scenario=None)
        paused = self.wallet.update_task(task["id"], {"status": "paused", "expected_revision": task["revision"]})
        self.assertEqual(paused["reserved_cents"], 0)
        self.assert_pool(70_000, 30_000, 0)
        self.assertEqual(self.wallet.state()["orders"][0]["status"], "rejected")
        self.assertEqual(self.purchase(paused)["status"], "rejected")
        with self.assertRaises(WalletError):
            self.wallet.execute(order["id"])
        resumed = self.wallet.update_task(task["id"], {"status": "active", "expected_revision": paused["revision"]})
        self.assertEqual(self.purchase(resumed)["status"], "paid")

    def test_close_unknown_keeps_exposure_and_late_reconciliation_preserves_pool(self):
        task = self.approve(self.proposal())
        unknown = self.purchase(task, scenario="timeout")
        unpaid = self.purchase(task, scenario=None)
        closed = self.wallet.close_task(task["id"], {"expected_revision": task["revision"]})
        self.assertEqual(closed["reserved_cents"], 1200)
        self.assert_pool(98_800, 1200, 0)
        self.now = datetime(2026, 10, 1, 12, tzinfo=TZ)
        self.assert_pool(98_800, 1200, 0)
        self.assertEqual(self.wallet.execute(unknown["id"])["status"], "unknown")
        self.assertEqual(self.wallet.reconcile(unknown["id"])["status"], "paid")
        self.assert_pool(98_800, 0, 1200)
        with self.assertRaises(WalletError):
            self.wallet.execute(unpaid["id"])
        self.assertEqual(self.purchase(closed)["status"], "rejected")

    def test_month_boundary_and_refund_do_not_restore_total_authorization(self):
        task = self.approve(self.proposal())
        order = self.purchase(task)
        self.now = datetime(2026, 10, 1, 12, tzinfo=TZ)
        self.assert_pool(70_000, 28_800, 1200)
        self.wallet.refund(order["id"])
        self.assert_pool(70_000, 28_800, 1200)
        self.wallet.close_task(task["id"], {"expected_revision": task["revision"]})
        self.assert_pool(98_800, 0, 1200)

    def test_expired_quote_releases_order_hold_but_not_task_allocation(self):
        task = self.approve(self.proposal())
        order = self.purchase(task, scenario=None)
        self.now += timedelta(minutes=16)
        state = self.wallet.state()
        self.assertEqual(state["orders"][0]["status"], "rejected")
        self.assertEqual(state["tasks"][0]["reserved_cents"], 0)
        self.assert_pool(70_000, 30_000, 0)
        with self.assertRaises(WalletError):
            self.wallet.execute(order["id"])

    def test_pool_reduction_cannot_reassign_committed_or_spent_amounts(self):
        task = self.approve(self.proposal())
        self.purchase(task)
        allocation = self.wallet.state()["allocation"]
        with self.assertRaises(WalletError) as result:
            self.wallet.configure_pool({"total_cents": 29_999, "expected_revision": allocation["revision"]})
        self.assertEqual(result.exception.code, "budget_in_use")
        allocation = self.wallet.configure_pool({"total_cents": 30_000, "expected_revision": allocation["revision"]})
        self.assertEqual(allocation["unallocated_cents"], 0)
        self.assert_pool(0, 28_800, 1200)

    def test_explicit_advanced_rules_still_restrict_spending(self):
        task = self.approve(self.proposal())
        self.wallet.update_policy({"per_payment_limit_cents": 1000, "approval_threshold_cents": 1000})
        self.assertEqual(self.purchase(task)["status"], "rejected")
        self.assertEqual(self.purchase(task, "tool-8")["status"], "paid")
        self.assertEqual(self.purchase(task, "subscription-15")["status"], "rejected")
        self.assertEqual(self.purchase(task, "outside-5")["status"], "rejected")

    def test_invalid_types_and_stale_versions_leave_pool_unchanged(self):
        task = self.proposal()
        for invalid in (True, -1, 0.1, MAX_CENTS + 1):
            with self.subTest(invalid=invalid):
                with self.assertRaises(WalletError):
                    self.wallet.decide_budget(task["id"], {"decision": "approve", "budget_cents": invalid, "expected_revision": 1})
        with self.assertRaises(WalletError):
            self.wallet.configure_pool({"total_cents": 50_000, "expected_revision": 1})
        self.assert_pool(100_000, 0, 0)

    def test_task_cannot_borrow_another_tasks_allocation_or_unassigned_pool(self):
        limited = self.approve(self.proposal(1200))
        self.approve(self.proposal(50_000))
        self.assertEqual(self.purchase(limited)["status"], "paid")
        self.assertEqual(self.purchase(limited, "tool-8")["status"], "rejected")
        self.assert_pool(48_800, 50_000, 1200)

    def test_paused_task_budget_confirmation_does_not_resume_it(self):
        task = self.approve(self.proposal())
        task = self.wallet.update_task(task["id"], {"status": "paused", "expected_revision": task["revision"]})
        task = self.wallet.propose_budget(task["id"], {"budget_cents": 40_000, "reason": "补充",
                                                      "expected_revision": task["revision"]})
        task = self.approve(task)
        self.assertEqual(task["status"], "paused")
        self.assertEqual(self.purchase(task)["status"], "rejected")
        self.assert_pool(60_000, 40_000, 0)

    def test_replaced_proposal_invalidates_old_confirmation(self):
        task = self.approve(self.proposal())
        first = self.wallet.propose_budget(task["id"], {"budget_cents": 40_000, "reason": "补充",
                                                       "expected_revision": task["revision"]})
        second = self.wallet.propose_budget(task["id"], {"budget_cents": 50_000, "reason": "重新估算",
                                                        "expected_revision": first["revision"]})
        with self.assertRaises(WalletError) as result:
            self.approve(first)
        self.assertEqual(result.exception.code, "revision_conflict")
        self.assert_pool(70_000, 30_000, 0)
        self.approve(second)
        self.assert_pool(50_000, 50_000, 0)

    def test_insufficient_addition_keeps_original_budget_and_pending_proposal(self):
        task = self.approve(self.proposal(80_000))
        proposed = self.wallet.propose_budget(task["id"], {"budget_cents": 110_000, "reason": "补充",
                                                          "expected_revision": task["revision"]})
        with self.assertRaises(WalletError) as result:
            self.approve(proposed)
        self.assertEqual(result.exception.code, "insufficient_allocation")
        actual = self.wallet.state()["tasks"][0]
        self.assertEqual(actual["budget_cents"], 80_000)
        self.assertEqual(actual["proposed_budget_cents"], 110_000)
        self.assertEqual(actual["revision"], proposed["revision"])
        self.assert_pool(20_000, 80_000, 0)


if __name__ == "__main__":
    unittest.main()
