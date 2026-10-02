"""来源：本项目原创。单任务工具的隔离账本、单次调用与并发边界测试。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from ai_wallet.agent_interface import TaskWallet
from ai_wallet.engine import TZ, Wallet
from ai_wallet.server import LocalServer


class TaskWalletTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.now = datetime(2026, 9, 10, 12, tzinfo=TZ)
        self.wallet = Wallet(Path(directory.name) / "isolated.sqlite", clock=lambda: self.now)
        self.agent = TaskWallet(self.wallet, "campaign")

    def buy(self, request_id="asset-1", product="image-12", agent=None):
        return (agent or self.agent).purchase(product, "宣传需要", request_id)

    def test_small_purchase_finishes_in_one_call_and_replay_spends_once(self):
        first = self.buy()
        self.assertEqual(first["status"], "paid")
        self.assertEqual(first["next_action"], "use_demo_delivery")
        self.assertTrue(first["delivery"]["simulated"])
        self.assertEqual(first, self.buy())
        self.assertEqual(first, self.buy(agent=TaskWallet(self.wallet, "campaign")))
        state = self.wallet.state()
        self.assertEqual(len(state["orders"]), 1)
        self.assertEqual(state["summary"]["month_spent_cents"], 1200)
        self.assertEqual(sum(item["action"] == "payment_paid" for item in state["audit"]), 1)

    def test_pending_cannot_self_approve_and_user_can_resume_original_request(self):
        pending = self.buy(product="report-35")
        self.assertEqual((pending["status"], pending["next_action"]), ("pending_approval", "wait_for_user"))
        self.assertEqual(pending, self.buy(product="report-35"))
        self.assertEqual(self.wallet.state()["summary"]["month_reserved_cents"], 3500)
        self.wallet.approve(pending["order_id"])
        observed = self.agent.status(pending["order_id"])
        self.assertEqual(observed["status"], "approved")
        self.assertEqual(self.wallet.state()["summary"]["month_spent_cents"], 0)
        paid = self.buy(product="report-35")
        self.assertEqual((paid["order_id"], paid["status"]), (pending["order_id"], "paid"))
        self.assertEqual(len(self.wallet.state()["orders"]), 1)
        for name in ("approve", "refund", "update_policy", "update_task", "reconcile", "execute"):
            self.assertFalse(hasattr(self.agent, name))

    def test_paid_dispatch_without_delivery_waits_for_host_without_writing(self):
        from ai_wallet.dispatch import _OrderDispatch
        order = self.wallet.request({'task_id': 'campaign', 'product_id': 'image-12',
                                     'reason': '独立交付', 'idempotency_key': 'separate-delivery'})
        dispatch = _OrderDispatch(self.wallet, 'campaign')
        execution = dispatch.claim(order['id'])
        dispatch.record(order['id'], execution, status='paid', amount_cents=1200,
                        currency='CNY', evidence_ref='DEMO-PAID', simulated=True)
        before = self.wallet.state()
        result = self.agent.status(order['id'])
        self.assertEqual((result['status'], result['next_action']), ('paid', 'wait_for_host'))
        self.assertNotIn('delivery', result)
        self.assertNotIn('dispatch', result)
        self.assertEqual(self.wallet.state(), before)

    def test_changed_payload_same_key_conflicts_and_namespace_separates_tasks(self):
        original = self.buy()
        for product, reason in (("tool-8", "宣传需要"), ("image-12", "其他用途")):
            changed = self.agent.purchase(product, reason, "asset-1")
            self.assertEqual(changed["error_code"], "idempotency_conflict")
        other = self.buy(agent=TaskWallet(self.wallet, "research"))
        self.assertNotEqual(original["order_id"], other["order_id"])
        self.assertEqual(self.wallet.state()["summary"]["month_spent_cents"], 2400)
        hidden = self.agent.status(other["order_id"])
        self.assertEqual(hidden, self.agent.status("does-not-exist"))
        self.assertNotIn(other["order_id"], json.dumps(self.agent.context()))

    def test_unknown_keeps_hold_and_read_or_replay_never_reconciles(self):
        agent = TaskWallet(self.wallet, "campaign", simulation="timeout")
        first = self.buy(agent=agent)
        self.assertEqual((first["status"], first["next_action"]), ("unknown", "wait_for_user_reconciliation"))
        before = self.wallet.state()
        for _ in range(3):
            self.assertEqual(first, self.buy(agent=agent))
            self.assertEqual(first, agent.status(first["order_id"]))
        self.assertEqual(self.wallet.state(), before)
        self.now += timedelta(days=35)
        self.assertEqual(agent.status(first["order_id"])["status"], "unknown")
        self.assertEqual(self.wallet.state()["summary"]["month_reserved_cents"], 1200)

    def test_failure_and_delivery_failure_do_not_blindly_retry(self):
        for simulation, status, action, spent in (
            ("decline", "failed", "wait_for_user", 0),
            ("delivery_failure", "delivery_failed", "wait_for_user_support", 1200),
        ):
            with self.subTest(simulation=simulation):
                agent = TaskWallet(self.wallet, "campaign", simulation=simulation)
                result = self.buy(simulation, agent=agent)
                self.assertEqual((result["status"], result["next_action"]), (status, action))
                self.assertNotIn("delivery", result)
                self.assertEqual(self.buy(simulation, agent=agent), result)
                self.assertEqual(self.wallet.state()["summary"]["month_spent_cents"], spent)

    def test_pause_and_scope_denials_cannot_be_overridden(self):
        self.wallet.update_policy({"enabled": False})
        self.assertFalse(self.agent.context()["can_purchase"])
        self.assertEqual(self.agent.context()["available_cents"], 0)
        self.assertEqual(self.buy()["next_action"], "stop")
        self.wallet.update_policy({"enabled": True, "allowed_categories": ["image"]})
        self.assertEqual(self.buy()["status"], "rejected")
        self.assertEqual(self.buy("new", "tool-8")["status"], "rejected")
        self.wallet.update_task("campaign", {"status": "paused", "expected_revision": 1})
        self.assertEqual(self.buy("paused")["status"], "rejected")
        self.assertEqual(self.wallet.state()["summary"]["month_spent_cents"], 0)

    def test_pause_between_request_and_execute_returns_current_denial(self):
        execute = self.wallet.execute
        def pause_then_execute(*args):
            self.wallet.update_policy({"enabled": False})
            return execute(*args)
        with patch.object(self.wallet, "execute", side_effect=pause_then_execute):
            result = self.buy()
        self.assertEqual((result["status"], result["next_action"]), ("rejected", "stop"))
        self.assertEqual(self.wallet.state()["summary"]["month_spent_cents"], 0)

    def test_lost_response_after_payment_commit_does_not_report_failure(self):
        execute = self.wallet.execute
        def settle_then_lose_response(*args):
            execute(*args)
            raise sqlite3.OperationalError("fixture response lost")
        with patch.object(self.wallet, "execute", side_effect=settle_then_lose_response):
            result = self.buy()
        self.assertEqual((result["status"], result["next_action"]), ("unavailable", "wait_for_host"))
        self.assertNotIn("fixture response lost", str(result))
        recovered = self.buy()
        self.assertEqual((recovered["order_id"], recovered["status"]), (result["order_id"], "paid"))
        state = self.wallet.state()
        self.assertEqual((len(state["orders"]), state["summary"]["month_spent_cents"]), (1, 1200))

    def test_unavailable_status_does_not_fabricate_payment_result(self):
        result = self.buy()
        with patch.object(self.wallet, "_get", side_effect=sqlite3.OperationalError("fixture path")):
            observed = self.agent.status(result["order_id"])
        self.assertEqual((observed["status"], observed["next_action"]), ("unavailable", "wait_for_host"))
        self.assertNotIn("fixture path", str(observed))

    def test_status_is_read_only_even_when_quote_expired(self):
        pending = self.buy(product="report-35")
        self.now += timedelta(minutes=16)
        with self.wallet._connection() as db:
            before = (self.wallet._get(db, pending["order_id"]), db.execute("SELECT COUNT(*) FROM audit").fetchone()[0])
        result = self.agent.status(pending["order_id"])
        self.assertEqual((result["status"], result["next_action"]), ("pending_approval", "stop"))
        with self.wallet._connection() as db:
            after = (self.wallet._get(db, pending["order_id"]), db.execute("SELECT COUNT(*) FROM audit").fetchone()[0])
        self.assertEqual(before, after)

    def test_concurrent_same_request_and_different_requests_respect_budget(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.buy(), range(8)))
        self.assertEqual(len({result["order_id"] for result in results}), 1)
        self.assertTrue(all(result["status"] == "paid" for result in results))
        self.wallet.update_policy({"monthly_limit_cents": 2400, "daily_limit_cents": 2400,
                                   "per_payment_limit_cents": 2400, "approval_threshold_cents": 2000})
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda index: self.buy(f"new-{index}"), range(8)))
        self.assertEqual(sum(result["status"] == "paid" for result in results), 1)
        self.assertEqual(self.wallet.state()["summary"]["month_spent_cents"], 2400)

    def test_context_has_only_current_task_and_budget_hint_not_authorization(self):
        self.buy(agent=TaskWallet(self.wallet, "research"))
        context = self.agent.context()
        self.assertEqual(context["task_id"], "campaign")
        self.assertEqual(context["available_cents"], 8800)
        self.assertNotIn("research", json.dumps(context))
        for forbidden in ("orders", "audit", "csrf_token", "agent_id", "request_key", "request_hash"):
            self.assertNotIn(forbidden, json.dumps(context))
        products = {product["id"]: product for product in context["products"]}
        self.assertFalse(products["subscription-15"]["scope_allowed"])
        self.assertFalse(products["outside-5"]["scope_allowed"])
        self.assertTrue(products["report-120"]["scope_allowed"])
        self.assertEqual(self.buy("over-limit", "report-120")["status"], "rejected")

    def test_model_cannot_supply_task_price_scenario_or_privileged_fields(self):
        for extra in ({"task_id": "research"}, {"amount_cents": 1}, {"simulation": "success"},
                      {"approved_by": "owner"}, {"scenario": "success"}):
            with self.subTest(extra=extra), self.assertRaises(TypeError):
                self.agent.purchase("image-12", "宣传需要", "asset-1", **extra)
        for value in (None, True, "", "x" * 101, "a\x00b"):
            self.assertEqual(self.buy(value)["status"], "error")
        self.assertEqual(self.wallet.state()["orders"], [])

    def test_response_size_against_actual_state_endpoint_on_isolated_fixture(self):
        result = self.buy()
        context = self.agent.context()
        server = LocalServer(0, self.wallet)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(server.origin + "/api/state", timeout=3) as response:
                state_body = response.read()
            compact = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            sizes = {"state_http_body_bytes": len(state_body),
                     "state_compact_json_bytes": len(compact(json.loads(state_body))),
                     "context_compact_json_bytes": len(compact(context)),
                     "purchase_compact_json_bytes": len(compact(result))}
            self.assertLess(sizes["context_compact_json_bytes"] + sizes["purchase_compact_json_bytes"],
                            sizes["state_compact_json_bytes"])
            print("Isolated one-purchase fixture UTF-8 bytes:", json.dumps(sizes))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
