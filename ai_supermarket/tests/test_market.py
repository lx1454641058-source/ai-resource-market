"""来源：本项目原创。目录、持久化、幂等、撤回、权限与反馈回归测试。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from ai_supermarket.catalog import Catalog, DEFAULT_CATALOG
from ai_supermarket.common import MarketError, compact
from ai_supermarket.host import TaskResources
from ai_supermarket.service import Market
from ai_supermarket.tools import dispatch


@contextmanager
def connection(path):
    # sqlite3's transaction context commits but does not close the handle on Windows.
    with closing(sqlite3.connect(path)) as db:
        with db:
            yield db


class MarketTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.catalog_root = self.root / 'catalog'
        shutil.copytree(DEFAULT_CATALOG.parent, self.catalog_root)
        self.catalog = self.catalog_root / 'index.json'
        self.database = self.root / 'market.sqlite'
        self.now = [1000.0]
        self.market = Market(self.database, 'alice', self.catalog, clock=lambda: self.now[0])

    def edit(self, resource_id, **updates):
        manifest = json.loads(self.catalog.read_text(encoding='utf-8'))
        item = next(r for r in manifest['resources'] if r['id'] == resource_id)
        item.update(updates)
        self.catalog.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')

    def acquire(self, resource_id='product-brief', request_id='req-1'):
        return self.market.acquire(resource_id, '1.0.0', request_id)

    def read(self, request_id='read-1', task_id='task-1'):
        acquired = self.acquire()
        return self.market.read(acquired['acquisition_id'], task_id, request_id)

    def count(self, table):
        with connection(self.database) as db:
            return db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(MarketError) as cm:
            function(*args, **kwargs)
        self.assertEqual(cm.exception.code, code)

    def test_original_resources_are_readable_and_hash_verified(self):
        catalog = Catalog(self.catalog)
        self.assertEqual(len(catalog.items), 32)
        for item in catalog.items.values():
            content = catalog.read(item)
            self.assertIn('本项目原创', content)
            self.assertEqual(item['license'], 'Project-Local')
            self.assertEqual(item['price_cents'], 0)

    def test_search_chinese_english_empty_result_and_bounded_summary(self):
        first = self.market.search('整理一份产品简报', kind='template')
        self.assertEqual(first['items'][0]['id'], 'product-brief')
        self.assertEqual(self.market.search('product analysis', kind='skill')['items'][0]['id'], 'product-analysis')
        self.assertEqual(self.market.search('zzzxxyy不存在的火星任务')['status'], 'no_match')
        self.assertLessEqual(len(self.market.search('产品分析 需求 成本 证据 访谈')['items']), 3)
        self.assertNotIn('content', compact(first))
        self.assertNotIn('path', compact(first))
        self.assertLess(len(compact(first).encode()), 1400)

    def test_cache_expires_and_refreshes_owned_state(self):
        self.assertFalse(self.market.search('产品简报')['cache_hit'])
        self.assertTrue(self.market.search('产品简报')['cache_hit'])
        self.acquire()
        self.assertTrue(self.market.search('产品简报')['items'][0]['owned'])
        self.now[0] += 601
        self.assertFalse(self.market.search('产品简报')['cache_hit'])

    def test_withdrawal_invalidates_cached_search_and_existing_delivery(self):
        self.market.search('产品简报')
        acquired = self.acquire()
        self.edit('product-brief', state='suspended')
        self.assertEqual(self.market.search('产品简报')['items'], [])
        self.assertEqual(self.market.status(acquired['acquisition_id'])['status'], 'unavailable')
        self.assert_code('not_found', self.market.read, acquired['acquisition_id'], 'task', 'read')

    def test_tampered_file_never_delivered_or_recorded(self):
        file = self.catalog_root / 'content' / 'product-brief.md'
        file.write_text('tampered', encoding='utf-8')
        self.assert_code('integrity_failed', self.acquire)
        self.assertEqual(self.count('acquisitions'), 0)

    def test_rehashed_same_version_cannot_replace_existing_entitlement(self):
        acquired = self.acquire()
        file = self.catalog_root / 'content' / 'product-brief.md'
        file.write_text('changed content', encoding='utf-8')
        self.edit('product-brief', sha256=hashlib.sha256(file.read_bytes()).hexdigest())
        self.assert_code('version_changed', self.market.read, acquired['acquisition_id'], 'task', 'read')
        self.assert_code('idempotency_conflict', self.acquire)

    def test_path_traversal_catalog_is_rejected(self):
        self.edit('product-brief', path='../outside.md')
        self.assert_code('invalid_path', Catalog, self.catalog)

    def test_unapproved_license_permissions_and_source_are_rejected(self):
        for key, value, code in [('source', 'unknown', 'unapproved_source'),
                                 ('license', 'GPL-3.0', 'unapproved_source'),
                                 ('permissions', ['shell'], 'unapproved_permissions')]:
            old = json.loads(self.catalog.read_text(encoding='utf-8'))
            self.edit('product-brief', **{key: value})
            self.assert_code(code, Catalog, self.catalog)
            self.catalog.write_text(json.dumps(old, ensure_ascii=False), encoding='utf-8')

    def test_duplicate_id_and_boolean_price_rejected(self):
        old = json.loads(self.catalog.read_text(encoding='utf-8'))
        old['resources'].append(old['resources'][0])
        self.catalog.write_text(json.dumps(old), encoding='utf-8')
        self.assert_code('invalid_catalog', Catalog, self.catalog)
        old['resources'].pop()
        self.catalog.write_text(json.dumps(old), encoding='utf-8')
        self.edit('product-brief', price_cents=True)
        self.assert_code('invalid_params', Catalog, self.catalog)

    def test_acquisition_is_persisted_and_replay_is_identical(self):
        first = self.acquire()
        other = Market(self.database, 'alice', self.catalog)
        self.assertEqual(first, other.acquire('product-brief', '1.0.0', 'req-1'))
        self.assertEqual(first, other.status(first['acquisition_id']))
        self.assertEqual(self.count('acquisitions'), 1)

    def test_same_id_changed_resource_is_rejected(self):
        self.acquire()
        self.assert_code('idempotency_conflict', self.acquire, 'prd-outline')
        self.assertEqual(self.count('acquisitions'), 1)

    def test_concurrent_acquire_creates_one_record(self):
        def acquire(_):
            return Market(self.database, 'alice', self.catalog).acquire('product-brief', '1.0.0', 'shared')
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(acquire, range(12)))
        self.assertEqual(len({x['acquisition_id'] for x in results}), 1)
        self.assertEqual(self.count('acquisitions'), 1)

    def test_concurrent_initialization_is_atomic(self):
        db = self.root / 'fresh.sqlite'
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: Market(db, 'agent', self.catalog), range(8)))
        with connection(db) as handle:
            self.assertEqual(handle.execute('SELECT value FROM meta').fetchone()[0], '2')

    def test_other_principal_cannot_read_status_or_feedback(self):
        acquired = self.acquire()
        loaded = self.read()
        bob = Market(self.database, 'bob', self.catalog)
        self.assert_code('not_found', bob.status, acquired['acquisition_id'])
        self.assert_code('not_found', bob.read, acquired['acquisition_id'], 'task', 'read')
        self.assert_code('not_found', bob.feedback, loaded['usage_id'])
        self.assertFalse(bob.search('产品简报')['items'][0]['owned'])

    def test_paid_resource_fails_closed_without_order_or_payment(self):
        self.edit('product-brief', price_cents=100)
        self.assertEqual(self.market.resolve('product-brief', '1.0.0')['next_action'], 'payment_unavailable')
        self.assertEqual(self.acquire()['status'], 'payment_unavailable')
        self.assertEqual(self.count('acquisitions'), 0)

    def test_owned_version_remains_usable_if_price_changes(self):
        self.acquire()
        self.edit('product-brief', price_cents=100)
        result = self.acquire(request_id='other-request')
        self.assertEqual(result['status'], 'acquired')
        self.assertEqual(result['payment'], 'not_required')

    def test_read_replay_uses_same_usage_id_and_cannot_change_resource(self):
        first = self.read()
        self.assertEqual(first, self.read())
        self.assertEqual(self.count('usage_events'), 1)
        second = self.acquire('prd-outline', 'second')
        self.assert_code('idempotency_conflict', self.market.read, second['acquisition_id'], 'task-1', 'read-1')

    def test_feedback_requires_read_deduplicates_and_does_not_claim_verification(self):
        acquired = self.acquire()
        self.assert_code('not_found', self.market.feedback, acquired['acquisition_id'])
        loaded = self.read()
        self.assertEqual(self.market.feedback(loaded['usage_id'], 'yes', 'pass')['status'], 'accepted')
        self.assertEqual(self.market.feedback(loaded['usage_id'], 'yes', 'pass')['status'], 'duplicate')
        self.assertEqual(self.count('feedback'), 1)
        quality = self.market.resolve('product-brief', '1.0.0')['quality']
        self.assertFalse(quality['independently_verified'])
        self.assertEqual(quality['observations'][0]['evidence'], 'agent_reported')
        self.assert_code('feedback_conflict', self.market.feedback, loaded['usage_id'], 'no')

    def test_unknown_feedback_is_not_counted_as_pass_and_conflict_rejected(self):
        loaded = self.read()
        self.market.feedback(loaded['usage_id'])
        quality = self.market.resolve('product-brief', '1.0.0')['quality']
        self.assertEqual(quality['observations'][0]['check_result'], 'not_run')
        other = self.read('read-2')
        self.assert_code('invalid_params', self.market.feedback, other['usage_id'], 'yes', 'pass', 'bad_output')

    def test_host_checks_are_not_exposed_or_forgeable_via_model_tools(self):
        loaded = self.read()
        result = dispatch(self.market, 'record_check', {'usage_id': loaded['usage_id']})
        self.assertEqual(result['error_code'], 'unknown_tool')
        result = dispatch(self.market, 'market_feedback', {'usage_id': loaded['usage_id'], 'evidence': 'host_check'})
        self.assertEqual(result['error_code'], 'invalid_params')
        self.market.record_check(loaded['usage_id'], 'template-shape-v1', True)
        quality = self.market.resolve('product-brief', '1.0.0')['quality']
        self.assertEqual(quality['observations'][0]['evidence'], 'host_check')

    def test_host_entry_checks_once_and_raw_task_is_not_stored(self):
        host = TaskResources(self.market, 'brief-task', '产品简报 confidential-unique-detail', 'template')
        self.assertEqual(host.candidates['items'][0]['id'], 'product-brief')
        loaded = host.use('product-brief', '1.0.0', 'brief')
        self.assertEqual(loaded, host.use('product-brief', '1.0.0', 'brief'))
        with connection(self.database) as db:
            self.assertEqual(db.execute('SELECT checks FROM task_checks').fetchone()[0], 1)
            self.assertNotIn('confidential-unique-detail', '\n'.join(db.iterdump()))

    def test_bad_input_cannot_expand_scope_or_change_identity(self):
        cases = [({'q': ''}, 'invalid_params'), ({'q': 'cost', 'limit': True}, 'invalid_params'),
                 ({'q': 'cost', 'limit': 999}, 'invalid_params'), ({'q': 'cost', 'principal': 'bob'}, 'invalid_params'),
                 ({'q': 'cost', 'purpose': 'commercial'}, 'invalid_params'), ({'q': 'x'*513}, 'invalid_params')]
        for args, code in cases:
            self.assertEqual(dispatch(self.market, 'market_search', args)['error_code'], code)

    def test_existing_foreign_database_is_not_migrated(self):
        path = self.root / 'wallet.sqlite'
        with connection(path) as db:
            db.execute('CREATE TABLE wallet (value TEXT)')
            db.execute("INSERT INTO wallet VALUES ('keep')")
        self.assert_code('wrong_database', Market, path)
        with connection(path) as db:
            self.assertEqual(db.execute('SELECT value FROM wallet').fetchone()[0], 'keep')
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [('wallet',)])


if __name__ == '__main__':
    unittest.main()
