"""来源：本项目原创。授权保存的事务内版本与影响清单检查；只用临时账本。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from ai_wallet.engine import TZ, Wallet, WalletError


class PolicyCommitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026, 9, 10, 12, tzinfo=TZ)
        self.wallet = Wallet(Path(self.temp.name) / "commit.sqlite", clock=lambda: self.now)
        self.sequence = 0

    def buy(self):
        self.sequence += 1
        return self.wallet.request(dict(task_id="campaign", product_id="image-12", reason="验证确认范围",
                                        idempotency_key=f"purchase-{self.sequence}"))

    def test_matching_revision_and_reordered_ids_commit_and_revoke(self):
        first, second = self.buy(), self.buy()
        result = self.wallet.update_policy({"monthly_limit_cents": 31000}, expected_revision=1,
                                         expected_pending_ids=[second["id"], first["id"]])
        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["monthly_limit_cents"], 31000)
        self.assertTrue(all(order["status"] == "rejected" for order in self.wallet.state()["orders"]))

    def test_changed_revision_rejects_without_mutation(self):
        self.wallet.update_policy({"monthly_limit_cents": 32000})
        with self.assertRaises(WalletError) as error:
            self.wallet.update_policy({"monthly_limit_cents": 31000}, expected_revision=1, expected_pending_ids=[])
        self.assertEqual(error.exception.code, "revision_conflict")
        self.assertEqual(self.wallet.state()["policy"]["monthly_limit_cents"], 32000)

    def test_purchase_created_after_preview_blocks_stale_empty_list(self):
        order = self.buy()
        with self.assertRaises(WalletError) as error:
            self.wallet.update_policy({"monthly_limit_cents": 31000}, expected_revision=1, expected_pending_ids=[])
        self.assertEqual(error.exception.code, "pending_orders_conflict")
        state = self.wallet.state()
        self.assertEqual(state["policy"]["revision"], 1)
        self.assertEqual(state["orders"][0]["id"], order["id"])
        self.assertEqual(state["orders"][0]["status"], "approved")

    def test_purchase_paid_after_preview_blocks_stale_impact_list(self):
        order = self.buy()
        self.wallet.execute(order["id"])
        with self.assertRaises(WalletError) as error:
            self.wallet.update_policy({"monthly_limit_cents": 31000}, expected_revision=1,
                                      expected_pending_ids=[order["id"]])
        self.assertEqual(error.exception.code, "pending_orders_conflict")
        self.assertEqual(self.wallet.state()["policy"]["revision"], 1)

    def test_expiration_before_commit_is_part_of_conflict_check(self):
        order = self.buy()
        self.now += timedelta(minutes=16)
        with self.assertRaises(WalletError) as error:
            self.wallet.update_policy({"monthly_limit_cents": 31000}, expected_revision=1,
                                      expected_pending_ids=[order["id"]])
        self.assertEqual(error.exception.code, "pending_orders_conflict")
        self.assertEqual(self.wallet.state()["orders"][0]["status"], "rejected")
        result = self.wallet.update_policy({"monthly_limit_cents": 31000}, expected_revision=1, expected_pending_ids=[])
        self.assertEqual(result["revision"], 2)

    def test_unknown_is_excluded_from_cancellation_snapshot_and_retains_hold(self):
        order = self.buy()
        self.wallet.execute(order["id"], "timeout")
        self.wallet.update_policy({"monthly_limit_cents": 31000}, expected_revision=1, expected_pending_ids=[])
        state = self.wallet.state()
        self.assertEqual(state["orders"][0]["status"], "unknown")
        self.assertEqual(state["summary"]["month_reserved_cents"], 1200)

    def test_simultaneous_commits_of_same_version_have_one_winner(self):
        def commit(amount):
            try:
                return self.wallet.update_policy({"monthly_limit_cents": amount}, expected_revision=1,
                                                 expected_pending_ids=[])["revision"]
            except WalletError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(commit, [31000, 32000]))
        self.assertCountEqual(results, [2, "revision_conflict"])

    def test_revision_and_pending_list_types_are_strict(self):
        for value in (True, False, 0, -1, "1", 1.0, []):
            with self.subTest(revision=value), self.assertRaises(WalletError):
                self.wallet.update_policy({"monthly_limit_cents": 31000}, expected_revision=value)
        for value in (True, "", {}, (), [True], [1], [None], [""], ["x", "x"], ["x" * 101]):
            with self.subTest(ids=value), self.assertRaises(WalletError):
                self.wallet.update_policy({"monthly_limit_cents": 31000}, expected_pending_ids=value)
        self.assertEqual(self.wallet.state()["policy"]["revision"], 1)

    def test_legacy_call_remains_supported_and_noop_keeps_revision(self):
        self.assertEqual(self.wallet.update_policy({"monthly_limit_cents": 30000},
                         expected_revision=1, expected_pending_ids=[])["revision"], 1)
        self.assertEqual(self.wallet.update_policy({"monthly_limit_cents": 31000})["revision"], 2)


if __name__ == "__main__":
    unittest.main()
