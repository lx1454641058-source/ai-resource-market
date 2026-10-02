"""来源：本项目原创。本机 HTTP 行为测试；仅操作临时模拟账本。"""

from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest

from ai_wallet.engine import Wallet
from ai_wallet.server import LocalServer


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.wallet = Wallet(Path(self.temp.name) / "http.sqlite")
        self.server = LocalServer(0, self.wallet)
        self.worker = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.worker.start()
        self.addCleanup(self.cleanup)
        status, state, _ = self.call("GET", "/api/state")
        self.assertEqual(status, 200)
        self.token = state["csrf_token"]

    def cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)
        self.temp.cleanup()

    def call(self, method, path, body=None, headers=None, raw=False):
        supplied = {}
        if method == "POST":
            supplied.update({"Origin": self.server.origin, "X-Wallet-Token": self.token,
                             "Content-Type": "application/json"})
            body = body if raw else json.dumps({} if body is None else body)
        supplied.update(headers or {})
        supplied = {k: v for k, v in supplied.items() if v is not None}
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=supplied)
            response = connection.getresponse()
            data = response.read()
            try:
                data = json.loads(data)
            except (ValueError, UnicodeError):
                pass
            return response.status, data, dict(response.getheaders())
        finally:
            connection.close()

    def create(self, **changes):
        payload = dict(task_id="campaign", product_id="image-12", reason="本机接口验证", idempotency_key="http-order")
        payload.update(changes)
        return self.call("POST", "/api/orders", payload)

    def test_health_and_state_explicitly_identify_demo(self):
        status, health, headers = self.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(health, dict(ok=True, mode="demo", real_payments=False, real_agent=False))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        status, state, _ = self.call("GET", "/api/state")
        self.assertEqual(state["mode"], "demo")
        self.assertEqual(state["csrf_token"], self.token)

    def test_valid_order_approval_execute_and_replay(self):
        status, data, _ = self.create(product_id="report-35")
        self.assertEqual(status, 200)
        order = data["order"]
        self.assertEqual(order["status"], "pending_approval")
        base = "/api/orders/" + order["id"]
        self.assertEqual(self.call("POST", base + "/execute")[0], 409)
        self.assertEqual(self.call("POST", base + "/approve")[1]["order"]["status"], "approved")
        paid = self.call("POST", base + "/execute")[1]["order"]
        self.assertEqual(paid["status"], "paid")
        self.assertEqual(self.call("POST", base + "/execute")[1]["order"], paid)

    def test_bad_host_and_cross_site_requests_are_rejected(self):
        for method, path in (("GET", "/api/state"), ("POST", "/api/wallet/pause")):
            for headers in ({"Host": "attacker.example"}, {"Host": "localhost"},
                            {"Sec-Fetch-Site": "cross-site"}):
                with self.subTest(method=method, headers=headers):
                    status, data, _ = self.call(method, path, headers=headers)
                    self.assertEqual(status, 403)
                    self.assertFalse(data["ok"])
        self.assertTrue(self.wallet.state()["policy"]["enabled"])

    def test_missing_or_foreign_origin_and_bad_token_cannot_mutate(self):
        for headers in ({"Origin": None}, {"Origin": "https://attacker.example"},
                        {"Origin": "null"}, {"X-Wallet-Token": None}, {"X-Wallet-Token": "wrong"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.call("POST", "/api/wallet/pause", headers=headers)[0], 403)
        self.assertTrue(self.wallet.state()["policy"]["enabled"])

    def test_non_json_content_type_is_rejected(self):
        for content_type in ("text/plain", "application/x-www-form-urlencoded", None):
            with self.subTest(content_type=content_type):
                self.assertEqual(self.call("POST", "/api/wallet/pause",
                    headers={"Content-Type": content_type})[0], 415)

    def test_invalid_json_duplicate_fields_and_non_object_are_rejected(self):
        for body in ("{", "[]", "null", '"string"', '{"enabled":NaN}',
                     '{"enabled":true,"enabled":false}', '{"monthly_limit_cents":Infinity}', b"{\xff}"):
            with self.subTest(body=body):
                status, data, _ = self.call("POST", "/api/policy", body, raw=True)
                self.assertEqual(status, 400)
                self.assertFalse(data["ok"])
        self.assertTrue(self.wallet.state()["policy"]["enabled"])

    def test_empty_oversized_and_chunked_bodies_are_rejected(self):
        self.assertEqual(self.call("POST", "/api/policy", "", raw=True)[0], 413)
        self.assertEqual(self.call("POST", "/api/policy", " " * 16385, raw=True)[0], 413)
        self.assertEqual(self.call("POST", "/api/policy", headers={"Transfer-Encoding": "chunked"})[0], 400)

    def test_path_traversal_and_unknown_actions_never_serve_files(self):
        for path in ("/../engine.py", "/%2e%2e/engine.py", "/static/../../engine.py",
                     "/..%5cengine.py", "/engine.py", "/api/orders/../execute"):
            with self.subTest(path=path):
                self.assertEqual(self.call("GET", path)[0], 404)
        self.assertEqual(self.call("POST", "/api/orders/" + "a" * 20 + "/delete")[0], 404)

    def test_extra_purchase_and_action_fields_rejected(self):
        for changes in ({"amount_cents": 1}, {"agent_id": "owner"}, {"approved_by": "owner"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.create(**changes)[0], 400)
        status, result, _ = self.create()
        self.assertEqual(status, 200)
        base = "/api/orders/" + result["order"]["id"]
        for action in ("approve", "reject", "execute", "reconcile", "refund"):
            with self.subTest(action=action):
                self.assertEqual(self.call("POST", base + "/" + action, {"amount_cents": 1})[0], 400)
        for action in ("pause", "resume"):
            self.assertEqual(self.call("POST", "/api/wallet/" + action, {"extra": 1})[0], 400)
        self.assertEqual(self.call("POST", "/api/policy", {"revision": 99})[0], 400)
        self.assertEqual(self.wallet.state()["orders"][0]["status"], "approved")

    def test_invalid_amounts_rejected_over_http(self):
        for value in (True, -1, 1.5, "30000"):
            with self.subTest(value=value):
                self.assertEqual(self.call("POST", "/api/policy", {"monthly_limit_cents": value})[0], 400)

    def test_request_key_conflict_is_http_409(self):
        status, first, _ = self.create()
        self.assertEqual(status, 200)
        self.assertEqual(self.create()[1]["order"], first["order"])
        status, conflict, _ = self.create(product_id="tool-8")
        self.assertEqual(status, 409)
        self.assertEqual(conflict["code"], "idempotency_conflict")

    def test_pause_resume_does_not_revalidate_previous_order(self):
        order = self.create()[1]["order"]
        self.assertEqual(self.call("POST", "/api/wallet/pause")[0], 200)
        self.assertEqual(self.call("POST", "/api/wallet/resume")[0], 200)
        self.assertEqual(self.call("POST", "/api/orders/" + order["id"] + "/execute")[0], 409)

    def test_task_http_lifecycle_and_stale_update(self):
        payload = {'title': '买家的视频项目', 'agent_id': 'creator-agent', 'budget_cents': 5000,
                   'idempotency_key': 'task-http-1'}
        status, result, _ = self.call('POST', '/api/tasks', payload)
        self.assertEqual(status, 200)
        task = result['task']
        self.assertEqual(self.call('POST', '/api/tasks', payload)[1]['task']['id'], task['id'])
        endpoint = '/api/tasks/' + task['id']
        self.assertEqual(self.call('POST', endpoint, {'status': 'paused', 'expected_revision': 1})[0], 200)
        self.assertEqual(self.call('POST', endpoint, {'status': 'active', 'expected_revision': 1})[0], 409)
        self.assertEqual(self.call('POST', endpoint, {'status': 'active', 'expected_revision': 2})[0], 200)

    def test_support_http_and_complete_export_without_access_secrets(self):
        order = self.create()[1]['order']
        status, result, _ = self.call('POST', '/api/support/tickets', {
            'order_id': order['id'], 'category': 'payment', 'description': '需要核对订单结果',
            'idempotency_key': 'support-http-1'})
        self.assertEqual(status, 200)
        ticket = result['ticket']
        status, listed, _ = self.call('GET', '/api/support/tickets')
        self.assertEqual(listed['tickets'][0]['id'], ticket['id'])
        self.assertFalse(listed['external_delivery'])
        status, _, _ = self.call('POST', '/api/support/tickets/' + ticket['id'], {
            'status': 'resolved', 'resolution': '已核对原订单，并记录处理结果', 'expected_revision': 1})
        self.assertEqual(status, 200)
        status, exported, _ = self.call('GET', '/api/export')
        self.assertEqual(status, 200)
        self.assertTrue(exported['simulated'])
        self.assertEqual(exported['tickets'][0]['status'], 'resolved')
        self.assertEqual(exported['orders'][0]['id'], order['id'])
        self.assertIn('audit', exported)
        self.assertNotIn(self.token, json.dumps(exported))
        self.assertNotIn('_key', exported['tickets'][0])

    def test_policy_commit_cannot_bypass_preview_with_null(self):
        payload = {'changes': {'monthly_limit_cents': 31000}, 'expected_revision': 1, 'expected_pending_ids': []}
        for changes in ({'expected_revision': None}, {'expected_revision': True}, {'expected_pending_ids': None}):
            self.assertEqual(self.call('POST', '/api/policy/commit', dict(payload, **changes))[0], 400)
        self.assertEqual(self.call('POST', '/api/policy/commit', payload)[0], 200)
        self.assertEqual(self.call('POST', '/api/policy/commit', payload)[0], 409)


if __name__ == "__main__":
    unittest.main()
