"""本项目原创。实际宿主取用的条件绑定、并发快照及旧任务边界。"""

import hashlib
import unittest
from unittest.mock import patch

from ai_supermarket.common import compact
from ai_supermarket.exchange import Exchange, PublishedCatalog
from ai_supermarket.host import TaskResources
from ai_supermarket.tests.test_exchange import CONTENT, listing
from ai_supermarket.tests import test_release_binding as release_fixtures
from ai_supermarket.tests import test_budget as budget_fixtures


class HostReleaseBindingTests(unittest.TestCase):
    setUp = release_fixtures.ReleaseBindingTests.setUp
    error = release_fixtures.ReleaseBindingTests.error
    count = release_fixtures.ReleaseBindingTests.count
    change_metadata = release_fixtures.ReleaseBindingTests.change_metadata

    def host(self):
        return TaskResources(self.buyer, 'host-task', '产品简报', purpose='personal_use')

    def use(self, host=None):
        return (host or self.host()).use('vendor:brief', '1.0.0', 'use')

    def test_new_host_use_is_bound_and_restart_replays_without_duplicate_records(self):
        first = self.use()
        self.assertEqual(CONTENT, first['content'])
        with self.buyer.store.connect() as db:
            acquisition = db.execute('SELECT id FROM acquisitions').fetchone()[0]
        status = self.buyer.status(acquisition)
        self.assertEqual('expected_release_v1', status['release_binding'])
        self.assertEqual(self.digest, status['release_digest'])
        self.assertEqual(first, self.use())
        self.assertEqual(1, self.count('acquisitions'))
        self.assertEqual(1, self.count('usage_events'))

    def test_changed_license_between_preflight_and_acquire_stops_before_write(self):
        original = self.buyer.acquire
        def changed(*args, **kwargs):
            meta = listing()
            meta['license_text'] += '\nChanged license conditions.'
            self.change_metadata(meta)
            return original(*args, **kwargs)
        with patch.object(self.buyer, 'acquire', changed):
            self.error('release_changed', self.use)
        self.assertEqual(0, self.count('acquisitions'))
        self.assertEqual(0, self.count('usage_events'))

    def test_old_unbound_logical_request_is_not_rewritten_or_renumbered(self):
        key = hashlib.sha256(compact(['host-task', 'use']).encode()).hexdigest()
        old = self.buyer.acquire(**self.ref, request_id=key)
        self.error('idempotency_conflict', self.use)
        self.assertEqual(1, self.count('acquisitions'))
        self.assertEqual(0, self.count('usage_events'))
        self.assertEqual('read', self.buyer.read(old['acquisition_id'], 'manual', 'read')['status'])

    def test_owned_withdrawn_stays_readable_but_suspended_does_not(self):
        self.buyer.acquire(**self.ref, request_id='old-rights')
        self.seller.availability('vendor:brief', '1.0.0', 1, 'withdrawn', '停售演练', 'withdraw')
        self.assertEqual(CONTENT, self.use()['content'])
        with self.buyer.store.connect() as db:
            bound = db.execute("SELECT id FROM acquisitions WHERE id LIKE 'ap_%'").fetchone()[0]
        self.assertEqual('expected_release_v1', self.buyer.status(bound)['release_binding'])
        stranger = Exchange(self.db, 'stranger')
        self.error('not_found', stranger.check_compatibility, **self.ref)
        self.reviewer.availability('vendor:brief', '1.0.0', 2, 'suspended', '隔离演练', 'suspend')
        self.error('not_found', self.use)

    def test_reprice_after_preflight_can_use_existing_rights(self):
        self.buyer.acquire(**self.ref, request_id='old-rights')
        original = self.buyer.acquire
        def repriced(*args, **kwargs):
            self.seller.reprice('vendor:brief', '1.0.0', 1, 500, 'price')
            return original(*args, **kwargs)
        with patch.object(self.buyer, 'acquire', repriced):
            self.assertEqual(CONTENT, self.use()['content'])
        self.assertEqual(2, self.count('acquisitions'))

    def test_changed_conditions_after_success_fail_on_restart(self):
        self.use()
        meta = listing()
        meta['source_statement'] += ' Changed.'
        self.change_metadata(meta)
        self.error('release_changed', self.use)
        self.assertEqual(1, self.count('acquisitions'))
        self.assertEqual(1, self.count('usage_events'))

    def test_missing_binding_audit_cannot_downgrade_host_replay(self):
        self.use()
        with self.buyer.store.connect(write=True) as db:
            # Corruption only in an isolated fixture; production has no such operation.
            db.execute("DELETE FROM exchange_audit WHERE action='acquire'")
        self.error('integrity_failed', self.use)
        self.assertEqual(1, self.count('acquisitions'))

    def check_snapshot(self, operation):
        # WAL lets another actual connection commit between the two reads.
        with self.buyer.store.connect() as db:
            self.assertEqual('wal', db.execute('PRAGMA journal_mode=WAL').fetchone()[0])
        real_get = PublishedCatalog.get
        changed = False
        meta = listing()
        meta['license_text'] += '\nConcurrent change.'
        def get(catalog, *args, **kwargs):
            nonlocal changed
            item = real_get(catalog, *args, **kwargs)
            if not changed:
                changed = True
                self.change_metadata(meta)
            return item
        with patch.object(PublishedCatalog, 'get', get):
            result = operation()
        self.assertTrue(changed)
        self.assertNotEqual(self.digest, self.buyer.resolve(**self.ref)['release_digest'])
        return result

    def test_resolve_license_and_digest_are_from_the_same_read_snapshot(self):
        details = self.check_snapshot(lambda: self.buyer.resolve(**self.ref))
        self.assertEqual(listing()['license_text'], details['license_text'])
        self.assertEqual(self.digest, details['release_digest'])
        self.error('release_changed', self.buyer.acquire, **self.ref, request_id='pinned',
                   expected_release_digest=details['release_digest'])

    def test_host_preflight_digest_is_from_the_same_read_snapshot(self):
        compatibility, digest = self.check_snapshot(lambda: self.buyer._host_preflight(**self.ref))
        self.assertEqual('ready', compatibility['status'])
        self.assertEqual(self.digest, digest)
        self.error('release_changed', self.buyer.acquire, **self.ref, request_id='pinned',
                   expected_release_digest=digest)


