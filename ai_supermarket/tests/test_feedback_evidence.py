"""来源：本项目原创。分源证据、并发修订、隔离及非破坏升级验收。"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_supermarket.catalog import Catalog
from ai_supermarket.common import MarketError, compact, parse_json
from ai_supermarket.exchange import Exchange
from ai_supermarket.exchange_store import EXCHANGE_SCHEMA
from ai_supermarket.migrate import copy_upgrade
from ai_supermarket.service import Market
from ai_supermarket.store import LEGACY_SCHEMA
from ai_supermarket.store import Store
from ai_supermarket.tools import dispatch


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / 'new.sqlite'
        self.market = Market(self.path, 'alice')

    def usage(self, key='1'):
        acquired = self.market.acquire('product-brief', '1.0.0', 'acquire-' + key)
        return self.market.read(acquired['acquisition_id'], 'task', 'read-' + key)['usage_id']

    def assert_code(self, code, method, *args, **kwargs):
        with self.assertRaises(MarketError) as cm:
            method(*args, **kwargs)
        self.assertEqual(code, cm.exception.code)

    def test_host_and_ai_evidence_coexist_in_both_arrival_orders(self):
        for reverse in (False, True):
            usage = self.usage(str(reverse))
            calls = [lambda: self.market.record_check(usage, 'format-v1', True),
                     lambda: self.market.feedback(usage, 'no')]
            if reverse:
                calls.reverse()
            for call in calls:
                self.assertEqual('accepted', call()['status'])
                self.assertEqual('duplicate', call()['status'])
            records = self.market.feedback_history(usage)['events']
            self.assertEqual(['agent_reported', 'host_check'], [r['evidence'] for r in records])
            self.assertEqual(['no', 'unknown'], [r['useful'] for r in records])

    def test_multiple_validators_and_unknown_are_not_global_success(self):
        usage = self.usage()
        self.market.record_check(usage, 'format-v1', True)
        self.market.record_check(usage, 'sources-v1', False)
        self.market.feedback(usage)
        quality = self.market.resolve('product-brief', '1.0.0')['quality']
        self.assertFalse(quality['independently_verified'])
        self.assertEqual([(None, 'not_run', 1), ('format-v1', 'pass', 1), ('sources-v1', 'fail', 1)],
                         [(r['validator'], r['check_result'], r['n']) for r in quality['observations']])

    def test_corrections_preserve_history_and_only_latest_counts(self):
        usage = self.usage()
        self.market.record_check(usage, 'format-v1', True)
        self.market.feedback(usage, 'yes', 'pass')
        original = self.market.feedback_history(usage)['events']
        self.market.correct_feedback(usage, 1, 'report_corrected', 'no', 'fail', 'bad_output')
        self.market.correct_check(usage, 'format-v1', False, 1, 'check_repeated')
        self.market.correct_feedback(usage, 2, 'appeal_resolved', 'unknown', 'not_run')
        # A lost response can be replayed even after a later correction without reverting it.
        self.assertEqual('duplicate', self.market.correct_feedback(
            usage, 1, 'report_corrected', 'no', 'fail', 'bad_output')['status'])
        events = self.market.feedback_history(usage)['events']
        self.assertEqual(5, len(events))
        self.assertEqual(original[0], events[0])
        self.assertEqual(original[1], events[3])
        self.assertEqual(events[0]['id'], events[1]['supersedes'])
        observations = self.market.resolve('product-brief', '1.0.0')['quality']['observations']
        self.assertEqual(['not_run', 'fail'], [r['check_result'] for r in observations])
        self.assertEqual([1, 1], [r['n'] for r in observations])

    def test_stale_conflicting_and_missing_corrections_are_rejected(self):
        usage = self.usage()
        self.assert_code('revision_conflict', self.market.correct_feedback, usage, 1, 'report_corrected')
        self.market.feedback(usage, 'yes')
        self.assert_code('invalid_params', self.market.correct_feedback, usage, True, 'report_corrected')
        self.assert_code('invalid_params', self.market.correct_feedback, usage, 1, 'invented')
        self.assert_code('revision_conflict', self.market.correct_feedback, usage, 9, 'report_corrected')
        self.market.correct_feedback(usage, 1, 'report_corrected', 'no')
        self.assert_code('feedback_conflict', self.market.correct_feedback, usage, 1, 'report_corrected', 'yes')
        self.assert_code('feedback_conflict', self.market.feedback, usage, 'no')

    def test_concurrent_delivery_and_correction_have_one_record_per_revision(self):
        usage = self.usage()
        def submit(_):
            m = Market(self.path, 'alice')
            return m.record_check(usage, 'format-v1', True)['status']
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(submit, range(12)))
        self.assertEqual(1, results.count('accepted'))
        def correct(_):
            return Market(self.path, 'alice').correct_check(usage, 'format-v1', False, 1, 'check_repeated')['status']
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(correct, range(12)))
        self.assertEqual(1, results.count('accepted'))
        self.assertEqual(2, len(self.market.feedback_history(usage)['events']))

    def test_other_principal_and_model_cannot_change_host_evidence(self):
        usage = self.usage()
        self.market.record_check(usage, 'format-v1', True)
        bob = Market(self.path, 'bob')
        self.assert_code('not_found', bob.feedback_history, usage)
        self.assert_code('not_found', bob.correct_check, usage, 'format-v1', False, 1, 'check_repeated')
        for name in ('correct_check', 'correct_feedback', 'feedback_history'):
            self.assertEqual('unknown_tool', dispatch(self.market, name, {'usage_id': usage})['error_code'])
        self.assertEqual('invalid_params', dispatch(self.market, 'market_feedback', {
            'usage_id': usage, 'evidence': 'host_check', 'validator': 'format-v1'})['error_code'])
        self.assertEqual(1, len(self.market.feedback_history(usage)['events']))

    def legacy(self, kind='catalog', evidence='host_check'):
        path = self.root / ('old-' + kind + '-' + evidence + '.sqlite')
        db = sqlite3.connect(path)
        try:
            db.executescript(LEGACY_SCHEMA + (EXCHANGE_SCHEMA if kind == 'exchange' else ''))
            item = Catalog().get('product-brief', '1.0.0')
            resource_id, purpose, digest = item['id'], 'project_local', item['sha256']
            if kind == 'exchange':
                from ai_supermarket.tests.test_exchange import listing, CONTENT
                resource_id, purpose = 'vendor:brief', 'commercial_use'
                digest = hashlib.sha256(CONTENT.encode()).hexdigest()
                db.execute('INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           (resource_id, '1.0.0', 'vendor', compact(listing()), CONTENT, digest,
                            1, 'approved', '模拟准入', 1.0, 1.0))
                db.execute('INSERT INTO offers VALUES (?,?,?,?,?,?)',
                           (resource_id, '1.0.0', 0, 1, 'active', 1.0))
            db.execute('INSERT INTO acquisitions VALUES (?,?,?,?,?,?,?,?)',
                       ('a_old', 'alice', 'request', resource_id, item['version'], purpose, digest, 1.0))
            db.execute('INSERT INTO usage_events VALUES (?,?,?,?,?,?)', ('u_old', 'a_old', 'alice', 'task', 'read', 2.0))
            db.execute('INSERT INTO feedback VALUES (?,?,?,?,?,?,?,?)',
                       ('u_old', 'alice', 'unknown', 'pass', None, evidence, 'format-v1' if evidence == 'host_check' else None, 3.0))
            if kind == 'exchange':
                db.execute('INSERT INTO exchange_audit VALUES (?,?,?,?,?,?,?)',
                           ('e_old', 'alice', 'example', None, None, compact({'test': True}), 1.0))
            db.commit()
        finally:
            db.close()
        return path

    def test_copy_upgrade_preserves_original_records_and_accepts_missing_evidence(self):
        for evidence in ('host_check', 'agent_reported'):
            old = self.legacy(evidence=evidence)
            before = old.read_bytes()
            target = self.root / ('upgraded-' + evidence + '.sqlite')
            result = copy_upgrade(old, target)
            self.assertTrue(result['verified'])
            self.assertFalse(result['switched'])
            self.assertEqual(before, old.read_bytes())
            m = Market(target, 'alice')
            self.assertEqual('read', m.read('a_old', 'task', 'read')['status'])
            action = (lambda: m.feedback('u_old', 'no')) if evidence == 'host_check' else (
                lambda: m.record_check('u_old', 'format-v1', True))
            self.assertEqual('accepted', action()['status'])
            self.assertEqual(2, len(m.feedback_history('u_old')['events']))
            self.assertEqual('pass', m.feedback_history('u_old')['events'][0 if evidence == 'agent_reported' else 1]['check_result'])

    def test_exchange_copy_preserves_audit_and_correct_database_type(self):
        old = self.legacy('exchange')
        target = self.root / 'exchange-v2.sqlite'
        result = copy_upgrade(old, target)
        self.assertEqual('exchange', result['kind'])
        m = Exchange(target, 'alice')
        self.assertEqual('accepted', m.feedback('u_old', 'no')['status'])
        self.assertEqual('vendor:brief', m.search('产品简报')['items'][0]['id'])
        self.assertEqual('read', m.read('a_old', 'task', 'read')['status'])
        with m.store.connect() as db:
            self.assertEqual('example', db.execute('SELECT action FROM exchange_audit').fetchone()[0])
        self.assert_code('wrong_database', Market, target)

    def test_old_open_requires_explicit_upgrade_and_never_changes_source(self):
        old = self.legacy()
        before = hashlib.sha256(old.read_bytes()).hexdigest()
        self.assert_code('upgrade_required', Market, old)
        self.assertEqual(before, hashlib.sha256(old.read_bytes()).hexdigest())

    def test_copy_rejects_existing_destination_and_wrong_schema(self):
        old = self.legacy()
        self.assert_code('destination_exists', copy_upgrade, old, old)
        destination = self.root / 'preserved.sqlite'
        destination.write_bytes(b'preserve-me')
        self.assert_code('destination_exists', copy_upgrade, old, destination)
        self.assertEqual(b'preserve-me', destination.read_bytes())
        missing = self.root / 'never-created.sqlite'
        self.assert_code('wrong_database', copy_upgrade, self.path, missing)
        self.assertFalse(missing.exists())

    def test_source_write_during_copy_is_detected_and_copied_rows_rolled_back(self):
        old = self.legacy()
        target = self.root / 'interrupted.sqlite'
        real_init = Store.__init__
        def init_and_write(store, path):
            real_init(store, path)
            db = sqlite3.connect(old)
            try:
                db.execute("INSERT INTO task_checks VALUES ('alice','another','fingerprint',1,0)")
                db.commit()
            finally:
                db.close()
        with patch.object(Store, '__init__', init_and_write):
            self.assert_code('source_changed', copy_upgrade, old, target)
        with Store(target).connect() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM acquisitions').fetchone()[0])
        retry = self.root / 'retry.sqlite'
        self.assertTrue(copy_upgrade(old, retry)['verified'])
        with Store(retry).connect() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM task_checks').fetchone()[0])

    def test_copy_rejects_feedback_owned_by_another_principal(self):
        old = self.legacy()
        missing = self.root / 'never-created.sqlite'
        db = sqlite3.connect(old)
        try:
            db.execute("UPDATE feedback SET principal='bob'")
            db.commit()
        finally:
            db.close()
        self.assert_code('invalid_database', copy_upgrade, old, missing)
        self.assertFalse(missing.exists())

    def test_upgrade_cli_returns_verified_summary_without_content(self):
        old = self.legacy()
        target = self.root / 'cli.sqlite'
        result = subprocess.run([sys.executable, '-m', 'ai_supermarket.migrate', '--source', str(old),
                                 '--output', str(target)], capture_output=True, cwd=Path(__file__).resolve().parents[2])
        self.assertEqual(0, result.returncode, result.stderr.decode())
        reply = parse_json(result.stdout)
        self.assertEqual('copied', reply['status'])
        self.assertNotIn('content', reply)
        self.assertFalse(reply['switched'])


if __name__ == '__main__':
    unittest.main()
