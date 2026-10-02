"""本项目原创。API历史记录的健康检查与隔离恢复，零网络/真实资金。"""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from ai_supermarket.api_quota import canonical, checksum
from ai_supermarket.common import MarketError, compact
from ai_supermarket.identity import Identities
from ai_supermarket.exchange import Exchange
from ai_supermarket import recovery
from tools import market_api_try as trial


class ApiRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.live = self.root / 'live'
        trial.prepare(self.live)
        self.app, _, self.marker = trial.application(self.live, local_provider=False)
        self.paths = recovery.paths_for(self.live)
        self.host = self.app.apis.hosts[self.marker['key_id']][trial.PROVIDER]
        self.calls = []
        def execute(*args):
            self.calls.append(args)
            return trial.execute(*args)
        self.host.execute = execute
        self.token = json.loads((self.live / 'buyer-credential.json').read_text())['token']
        self.acquired = self.host.market.acquire('api-trial-seller:stock', '1.0.0', 'api-recovery-acquire',
            purpose='personal_use', expected_release_digest=self.marker['release_digest'])['acquisition_id']
        self.payload = {'acquisition_id': self.acquired, 'request_id': 'original-api-call',
                        'input': {'warehouse': 'fictional'}, 'expected_release_digest': self.marker['release_digest']}

    def call(self, receipt=True):
        result = self.app.apis.call(self.token, 'call', self.payload)
        if receipt:
            envelope = self.app.apis.call(self.token, 'result', self.payload)
            self.app.apis.call(self.token, 'receipt', {**self.payload, 'usage_id': envelope['usage_id'],
                'manifest_sha256': checksum(canonical(envelope['manifest'], 8192))})
        return result

    def meta(self, path=None):
        with closing(recovery.connect_existing(path or self.paths['market'])) as db:
            return {key: raw for key, raw in db.execute('SELECT key,value FROM meta') if key.startswith('api_')}

    def set_meta(self, key, raw):
        with closing(recovery.connect_existing(self.paths['market'], writable=True)) as db, db:
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, raw))

    def invalid(self):
        self.assertEqual({'status': 'not_ready'}, recovery.readiness(*self.paths.values()))
        with closing(recovery.connect_existing(self.paths['market'])) as db:
            with self.assertRaises(MarketError): recovery.validate_database(db, 'market')

    def test_completed_api_and_receipt_remain_ready_without_more_execution(self):
        self.assertEqual('api_call_completed', self.call()['status'])
        before = {k: recovery.digest_file(v) for k, v in self.paths.items()}
        self.assertEqual({'status': 'ready'}, self.app.ready())
        with closing(recovery.connect_existing(self.paths['market'])) as db:
            recovery.validate_database(db, 'market')
        self.assertEqual(before, {k: recovery.digest_file(v) for k, v in self.paths.items()})
        self.assertEqual(1, len(self.calls)); self.assertEqual(3, len(self.meta()))

    def test_reserved_original_operation_is_preserved_without_completion(self):
        def unknown(*args): raise OSError('synthetic lost response')
        self.host.execute = unknown
        self.assertEqual('reserved', self.call(receipt=False)['status'])
        self.assertEqual(1, len(self.meta()))
        self.assertEqual({'status': 'ready'}, self.app.ready())
        snapshot = self.root / 'unknown-snapshot'
        self.assertEqual('snapshotted', recovery.snapshot_bundle(*self.paths.values(), snapshot)['status'])
        self.assertEqual(self.meta(), self.meta(snapshot / 'market.sqlite'))

    def test_local_api_delivery_without_remote_dispatch_record_is_valid(self):
        result = self.host.use(self.acquired, self.payload['input'], 'local-api')
        self.assertEqual('api_result_delivered', result['status'])
        self.assertEqual(1, len(self.meta())); self.assertEqual({'status': 'ready'}, self.app.ready())

    def test_snapshot_restore_preserves_records_but_gates_old_credentials_and_budget(self):
        self.call(); original = self.meta(); snapshot = self.root / 'snapshot'; restored = self.root / 'restored'
        quota_sha = recovery.digest_file(self.live / 'quota.sqlite')
        self.assertEqual('snapshotted', recovery.snapshot_bundle(*self.paths.values(), snapshot)['status'])
        self.assertEqual('verified', recovery.verify_bundle(snapshot)['status'])
        self.assertEqual('restored_quarantined', recovery.restore_bundle(snapshot, restored)['status'])
        paths = recovery.paths_for(restored)
        self.assertEqual(original, self.meta(paths['market']))
        self.assertEqual({'status': 'not_ready'}, recovery.readiness(*paths.values()))
        with self.assertRaises(MarketError): Exchange(paths['market'], trial.BUYER)
        with self.assertRaises(MarketError): Identities(paths['identities']).authenticate(self.token)
        with closing(recovery.connect_existing(paths['budget'])) as db:
            self.assertEqual('paused', db.execute('SELECT state FROM budgets').fetchone()[0])
        self.assertFalse((restored / 'quota.sqlite').exists())
        self.assertEqual(quota_sha, recovery.digest_file(self.live / 'quota.sqlite'))
        evidence = {key: 'isolated-review-' + key for key in recovery.REVIEW_FIELDS}
        self.assertEqual('released', recovery.release_bundle(restored, evidence)['status'])
        self.assertEqual({'status': 'ready'}, recovery.readiness(*paths.values()))
        with self.assertRaises(MarketError): Identities(paths['identities']).authenticate(self.token)
        self.assertEqual(1, len(self.calls))

    def test_usage_shape_and_original_links_are_validated(self):
        self.call(); originals = self.meta()
        key = next(k for k in originals if k.startswith('api_usage:'))
        value = json.loads(originals[key])
        for change in ({'task_id': 'another-task'}, {'acquisition_id': 'absent'}, {'provider': 'other'},
                       {'expires_at': True}, {'bytes': 65537}, {'license_sha256': '0' * 64},
                       {'purpose': 'commercial_use'}, {'release_digest': '0' * 64}, {'extra': 'unrecognized'}):
            with self.subTest(change=change):
                self.set_meta(key, compact({**value, **change})); self.invalid()
                self.set_meta(key, originals[key])

    def test_corrupt_receipt_hash_timestamp_and_usage_are_rejected(self):
        self.call(); originals = self.meta()
        key = next(k for k in originals if k.startswith('api_client_receipt:'))
        original = json.loads(originals[key])
        for change in ({'receipt_id': 'ar_' + '0' * 64}, {'acknowledged_at': True}, {'acknowledged_at': -1},
                       {'acknowledged_at': 10**500}):
            self.set_meta(key, compact({**original, **change})); self.invalid(); self.set_meta(key, originals[key])
        forged = deepcopy(original); forged['offer']['manifest_sha256'] = '0' * 64
        forged['receipt_id'] = 'ar_' + checksum(canonical(forged['offer'], 8192))
        self.set_meta(key, compact(forged)); self.invalid()

    def test_receipt_with_recomputed_ids_cannot_change_original_budget_scope(self):
        self.call(); originals = self.meta()
        key = next(k for k in originals if k.startswith('api_client_receipt:'))
        record = json.loads(originals[key]); record['offer']['scope']['root_id'] = 'different-root'
        record['receipt_id'] = 'ar_' + checksum(canonical(record['offer'], 8192))
        forged_key = 'api_client_receipt:' + checksum(canonical([record['offer']['scope'], record['offer']['request_id']], 4096))
        self.set_meta(forged_key, compact(record)); self.invalid()

    def test_dispatch_record_key_and_cross_usage_scope_are_validated(self):
        self.call(); originals = self.meta()
        key = next(k for k in originals if k.startswith('api_operation_authority:'))
        original = json.loads(originals[key])
        for change in ({'operation_id': '0' * 64}, {'key_id': 'not-a-key'}, {'request_sha256': 'bad'},
                       {'principal': 'other'}, {'provider': 'other'}, {'task_id': 'other'}):
            self.set_meta(key, compact({**original, **change})); self.invalid(); self.set_meta(key, originals[key])

    def test_unknown_prefix_and_malformed_records_still_block_snapshot(self):
        for key, raw in (('unrecognized:record', '{}'), ('api_usage:u_' + '0' * 32, '{}'),
                         ('api_operation_authority:' + '0' * 64, ' ' * 8193)):
            with self.subTest(key=key):
                self.set_meta(key, raw); self.invalid()
                target = self.root / 'must-not-create'
                with self.assertRaises(MarketError): recovery.snapshot_bundle(*self.paths.values(), target)
                self.assertFalse(target.exists())
                with closing(recovery.connect_existing(self.paths['market'], writable=True)) as db, db:
                    db.execute('DELETE FROM meta WHERE key=?', (key,))

    def test_expired_keys_and_withdrawn_offer_do_not_invalidate_historical_records(self):
        self.call()
        self.app.gateway.identities.revoke_key(self.marker['key_id'])
        with closing(recovery.connect_existing(self.paths['market'], writable=True)) as db, db:
            db.execute("UPDATE offers SET availability='withdrawn'")
        self.assertEqual({'status': 'ready'}, self.app.ready())
        with self.assertRaises(MarketError): self.app.apis.call(self.token, 'call', self.payload)


if __name__ == '__main__': unittest.main()
