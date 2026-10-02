"""来源：本项目原创。本地资源交付、权益复用与中断证据验收。"""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_supermarket.common import MarketError
from ai_supermarket.exchange import Exchange
from ai_supermarket.tests.test_exchange import listing, CONTENT, CHECKS
from tools import market_deliver as delivery


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / 'market.sqlite'
        self.buyer = Exchange(self.database, 'buyer')
        self.seller = Exchange(self.database, 'seller', 'seller')
        self.reviewer = Exchange(self.database, 'reviewer', 'reviewer')
        self.output = self.root / 'bundle'
        self.request = {'task_id': 'task-1', 'purpose': 'personal_use', 'needs': [], 'selections': {}}

    def add(self, slug='brief', price=0, metadata=None):
        self.seller.draft(slug, '1.0.0', metadata or listing(price), CONTENT, 'draft-' + slug)
        selected = {'id': 'seller:' + slug, 'version': '1.0.0'}
        self.seller.submit(selected['id'], '1.0.0', 1, 'submit-' + slug)
        self.reviewer.review(selected['id'], '1.0.0', 1, 'approve', CHECKS,
                             '原创工程测试，非独立审核', 'review-' + slug)
        self.request['needs'].append({'id': slug, 'query': '产品简报', 'kind': 'template', 'required': True})
        self.request['selections'][slug] = selected
        return selected

    def counts(self):
        with self.buyer.store.connect() as db:
            return tuple(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                         for table in ('acquisitions', 'usage_events', 'feedback'))

    def run_delivery(self, output=None, request=None, buyer=None):
        return delivery.deliver(buyer or self.buyer, request or self.request, output or self.output)

    def test_unique_content_and_full_conditions_with_compact_stdout(self):
        selected = self.add()
        self.request['needs'].append({**self.request['needs'][0], 'id': 'second'})
        self.request['selections']['second'] = selected
        with patch.object(self.buyer, 'resolve', wraps=self.buyer.resolve) as resolve:
            result = self.run_delivery()
        self.assertEqual(2, resolve.call_count)  # One plan check + one execution check.
        self.assertEqual('delivered', result['status'])
        self.assertEqual((1, 1, 0), self.counts())
        self.assertNotIn('content', result)
        self.assertFalse(result['task_completed'])
        self.assertFalse(result['execution_authorized'])
        raw = (self.output / 'bundle.json').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result['manifest_sha256'])
        bundle = json.loads(raw)
        self.assertEqual(2, len(bundle['needs']))
        row = bundle['resources'][0]
        self.assertEqual(CONTENT, (self.output / row['path']).read_text(encoding='utf-8'))
        self.assertEqual(hashlib.sha256(CONTENT.encode()).hexdigest(), row['content_hash'])
        self.assertEqual(listing()['license_text'], row['conditions']['license_text'])
        self.assertEqual(listing()['source_statement'], row['conditions']['source_statement'])

    def test_retry_new_task_and_repriced_owned_resource(self):
        selected = self.add()
        self.run_delivery()
        self.seller.reprice(selected['id'], '1.0.0', 1, 900, 'reprice')
        result = self.run_delivery(self.root / 'retry')
        self.assertEqual('delivered', result['status'])
        self.assertEqual((1, 1, 0), self.counts())
        bundle = json.loads((self.root / 'retry/bundle.json').read_bytes())
        self.assertTrue(bundle['resources'][0]['reused_entitlement'])
        self.assertEqual(0, bundle['resources'][0]['new_resource_cents'])
        next_task = {**self.request, 'task_id': 'task-2'}
        self.assertEqual('delivered', self.run_delivery(self.root / 'next', next_task)['status'])
        self.assertEqual((1, 2, 0), self.counts())
        outsider = Exchange(self.database, 'outsider')
        self.assertEqual('needs_attention', self.run_delivery(self.root / 'other', buyer=outsider)['status'])
        commercial = {**self.request, 'purpose': 'commercial_use'}
        self.assertEqual('needs_attention', self.run_delivery(self.root / 'commercial', commercial)['status'])
        self.assertEqual((1, 2, 0), self.counts())

    def test_preflight_missing_required_and_paid_prevent_all_writes(self):
        self.add()
        self.add('paid', 1)
        self.assertEqual('needs_attention', self.run_delivery()['status'])
        self.assertEqual((0, 0, 0), self.counts())
        self.assertFalse(self.output.exists())
        self.request['selections']['paid'] = None
        self.assertEqual('needs_attention', self.run_delivery()['status'])
        self.request['needs'][1]['required'] = False
        self.assertEqual('delivered', self.run_delivery()['status'])

    def test_license_rejection_and_unsupported_kind_are_not_delivered(self):
        metadata = listing()
        metadata['allowed_uses'] = ['personal_use']
        self.add(metadata=metadata)
        request = {**self.request, 'purpose': 'commercial_use'}
        self.assertEqual('needs_attention', self.run_delivery(request=request)['status'])
        self.request['needs'][0]['kind'] = 'image'
        self.assertEqual('needs_attention', self.run_delivery()['status'])
        self.assertEqual((0, 0, 0), self.counts())
        self.assertFalse(self.output.exists())

    def test_permissive_policy_cannot_authorize_external_dependency(self):
        metadata = listing()
        metadata['usage_contract']['dependencies'] = [
            {'id': 'fixture-dependency', 'license_name': 'MIT', 'max_cost_cents': 0}]
        self.add(metadata=metadata)
        policy = {'capabilities': ['read_markdown'], 'permissions': [],
                  'dependencies': ['fixture-dependency'], 'extra_budget_cents': 0}
        buyer = Exchange(self.database, 'buyer', host_policy=policy)
        with self.assertRaises(MarketError) as caught:
            self.run_delivery(buyer=buyer)
        self.assertEqual('delivery_scope_unsupported', caught.exception.code)
        self.assertEqual((0, 0, 0), self.counts())
        self.assertFalse(self.output.exists())

    def test_changed_price_at_acquisition_is_never_paid(self):
        selected = self.add()
        acquire = self.buyer.acquire
        def changed(*args, **kwargs):
            self.seller.reprice(selected['id'], '1.0.0', 1, 99, 'race-price')
            return acquire(*args, **kwargs)
        with patch.object(self.buyer, 'acquire', side_effect=changed):
            result = self.run_delivery()
        self.assertEqual('payment_unavailable', result['error_code'])
        self.assertEqual('delivery_incomplete', result['status'])
        self.assertEqual((0, 0, 0), self.counts())
        self.assertFalse((self.output / 'bundle.json').exists())

    def test_suspension_between_resources_keeps_partial_evidence(self):
        self.add()
        second = self.add('second')
        original = self.buyer.read
        def read_then_suspend(*args, **kwargs):
            result = original(*args, **kwargs)
            self.reviewer.availability(second['id'], '1.0.0', 1, 'suspended', 'fixture', 'suspend')
            return result
        with patch.object(self.buyer, 'read', side_effect=read_then_suspend):
            result = self.run_delivery()
        self.assertEqual('plan_changed', result['error_code'])
        self.assertEqual((1, 1, 0), self.counts())
        self.assertEqual('delivered', result['resources'][0]['state'])
        self.assertFalse(result['delivery_completed'])
        self.assertFalse((self.output / 'bundle.json').exists())
        self.assertTrue((self.output / 'failure.json').is_file())

    def test_disk_failure_then_manual_retry_reuses_usage(self):
        self.add()
        original = delivery.write_new
        def fail_content(path, raw):
            if path.suffix == '.md':
                raise OSError('synthetic disk error')
            return original(path, raw)
        with patch.object(delivery, 'write_new', side_effect=fail_content):
            result = self.run_delivery()
        self.assertEqual('read', result['resources'][0]['state'])
        self.assertTrue(result['failure_record_saved'])
        self.assertEqual((1, 1, 0), self.counts())
        self.assertFalse((self.output / 'bundle.json').exists())
        self.assertEqual('delivered', self.run_delivery(self.root / 'retry')['status'])
        self.assertEqual((1, 1, 0), self.counts())

    def test_no_overwrite_or_linked_destination(self):
        self.add()
        self.output.mkdir()
        keep = self.output / 'resource-01.md'
        keep.write_text('user work', encoding='utf-8')
        with self.assertRaises(MarketError) as caught:
            self.run_delivery()
        self.assertEqual('output_exists', caught.exception.code)
        self.assertEqual('user work', keep.read_text(encoding='utf-8'))
        with patch.object(Path, 'is_junction', return_value=True):
            with self.assertRaises(delivery.ReleaseError):
                self.run_delivery(self.root / 'linked')
        self.assertEqual((0, 0, 0), self.counts())

    def test_corrupt_body_does_not_create_completion_record(self):
        self.add()
        original = self.buyer.read
        def corrupt(*args, **kwargs):
            return {**original(*args, **kwargs), 'content': 'corrupted'}
        with patch.object(self.buyer, 'read', side_effect=corrupt):
            result = self.run_delivery()
        self.assertEqual('integrity_failed', result['error_code'])
        self.assertFalse((self.output / 'bundle.json').exists())
        self.assertEqual((1, 1, 0), self.counts())

    def test_cli_isolated_and_missing_database_does_not_initialize(self):
        self.add()
        request = self.root / 'input.json'
        request.write_text(json.dumps(self.request), encoding='utf-8')
        script = Path(delivery.__file__).resolve()
        args = [sys.executable, '-I', '-B', str(script), '--principal', 'buyer',
                '--input', str(request), '--output', str(self.output), '--database']
        missing = self.root / 'missing.sqlite'
        process = subprocess.run([*args, str(missing)], capture_output=True, cwd=self.root, timeout=20)
        self.assertEqual(1, process.returncode)
        self.assertFalse(missing.exists())
        self.assertFalse(self.output.exists())
        process = subprocess.run([*args, str(self.database)], capture_output=True, cwd=self.root, timeout=20)
        self.assertEqual(0, process.returncode, process.stderr)
        self.assertEqual('delivered', json.loads(process.stdout)['status'])

    def test_unrecordable_disk_failure_is_explicit_and_does_not_hide_read(self):
        self.add()
        with patch.object(delivery, 'write_new', side_effect=OSError('fixture')):
            result = self.run_delivery()
        self.assertFalse(result['failure_record_saved'])
        self.assertFalse(result['delivery_completed'])
        self.assertEqual((1, 1, 0), self.counts())

    def test_failed_completion_record_never_returns_success(self):
        self.add()
        original = delivery.write_new
        def fail_manifest(path, raw):
            if path.name == 'bundle.json':
                path.write_bytes(raw[:50])  # Simulate an interrupted final write.
                raise OSError('fixture')
            return original(path, raw)
        with patch.object(delivery, 'write_new', side_effect=fail_manifest):
            result = self.run_delivery()
        self.assertEqual('delivery_incomplete', result['status'])
        self.assertFalse(result['delivery_completed'])
        self.assertTrue(result['failure_record_saved'])
        self.assertEqual('delivered', result['resources'][0]['state'])
        self.assertEqual((1, 1, 0), self.counts())

    def test_cli_respects_recovery_gate_before_delivery(self):
        self.add()
        request = self.root / 'input.json'
        request.write_text(json.dumps(self.request), encoding='utf-8')
        with self.buyer.store.connect(write=True) as db:
            db.execute("INSERT INTO meta(key,value) VALUES ('recovery_state','pending')")
        process = subprocess.run([sys.executable, '-I', '-B', str(Path(delivery.__file__).resolve()),
                                  '--database', str(self.database), '--principal', 'buyer',
                                  '--input', str(request), '--output', str(self.output)],
                                 capture_output=True, cwd=self.root, timeout=20)
        self.assertEqual(1, process.returncode)
        self.assertEqual('recovery_required', json.loads(process.stdout)['error_code'])
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
