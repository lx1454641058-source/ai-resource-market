"""本项目原创。纯读运营入口；全部使用合成付款/退款证据，无外部资金操作。"""
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

from ai_supermarket.common import MarketError
from ai_supermarket.payment_orders import _get, _put
from ai_supermarket.tests import test_payment_refunds as fixture
from tools import market_operations as operations


class MarketOperationsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.PaymentRefundTests.setUpClass()

    def setUp(self):
        self.f = fixture.PaymentRefundTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.host = self.f.f
        self.database = self.host.database

    def call(self, command, **kwargs):
        args = {'buyer': 'alice', 'seller': 'vendor', 'order_id': self.host.order_id, **kwargs}
        return operations.inspect(command, self.database, **args)

    def snapshot(self):
        return {str(path.relative_to(self.database.parent)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.database.parent.rglob('*') if path.is_file()}

    def test_three_reads_match_existing_views_without_mutation_or_network(self):
        self.f.observe()
        self.f.apply()
        expected = self.f.refunds.status(self.host.order_id)
        history = self.f.client.history(self.host.order_id, 'refund-1', 30)
        before = self.snapshot()
        with patch('socket.socket', side_effect=AssertionError('no network')):
            order = self.call('order-inspect')
            self.assertTrue(order['result']['paid_recorded'])
            self.assertEqual(expected, self.call('accounting-status')['result'])
            self.assertEqual(history, self.call('refund-history', refund_id='refund-1')['result'])
        self.assertEqual(before, self.snapshot())

    def test_wrong_buyer_seller_or_order_rejected(self):
        for kwargs in ({'buyer': 'bob'}, {'seller': 'other'}, {'order_id': 'missing'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(MarketError) as caught:
                self.call('order-inspect', **kwargs)
            self.assertEqual('order_not_found', caught.exception.code)

    def test_missing_database_does_not_create_any_path(self):
        absent = self.database.parent / 'must-not-exist' / 'market.sqlite'
        output = io.StringIO()
        with redirect_stdout(output):
            code = operations.main(['order-inspect', '--database', str(absent), '--buyer', 'alice',
                                    '--seller', 'vendor', '--order-id', self.host.order_id])
        self.assertEqual(2, code)
        self.assertEqual('database_not_found', json.loads(output.getvalue())['error_code'])
        self.assertFalse(absent.parent.exists())

    def test_missing_refund_is_local_absence_not_channel_confirmation(self):
        before = self.snapshot()
        result = self.call('refund-history', refund_id='missing')['result']
        self.assertEqual('not_queried', result['status'])
        self.assertIsNone(result['refund_cents'])
        self.assertFalse(result['refund_confirmed'])
        self.assertEqual(before, self.snapshot())

    def test_corrupt_refund_source_and_missing_ledger_fail_closed(self):
        self.f.observe()
        self.f.apply()
        key = self.f.client._key(self.host.order_id, 'refund-1')
        with self.host.buyer.store.connect(write=True) as db:
            query = _get(db, key)
            query['binding']['refund_cents'] = 99
            _put(db, key, query)
        with self.assertRaises(MarketError):
            self.call('refund-history', refund_id='refund-1')
        with self.assertRaises(MarketError):
            self.call('accounting-status')
        with self.host.buyer.store.connect(write=True) as db:
            db.execute('DELETE FROM meta WHERE key=?', ('payment_refunds:' + self.host.order_id,))
        with self.assertRaises(MarketError):
            self.call('accounting-status')

    def test_sqlite_connection_denies_mutation(self):
        before = self.snapshot()
        with operations.readonly_database(self.database) as db:
            for query in ('CREATE TABLE forbidden(x)', 'DELETE FROM meta', "ATTACH ':memory:' AS extra"):
                with self.subTest(query=query), self.assertRaises(sqlite3.DatabaseError):
                    db.execute(query)
        self.assertEqual(before, self.snapshot())

    def test_subprocess_json_from_other_directory_and_secret_safe_errors(self):
        script = Path(operations.__file__).resolve()
        args = [sys.executable, '-B', str(script), 'order-inspect', '--database', str(self.database),
                '--buyer', 'alice', '--seller', 'vendor', '--order-id', self.host.order_id]
        before = self.snapshot()
        proc = subprocess.run(args, cwd=self.database.parent, capture_output=True, encoding='utf-8', check=False)
        self.assertEqual(0, proc.returncode, proc.stderr)
        output = json.loads(proc.stdout)
        self.assertEqual('', proc.stderr)
        self.assertEqual({'order_id', 'order_status', 'buyer', 'seller', 'environment', 'currency',
                          'price_cents', 'paid_recorded', 'entitlement_recorded', 'attempt_count'},
                         set(output['result']))
        for private in ('prepared', 'confirmation', 'trade_no', 'Payment-Proof', 'PRIVATE KEY', 'content'):
            self.assertNotIn(private, proc.stdout)
        bad = subprocess.run(args + ['--unknown', 'SYNTHETIC-SECRET-DO-NOT-ECHO'],
                             cwd=self.database.parent, capture_output=True, encoding='utf-8', check=False)
        self.assertEqual(2, bad.returncode)
        self.assertNotIn('SYNTHETIC-SECRET', bad.stdout + bad.stderr)
        self.assertEqual('invalid_arguments', json.loads(bad.stdout)['error_code'])
        self.assertEqual(before, self.snapshot())

    def test_unpaid_order_has_no_accounting_or_refund_success(self):
        # Existing helper creates another approved resource and unpaid order.
        from ai_supermarket.tests.test_payment_bills import PaymentBillTests
        unpaid = PaymentBillTests.new_order(self.host, 'unpaid')
        self.assertFalse(self.call('order-inspect', order_id=unpaid)['result']['paid_recorded'])
        for command, args in (('accounting-status', {}), ('refund-history', {'refund_id': 'none'})):
            with self.subTest(command=command), self.assertRaises(MarketError) as caught:
                self.call(command, order_id=unpaid, **args)
            self.assertEqual('payment_not_confirmed', caught.exception.code)

    def test_wal_database_refused_without_creating_sidecars(self):
        path = self.database.parent / 'wal.sqlite'
        db = sqlite3.connect(path)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE meta(key TEXT, value TEXT)')
        db.commit()
        db.close()
        before = self.snapshot()
        with self.assertRaises(MarketError) as caught:
            with operations.readonly_database(path):
                self.fail('must not open WAL')
        self.assertEqual('read_only_wal_unsupported', caught.exception.code)
        self.assertEqual(before, self.snapshot())


if __name__ == '__main__':
    unittest.main()
