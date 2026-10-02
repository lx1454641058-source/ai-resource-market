"""来源：本项目原创。保护模式新增入口与本地问题记录的隔离回归测试。"""

from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest

from ai_wallet.engine import Wallet, WalletError
from ai_wallet.server import LocalServer
from ai_wallet.support import SupportDesk


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.wallet = Wallet(Path(self.temp.name) / "boundary.sqlite")
        self.password = "only-for-isolated-tests-123"
        self.server = LocalServer(0, self.wallet, owner_password=self.password)
        self.worker = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .01}, daemon=True)
        self.worker.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)
        self.temp.cleanup()

    def call(self, method, path, body=None, cookie="", token=""):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Content-Type": "application/json", "Origin": self.server.origin,
                   "Cookie": cookie, "X-Wallet-Token": token}
        try:
            connection.request(method, path, json.dumps(body or {}) if method == "POST" else None, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            connection.close()

    def login(self):
        code, _, headers = self.call("POST", "/api/session/login", {"password": self.password})
        self.assertEqual(code, 200)
        cookie = headers["Set-Cookie"].split(";")[0]
        code, state, _ = self.call("GET", "/api/state", cookie=cookie)
        self.assertEqual(code, 200)
        return cookie, state["csrf_token"]

    def test_every_new_private_entry_requires_login_and_mutations_require_token(self):
        endpoints = [
            ("GET", "/api/support/tickets"), ("GET", "/api/export"),
            ("POST", "/api/tasks"), ("POST", "/api/tasks/campaign"),
            ("POST", "/api/support/tickets"), ("POST", "/api/support/tickets/" + "a" * 20),
            ("POST", "/api/policy/commit"),
        ]
        for method, endpoint in endpoints:
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.call(method, endpoint, token=self.server.csrf_token)[0], 401)
        cookie, token = self.login()
        for method, endpoint in endpoints:
            if method == "POST":
                with self.subTest(no_csrf=endpoint):
                    self.assertEqual(self.call(method, endpoint, cookie=cookie)[0], 403)
        self.assertEqual(len(self.wallet.state()["tasks"]), 3)
        self.assertEqual(SupportDesk(self.wallet).list(), [])
        self.assertEqual(self.wallet.state()["policy"]["revision"], 1)
        self.call("POST", "/api/session/logout", cookie=cookie, token=token)
        for method, endpoint in endpoints:
            with self.subTest(logged_out=endpoint):
                self.assertEqual(self.call(method, endpoint, cookie=cookie, token=token)[0], 401)

    def test_background_gets_cannot_extend_idle_login_but_explicit_post_can(self):
        now = [0]
        self.server.access._clock = lambda: now[0]
        cookie, token = self.login()
        for moment in (600, 1200, 1799):
            now[0] = moment
            self.assertEqual(self.call("GET", "/api/state", cookie=cookie)[0], 200)
            self.assertTrue(self.call("GET", "/api/session", cookie=cookie)[1]["authenticated"])
        now[0] = 1800
        self.assertEqual(self.call("GET", "/api/state", cookie=cookie)[0], 401)
        self.assertFalse(self.call("GET", "/api/session", cookie=cookie)[1]["authenticated"])
        cookie, token = self.login()
        now[0] = 3000
        self.assertEqual(self.call("POST", "/api/wallet/pause", cookie=cookie, token=token)[0], 200)
        now[0] = 4000
        self.assertEqual(self.call("GET", "/api/state", cookie=cookie)[0], 200)
        now[0] = 4800
        self.assertEqual(self.call("GET", "/api/state", cookie=cookie)[0], 401)

    def test_export_keeps_history_but_never_exposes_internal_replay_keys_or_session_token(self):
        task = self.wallet.create_task(dict(title="审计任务", agent_id="audit-bot", budget_cents=8000,
                                           idempotency_key="private-task-replay-key"))
        order = self.wallet.request(dict(task_id=task["id"], product_id="image-12", reason="审计用途",
                                         idempotency_key="private-order-replay-key"))
        desk = SupportDesk(self.wallet)
        ticket = desk.create(dict(order_id=order["id"], category="other", description="仅保存在本机",
                                  idempotency_key="private-support-replay-key"))
        cookie, token = self.login()
        code, exported, _ = self.call("GET", "/api/export", cookie=cookie)
        self.assertEqual(code, 200)
        self.assertEqual(exported["orders"][0]["id"], order["id"])
        self.assertEqual(exported["tickets"][0]["id"], ticket["id"])
        rendered = json.dumps(exported)
        for private in ("private-task-replay-key", "private-order-replay-key", "private-support-replay-key", token, self.password):
            self.assertNotIn(private, rendered)
        self.assertTrue(exported["audit"])


class SupportConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.wallet = Wallet(Path(self.temp.name) / "support.sqlite")
        self.desk = SupportDesk(self.wallet)
        self.order = self.wallet.request(dict(task_id="campaign", product_id="image-12", reason="验证问题流程",
                                              idempotency_key="order"))
        self.body = dict(order_id=self.order["id"], category="payment", description="需要核对付款状态",
                         idempotency_key="one-issue")

    def test_concurrent_create_is_one_ticket_and_never_changes_order(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            tickets = list(pool.map(lambda _: self.desk.create(self.body), range(8)))
        self.assertEqual(len({ticket["id"] for ticket in tickets}), 1)
        self.assertEqual(len(self.desk.list()), 1)
        self.assertEqual(self.wallet.state()["orders"], [self.order])

    def test_concurrent_ticket_updates_allow_one_winner_and_resolution_does_not_refund(self):
        self.wallet.execute(self.order["id"], "delivery_failure")
        before = self.wallet.state()["orders"]
        ticket = self.desk.create(self.body)
        def update(description):
            try:
                return self.desk.update(ticket["id"], dict(status="resolved", resolution=description,
                                                           expected_revision=1))["revision"]
            except WalletError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(update, ["处理结果甲", "处理结果乙"]))
        self.assertCountEqual(outcomes, [2, "revision_conflict"])
        self.assertEqual(self.wallet.state()["orders"], before)
        self.assertEqual(self.wallet.state()["summary"]["month_spent_cents"], 1200)

    def test_issue_snapshot_is_immutable_when_order_changes(self):
        ticket = self.desk.create(self.body)
        self.wallet.execute(self.order["id"])
        self.wallet.refund(self.order["id"])
        self.assertEqual(self.desk.list()[0]["order_snapshot"], ticket["order_snapshot"])
        self.assertEqual(self.desk.list()[0]["order_snapshot"]["status"], "approved")


if __name__ == "__main__":
    unittest.main()
