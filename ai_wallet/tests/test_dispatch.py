"""来源：本项目原创。持久提交的并发、进程退出与原单核对测试；无真实付款。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_wallet.allocation import AllocationWallet
from ai_wallet.agent_interface import TaskWallet
from ai_wallet.checkout_lab import CheckoutLab
from ai_wallet.dispatch import _OrderDispatch
from ai_wallet.engine import WalletError


class DispatchTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'ledger.sqlite'
        self.lab = CheckoutLab(self.path)
        checkout = self.lab.prepare('gallery', 1)
        self.wallet = self.lab.wallet
        self.task = self.lab.task_id
        self.order = self.wallet.request({'task_id': self.task, 'product_id': checkout,
            'reason': '独立测试内部提交边界', 'idempotency_key': 'dispatch-test'})['id']
        self.dispatch = _OrderDispatch(self.wallet, self.task)

    def saved(self):
        with self.wallet._read_connection() as db:
            return self.wallet._get(db, self.order)

    def record(self, dispatch_id, **changes):
        args = dict(status='paid', amount_cents=1200, currency='CNY', evidence_ref='DEMO-evidence', simulated=True)
        args.update(changes)
        return self.dispatch.record(self.order, dispatch_id, **args)

    def binding(self, **changes):
        value = dict(origin='https://gallery.example', account_ref='account-1',
                     site_order_id='site-order-1', session_ref='checkout-1', amount_cents=1200, currency='CNY')
        value.update(changes)
        return value

    def another_order(self):
        checkout = self.lab.prepare('gallery', 1)
        return self.wallet.request({'task_id': self.task, 'product_id': checkout,
            'reason': '独立测试另一钱包请求', 'idempotency_key': 'other-' + checkout})['id']

    def test_binding_is_immutable_and_reopen_recovers_without_writes(self):
        binding = self.binding()
        original = self.dispatch.bind(self.order, binding)
        self.assertEqual(self.dispatch.bind(self.order, binding), original)
        before = self.path.read_bytes()
        reopened = _OrderDispatch(AllocationWallet(self.path), self.task)
        recovered = reopened.recovery(self.order)
        self.assertEqual(recovered['checkout'], binding)
        self.assertEqual(recovered['next_action'], 'recheck_checkout')
        self.assertEqual(before, self.path.read_bytes())
        with self.assertRaises(WalletError):
            self.dispatch.bind(self.order, self.binding(session_ref='other-session'))
        self.assertEqual(self.saved(), original)

    def test_binding_normalizes_origins_and_rejects_sensitive_or_invalid_urls(self):
        normalized = self.dispatch.bind(self.order, self.binding(origin='https://GALLERY.example:443/'))
        self.assertEqual(normalized['checkout']['origin'], 'https://gallery.example')
        for origin in ('https://user:secret@gallery.example', 'https://gallery.example/pay',
                       'https://gallery.example?token=secret', 'https://gallery.example#key',
                       'http://gallery.example', 'https://gallery.example:invalid',
                       'https://gallery.example\n', None):
            with self.subTest(origin=origin), self.assertRaises(WalletError):
                self.dispatch.bind(self.order, self.binding(origin=origin))
        for changes in ({'account_ref': 'personal@example.com'}, {'session_ref': 'cookie=value'},
                        {'site_order_id': []}, {'amount_cents': True}, {'currency': 'USD'}, {'amount_cents': 1201}):
            with self.subTest(changes=changes), self.assertRaises(WalletError):
                self.dispatch.bind(self.order, self.binding(**changes))

    def test_new_request_cannot_bind_same_site_order_even_after_failed_payment(self):
        self.dispatch.bind(self.order, self.binding())
        token = self.dispatch.claim(self.order, checkout=self.binding())
        self.record(token, status='failed')
        other = self.another_order()
        with self.assertRaises(WalletError) as error:
            self.dispatch.bind(other, self.binding(origin='https://GALLERY.example:443/', session_ref='new-session'))
        self.assertEqual(error.exception.code, 'checkout_duplicate')
        self.assertIsNone(self.dispatch.recovery(other)['dispatch_id'])
        self.assertEqual(self.dispatch.recovery(other)['status'], 'rejected')
        self.assertIsNone(self.dispatch.claim(other))
        self.assertEqual(self.lab.planner.status(self.task)['available_cents'], 6000)

    def test_concurrent_site_order_bindings_only_one_wins(self):
        other = self.another_order()
        def bind(order):
            try:
                return _OrderDispatch(AllocationWallet(self.path), self.task).bind(order, self.binding())['id']
            except WalletError as exc:
                return exc.code
        with ThreadPoolExecutor(2) as workers:
            results = list(workers.map(bind, [self.order, other]))
        self.assertEqual(results.count('checkout_duplicate'), 1)

    def test_claim_requires_fresh_matching_binding_and_default_execute_cannot_bypass(self):
        self.dispatch.bind(self.order, self.binding())
        with self.assertRaises(WalletError):
            self.wallet.execute(self.order)
        with self.assertRaises(WalletError):
            self.dispatch.claim(self.order)
        for changes in ({'amount_cents': 1201}, {'origin': 'https://market.example'},
                        {'account_ref': 'different'}, {'site_order_id': 'other'}, {'session_ref': 'different'}):
            with self.subTest(changes=changes), self.assertRaises(WalletError):
                self.dispatch.claim(self.order, checkout=self.binding(**changes))
        self.assertEqual(self.saved()['status'], 'approved')
        self.assertIsNotNone(self.dispatch.claim(self.order, checkout=self.binding()))
        self.assertEqual(self.dispatch.recovery(self.order)['next_action'], 'verify_original')
        self.assertIsNone(self.dispatch.claim(self.order, checkout=self.binding()))

    def test_unrelated_account_same_number_is_not_mistaken_for_same_site_order(self):
        self.dispatch.bind(self.order, self.binding())
        other = self.another_order()
        self.assertEqual(self.dispatch.bind(other, self.binding(account_ref='account-2'))['id'], other)

    def test_binding_cannot_be_added_after_dispatch_and_recovery_is_task_scoped(self):
        self.dispatch.claim(self.order)
        with self.assertRaises(WalletError):
            self.dispatch.bind(self.order, self.binding())
        with self.assertRaises(WalletError):
            _OrderDispatch(self.wallet, 'different-task').recovery(self.order)

    def test_ai_status_waits_for_host_without_disclosing_checkout_account_references(self):
        self.dispatch.bind(self.order, self.binding())
        tool = TaskWallet(self.wallet, self.task)
        approved = tool.status(self.order)
        self.assertEqual(approved['next_action'], 'wait_for_host')
        self.assertNotIn('account-1', str(approved))
        self.assertNotIn('checkout', approved)
        self.dispatch.claim(self.order, checkout=self.binding())
        unknown = tool.status(self.order)
        self.assertEqual(unknown['next_action'], 'wait_for_host')
        self.assertEqual(unknown['status'], 'unknown')
        self.assertEqual(self.saved()['status'], 'unknown')

    def test_concurrent_independent_hosts_only_one_claim_survives_reopen(self):
        def claim(_):
            return _OrderDispatch(AllocationWallet(self.path), self.task).claim(self.order)
        with ThreadPoolExecutor(6) as workers:
            claims = list(workers.map(claim, range(12)))
        self.assertEqual(sum(value is not None for value in claims), 1)
        saved = self.saved()
        self.assertEqual(saved['status'], 'unknown')
        self.assertEqual(saved['dispatch']['id'], next(x for x in claims if x))
        self.assertIsNone(claim(None))
        self.assertEqual(self.lab.planner.status(self.task)['available_cents'], 4800)

    def test_child_process_exits_after_side_effect_and_restart_does_not_repeat(self):
        marker = self.path.parent / 'simulated-effect.txt'
        script = (
            'import os,sys\n'
            'from pathlib import Path\n'
            'from ai_wallet.allocation import AllocationWallet\n'
            'from ai_wallet.dispatch import _OrderDispatch\n'
            'd=_OrderDispatch(AllocationWallet(sys.argv[1]),sys.argv[2])\n'
            'if d.claim(sys.argv[3]) is not None:\n'
            '    with Path(sys.argv[4]).open("a") as f:\n'
            '        f.write("effect\\n"); f.flush(); os.fsync(f.fileno())\n'
            '    os._exit(23)\n'
        )
        args = [sys.executable, '-c', script, str(self.path), self.task, self.order, str(marker)]
        first = subprocess.run(args, capture_output=True, timeout=15)
        self.assertEqual(first.returncode, 23, first.stderr.decode(errors='replace'))
        second = subprocess.run(args, capture_output=True, timeout=15)
        self.assertEqual(second.returncode, 0, second.stderr.decode(errors='replace'))
        self.assertEqual(marker.read_text(), 'effect\n')
        self.assertEqual(self.saved()['status'], 'unknown')
        self.assertEqual(self.wallet.state()['allocation']['spent_cents'], 0)
        # An observation of the original simulated record resolves the same order.
        result = self.record(self.saved()['dispatch']['id'])
        self.assertEqual(result['status'], 'paid')
        self.assertIsNone(result['delivery'])

    def test_claim_without_side_effect_keeps_hold_after_restart_and_close(self):
        token = self.dispatch.claim(self.order)
        restored = AllocationWallet(self.path)
        self.assertIsNone(_OrderDispatch(restored, self.task).claim(self.order))
        task = self.lab.planner.status(self.task)
        restored.close_task(self.task, {'expected_revision': task['revision']})
        state = restored.state()['allocation']
        self.assertEqual((state['allocated_cents'], state['unallocated_cents']), (1200, 8800))
        self.record(token, status='failed')
        self.assertEqual(restored.state()['allocation']['unallocated_cents'], 10000)

    def test_write_failure_never_hands_execution_to_caller(self):
        with patch.object(self.wallet, '_audit', side_effect=OSError('write unavailable')):
            with self.assertRaises(OSError):
                self.dispatch.claim(self.order)
        self.assertEqual(self.saved()['status'], 'approved')
        self.assertNotIn('dispatch', self.saved())

    def test_cross_task_and_wrong_execution_evidence_cannot_change_original(self):
        with self.assertRaises(WalletError):
            _OrderDispatch(self.wallet, 'different-task').claim(self.order)
        token = self.dispatch.claim(self.order)
        before = self.saved()
        with self.assertRaises(WalletError):
            self.record('wrong-dispatch')
        with self.assertRaises(WalletError):
            _OrderDispatch(self.wallet, 'different-task').record(self.order, token, status='paid',
                amount_cents=1200, currency='CNY', evidence_ref='DEMO-evidence', simulated=True)
        self.assertEqual(self.saved(), before)

    def test_invalid_or_mismatched_evidence_retains_unknown_and_cannot_overwrite_result(self):
        token = self.dispatch.claim(self.order)
        for changes in ({'amount_cents': 1201}, {'amount_cents': True}, {'currency': 'USD'},
                        {'status': 'unknown'}, {'simulated': 'true'}, {'evidence_ref': ''}):
            with self.subTest(changes=changes), self.assertRaises(WalletError):
                self.record(token, **changes)
            self.assertEqual(self.saved()['status'], 'unknown')
        first = self.record(token)
        self.assertEqual(self.record(token), first)
        with self.assertRaises(WalletError):
            self.record(token, status='failed')
        self.assertEqual(self.saved(), first)

    def test_simulation_controls_cannot_resolve_or_refund_dispatch_orders(self):
        token = self.dispatch.claim(self.order)
        self.assertEqual(self.wallet.execute(self.order)['status'], 'unknown')
        with self.assertRaises(WalletError):
            self.wallet.reconcile(self.order)
        self.record(token)
        with self.assertRaises(WalletError):
            self.wallet.refund(self.order)
        self.assertEqual(self.saved()['status'], 'paid')

    def test_pause_before_claim_prevents_execution(self):
        self.wallet.update_policy({'enabled': False})
        self.assertIsNone(self.dispatch.claim(self.order))
        self.assertNotIn('dispatch', self.saved())

    def test_expired_order_prevents_claim(self):
        now = self.wallet._now()
        self.wallet.clock = lambda: now + timedelta(minutes=16)
        self.assertIsNone(self.dispatch.claim(self.order))
        self.assertNotIn('dispatch', self.saved())

    def test_late_observation_retains_current_exposure(self):
        token = self.dispatch.claim(self.order)
        now = self.wallet._now()
        self.wallet.clock = lambda: now + timedelta(days=32)
        self.assertEqual(self.wallet.state()['allocation']['allocated_cents'], 6000)
        result = self.record(token)
        self.assertTrue(result['budget_at'].startswith(self.wallet._now().date().isoformat()))
        self.assertEqual(self.wallet.state()['allocation']['spent_cents'], 1200)


if __name__ == '__main__':
    unittest.main()
