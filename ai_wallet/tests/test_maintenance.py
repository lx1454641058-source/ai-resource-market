"""来源：本项目原创。只在临时目录验证备份与恢复，不操作用户账本。"""
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from ai_wallet.engine import Wallet
from ai_wallet.maintenance import copy_ledger
from ai_wallet.support import SupportDesk


class MaintenanceTests(unittest.TestCase):
    def test_roundtrip_retains_unknown_hold_tasks_issues_and_idempotence(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            wallet = Wallet(root / 'source.sqlite')
            task = wallet.create_task({'title': '备份项目', 'agent_id': 'creative-ai', 'budget_cents': 5000,
                                       'idempotency_key': 'task-1'})
            payload = {'task_id': task['id'], 'product_id': 'image-12', 'reason': '恢复验收', 'idempotency_key': 'order-1'}
            order = wallet.request(payload)
            wallet.execute(order['id'], 'timeout')
            SupportDesk(wallet).create({'order_id': order['id'], 'category': 'payment',
                                        'description': '未知付款需要核验', 'idempotency_key': 'ticket-1'})
            original = wallet.state()
            copy_ledger(root / 'source.sqlite', root / 'backup.sqlite')
            copy_ledger(root / 'backup.sqlite', root / 'restored.sqlite')
            restored = Wallet(root / 'restored.sqlite')
            self.assertEqual(restored.state(), original)
            self.assertEqual(restored.request(payload)['id'], order['id'])
            self.assertEqual(restored.state()['summary']['month_reserved_cents'], 1200)
            self.assertEqual(len(SupportDesk(restored).list()), 1)
            restored.reconcile(order['id'])
            self.assertEqual(wallet.state()['orders'][0]['status'], 'unknown')

    def test_existing_target_and_invalid_source_never_overwrite(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            wallet = Wallet(root / 'source.sqlite')
            before = (root / 'source.sqlite').read_bytes()
            with self.assertRaises(ValueError):
                copy_ledger(wallet.database, wallet.database)
            (root / 'occupied').write_bytes(b'existing-data')
            with self.assertRaises(ValueError):
                copy_ledger(wallet.database, root / 'occupied')
            self.assertEqual((root / 'occupied').read_bytes(), b'existing-data')
            with self.assertRaises(ValueError):
                copy_ledger(root / 'missing', root / 'new.sqlite')
            self.assertFalse((root / 'missing').exists())
            (root / 'broken').write_bytes(b'not-sqlite')
            with self.assertRaises(sqlite3.Error):
                copy_ledger(root / 'broken', root / 'new.sqlite')
            self.assertFalse((root / 'new.sqlite').exists())
            self.assertEqual((root / 'source.sqlite').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