class HostBindingBudgetTests(unittest.TestCase):
    setUp = budget_fixtures.BudgetTests.setUp
    error = budget_fixtures.BudgetTests.error
    publish = budget_fixtures.BudgetHostTests.publish

    def test_dependency_cost_change_cannot_use_an_old_smaller_reservation(self):
        market, _ = self.publish()
        original = market.acquire
        def changed(*args, **kwargs):
            meta = listing()
            meta['usage_contract']['dependencies'] = [
                {'id': 'tool:1', 'license_name': 'MIT', 'max_cost_cents': 90}]
            with market.store.connect(write=True) as db:
                db.execute('UPDATE submissions SET metadata=?', (compact(meta),))
            return original(*args, **kwargs)
        host = TaskResources(market, 'main', '产品简报', budget=self.alice)
        with patch.object(market, 'acquire', changed):
            self.error('release_changed', host.use, 'vendor:brief', '1.0.0', 'use')
        self.assertEqual(0, self.alice.summary()['held_cents'])
        with market.store.connect() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM acquisitions').fetchone()[0])
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM usage_events').fetchone()[0])

    def test_change_after_acquisition_blocks_read_and_retains_reservation_for_recovery(self):
        market, _ = self.publish()
        original = market.read
        def changed(*args, **kwargs):
            meta = listing()
            meta['usage_contract']['dependencies'] = [
                {'id': 'tool:1', 'license_name': 'MIT', 'max_cost_cents': 80}]
            meta['license_text'] += ' Changed.'
            with market.store.connect(write=True) as db:
                db.execute('UPDATE submissions SET metadata=?', (compact(meta),))
            return original(*args, **kwargs)
        host = TaskResources(market, 'main', '产品简报', budget=self.alice)
        with patch.object(market, 'read', changed):
            self.error('release_changed', host.use, 'vendor:brief', '1.0.0', 'use')
        self.assertEqual(80, self.alice.summary()['held_cents'])
        self.assertEqual(0, self.alice.summary()['spent_cents'])
        with market.store.connect() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM usage_events').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
