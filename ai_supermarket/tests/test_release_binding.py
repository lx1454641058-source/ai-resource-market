"""本项目原创。发行绑定候选：实际工具契约、持久化、旧调用与失败关闭。"""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ai_supermarket.common import MarketError, compact
from ai_supermarket.exchange import Exchange
from ai_supermarket.exchange_tools import ExchangeInterface
from ai_supermarket.tests.test_exchange import CONTENT, CHECKS, listing
from ai_supermarket.tests import test_assets as asset_fixtures
from ai_supermarket.tests import test_http_process_recovery as crash_fixtures
from unittest.mock import patch
from tools.market_deliver import deliver
from tools.market_bundle_check import check_bundle
from ai_supermarket.preparation import ResourcePreparation, validate_request


class ReleaseBindingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.db = self.root / 'market.sqlite'
        self.buyer = Exchange(self.db, 'buyer')
        self.seller = Exchange(self.db, 'vendor', 'seller')
        self.reviewer = Exchange(self.db, 'reviewer', 'reviewer')
        self.ref = {'id': 'vendor:brief', 'version': '1.0.0', 'purpose': 'personal_use'}
        self.seller.draft('brief', '1.0.0', listing(), CONTENT, 'draft')
        self.seller.submit('vendor:brief', '1.0.0', 1, 'submit')
        self.reviewer.review('vendor:brief', '1.0.0', 1, 'approve', CHECKS,
                             '原创隔离测试，非独立审核', 'review')
        self.digest = self.buyer.resolve(**self.ref)['release_digest']

    def acquire(self, request_id='acquire'):
        return self.buyer.acquire(**self.ref, request_id=request_id, expected_release_digest=self.digest)

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(MarketError) as caught:
            function(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)

    def count(self, table):
        with self.buyer.store.connect() as db:
            return db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

    def change_metadata(self, metadata):
        with self.buyer.store.connect(write=True) as db:
            db.execute('UPDATE submissions SET metadata=? WHERE resource_id=? AND version=?',
                       (compact(metadata), 'vendor:brief', '1.0.0'))

    def test_tool_schema_and_real_resolve_acquire_read_status(self):
        tools = ExchangeInterface(self.buyer)
        schema = next(t['inputSchema'] for t in tools.tool_list() if t['name'] == 'market_acquire')
        self.assertIn('expected_release_digest', schema['properties'])
        self.assertNotIn('expected_release_digest', schema['required'])
        details = tools.call('market_resolve', self.ref)
        self.assertEqual(hashlib.sha256(CONTENT.encode()).hexdigest(), details['content_hash'])
        self.assertEqual('market-release-v1', details['release_digest_schema'])
        acquired = tools.call('market_acquire', {**self.ref, 'request_id': 'pinned',
                                               'expected_release_digest': details['release_digest']})
        self.assertEqual('acquired', acquired['status'])
        self.assertEqual(self.digest, acquired['release_digest'])
        self.assertEqual('expected_release_v1', acquired['release_binding'])
        self.assertEqual(self.digest, self.buyer.status(acquired['acquisition_id'])['release_digest'])
        self.assertEqual(CONTENT, self.buyer.read(acquired['acquisition_id'], 'task', 'read')['content'])

    def test_all_immutable_conditions_affect_digest_and_old_expectation_fails_before_write(self):
        variations = []
        for key in ('license_text', 'source_statement', 'input', 'output', 'check'):
            metadata = listing()
            metadata[key] += ' altered'
            variations.append(metadata)
        metadata = listing()
        metadata['usage_contract']['permissions'] = ['file_read']
        variations.append(metadata)
        metadata = listing()
        metadata['usage_contract']['dependencies'] = [{'id': 'dep', 'license_name': 'MIT', 'max_cost_cents': 1}]
        variations.append(metadata)
        metadata = listing()
        metadata['allowed_uses'] = ['personal_use']
        variations.append(metadata)
        for metadata in variations:
            with self.subTest(metadata=metadata):
                self.change_metadata(metadata)
                self.assertNotEqual(self.digest, self.buyer.resolve(**self.ref)['release_digest'])
                self.error('release_changed', self.acquire)
                self.assertEqual(0, self.count('acquisitions'))

    def test_object_key_order_does_not_change_canonical_digest(self):
        def reorder(value):
            if isinstance(value, dict):
                return {key: reorder(value[key]) for key in reversed(list(value))}
            if isinstance(value, list):
                return [reorder(item) for item in value]
            return value
        self.change_metadata(reorder(listing()))
        self.assertEqual(self.digest, self.buyer.resolve(**self.ref)['release_digest'])
        self.assertEqual('acquired', self.acquire()['status'])

    def test_body_and_body_hash_replaced_after_acquisition_stops_pinned_read(self):
        acquired = self.acquire()
        content = '# Changed\n'
        with self.buyer.store.connect(write=True) as db:
            db.execute('UPDATE submissions SET content=?,content_hash=?',
                       (content, hashlib.sha256(content.encode()).hexdigest()))
        self.error('release_changed', self.buyer.read, acquired['acquisition_id'], 'task', 'read')
        self.assertEqual(0, self.count('usage_events'))

    def test_metadata_changed_after_acquisition_blocks_read_status_and_replay(self):
        acquired = self.acquire()
        metadata = listing()
        metadata['license_text'] += ' Changed license.'
        self.change_metadata(metadata)
        self.error('release_changed', self.buyer.read, acquired['acquisition_id'], 'task', 'read')
        self.assertEqual('unavailable', self.buyer.status(acquired['acquisition_id'])['status'])
        self.error('release_changed', self.acquire)
        self.assertEqual(0, self.count('usage_events'))

    def test_missing_audit_binding_cannot_downgrade_pinned_right(self):
        acquired = self.acquire()
        with self.buyer.store.connect(write=True) as db:
            db.execute('DELETE FROM exchange_audit WHERE id=?', ('e_' + acquired['acquisition_id'],))
        self.error('integrity_failed', self.buyer.read, acquired['acquisition_id'], 'task', 'read')
        self.assertEqual('unavailable', self.buyer.status(acquired['acquisition_id'])['status'])
        self.error('integrity_failed', self.acquire)

    def test_replay_cannot_remove_or_add_pin_and_no_duplicate_acquisition(self):
        acquired = self.acquire()
        self.assertEqual(acquired, self.acquire())
        self.error('idempotency_conflict', self.buyer.acquire, **self.ref, request_id='acquire')
        self.assertEqual(1, self.count('acquisitions'))
        legacy = self.buyer.acquire(**self.ref, request_id='legacy')
        self.assertNotIn('release_digest', legacy)
        self.assertEqual(legacy, self.buyer.acquire(**self.ref, request_id='legacy'))
        self.error('idempotency_conflict', self.acquire, 'legacy')
        self.assertEqual(2, self.count('acquisitions'))

    def test_reopen_preserves_binding_and_same_read_request(self):
        acquired = self.acquire()
        loaded = self.buyer.read(acquired['acquisition_id'], 'task', 'read')
        restarted = Exchange(self.db, 'buyer')
        self.assertEqual(acquired, restarted.acquire(**self.ref, request_id='acquire', expected_release_digest=self.digest))
        self.assertEqual(loaded, restarted.read(acquired['acquisition_id'], 'task', 'read'))
        self.assertEqual((1, 1), (self.count('acquisitions'), self.count('usage_events')))

    def test_price_changes_do_not_change_release_or_bypass_real_price_checks(self):
        self.seller.reprice('vendor:brief', '1.0.0', 1, 200, 'price')
        self.assertEqual(self.digest, self.buyer.resolve(**self.ref)['release_digest'])
        self.assertEqual('payment_unavailable', self.acquire()['status'])
        self.assertEqual(0, self.count('acquisitions'))

    def test_withdrawal_keeps_existing_pin_but_suspension_blocks_it(self):
        acquired = self.acquire()
        self.seller.availability('vendor:brief', '1.0.0', 1, 'withdrawn', 'fixture', 'withdraw')
        self.assertEqual(CONTENT, self.buyer.read(acquired['acquisition_id'], 'task', 'read')['content'])
        self.reviewer.availability('vendor:brief', '1.0.0', 2, 'suspended', 'fixture', 'suspend')
        self.assertEqual('unavailable', self.buyer.status(acquired['acquisition_id'])['status'])
        with self.assertRaises(MarketError):
            self.buyer.read(acquired['acquisition_id'], 'task', 'replay')

    def test_invalid_digest_rejected_without_ledger_changes(self):
        for digest in ('', 'A' * 64, 'a' * 63, True, []):
            self.error('invalid_params', self.buyer.acquire, **self.ref, request_id='bad', expected_release_digest=digest)
        self.assertEqual(0, self.count('acquisitions'))

    def test_audit_insert_failure_rolls_back_acquisition(self):
        with self.buyer.store.connect(write=True) as db:
            db.execute("CREATE TRIGGER refuse_binding BEFORE INSERT ON exchange_audit WHEN NEW.action='acquire' BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.acquire()
        self.assertEqual(0, self.count('acquisitions'))

    def test_pinned_asset_read_survives_reopen_and_detects_conditions_change(self):
        fixture = asset_fixtures.AssetTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        draft = fixture.draft()
        fixture.approve(draft)
        details = fixture.buyer.resolve(draft['id'], '1.0.0', 'personal_use')
        acquired = fixture.buyer.acquire(draft['id'], '1.0.0', 'pinned', 'personal_use',
                                         expected_release_digest=details['release_digest'])
        self.assertEqual('read', fixture.buyer.read_asset(acquired['acquisition_id'], 'task', 'asset')['status'])
        with fixture.buyer.store.connect(write=True) as db:
            row = db.execute('SELECT metadata FROM submissions').fetchone()
            metadata = json.loads(row['metadata'])
            metadata['license_text'] += '\nchanged'
            db.execute('UPDATE submissions SET metadata=?', (compact(metadata),))
        self.error('release_changed', fixture.buyer.read_asset, acquired['acquisition_id'], 'task', 'again')

    def delivery_request(self):
        return {'task_id': 'delivery-task', 'purpose': 'personal_use',
                'needs': [{'id': 'brief', 'query': '产品简报', 'kind': 'template', 'required': True}],
                'selections': {'brief': {key: self.ref[key] for key in ('id', 'version')}}}

    def test_local_delivery_pins_and_bundle_checker_validates_binding(self):
        folder = self.root / 'delivery'
        reply = deliver(self.buyer, self.delivery_request(), folder)
        self.assertEqual('delivered', reply['status'])
        bundle = json.loads((folder / 'bundle.json').read_bytes())
        acquired = bundle['resources'][0]['acquisition_id']
        self.assertEqual(self.digest, self.buyer.status(acquired)['release_digest'])
        self.assertEqual('verified_for_read', check_bundle(self.buyer, folder, reply['manifest_sha256'],
                                                         'delivery-task', 'personal_use')['status'])
        with self.buyer.store.connect(write=True) as db:
            db.execute('DELETE FROM exchange_audit WHERE id=?', ('e_' + acquired,))
        self.error('integrity_failed', check_bundle, self.buyer, folder, reply['manifest_sha256'],
                   'delivery-task', 'personal_use')

    def test_conditions_change_between_preflight_and_acquire_prevents_delivery(self):
        original = self.buyer.acquire
        def changed(*args, **kwargs):
            metadata = listing()
            metadata['license_text'] += ' changed'
            self.change_metadata(metadata)
            return original(*args, **kwargs)
        folder = self.root / 'delivery'
        with patch.object(self.buyer, 'acquire', side_effect=changed):
            reply = deliver(self.buyer, self.delivery_request(), folder)
        self.assertEqual('delivery_incomplete', reply['status'])
        self.assertEqual('release_changed', reply['error_code'])
        self.assertFalse((folder / 'bundle.json').exists())
        self.assertEqual(0, self.count('acquisitions'))

    def test_old_entitlement_is_preserved_and_new_delivery_gets_separate_binding(self):
        old = self.buyer.acquire(**self.ref, request_id='old-client')
        folder = self.root / 'delivery'
        reply = deliver(self.buyer, self.delivery_request(), folder)
        self.assertEqual('delivered', reply['status'])
        row = json.loads((folder / 'bundle.json').read_bytes())['resources'][0]
        self.assertNotEqual(old['acquisition_id'], row['acquisition_id'])
        self.assertTrue(row['reused_entitlement'])
        self.assertEqual(0, row['new_resource_cents'])
        self.assertNotIn('release_digest', self.buyer.status(old['acquisition_id']))
        retry = deliver(self.buyer, self.delivery_request(), self.root / 'retry')
        self.assertEqual('delivered', retry['status'])
        self.assertEqual(2, self.count('acquisitions'))

    def test_selected_digest_survives_separate_check_and_delivery(self):
        request = self.delivery_request()
        request['selections']['brief']['expected_release_digest'] = self.digest
        helper = ResourcePreparation(self.buyer)
        self.assertEqual('prechecked', helper.check(request)['status'])
        self.assertEqual('delivered', deliver(self.buyer, request, self.root / 'pinned')['status'])
        metadata = listing()
        metadata['license_text'] += ' later change'
        self.change_metadata(metadata)
        result = deliver(self.buyer, request, self.root / 'changed')
        self.assertEqual('needs_attention', result['status'])
        self.assertEqual('release_changed', result['needs'][0]['error_code'])
        self.assertFalse((self.root / 'changed').exists())
        self.assertEqual(1, self.count('acquisitions'))

    def test_duplicate_resource_with_conflicting_pins_stops_all_delivery(self):
        request = self.delivery_request()
        request['needs'].append({**request['needs'][0], 'id': 'another'})
        request['selections']['brief']['expected_release_digest'] = self.digest
        request['selections']['another'] = {**request['selections']['brief'], 'expected_release_digest': '0' * 64}
        result = deliver(self.buyer, request, self.root / 'conflict')
        self.assertEqual('needs_attention', result['status'])
        self.assertEqual(0, self.count('acquisitions'))
        self.assertFalse((self.root / 'conflict').exists())

    def test_invalid_selected_digest_is_not_ignored(self):
        for value in (None, '', 'A' * 64, True, []):
            request = self.delivery_request()
            request['selections']['brief']['expected_release_digest'] = value
            self.error('invalid_params', validate_request, request, checking=True)


class PinnedProcessRecoveryTests(crash_fixtures.HTTPProcessRecoveryTests):
    def test_pinned_acquire_commit_crash_replay_keeps_digest(self):
        server = self.launch(fault='market_acquire')
        ref = self.publish(server)
        resolved = self.client(server).call('market_resolve', ref)
        request = {**ref, 'request_id': 'lost-pinned', 'expected_release_digest': resolved['release_digest']}
        self.crashed_call(server, 'market_acquire', request)
        rows = self.rows(self.paths, 'acquisitions')
        self.assertEqual(1, len(rows))
        restarted = self.launch()
        replay = self.client(restarted).call('market_acquire', request)
        self.assertEqual('acquired', replay['status'])
        self.assertEqual(rows[0]['id'], replay['acquisition_id'])
        self.assertEqual(resolved['release_digest'], replay['release_digest'])
        self.assertEqual(rows, self.rows(self.paths, 'acquisitions'))
