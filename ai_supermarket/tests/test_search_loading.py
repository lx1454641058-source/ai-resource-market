"""本项目原创。用SQLite授权器证明搜索不读取正文，而非仅检查响应里没有正文。"""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ai_supermarket.common import MarketError
from ai_supermarket.exchange import Exchange, PublishedCatalog
from ai_supermarket.host import TaskResources
from ai_supermarket.tests.test_exchange import listing, CONTENT, CHECKS


class SearchLoadingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.database = Path(folder.name) / 'market.sqlite'
        self.seller = Exchange(self.database, 'vendor', 'seller')
        self.reviewer = Exchange(self.database, 'reviewer', 'reviewer')
        self.buyer = Exchange(self.database, 'buyer')
        self.seller.draft('brief', '1.0.0', listing(), CONTENT, 'draft')
        self.seller.submit('vendor:brief', '1.0.0', 1, 'submit')
        self.reviewer.review('vendor:brief', '1.0.0', 1, 'approve', CHECKS, '原创样例工程自检', 'review')

    @contextmanager
    def forbid_bodies(self):
        original = self.buyer.store.connect
        reads = []
        @contextmanager
        def guarded(write=False):
            with original(write=write) as db:
                def authorize(action, table, column, database, trigger):
                    if action == sqlite3.SQLITE_READ:
                        reads.append((table, column))
                        if (table == 'submissions' and column == 'content') or table == 'asset_objects':
                            return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                db.set_authorizer(authorize)
                try:
                    yield db
                finally:
                    db.set_authorizer(None)
        with patch.object(self.buyer.store, 'connect', guarded):
            yield reads

    def test_task_entry_cold_cache_and_warm_cache_do_not_select_bodies(self):
        with self.forbid_bodies() as reads:
            host = TaskResources(self.buyer, 'task', '产品简报', purpose='personal_use')
            self.assertEqual('vendor:brief', host.candidates['items'][0]['id'])
            warm = self.buyer.search('产品简报', purpose='personal_use')
            self.assertTrue(warm['cache_hit'])
            self.assertEqual('no_match', self.buyer.search('没有这种资源')['status'])
        self.assertIn(('submissions', 'metadata'), reads)
        self.assertNotIn(('submissions', 'content'), reads)
        self.assertTrue(all('content' not in item for _, items in self.buyer.cache.values() for item in items))

    def test_owned_price_and_withdrawal_still_refresh(self):
        self.buyer.search('产品简报')
        owned = self.buyer.acquire('vendor:brief', '1.0.0', 'use')
        self.seller.reprice('vendor:brief', '1.0.0', 1, 500, 'price')
        with self.forbid_bodies():
            result = self.buyer.search('产品简报')
            self.assertFalse(result['cache_hit'])
            self.assertTrue(result['items'][0]['owned'])
            self.assertEqual(500, result['items'][0]['price_cents'])
        self.seller.availability('vendor:brief', '1.0.0', 2, 'withdrawn', '停止新获取', 'withdraw')
        with self.forbid_bodies():
            self.assertEqual('no_match', self.buyer.search('产品简报')['status'])
        self.assertEqual(CONTENT, self.buyer.read(owned['acquisition_id'], 'task', 'read')['content'])

    def test_search_snapshot_cannot_be_used_as_a_body(self):
        with self.buyer.store.connect() as db:
            catalog = PublishedCatalog(db, include_content=False)
        item = catalog.get('vendor:brief', '1.0.0')
        self.assertNotIn('content', item)
        with self.assertRaises(MarketError) as error:
            catalog.read(item)
        self.assertEqual('invalid_state', error.exception.code)

    def test_tampered_content_is_still_rejected_at_acquisition(self):
        with self.buyer.store.connect(write=True) as db:
            db.execute("UPDATE submissions SET content='tampered' WHERE resource_id='vendor:brief'")
        with self.forbid_bodies():
            self.assertEqual('ok', self.buyer.search('产品简报')['status'])
        with self.assertRaises(MarketError) as error:
            self.buyer.acquire('vendor:brief', '1.0.0', 'acquire')
        self.assertEqual('integrity_failed', error.exception.code)


if __name__ == '__main__':
    unittest.main()
