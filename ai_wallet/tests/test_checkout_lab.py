"""来源：本项目原创。隔离网页结账、支付权限、预算与结果分离的验收。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlencode

from ai_wallet.checkout_lab import CheckoutLab, LabServer
from ai_wallet.engine import WalletError


class CheckoutLabTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'isolated.sqlite'
        self.lab = CheckoutLab(self.path)

    def authorize(self, shop='gallery', quantity=1):
        checkout = self.lab.prepare(shop, quantity)
        result = self.lab.authorize_checkout(shop, checkout)
        return checkout, result['payment_ref']

    def test_browsing_checkout_does_not_allocate_payment_and_two_stores_share_task_budget(self):
        first = self.lab.prepare('gallery', 2)
        self.assertEqual(self.lab.view('gallery', first)['status'], 'awaiting_payment')
        self.assertEqual(self.lab.wallet.state()['orders'], [])
        token = self.lab.authorize_checkout('gallery', first)['payment_ref']
        result = self.lab.pay('gallery', first, token)
        self.assertEqual(result['amount_cents'], 2400)
        second, other_token = self.authorize('market', 1)
        result = self.lab.pay('market', second, other_token)
        self.assertEqual(result['status'], 'paid')
        self.assertTrue(result['delivery']['simulated'])
        self.assertEqual(result['receipt']['amount_cents'], 1900)
        state = self.lab.wallet.state()['allocation']
        self.assertEqual((state['unallocated_cents'], state['allocated_cents'], state['spent_cents']), (4000, 1700, 4300))

    def test_repeated_authorization_and_concurrent_checkout_submit_only_charge_once(self):
        checkout, token = self.authorize()
        self.assertEqual(token, self.lab.authorize_checkout('gallery', checkout)['payment_ref'])
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: self.lab.pay('gallery', checkout, token), range(8)))
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(self.lab.charge_count, 1)
        self.assertEqual(self.lab.wallet.state()['allocation']['spent_cents'], 1200)

    def test_no_payment_permission_before_wallet_authorizes_and_cross_checkout_grant_fails(self):
        checkout = self.lab.prepare('gallery', 1)
        with self.assertRaises(WalletError):
            self.lab.pay('gallery', checkout, 'DEMO-forged')
        second, token = self.authorize()
        for malformed in ('测试凭据', True, None, ''):
            with self.subTest(malformed=malformed), self.assertRaises(WalletError):
                self.lab.pay('gallery', second, malformed)
        with self.assertRaises(WalletError):
            self.lab.pay('gallery', checkout, token)
        with self.assertRaises(WalletError):
            self.lab.pay('market', second, token)
        self.assertEqual(self.lab.charge_count, 0)

    def test_excess_budget_and_paused_task_never_reach_channel(self):
        large, token = self.authorize(quantity=6)
        self.assertIsNone(token)
        self.assertEqual(self.lab.view('gallery', large)['status'], 'rejected')
        checkout, token = self.authorize()
        task = self.lab.planner.status(self.lab.task_id)
        self.lab.wallet.update_task(self.lab.task_id, {'status': 'paused', 'expected_revision': task['revision']})
        result = self.lab.pay('gallery', checkout, token)
        self.assertEqual(result['status'], 'rejected')
        self.assertIn('任务已暂停', result['message'])
        self.assertEqual(self.lab.charge_count, 0)

    def test_price_change_and_expiry_release_unused_payment_hold(self):
        checkout, token = self.authorize()
        self.lab.prices['gallery'] += 1
        result = self.lab.pay('gallery', checkout, token)
        self.assertEqual(result['status'], 'rejected')
        self.assertIn('金额已变化', result['message'])
        second, second_token = self.authorize('market')
        now = self.lab.wallet._now()
        self.lab.wallet.clock = lambda: now + timedelta(minutes=3)
        self.assertEqual(self.lab.pay('market', second, second_token)['status'], 'rejected')
        self.assertEqual(self.lab.charge_count, 0)
        task = self.lab.planner.status(self.lab.task_id)
        self.assertEqual(task['available_cents'], 6000)

    def test_unknown_payment_survives_close_and_lookup_never_charges_again(self):
        checkout, token = self.authorize()
        self.assertEqual(self.lab.pay('gallery', checkout, token, scenario='timeout')['status'], 'unknown')
        self.assertEqual(self.lab.pay('gallery', checkout, token)['status'], 'unknown')
        self.assertEqual(self.lab.charge_count, 1)
        task = self.lab.planner.status(self.lab.task_id)
        self.lab.wallet.close_task(self.lab.task_id, {'expected_revision': task['revision']})
        allocation = self.lab.wallet.state()['allocation']
        self.assertEqual((allocation['unallocated_cents'], allocation['allocated_cents']), (8800, 1200))
        reconciled = self.lab.reconcile('gallery', checkout)
        self.assertEqual(reconciled['status'], 'paid')
        self.assertIsNone(reconciled['delivery'])
        self.lab.deliver(checkout)
        self.assertTrue(self.lab.view('gallery', checkout)['delivery']['simulated'])
        self.assertEqual(self.lab.charge_count, 1)
        self.assertEqual(self.lab.wallet.state()['allocation']['spent_cents'], 1200)

    def test_decline_and_delivery_failure_have_different_money_effects(self):
        checkout, token = self.authorize()
        result = self.lab.pay('gallery', checkout, token, scenario='decline')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.lab.wallet.state()['allocation']['spent_cents'], 0)
        with self.assertRaises(WalletError):
            self.lab.deliver(checkout)
        second, second_token = self.authorize('market')
        result = self.lab.pay('market', second, second_token, scenario='delivery_failure')
        self.assertEqual(result['status'], 'delivery_failed')
        self.assertIsNone(result['delivery'])
        self.assertIsNotNone(result['receipt'])
        self.assertEqual(self.lab.wallet.state()['allocation']['spent_cents'], 1900)

    def test_experiment_refuses_existing_file_without_overwriting(self):
        original = self.path.read_bytes()
        with self.assertRaises(ValueError):
            CheckoutLab(self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_browser_host_must_persist_claim_before_store_can_charge(self):
        self.lab.require_host_claim = True
        checkout, token = self.authorize()
        with self.assertRaises(WalletError) as error:
            self.lab.pay('gallery', checkout, token)
        self.assertEqual(error.exception.code, 'host_claim_required')
        self.assertEqual(self.lab.charge_count, 0)
        self.assertTrue(self.lab.claim_for_browser('gallery', checkout, origin='https://gallery.example', amount_cents=1200))
        self.assertEqual(self.lab.view('gallery', checkout)['status'], 'unknown')
        self.assertEqual(self.lab.charge_count, 0)
        self.assertFalse(self.lab.claim_for_browser('gallery', checkout, origin='https://gallery.example', amount_cents=1200))
        self.assertEqual(self.lab.pay('gallery', checkout, token)['status'], 'paid')
        self.assertEqual(self.lab.pay('gallery', checkout, token)['status'], 'paid')
        self.assertEqual(self.lab.charge_count, 1)

    def test_browser_host_mismatch_pause_or_expiry_cannot_claim(self):
        self.lab.require_host_claim = True
        checkout, _ = self.authorize()
        for origin, amount in [('https://other.example', 1200), ('https://gallery.example', 1201),
                               ('https://gallery.example', True)]:
            self.assertFalse(self.lab.claim_for_browser('gallery', checkout, origin=origin, amount_cents=amount))
        now = self.lab.wallet._now()
        self.lab.wallet.clock = lambda: now + timedelta(minutes=3)
        self.assertFalse(self.lab.claim_for_browser('gallery', checkout, origin='https://gallery.example', amount_cents=1200))
        self.lab.wallet.clock = lambda: now
        self.lab.wallet.update_policy({'enabled': False})
        self.assertFalse(self.lab.claim_for_browser('gallery', checkout, origin='https://gallery.example', amount_cents=1200))
        self.assertEqual(self.lab.charge_count, 0)

    def test_browser_host_timeout_consumes_claim_and_keeps_original_for_lookup(self):
        self.lab.require_host_claim = True
        checkout, token = self.authorize()
        self.assertTrue(self.lab.claim_for_browser('gallery', checkout, origin='https://gallery.example', amount_cents=1200))
        self.assertEqual(self.lab.pay('gallery', checkout, token, scenario='timeout')['status'], 'unknown')
        self.assertEqual(self.lab.pay('gallery', checkout, token)['status'], 'unknown')
        self.assertFalse(self.lab.claim_for_browser('gallery', checkout, origin='https://gallery.example', amount_cents=1200))
        self.assertEqual(self.lab.reconcile('gallery', checkout)['status'], 'paid')
        self.assertEqual(self.lab.charge_count, 1)

    def test_browser_host_final_price_change_records_no_charge_after_claim(self):
        self.lab.require_host_claim = True
        checkout, token = self.authorize()
        self.lab.claim_for_browser('gallery', checkout, origin='https://gallery.example', amount_cents=1200)
        self.lab.prices['gallery'] += 1
        self.assertEqual(self.lab.pay('gallery', checkout, token)['status'], 'failed')
        self.assertEqual(self.lab.charge_count, 0)
        self.assertEqual(self.lab.planner.status(self.lab.task_id)['available_cents'], 6000)

    def test_browser_host_lost_in_memory_permission_never_reconstructs_execution(self):
        self.lab.require_host_claim = True
        checkout, token = self.authorize()
        self.lab.claim_for_browser('gallery', checkout, origin='https://gallery.example', amount_cents=1200)
        del self.lab.quotes[checkout]['host_dispatch_id']
        self.assertFalse(self.lab.claim_for_browser('gallery', checkout, origin='https://gallery.example', amount_cents=1200))
        self.assertEqual(self.lab.pay('gallery', checkout, token)['status'], 'unknown')
        self.assertEqual(self.lab.charge_count, 0)

    def test_browser_fixture_subprocess_only_prepares_and_closes_without_browser(self):
        commands = [{'action': 'prepare', 'shop': 'market', 'quantity': 1}, {'action': 'state'}, {'action': 'close'}]
        result = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'ai_wallet.tests.browser_fixture'],
            input=''.join(json.dumps(command) + '\n' for command in commands),
            capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        prepared, state = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertTrue(prepared['url'].startswith('http://127.0.0.1:'))
        self.assertEqual(prepared['amount_cents'], 1900)
        self.assertEqual(state, {'spent_cents': 0, 'charges': 0, 'statuses': ['approved']})

    def test_two_http_origins_normal_forms_and_untrusted_price_fields(self):
        servers = [LabServer(self.lab, shop) for shop in ('gallery', 'market')]
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            def request(server, method, path, data=None, origin=None):
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                try:
                    body = urlencode(data) if data is not None else None
                    headers = {'Origin': origin or server.origin, 'Content-Type': 'application/x-www-form-urlencoded'}
                    conn.request(method, path, body=body, headers=headers)
                    response = conn.getresponse()
                    return response.status, response.getheader('Location'), response.read().decode('utf-8')
                finally:
                    conn.close()
            self.assertNotEqual(servers[0].origin, servers[1].origin)
            for server in servers:
                self.assertEqual(request(server, 'GET', '/')[0], 200)
                status, location, _ = request(server, 'POST', '/prepare', {'csrf': server.csrf, 'quantity': 1})
                self.assertEqual(status, 303)
                checkout = location.rsplit('/', 1)[1]
                self.assertIn('awaiting_payment', request(server, 'GET', location)[2])
                ref = self.lab.authorize_checkout(server.shop, checkout)['payment_ref']
                status, _, _ = request(server, 'POST', '/pay/' + checkout,
                                       {'csrf': server.csrf, 'payment_ref': ref, 'amount_cents': 1})
                self.assertEqual(status, 400)
                status, _, _ = request(server, 'POST', '/pay/' + checkout,
                                       {'csrf': server.csrf, 'payment_ref': ref}, origin='https://outside.example')
                self.assertEqual(status, 403)
                status, _, _ = request(server, 'POST', '/pay/' + checkout, {'csrf': server.csrf, 'payment_ref': ref})
                self.assertEqual(status, 303)
                page = request(server, 'GET', location)[2]
                self.assertIn('结账状态：paid', page)
                self.assertNotIn(ref, page)  # 已提交的凭据不回显到回执页面。
            self.assertEqual(self.lab.charge_count, 2)
            self.assertEqual(self.lab.wallet.state()['allocation']['spent_cents'], 3100)
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
