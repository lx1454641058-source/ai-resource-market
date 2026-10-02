"""来源：本项目原创。任务生命周期行为测试；仅使用临时 SQLite 与模拟付款。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ai_wallet.engine import MAX_CENTS, Wallet, WalletError


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "tasks.sqlite"
        self.wallet = Wallet(self.path)
        self.sequence = 0

    def create(self, **changes):
        self.sequence += 1
        payload = dict(title="客户资料任务", agent_id="research-bot", budget_cents=8000,
                       idempotency_key=f"task-{self.sequence}")
        payload.update(changes)
        return self.wallet.create_task(payload)

    def buy(self, task_id, product="image-12", **changes):
        self.sequence += 1
        payload = dict(task_id=task_id, product_id=product, reason="完成客户交付",
                       idempotency_key=f"purchase-{self.sequence}")
        payload.update(changes)
        return self.wallet.request(payload)

    def task(self, task_id):
        return next(task for task in self.wallet.state()["tasks"] if task["id"] == task_id)

    def test_seed_tasks_initialize_once_without_resetting_existing_ledger(self):
        paid = self.wallet.execute(self.buy("campaign")["id"])
        # Reproduce a pre-task-lifecycle database: the old schema has settings row 1 only.
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DELETE FROM settings WHERE id=2")
            db.commit()
        self.wallet = Wallet(self.path)
        state = self.wallet.state()
        self.assertEqual(len(state["tasks"]), 3)
        self.assertTrue(all(task["status"] == "active" and task["revision"] == 1 for task in state["tasks"]))
        self.assertEqual(state["orders"][0], paid)
        self.assertEqual(self.task("campaign")["spent_cents"], 1200)
        self.wallet = Wallet(self.path)
        self.assertEqual(len(self.wallet.state()["tasks"]), 3)

    def test_new_task_can_purchase_and_survives_restart(self):
        task = self.create(title="新增设计项目", budget_cents=1200)
        self.assertEqual((task["status"], task["revision"]), ("active", 1))
        order = self.buy(task["id"])
        self.assertEqual((order["status"], order["task_title"], order["agent_id"]),
                         ("approved", "新增设计项目", "research-bot"))
        self.wallet.execute(order["id"])
        before = self.wallet.state()
        self.wallet = Wallet(self.path)
        self.assertEqual(self.wallet.state(), before)
        self.assertEqual(self.task(task["id"])["available_cents"], 0)

    def test_creation_replay_has_one_task_and_different_parameters_conflict(self):
        first = self.create(idempotency_key="create-replay")
        self.assertEqual(self.create(idempotency_key="create-replay"), first)
        for change in ({"title": "另一个"}, {"agent_id": "another-agent"}, {"budget_cents": 9000}):
            with self.subTest(change=change), self.assertRaises(WalletError) as error:
                self.create(idempotency_key="create-replay", **change)
            self.assertEqual(error.exception.code, "idempotency_conflict")
        self.assertEqual(len(self.wallet.state()["tasks"]), 4)
        self.wallet.update_task(first["id"], {"status": "archived", "expected_revision": 1})
        self.assertEqual(self.create(idempotency_key="create-replay")["status"], "archived")
        self.assertEqual(len(self.wallet.state()["tasks"]), 4)

    def test_creation_replay_is_atomic_across_concurrent_callers(self):
        payload = dict(title="并发任务", agent_id="bot", budget_cents=8000, idempotency_key="shared-key")
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.wallet.create_task(payload), range(8)))
        self.assertEqual(len({task["id"] for task in results}), 1)
        self.assertEqual(len(self.wallet.state()["tasks"]), 4)

    def test_revision_is_required_and_stale_update_cannot_overwrite(self):
        task = self.create()
        for payload in ({"title": "missing revision"}, {"expected_revision": True, "title": "bad"},
                        {"expected_revision": 0, "title": "bad"}, {"expected_revision": 1}):
            with self.subTest(payload=payload), self.assertRaises(WalletError):
                self.wallet.update_task(task["id"], payload)
        updated = self.wallet.update_task(task["id"], {"title": "已修改", "expected_revision": 1})
        self.assertEqual(updated["revision"], 2)
        with self.assertRaises(WalletError) as error:
            self.wallet.update_task(task["id"], {"budget_cents": 100, "expected_revision": 1})
        self.assertEqual(error.exception.code, "revision_conflict")
        self.assertEqual(self.task(task["id"])["budget_cents"], 8000)

    def test_two_simultaneous_updates_allow_only_one_revision_winner(self):
        task = self.create()
        def update(title):
            try:
                return self.wallet.update_task(task["id"], {"title": title, "expected_revision": 1})["revision"]
            except WalletError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(update, ["版本甲", "版本乙"]))
        self.assertCountEqual(results, [2, "revision_conflict"])

    def test_pause_revokes_only_own_unpaid_orders_and_resume_does_not_revive(self):
        task = self.create()
        approved = self.buy(task["id"])
        pending = self.buy(task["id"], "report-35")
        other = self.buy("campaign")
        paused = self.wallet.update_task(task["id"], {"status": "paused", "expected_revision": 1})
        self.assertEqual(paused["revision"], 2)
        orders = {order["id"]: order for order in self.wallet.state()["orders"]}
        self.assertEqual(orders[approved["id"]]["status"], "rejected")
        self.assertEqual(orders[pending["id"]]["status"], "rejected")
        self.assertEqual(orders[other["id"]]["status"], "approved")
        self.assertEqual(self.task(task["id"])["reserved_cents"], 0)
        self.assertEqual(self.buy(task["id"])["status"], "rejected")
        for operation, order in ((self.wallet.approve, pending), (self.wallet.execute, approved)):
            with self.assertRaises(WalletError):
                operation(order["id"])
        self.wallet.update_task(task["id"], {"status": "active", "expected_revision": 2})
        with self.assertRaises(WalletError):
            self.wallet.execute(approved["id"])
        self.assertEqual(self.buy(task["id"])["status"], "approved")

    def test_archive_preserves_unknown_and_paid_allows_reconcile_refund_not_restore(self):
        task = self.create()
        paid = self.wallet.execute(self.buy(task["id"])["id"])
        unknown = self.wallet.execute(self.buy(task["id"])["id"], "timeout")
        self.wallet.update_task(task["id"], {"status": "archived", "expected_revision": 1})
        self.assertEqual(self.task(task["id"])["reserved_cents"], 1200)
        self.assertEqual(self.wallet.execute(unknown["id"])["status"], "unknown")
        self.assertEqual(self.wallet.reconcile(unknown["id"])["status"], "paid")
        self.assertEqual(self.wallet.refund(paid["id"])["status"], "refunded")
        self.assertEqual(self.task(task["id"])["spent_cents"], 2400)
        for change in ({"status": "active"}, {"status": "paused"}, {"title": "改名"}, {"budget_cents": 9000}):
            with self.subTest(change=change), self.assertRaises(WalletError) as error:
                self.wallet.update_task(task["id"], dict(change, expected_revision=2))
            self.assertEqual(error.exception.code, "task_archived")
        self.assertEqual(self.buy(task["id"])["status"], "rejected")

    def test_purchase_replay_after_task_pause_returns_original_result(self):
        task = self.create()
        paid = self.wallet.execute(self.buy(task["id"], idempotency_key="original-paid")["id"])
        unpaid = self.buy(task["id"], idempotency_key="original-held")
        self.wallet.update_task(task["id"], {"status": "paused", "expected_revision": 1})
        self.assertEqual(self.buy(task["id"], idempotency_key="original-paid"), paid)
        replay = self.buy(task["id"], idempotency_key="original-held")
        self.assertEqual((replay["id"], replay["status"]), (unpaid["id"], "rejected"))
        self.assertEqual(len(self.wallet.state()["orders"]), 2)

    def test_budget_cannot_drop_below_paid_plus_held_including_unknown_refunds(self):
        task = self.create()
        paid = self.wallet.execute(self.buy(task["id"])["id"])
        self.wallet.execute(self.buy(task["id"])["id"], "timeout")
        self.wallet.refund(paid["id"])
        for amount in (0, 1200, 2399):
            with self.subTest(amount=amount), self.assertRaises(WalletError) as error:
                self.wallet.update_task(task["id"], {"budget_cents": amount, "expected_revision": 1})
            self.assertEqual(error.exception.code, "budget_in_use")
        updated = self.wallet.update_task(task["id"], {"budget_cents": 2400, "expected_revision": 1})
        self.assertEqual(updated["budget_cents"], 2400)
        self.assertEqual(self.task(task["id"])["available_cents"], 0)

    def test_increase_budget_and_rename_preserve_unpaid_orders_and_title_snapshot(self):
        task = self.create(title="原任务", budget_cents=3500)
        order = self.buy(task["id"], "report-35")
        self.wallet.update_task(task["id"], {"title": "新任务名称", "budget_cents": 8000, "expected_revision": 1})
        approved = self.wallet.approve(order["id"])
        self.assertEqual(approved["task_title"], "原任务")
        self.assertEqual(self.wallet.execute(order["id"])["status"], "paid")
        self.assertEqual(self.buy(task["id"])["task_title"], "新任务名称")

    def test_new_tasks_still_share_wallet_global_limits(self):
        self.wallet.update_policy({"monthly_limit_cents": 1200, "daily_limit_cents": 1200,
            "per_payment_limit_cents": 1200, "approval_threshold_cents": 1200})
        first, second = self.create(), self.create()
        self.assertEqual(self.buy(first["id"])["status"], "approved")
        self.assertEqual(self.buy(second["id"])["status"], "rejected")

    def test_validated_fields_and_immutable_agent(self):
        task = self.create()
        for field in ("budget_cents",):
            for value in (True, False, -1, 1.2, "1200", None, MAX_CENTS + 1):
                with self.subTest(value=value), self.assertRaises(WalletError):
                    self.create(**{field: value})
                with self.assertRaises(WalletError):
                    self.wallet.update_task(task["id"], {field: value, "expected_revision": 1})
        for changes in ({"title": ""}, {"title": "x" * 121}, {"title": "a\nb"},
                        {"agent_id": ""}, {"agent_id": "x" * 101}, {"idempotency_key": "x" * 101},
                        {"status": "active"}, {"unexpected": "value"}):
            with self.subTest(changes=changes), self.assertRaises(WalletError):
                self.create(**changes)
        for changes in ({"agent_id": "hijacked"}, {"status": "deleted"}, {"status": True},
                        {"title": "\x00bad"}):
            with self.subTest(changes=changes), self.assertRaises(WalletError):
                self.wallet.update_task(task["id"], dict(changes, expected_revision=1))
        self.assertEqual(self.task(task["id"])["revision"], 1)
        zero = self.create(budget_cents=0)
        self.assertEqual(self.buy(zero["id"])["status"], "rejected")

    def test_noop_keeps_revision_and_public_state_hides_replay_metadata(self):
        task = self.create()
        unchanged = self.wallet.update_task(task["id"], {"title": task["title"], "expected_revision": 1})
        self.assertEqual(unchanged, task)
        self.assertFalse(any(key.startswith("_") for key in task))
        self.assertFalse(any(key.startswith("_") for key in self.task(task["id"])))

    def test_task_status_rechecked_before_execution_even_with_stale_unpaid_record(self):
        task = self.create()
        approved = self.buy(task["id"])
        pending = self.buy(task["id"], "report-35")
        # Simulate an older imported ledger whose task was paused without cancellation.
        with self.wallet._connection() as db:
            tasks = self.wallet._tasks(db)
            next(item for item in tasks if item["id"] == task["id"])["status"] = "paused"
            self.wallet._save_tasks(db, tasks)
        for operation, order in ((self.wallet.approve, pending), (self.wallet.execute, approved)):
            with self.assertRaises(WalletError) as error:
                operation(order["id"])
            self.assertEqual(error.exception.code, "task_unavailable")
        self.assertEqual(self.wallet.state()["summary"]["month_spent_cents"], 0)


if __name__ == "__main__":
    unittest.main()
