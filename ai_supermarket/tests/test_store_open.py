"""来源：本项目原创。已有账本只读打开、首次初始化竞争与拒绝错误账本的回归。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from ai_supermarket.budget import BudgetStore
from ai_supermarket.common import MarketError
from ai_supermarket.enrollment import EnrollmentStore
from ai_supermarket.exchange_store import ExchangeStore
from ai_supermarket.gateway import AuthenticatedGateway
from ai_supermarket.identity import Identities, IdentityStore
from ai_supermarket.measurement import MeasurementStore
from ai_supermarket.store import LEGACY_SCHEMA, Store


STORES = (Store, ExchangeStore, IdentityStore, BudgetStore, MeasurementStore, EnrollmentStore)


class StoreOpenTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def test_existing_ledgers_open_while_other_writer_holds_reserved_lock(self):
        original_connect = sqlite3.connect
        def short_wait(*args, **kwargs):
            kwargs['timeout'] = .1
            return original_connect(*args, **kwargs)
        for kind in STORES:
            with self.subTest(kind=kind.__name__):
                path = self.root / (kind.__name__ + '.sqlite')
                kind(path)
                with closing(original_connect(path)) as writer:
                    writer.execute('BEGIN IMMEDIATE')
                    writer.execute("INSERT INTO meta VALUES('uncommitted-test','private')")
                    try:
                        with patch('ai_supermarket.store.sqlite3.connect', side_effect=short_wait):
                            opened = kind(path)
                            with opened.connect() as reader:
                                self.assertIsNone(reader.execute("SELECT value FROM meta WHERE key='uncommitted-test'").fetchone())
                    finally:
                        writer.rollback()

    def test_authenticated_search_can_read_while_market_writer_is_active(self):
        market, identities = self.root / 'market.sqlite', self.root / 'identities.sqlite'
        ExchangeStore(market)
        accounts = Identities(identities)
        accounts.create_account('reader', 'buyer')
        token = accounts.issue_key('reader')['token']
        gateway = AuthenticatedGateway(market, identities)
        original_connect = sqlite3.connect
        def short_wait(*args, **kwargs):
            kwargs['timeout'] = .1
            return original_connect(*args, **kwargs)
        with closing(original_connect(market)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            try:
                with patch('ai_supermarket.store.sqlite3.connect', side_effect=short_wait):
                    result = gateway.call(token, 'market_search', {'q': 'absent', 'purpose': 'personal_use'})
                self.assertEqual('no_match', result['status'])
            finally:
                writer.rollback()

    def test_concurrent_first_open_initializes_each_kind_once(self):
        for kind in STORES:
            with self.subTest(kind=kind.__name__):
                path = self.root / (kind.__name__ + '.sqlite')
                barrier = threading.Barrier(4)
                def open_one(_):
                    barrier.wait(timeout=10)
                    return kind(path)
                with ThreadPoolExecutor(max_workers=4) as pool:
                    list(pool.map(open_one, range(4)))
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(kind.tables, {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")})
                    self.assertEqual([('ok',)], db.execute('PRAGMA quick_check').fetchall())

    def test_wrong_and_legacy_ledgers_remain_byte_identical(self):
        cases = [('foreign', 'CREATE TABLE unrelated(value TEXT);', 'wrong_database'),
                 ('legacy', LEGACY_SCHEMA, 'upgrade_required'),
                 ('future', Store.schema.replace("('schema_version', '2')", "('schema_version', '999')"), 'wrong_database')]
        for name, schema, expected in cases:
            with self.subTest(name=name):
                path = self.root / (name + '.sqlite')
                with closing(sqlite3.connect(path)) as db:
                    db.executescript(schema)
                before = hashlib.sha256(path.read_bytes()).hexdigest()
                with self.assertRaises(MarketError) as caught:
                    Store(path)
                self.assertEqual(expected, caught.exception.code)
                self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_first_open_rechecks_if_another_creator_populates_empty_file(self):
        class CompetingStore(Store):
            @contextmanager
            def connect(inner, write=False):
                with super(CompetingStore, inner).connect(write=write) as db:
                    yield db
                if not write:
                    with closing(sqlite3.connect(inner.path)) as other:
                        other.execute('CREATE TABLE unrelated(value TEXT)')
                        other.commit()
        path = self.root / 'race.sqlite'
        with self.assertRaises(MarketError) as caught:
            CompetingStore(path)
        self.assertEqual('wrong_database', caught.exception.code)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual([('unrelated',)], db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())

    def test_existing_recovery_and_snapshot_gates_are_checked_on_open(self):
        path = self.root / 'market.sqlite'
        ExchangeStore(path)
        with closing(sqlite3.connect(path)) as db:
            db.execute("INSERT INTO meta VALUES('recovery_state','pending')")
            db.commit()
        with self.assertRaises(MarketError) as caught:
            ExchangeStore(path)
        self.assertEqual('recovery_required', caught.exception.code)
        (self.root / '.ai-supermarket-snapshot.json').write_text('{}', encoding='utf-8')
        with self.assertRaises(MarketError) as caught:
            Store(path)
        self.assertEqual('snapshot_read_only', caught.exception.code)


if __name__ == '__main__':
    unittest.main()
