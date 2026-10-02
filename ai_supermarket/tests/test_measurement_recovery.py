"""来源：本项目原创。四库恢复、计量复核及遗漏费用修复；只用软件合成账单。"""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

from ai_supermarket import recovery
from ai_supermarket.common import MarketError, compact
from ai_supermarket.http_app import MarketHTTP
from ai_supermarket.measurement import TaskMeter, compare_runs, summarize_runs
from ai_supermarket.tests import test_recovery


class MeasurementRecoveryTests(unittest.TestCase):
    def setUp(self):
        test_recovery.RecoveryTests.setUp(self)
        self.measurement = self.live / 'measurement.sqlite'
        self.meter = self.new('measured')
        self.call = self.meter.begin('model', 'provider-v1', 'task')
        self.meter.end(self.call, 'succeeded', 10)
        self.meter.receipt(self.call, 'provider:initial', input_tokens=100, output_tokens=20, cost_micro_cny=125)
        self.meter.finish('pass', 'check-v1', 'artifact-v1', coverage_complete=True)
        self.pending = self.new('pending')
        self.pending_call = self.pending.begin('model', 'provider-v1', 'task')
        self.evidence = {**self.evidence, 'measurement_reconciliation': 'demo-only:measurement'}

    def new(self, name, path=None):
        return TaskMeter(path or self.measurement, name, 'alice', experiment='experiment', case='case',
                         arm='market', configuration_hash='a' * 64, evidence_mode='synthetic')

    def backup(self):
        return recovery.snapshot_bundle(*self.paths.values(), self.snapshot, measurement=self.measurement)

    def restore(self):
        self.backup()
        recovery.restore_bundle(self.snapshot, self.restored, require_measurement=True)
        self.restored_paths = recovery.paths_for(self.restored, include_measurement=True)
        return TaskMeter.existing(self.restored_paths['measurement'], 'measured', 'alice')

    def assert_code(self, code, callback, *args, **kwargs):
        with self.assertRaises(MarketError) as caught:
            callback(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)

    def review(self, meter):
        return meter.review_recovery('coverage:checked', 'receipts:checked', 'outcome:checked', coverage_complete=True)

    def test_four_ledger_snapshot_preserves_source_and_quarantines_old_cost_claims(self):
        before = {path: recovery.digest_file(path) for path in [*self.paths.values(), self.measurement]}
        restored = self.restore()
        self.assertEqual(before, {p: recovery.digest_file(p) for p in before})
        self.assertEqual(4, recovery.verify_bundle(self.snapshot, require_measurement=True)['files'])
        self.assertEqual(self.meter.call_records(), restored.call_records())
        report = restored.report()
        self.assertEqual(125, report['known_cost_micro_cny'])
        self.assertIsNone(report['total_cost_micro_cny'])
        self.assertIsNone(report['input_tokens'])
        self.assertTrue(report['recovery_review_required'])
        baseline = {**self.meter.report(), 'arm': 'direct'}
        self.assertEqual('insufficient_evidence', compare_runs(baseline, report)['status'])
        self.assertIsNone(summarize_runs([report])['cost_per_qualified_task'])

    def test_market_release_does_not_automatically_attest_any_old_run(self):
        restored = self.restore()
        recovery.release_bundle(self.restored, self.evidence)
        self.assertTrue(restored.report()['recovery_review_required'])
        self.assertEqual('reviewed', self.review(restored)['status'])
        self.assertEqual(125, restored.report()['total_cost_micro_cny'])
        self.assertFalse(restored.report()['recovery_review_required'])
        self.assertFalse(restored.report()['savings_proven'])
        self.assertFalse(restored.report()['independent_quality_verified'])
        self.assert_code('measurement_recovery_frozen', restored.observe, 'model', 'model', 'task',
                         lambda: self.fail('Old tasks must never automatically execute a model callback'))

    def test_review_before_release_is_not_comparable_and_new_runs_require_both_gates(self):
        restored = self.restore()
        self.review(restored)
        self.assertIsNone(restored.report()['total_cost_micro_cny'])
        self.assert_code('recovery_required', self.new, 'new', self.restored_paths['measurement'])
        # Simulate a crash after measurement release commits but before market release.
        with closing(recovery.connect_existing(self.restored_paths['measurement'], writable=True)) as db, db:
            db.execute("UPDATE meta SET value='released' WHERE key='recovery_state'")
        self.assert_code('recovery_required', self.new, 'new', self.restored_paths['measurement'])
        self.assertIsNone(restored.report()['total_cost_micro_cny'])
        recovery.release_bundle(self.restored, self.evidence)
        self.assertEqual(125, restored.report()['total_cost_micro_cny'])
        fresh = self.new('new', self.restored_paths['measurement'])
        result, _ = fresh.observe('other', 'local-operation', 'new-task', lambda: {'status': 'done'})
        self.assertEqual('done', result['status'])

    def test_pending_call_late_receipt_finish_and_review_preserve_unknown_until_proven(self):
        self.restore()
        restored = TaskMeter.existing(self.restored_paths['measurement'], 'pending', 'alice')
        self.assert_code('measurement_incomplete', self.review, restored)
        restored.end(self.pending_call, 'unknown', 20)
        restored.receipt(self.pending_call, 'provider:late', input_tokens=40, output_tokens=5)
        self.assertEqual(1, restored.report()['unknown_calls'])
        restored.resolve_recovered_call(self.pending_call, 'failed', 25, 'provider:status')
        restored.finish('fail', 'check-v1', 'failed-artifact', coverage_complete=True)
        self.assert_code('measurement_incomplete', self.review, restored)
        restored.receipt(self.pending_call, 'provider:late', cost_micro_cny=80)
        self.review(restored)
        recovery.release_bundle(self.restored, self.evidence)
        report = restored.report()
        self.assertEqual((80, 0, 'fail'), (report['total_cost_micro_cny'], report['unknown_calls'], report['outcome']))
        self.assertIsNone(summarize_runs([report])['cost_per_qualified_task'])
        self.assert_code('measurement_conflict', restored.receipt, self.pending_call, 'provider:late', cost_micro_cny=81)

    def test_missing_historical_call_can_be_imported_without_executing_and_invalidates_review(self):
        restored = self.restore()
        recovery.release_bundle(self.restored, self.evidence)
        self.review(restored)
        restored.recover_call('missing-call', 'model', 'provider-v1', 'child', 1000.0, 'trace:missing')
        restored.recover_call('missing-call', 'model', 'provider-v1', 'child', 1000.0, 'trace:missing')
        self.assertEqual(2, restored.report()['calls'])
        self.assertTrue(restored.report()['recovery_review_required'])
        self.assertIsNone(restored.report()['total_cost_micro_cny'])
        self.assert_code('measurement_incomplete', self.review, restored)
        self.assert_code('duplicate_receipt', restored.receipt, 'missing-call', 'provider:initial', cost_micro_cny=50)
        restored.resolve_recovered_call('missing-call', 'succeeded', 7, 'provider:missing-status')
        restored.receipt('missing-call', 'provider:missing', input_tokens=10, output_tokens=2, cost_micro_cny=50)
        self.review(restored)
        self.assertEqual(175, restored.report()['total_cost_micro_cny'])
        self.assert_code('measurement_conflict', restored.recover_call, 'missing-call', 'model', 'provider-v2', 'child', 1000.0, 'trace:x')
        self.assert_code('measurement_conflict', restored.resolve_recovered_call, 'missing-call', 'failed', 7, 'provider:changed')

    def test_late_metadata_enrichment_invalidates_only_affected_run_review(self):
        restored = self.restore()
        recovery.release_bundle(self.restored, self.evidence)
        self.review(restored)
        restored.receipt(self.call, 'provider:initial', cached_input_tokens=90)
        self.assertTrue(restored.report()['recovery_review_required'])
        self.review(restored)
        self.assertFalse(restored.report()['recovery_review_required'])

        self.assertEqual(125, restored.report()['total_cost_micro_cny'])
        restored.receipt(self.call, 'provider:initial', cost_micro_cny=125)
        self.assertFalse(restored.report()['recovery_review_required'])

    def test_explicit_reconciliation_can_fill_old_coverage_gap_without_overwriting_original_claim(self):
        partial = self.new('partial')
        partial.finish('unknown', 'old-validator', 'old-incomplete-artifact', coverage_complete=False)
        self.restore()
        restored = TaskMeter.existing(self.restored_paths['measurement'], 'partial', 'alice')
        self.assert_code('measurement_incomplete', restored.review_recovery, 'coverage', 'receipts', 'outcome')
        restored.recover_call('found-in-trace', 'other', 'accounted-cost', 'task', 1000, 'trace:checked')
        restored.resolve_recovered_call('found-in-trace', 'succeeded', 10, 'execution:checked')
        restored.receipt('found-in-trace', 'bill:found', cost_micro_cny=10)
        self.review(restored)
        recovery.release_bundle(self.restored, self.evidence)
        report = restored.report()
        self.assertFalse(report['source_coverage_attested_by_host'])
        self.assertTrue(report['coverage_attested_by_host'])
        self.assertEqual(10, report['total_cost_micro_cny'])
        self.assertEqual('unknown', report['outcome'])

    def test_withdrawing_review_keeps_original_fees_and_audit_history(self):
        restored = self.restore()
        recovery.release_bundle(self.restored, self.evidence)
        self.review(restored)
        restored.invalidate_recovery('incident:new-discrepancy')
        restored.invalidate_recovery('incident:new-discrepancy')
        self.assertTrue(restored.report()['recovery_review_required'])
        self.assertEqual(125, restored.report()['known_cost_micro_cny'])
        self.assertIsNone(restored.report()['total_cost_micro_cny'])
        with restored.store.connect() as db:
            actions = [json.loads(r[0])['action'] for r in db.execute("SELECT value FROM meta WHERE key LIKE 'measurement_recovery_audit:%'")]
        self.assertEqual(1, actions.count('review_withdrawn'))
        self.assertEqual(1, actions.count('run_reviewed'))

    def test_recovery_actions_are_scoped_and_do_not_apply_to_ordinary_runs(self):
        self.assert_code('invalid_state', self.review, self.meter)
        restored = self.restore()
        self.assert_code('not_found', TaskMeter.existing, self.restored_paths['measurement'], 'measured', 'bob')
        self.assert_code('not_found', restored.resolve_recovered_call, self.pending_call, 'succeeded', 1, 'ref')
        self.assert_code('measurement_conflict', restored.recover_call, self.pending_call, 'model', 'provider-v1', 'task', 1000, 'ref')
        self.assert_code('not_found', TaskMeter.existing, self.root / 'absent.sqlite', 'missing', 'alice')
        self.assertFalse((self.root / 'absent.sqlite').exists())

    def test_all_four_writer_locks_and_wal_commits_are_in_snapshot(self):
        paths = {**self.paths, 'measurement': self.measurement}
        original = recovery.copy_db
        def copying(source, destination, deadline):
            for path in paths.values():
                with closing(sqlite3.connect(path, timeout=0.01)) as db:
                    with self.assertRaises(sqlite3.OperationalError):
                        db.execute('BEGIN IMMEDIATE')
            return original(source, destination, deadline)
        with test_recovery.ExitStackForWAL(paths):
            self.meter.receipt(self.call, 'provider:initial', cached_input_tokens=10)
            with patch.object(recovery, 'copy_db', side_effect=copying):
                self.backup()
        with closing(recovery.connect_existing(self.snapshot / 'measurement.sqlite')) as db:
            self.assertEqual(10, db.execute('SELECT cached_input_tokens FROM calls WHERE id=?', (self.call,)).fetchone()[0])
        self.assertFalse((self.snapshot / 'measurement.sqlite-wal').exists())

    def test_measurement_corruption_missing_and_duplicate_sources_abort_without_output(self):
        self.assert_code('not_found', recovery.snapshot_bundle, *self.paths.values(), self.snapshot,
                         measurement=self.root / 'missing.sqlite')
        self.assert_code('invalid_params', recovery.snapshot_bundle, *self.paths.values(), self.snapshot,
                         measurement=self.paths['budget'])
        with self.meter.store.connect(write=True) as db:
            db.execute('UPDATE calls SET cached_input_tokens=999 WHERE id=?', (self.call,))
        self.assert_code('invalid_database', self.backup)
        self.assertFalse(self.snapshot.exists())

    def test_manifest_missing_meter_and_downgrade_are_refused(self):
        self.backup()
        path = self.snapshot / 'manifest.json'
        original = json.loads(path.read_text())
        manifest = {**original, 'files': {k: v for k, v in original['files'].items() if k != 'measurement'}}
        path.write_text(compact(manifest))
        self.assert_code('invalid_params', recovery.verify_bundle, self.snapshot)
        manifest.update(format=1, scope='exchange_identity_budget')
        path.write_text(compact(manifest))
        self.assert_code('invalid_manifest', recovery.verify_bundle, self.snapshot)

    def test_old_three_ledger_format_remains_supported_but_required_meter_is_not_silently_omitted(self):
        recovery.snapshot_bundle(*self.paths.values(), self.snapshot)
        self.assertEqual(3, recovery.verify_bundle(self.snapshot)['files'])
        self.assert_code('measurement_required', recovery.verify_bundle, self.snapshot, require_measurement=True)
        self.assert_code('measurement_required', recovery.restore_bundle, self.snapshot, self.restored, require_measurement=True)
        self.assertFalse(self.restored.exists())

    def test_mixed_batches_and_missing_required_readiness_cannot_release_or_resnapshot(self):
        self.restore()
        with closing(recovery.connect_existing(self.restored_paths['measurement'], writable=True)) as db, db:
            db.execute("UPDATE meta SET value='wrong-batch' WHERE key='recovery_id'")
        self.assertEqual({'status': 'not_ready'}, recovery.readiness(**self.restored_paths))
        self.assert_code('invalid_database', recovery.release_bundle, self.restored, self.evidence)
        self.assert_code('measurement_required', recovery.snapshot_bundle, *recovery.paths_for(self.restored).values(), self.root / 'omitted')

    def test_release_requires_measurement_evidence_and_record_cannot_omit_it(self):
        self.restore()
        ordinary = {key: self.evidence[key] for key in recovery.REVIEW_FIELDS}
        self.assert_code('invalid_params', recovery.release_bundle, self.restored, ordinary)
        record_path = self.restored / 'restore-complete.json'
        record = json.loads(record_path.read_text())
        record.pop('measurement_included')
        record_path.write_text(compact(record))
        self.assert_code('incomplete_restore', recovery.release_bundle, self.restored, ordinary)

    def test_partial_measurement_restore_never_activates_service_or_callbacks(self):
        self.backup()
        with patch.object(recovery, 'restore_measurement', side_effect=OSError('injected interruption')):
            with self.assertRaises(OSError):
                recovery.restore_bundle(self.snapshot, self.restored)
        self.assertFalse((self.restored / 'restore-complete.json').exists())
        paths = recovery.paths_for(self.restored, include_measurement=True)
        self.assertEqual({'status': 'not_ready'}, recovery.readiness(**paths))
        self.assert_code('recovery_required', TaskMeter.existing, paths['measurement'], 'measured', 'alice')

    def test_http_readiness_requires_the_configured_fourth_ledger_after_restore(self):
        self.restore()
        recovery.release_bundle(self.restored, self.evidence)
        params = dict(allowed_hosts=['market.test:8765'], budget_database=self.restored_paths['budget'])
        app = MarketHTTP(self.restored_paths['market'], self.restored_paths['identities'], **params)
        self.assertEqual({'status': 'not_ready'}, app.ready())
        app = MarketHTTP(self.restored_paths['market'], self.restored_paths['identities'], **params,
                         measurement_database=self.restored_paths['measurement'])
        self.assertEqual({'status': 'ready'}, app.ready())
        self.restored_paths['measurement'].rename(self.restored / 'temporarily-offline.sqlite')
        self.assertEqual({'status': 'not_ready'}, app.ready())
        self.assertFalse(self.restored_paths['measurement'].exists())

    def test_second_restore_resets_reviews_and_preserves_recovery_audit(self):
        restored = self.restore()
        recovery.release_bundle(self.restored, self.evidence)
        self.review(restored)
        new_snapshot, second = self.root / 'snapshot2', self.root / 'restored2'
        recovery.snapshot_bundle(*recovery.paths_for(self.restored).values(), new_snapshot,
                                 measurement=self.restored_paths['measurement'])
        recovery.restore_bundle(new_snapshot, second)
        again = TaskMeter.existing(second / 'measurement.sqlite', 'measured', 'alice')
        self.assertTrue(again.report()['recovery_review_required'])
        with again.store.connect() as db:
            audit = db.execute("SELECT value FROM meta WHERE key LIKE 'measurement_recovery_audit:%'").fetchall()
        self.assertEqual(1, len(audit))
        self.assertNotEqual(restored.report()['recovery_id'], again.report()['recovery_id'])

    def test_cli_four_ledger_snapshot_restore_and_release(self):
        def invoke(*args):
            run = subprocess.run([sys.executable, '-m', 'ai_supermarket.recovery', *map(str, args)],
                                 capture_output=True, timeout=15)
            self.assertEqual(b'', run.stderr)
            return run.returncode, json.loads(run.stdout)
        code, result = invoke('snapshot', '--market', self.paths['market'], '--identities', self.paths['identities'],
                              '--budget', self.paths['budget'], '--measurement', self.measurement, '--output', self.snapshot)
        self.assertEqual((0, 'snapshotted'), (code, result['status']))
        self.assertEqual(4, invoke('verify', '--bundle', self.snapshot, '--require-measurement')[1]['files'])
        self.assertTrue(invoke('restore', '--bundle', self.snapshot, '--require-measurement', '--output', self.restored)[1]['measurement_included'])
        proof = self.root / 'review.json'
        proof.write_text(compact(self.evidence))
        self.assertEqual((0, 'released'), (lambda r: (r[0], r[1]['status']))(
            invoke('release', '--bundle', self.restored, '--evidence', proof)))
