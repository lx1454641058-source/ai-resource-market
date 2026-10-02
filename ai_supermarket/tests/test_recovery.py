"""来源：本项目原创。恢复、联合快照、隔离与就绪验收；仅使用临时软件数据。"""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_supermarket.budget import BudgetLedger
from ai_supermarket.common import MarketError, compact
from ai_supermarket.exchange import Exchange
from ai_supermarket.gateway import AuthenticatedGateway
from ai_supermarket.identity import Identities
from ai_supermarket import recovery
from ai_supermarket.tests.test_exchange import listing, CONTENT, CHECKS


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.live = self.root / 'live'
        self.live.mkdir()
        self.paths = recovery.paths_for(self.live)
        self.identities = Identities(self.paths['identities'])
        for name, role in [('alice', 'buyer'), ('bob', 'buyer'), ('vendor', 'seller'), ('operator', 'reviewer')]:
            self.identities.create_account(name, role)
        self.alice_key = self.identities.issue_key('alice')
        self.bob_key = self.identities.issue_key('bob')
        self.identities.disable_account('bob')
        seller = Exchange(self.paths['market'], 'vendor', 'seller')
        draft = seller.draft('brief', '1.0.0', listing(), CONTENT, 'draft')
        self.resource = draft['id']
        seller.submit(self.resource, '1.0.0', 1, 'submit')
        Exchange(self.paths['market'], 'operator', 'reviewer').review(
            self.resource, '1.0.0', 1, 'approve', CHECKS, '原创软件恢复测试', 'review')
        self.buyer = Exchange(self.paths['market'], 'alice', 'buyer')
        self.acquisition = self.buyer.acquire(self.resource, '1.0.0', 'get')['acquisition_id']
        self.usage = self.buyer.read(self.acquisition, 'task', 'read')['usage_id']
        self.buyer.feedback(self.usage, 'yes', 'pass')
        self.ledger = BudgetLedger(self.paths['budget'])
        self.ledger.create('root', 'owner', 100, 'software-demo-only')
        self.ledger.grant_task('root', 'task', 'alice')
        self.ledger.grant_task('root', 'child', 'bob', parent_id='task')
        scope = self.ledger.scope('root', 'task', 'alice')
        self.unknown = scope.reserve('unknown', 'model', 'demo-provider', 60)['reservation_id']
        scope.begin(self.unknown)
        scope.mark_unknown(self.unknown)
        paid = scope.reserve('paid', 'dependency', 'demo-check', 30)['reservation_id']
        scope.begin(paid)
        scope.settle(paid, 20, 'demo-receipt')
        self.ledger.close_task('root', 'child')
        self.snapshot = self.root / 'snapshot'
        self.restored = self.root / 'restored'
        self.evidence = {key: 'demo-only:' + key for key in recovery.REVIEW_FIELDS}

    def backup(self):
        return recovery.snapshot_bundle(*self.paths.values(), self.snapshot)

    def assert_code(self, code, callback, *args):
        with self.assertRaises(MarketError) as caught:
            callback(*args)
        self.assertEqual(code, caught.exception.code)

    def rows(self, path, table):
        with closing(recovery.connect_existing(path)) as db:
            return [tuple(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]

    def test_full_restore_preserves_rights_feedback_unknown_and_revocation(self):
        before = {kind: recovery.digest_file(path) for kind, path in self.paths.items()}
        self.assertEqual('snapshotted', self.backup()['status'])
        self.assertEqual('verified', recovery.verify_bundle(self.snapshot)['status'])
        self.assertEqual(before, {k: recovery.digest_file(p) for k, p in self.paths.items()})
        self.assertEqual('restored_quarantined', recovery.restore_bundle(self.snapshot, self.restored)['status'])
        restored = recovery.paths_for(self.restored)
        self.assertEqual({'status': 'not_ready'}, recovery.readiness(*restored.values()))
        self.assert_code('recovery_required', Exchange, restored['market'], 'alice', 'buyer')
        for table in ('acquisitions', 'usage_events', 'feedback', 'submissions', 'offers', 'quotes'):
            self.assertEqual(self.rows(self.paths['market'], table), self.rows(restored['market'], table))
        identities = Identities(restored['identities'])
        self.assert_code('unauthenticated', identities.authenticate, self.alice_key['token'])
        self.assert_code('unauthenticated', identities.authenticate, self.bob_key['token'])
        self.assert_code('account_unavailable', identities.issue_key, 'bob')
        ledger = BudgetLedger(restored['budget'])
        scope = ledger.scope('root', 'task', 'alice')
        summary = scope.summary()
        self.assertEqual(('paused', 20, 60, 0),
                         (summary['state'], summary['spent_cents'], summary['held_cents'], summary['available_cents']))
        self.assert_code('budget_inactive', scope.reserve, 'new', 'model', 'demo', 1)
        self.assertEqual(self.rows(self.paths['budget'], 'reservations'), self.rows(restored['budget'], 'reservations'))
        self.assertEqual(self.rows(self.paths['budget'], 'budget_tasks'), self.rows(restored['budget'], 'budget_tasks'))
        self.assertEqual('released', recovery.release_bundle(self.restored, self.evidence)['status'])
        self.assertEqual({'status': 'ready'}, recovery.readiness(*restored.values()))
        new_key = identities.issue_key('alice')
        gateway = AuthenticatedGateway(restored['market'], restored['identities'])
        result = gateway.call(new_key['token'], 'market_acquire',
                              {'id': self.resource, 'version': '1.0.0', 'request_id': 'get'})
        self.assertEqual(self.acquisition, result['acquisition_id'])
        result = gateway.call(new_key['token'], 'market_read',
                              {'acquisition_id': self.acquisition, 'task_id': 'task', 'request_id': 'read'})
        self.assertEqual(self.usage, result['usage_id'])
        self.assertEqual(CONTENT, result['content'])
        self.assertEqual('paused', scope.summary()['state'])
        self.assert_code('unauthenticated', identities.authenticate, self.alice_key['token'])

    def test_wal_commits_are_in_snapshot(self):
        with ExitStackForWAL(self.paths) as connections:
            self.ledger.close_task('root', 'task')
            self.backup()
            self.assertEqual('verified', recovery.verify_bundle(self.snapshot)['status'])
            copied = recovery.paths_for(self.snapshot)
            self.assertEqual(self.rows(self.paths['budget'], 'budget_tasks'), self.rows(copied['budget'], 'budget_tasks'))

    def test_all_writer_locks_held_before_first_copy_and_released_after(self):
        original = recovery.copy_db
        calls = []
        def copying(source, destination, deadline):
            for path in self.paths.values():
                with closing(sqlite3.connect(path, timeout=0.01)) as db:
                    with self.assertRaises(sqlite3.OperationalError):
                        db.execute('BEGIN IMMEDIATE')
            calls.append(str(source))
            return original(source, destination, deadline)
        with patch.object(recovery, 'copy_db', side_effect=copying):
            self.backup()
        self.assertEqual(3, len(calls))
        self.ledger.close_task('root', 'task')

    def test_busy_source_aborts_without_manifest_and_releases_other_locks(self):
        with closing(sqlite3.connect(self.paths['market'])) as db:
            db.execute('BEGIN IMMEDIATE')
            with self.assertRaises(sqlite3.OperationalError):
                self.backup()
            self.assertFalse((self.snapshot / 'manifest.json').exists())
        self.ledger.close_task('root', 'task')

    def test_missing_wrong_duplicate_and_existing_destinations_not_changed(self):
        self.assert_code('not_found', recovery.snapshot_bundle, self.root / 'missing',
                         self.paths['identities'], self.paths['budget'], self.snapshot)
        self.assertFalse((self.root / 'missing').exists())
        self.assert_code('invalid_params', recovery.snapshot_bundle, self.paths['market'],
                         self.paths['market'], self.paths['budget'], self.snapshot)
        self.assert_code('wrong_database', recovery.snapshot_bundle, self.paths['identities'],
                         self.paths['market'], self.paths['budget'], self.snapshot)
        self.snapshot.mkdir()
        sentinel = self.snapshot / 'keep'
        sentinel.write_text('keep', encoding='utf-8')
        self.assert_code('destination_exists', self.backup)
        self.assertEqual('keep', sentinel.read_text(encoding='utf-8'))

    def test_tampered_database_manifest_path_and_extra_journal_refused(self):
        self.backup()
        path = self.snapshot / 'manifest.json'
        original = path.read_text(encoding='utf-8')
        manifest = json.loads(original)
        manifest['files']['market']['file'] = '../live/market.sqlite'
        path.write_text(compact(manifest), encoding='utf-8')
        self.assert_code('invalid_manifest', recovery.verify_bundle, self.snapshot)
        path.write_text(original, encoding='utf-8')
        journal = self.snapshot / 'market.sqlite-wal'
        journal.write_bytes(b'not covered by hash')
        self.assert_code('invalid_manifest', recovery.verify_bundle, self.snapshot)

    def test_database_tampering_cannot_restore(self):
        self.backup()
        # Mutating a sealed snapshot is caught even if it is still valid SQLite.
        with closing(sqlite3.connect(self.snapshot / 'budget.sqlite')) as db, db:
            db.execute("UPDATE budgets SET limit_cents=1000")
        self.assert_code('integrity_failed', recovery.restore_bundle, self.snapshot, self.restored)
        self.assertFalse(self.restored.exists())

    def test_sealed_snapshot_cannot_be_used_directly_as_service_or_budget(self):
        self.backup()
        paths = recovery.paths_for(self.snapshot)
        self.assert_code('snapshot_read_only', Exchange, paths['market'], 'alice', 'buyer')
        self.assert_code('snapshot_read_only', Identities, paths['identities'])
        self.assert_code('snapshot_read_only', BudgetLedger, paths['budget'])
        self.assertEqual({'status': 'not_ready'}, recovery.readiness(*paths.values()))
        self.assertEqual('verified', recovery.verify_bundle(self.snapshot)['status'])

    def test_source_content_corruption_and_custom_trigger_rejected(self):
        with closing(sqlite3.connect(self.paths['market'])) as db, db:
            db.execute("UPDATE submissions SET content='changed'")
        self.assert_code('integrity_failed', self.backup)
        self.assertFalse(self.snapshot.exists())
        with closing(sqlite3.connect(self.paths['market'])) as db, db:
            db.execute('UPDATE submissions SET content=?', (CONTENT,))
            db.execute('CREATE TRIGGER extra AFTER INSERT ON task_checks BEGIN SELECT 1; END')
        self.assert_code('wrong_database', self.backup)

    def test_copy_failure_never_writes_completed_manifest(self):
        with patch.object(recovery, 'copy_db', side_effect=OSError('demo failure')):
            with self.assertRaises(OSError):
                self.backup()
        self.assertFalse((self.snapshot / 'manifest.json').exists())
        self.ledger.close_task('root', 'task')

    def test_partial_restore_and_missing_review_cannot_release(self):
        self.backup()
        with patch.object(recovery, 'write_new', side_effect=OSError('demo failure')):
            with self.assertRaises(OSError):
                recovery.restore_bundle(self.snapshot, self.restored)
        self.assert_code('incomplete_restore', recovery.release_bundle, self.restored, self.evidence)
        self.assert_code('invalid_params', recovery.release_bundle, self.restored, {})
        self.assert_code('destination_exists', recovery.restore_bundle, self.snapshot, self.restored)

    def test_preexisting_object_cannot_bypass_gate(self):
        with closing(sqlite3.connect(self.paths['market'])) as db, db:
            db.execute("INSERT INTO meta VALUES ('recovery_state','pending')")
        self.assert_code('recovery_required', self.buyer.search, '产品简报')
        gateway = AuthenticatedGateway(self.paths['market'], self.paths['identities'])
        self.assertEqual('recovery_required', gateway.call(self.alice_key['token'], 'market_search',
                                                         {'q': '产品简报'})['error_code'])

    def test_readiness_is_minimal_non_initializing_and_budget_optional(self):
        before = {k: recovery.digest_file(p) for k, p in self.paths.items()}
        self.assertEqual({'status': 'ready'}, recovery.readiness(*self.paths.values()))
        self.assertEqual(before, {k: recovery.digest_file(p) for k, p in self.paths.items()})
        missing = self.root / 'absent' / 'ledger.sqlite'
        self.assertEqual({'status': 'not_ready'}, recovery.readiness(missing, self.paths['identities']))
        self.assertFalse(missing.parent.exists())
        self.assertEqual({'status': 'not_ready'}, recovery.readiness(self.paths['market'], self.paths['identities'], missing))
        self.assertEqual({'status': 'ready'}, recovery.readiness(self.paths['market'], self.paths['identities']))

    def test_mixed_restore_batch_and_duplicate_release_are_refused(self):
        self.backup()
        recovery.restore_bundle(self.snapshot, self.restored)
        paths = recovery.paths_for(self.restored)
        with closing(sqlite3.connect(paths['budget'])) as db, db:
            db.execute("UPDATE meta SET value='restore_other' WHERE key='recovery_id'")
        self.assert_code('recovery_mismatch', recovery.release_bundle, self.restored, self.evidence)
        self.assert_code('recovery_mismatch', recovery.snapshot_bundle, *paths.values(), self.root / 'mixed')
        self.assertEqual({'status': 'not_ready'}, recovery.readiness(*paths.values()))
        with closing(recovery.connect_existing(paths['market'])) as db:
            batch = db.execute("SELECT value FROM meta WHERE key='recovery_id'").fetchone()[0]
        with closing(sqlite3.connect(paths['budget'])) as db, db:
            db.execute("UPDATE meta SET value=? WHERE key='recovery_id'", (batch,))
        recovery.release_bundle(self.restored, self.evidence)
        self.assert_code('invalid_state', recovery.release_bundle, self.restored, self.evidence)

    def test_mid_restore_failure_keeps_market_unavailable(self):
        self.backup()
        original = recovery.validate_database
        calls = [0]
        def checking(db, kind, **kwargs):
            calls[0] += 1
            # verify_bundle validates three files, then restore gates market before identities.
            if calls[0] == 5:
                raise OSError('demo identity stage failure')
            return original(db, kind, **kwargs)
        with patch.object(recovery, 'validate_database', side_effect=checking):
            with self.assertRaises(OSError):
                recovery.restore_bundle(self.snapshot, self.restored)
        paths = recovery.paths_for(self.restored)
        self.assertFalse((self.restored / 'restore-complete.json').exists())
        self.assert_code('recovery_required', Exchange, paths['market'], 'alice', 'buyer')
        self.assertEqual({'status': 'not_ready'}, recovery.readiness(*paths.values()))
        self.assert_code('incomplete_restore', recovery.release_bundle, self.restored, self.evidence)

    def test_cli_snapshot_verify_restore_release_and_generic_failure(self):
        def invoke(*args):
            result = subprocess.run([sys.executable, '-m', 'ai_supermarket.recovery', *map(str, args)],
                                    capture_output=True, timeout=15)
            return result.returncode, json.loads(result.stdout)
        code, result = invoke('snapshot', '--market', self.paths['market'], '--identities', self.paths['identities'],
                              '--budget', self.paths['budget'], '--output', self.snapshot)
        self.assertEqual((0, 'snapshotted'), (code, result['status']))
        self.assertEqual((0, 'verified'), (lambda r: (r[0], r[1]['status']))(invoke('verify', '--bundle', self.snapshot)))
        code, result = invoke('restore', '--bundle', self.snapshot, '--output', self.restored)
        self.assertEqual((0, 'restored_quarantined'), (code, result['status']))
        proof = self.root / 'evidence.json'
        proof.write_text(compact(self.evidence), encoding='utf-8')
        code, result = invoke('release', '--bundle', self.restored, '--evidence', proof)
        self.assertEqual((0, 'released'), (code, result['status']))
        code, result = invoke('verify', '--bundle', self.root / 'private-missing-path')
        self.assertEqual(1, code)
        self.assertNotIn('private-missing-path', compact(result))


class ExitStackForWAL:
    """Keep WAL readers open so snapshots must include committed WAL frames."""
    def __init__(self, paths):
        self.paths, self.connections = paths, []

    def __enter__(self):
        for path in self.paths.values():
            db = sqlite3.connect(path)
            db.execute('PRAGMA journal_mode=WAL')
            self.connections.append(db)
        return self.connections

    def __exit__(self, *args):
        for db in self.connections:
            db.close()


if __name__ == '__main__':
    unittest.main()
