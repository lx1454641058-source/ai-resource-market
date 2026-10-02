"""来源：本项目原创。临时账本中的问题接续、防重复和版本冲突验证。"""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ai_wallet.engine import Wallet, WalletError
from ai_wallet.support import SupportDesk


class SupportTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.wallet = Wallet(Path(self.temp.name) / 'test.sqlite')
        self.desk = SupportDesk(self.wallet)
        self.order = self.wallet.request({'task_id': 'campaign', 'product_id': 'image-12',
                                         'reason': '测试用途', 'idempotency_key': 'test-order'})
        self.body = {'order_id': self.order['id'], 'category': 'delivery',
                     'description': '购买的素材尚未交付', 'idempotency_key': 'issue-1'}

    def test_idempotence_persistence_and_private_fields_hidden(self):
        first = self.desk.create(self.body)
        self.assertEqual(self.desk.create(self.body), first)
        self.assertFalse(any(key.startswith('_') for key in first))
        restarted = SupportDesk(Wallet(self.wallet.database))
        self.assertEqual(restarted.list(), [first])
        self.assertEqual(first['order_snapshot']['status'], 'approved')
        self.assertEqual(len(self.wallet.state()['orders']), 1)

    def test_reused_key_for_other_issue_fails(self):
        self.desk.create(self.body)
        with self.assertRaises(WalletError) as exc:
            self.desk.create(dict(self.body, description='另一件事'))
        self.assertEqual(exc.exception.status, 409)

    def test_progress_resolution_and_stale_edit(self):
        ticket = self.desk.create(self.body)
        updated = self.desk.update(ticket['id'], {'status': 'in_progress', 'resolution': '已核对原订单，等待供应商确认', 'expected_revision': 1})
        self.assertEqual(updated['revision'], 2)
        with self.assertRaises(WalletError):
            self.desk.update(ticket['id'], {'status': 'resolved', 'resolution': '旧页面操作', 'expected_revision': 1})
        updated = self.desk.update(ticket['id'], {'status': 'resolved', 'resolution': '已核实并记录最终处理结果', 'expected_revision': 2})
        self.assertEqual(updated['status'], 'resolved')
        with self.assertRaises(WalletError):
            self.desk.update(ticket['id'], {'status': 'resolved', 'resolution': '', 'expected_revision': 3})

    def test_invalid_order_category_and_revision_fail_without_changes(self):
        for body in (dict(self.body, order_id='missing'), dict(self.body, category='unsupported'), dict(self.body, description='')):
            with self.assertRaises(WalletError):
                self.desk.create(body)
        self.assertEqual(self.desk.list(), [])
        ticket = self.desk.create(self.body)
        with self.assertRaises(WalletError):
            self.desk.update(ticket['id'], {'status': 'resolved', 'resolution': '说明', 'expected_revision': True})
        self.assertEqual(self.desk.list()[0]['revision'], 1)


if __name__ == '__main__':
    unittest.main()
