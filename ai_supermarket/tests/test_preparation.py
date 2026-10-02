"""来源：本项目原创。多资源任务的缺口、权益、总费用及只读边界。"""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_supermarket.common import MarketError
from ai_supermarket.exchange import Exchange
from ai_supermarket.preparation import ResourcePreparation
from ai_supermarket.tests.test_exchange import listing, CONTENT, CHECKS


class PreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / 'market.sqlite'
        self.buyer = Exchange(self.db, 'buyer')
        self.helper = ResourcePreparation(self.buyer)
        self.request = {'task_id': 'task-1', 'purpose': 'personal_use', 'needs': [
            {'id': 'brief', 'query': '产品简报', 'kind': 'template', 'required': True}]}

    def publish(self, slug='brief', price=0, metadata=None):
        seller = Exchange(self.db, 'seller', 'seller')
        seller.draft(slug, '1.0.0', metadata or listing(price), CONTENT, 'draft-' + slug)
        id = 'seller:' + slug
        seller.submit(id, '1.0.0', 1, 'submit-' + slug)
        Exchange(self.db, 'reviewer', 'reviewer').review(id, '1.0.0', 1, 'approve', CHECKS,
                                                       '原创测试样例，非真实外部审核', 'review-' + slug)
        return {'id': id, 'version': '1.0.0'}

    def selected(self, selection):
        return {**deepcopy(self.request), 'selections': {'brief': selection}}

    def test_discovery_deduplicates_search_without_loading_body_or_writing_ledger(self):
        self.publish()
        request = deepcopy(self.request)
        request['needs'].append({**request['needs'][0], 'id': 'second'})
        before = self.db.read_bytes()
        with patch.object(self.buyer, 'search', wraps=self.buyer.search) as search:
            found = self.helper.discover(request)
        self.assertEqual(1, search.call_count)
        self.assertEqual(2, len(found['needs']))
        self.assertEqual('ok', found['needs'][0]['status'])
        self.assertNotIn('content', found['needs'][0]['items'][0])
        self.assertEqual(before, self.db.read_bytes())

    def test_free_multi_need_plan_resolves_once_and_cannot_claim_completion(self):
        selected = self.publish()
        request = self.selected(selected)
        request['needs'].append({**request['needs'][0], 'id': 'second'})
        request['selections']['second'] = selected
        before = self.db.read_bytes()
        with patch.object(self.buyer, 'resolve', wraps=self.buyer.resolve) as resolve:
            result = self.helper.check(request)
        self.assertEqual(1, resolve.call_count)
        self.assertEqual('prechecked', result['status'])
        self.assertEqual(1, len(result['resources']))
        self.assertEqual(0, result['estimated_total_cents'])
        self.assertIn('license_text', result['resources'][0]['conditions'])
        for name in ('execution_authorized', 'task_completed', 'purchases_executed', 'content_loaded', 'budget_reserved'):
            self.assertFalse(result[name])
        self.assertEqual(before, self.db.read_bytes())

    def test_image_is_explicit_gap_and_optional_decline_is_allowed(self):
        selected = self.publish()
        request = self.selected(selected)
        request['needs'].append({'id': 'picture', 'query': '封面', 'kind': 'image', 'required': True})
        discovered = self.helper.discover({k: v for k, v in request.items() if k != 'selections'})
        self.assertEqual('no_match', discovered['needs'][1]['status'])
        request['selections']['picture'] = selected
        self.assertEqual('kind_mismatch', self.helper.check(request)['needs'][1]['status'])
        request['selections']['picture'] = None
        result = self.helper.check(request)
        self.assertEqual('needs_attention', result['status'])
        self.assertIsNone(result['estimated_total_cents'])
        request['needs'][1]['required'] = False
        self.assertEqual('prechecked', self.helper.check(request)['status'])

    def test_absence_error_and_unselected_are_distinct(self):
        self.assertEqual('no_match', self.helper.discover(self.request)['needs'][0]['status'])
        with patch.object(self.buyer, 'search', side_effect=MarketError('recovery_required', 'fixture')):
            row = self.helper.discover(self.request)['needs'][0]
            self.assertEqual('unavailable', row['status'])
            self.assertEqual('recovery_required', row['error_code'])
        result = self.helper.check(self.selected(None))
        self.assertEqual('missing_required', result['needs'][0]['status'])
        self.request['needs'][0]['required'] = False
        self.assertEqual('no_resources_selected', self.helper.check(self.selected(None))['status'])

    def test_paid_and_combined_budget_remain_blocked_without_purchases(self):
        first, second = self.publish(price=60), self.publish('second', 60)
        request = self.selected(first)
        request['needs'].append({**request['needs'][0], 'id': 'second'})
        request['selections']['second'] = second
        result = ResourcePreparation(self.buyer, 100).check(request)
        self.assertEqual(120, result['estimated_total_cents'])
        self.assertTrue(result['over_budget'])
        self.assertTrue(all(row['status'] == 'payment_unavailable' for row in result['needs']))
        with self.buyer.store.connect() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM acquisitions').fetchone()[0])

    def test_unknown_total_still_reports_known_budget_overrun(self):
        request = self.selected(self.publish(price=60))
        request['needs'].append({'id': 'picture', 'query': '封面', 'kind': 'image', 'required': True})
        request['selections']['picture'] = None
        result = ResourcePreparation(self.buyer, 20).check(request)
        self.assertIsNone(result['estimated_total_cents'])
        self.assertEqual(60, result['known_estimate_cents'])
        self.assertTrue(result['over_budget'])

    def test_owned_price_is_zero_but_dependency_cost_is_retained(self):
        selected = self.publish()
        owned = self.buyer.acquire(**selected, request_id='old', purpose='personal_use')
        Exchange(self.db, 'seller', 'seller').reprice(selected['id'], '1.0.0', 1, 500, 'price')
        result = self.helper.check(self.selected(selected))
        self.assertEqual(0, result['estimated_total_cents'])
        self.assertEqual(owned['acquisition_id'], result['resources'][0]['acquisition_id'])
        meta = listing()
        meta['usage_contract']['dependencies'] = [{'id': 'fixture-render', 'license_name': 'MIT', 'max_cost_cents': 40}]
        dependency = self.publish('dependency', metadata=meta)
        policy = {'capabilities': ['read_markdown'], 'permissions': [],
                  'dependencies': ['fixture-render'], 'extra_budget_cents': 100}
        buyer = Exchange(self.db, 'buyer', host_policy=policy)
        buyer.acquire(**dependency, request_id='dependency-old', purpose='personal_use')
        result = ResourcePreparation(buyer, 20).check(self.selected(dependency))
        self.assertEqual(40, result['estimated_total_cents'])
        self.assertTrue(result['over_budget'])

    def test_license_kind_and_changed_availability_are_rechecked(self):
        meta = listing()
        meta['allowed_uses'] = ['personal_use']
        selected = self.publish(metadata=meta)
        request = self.selected(selected)
        request['purpose'] = 'commercial_use'
        self.assertEqual('license_denied', self.helper.check(request)['needs'][0]['error_code'])
        request['purpose'] = 'personal_use'
        request['needs'][0]['kind'] = 'skill'
        self.assertEqual('kind_mismatch', self.helper.check(request)['needs'][0]['status'])
        request['needs'][0]['kind'] = 'template'
        Exchange(self.db, 'seller', 'seller').availability(selected['id'], '1.0.0', 1, 'withdrawn', 'fixture', 'withdraw')
        self.assertEqual('needs_attention', self.helper.check(request)['status'])

    def test_unknown_dependency_keeps_known_partial_estimate(self):
        meta = listing()
        meta['usage_contract']['dependencies'] = [
            {'id': 'known', 'license_name': 'MIT', 'max_cost_cents': 40},
            {'id': 'unknown', 'license_name': 'MIT', 'max_cost_cents': None}]
        selected = self.publish(metadata=meta)
        policy = {'capabilities': ['read_markdown'], 'permissions': [],
                  'dependencies': ['known', 'unknown'], 'extra_budget_cents': 100}
        buyer = Exchange(self.db, 'buyer', host_policy=policy)
        result = ResourcePreparation(buyer, 20).check(self.selected(selected))
        self.assertEqual('cost_unknown', result['needs'][0]['status'])
        self.assertIsNone(result['estimated_total_cents'])
        self.assertEqual(40, result['known_estimate_cents'])
        self.assertTrue(result['over_budget'])

    def test_invalid_requests_fail_before_any_search(self):
        for mutate in (lambda r: r['needs'].append(r['needs'][0]),
                       lambda r: r['needs'][0].update(required=1),
                       lambda r: r.update(permission='network'),
                       lambda r: r.update(needs=r['needs'] * 9)):
            request = deepcopy(self.request)
            mutate(request)
            with patch.object(self.buyer, 'search') as search, self.assertRaises(MarketError):
                self.helper.discover(request)
            search.assert_not_called()
        with self.assertRaises(MarketError):
            self.helper.check({**self.request, 'selections': {}})
        with self.assertRaises(MarketError):
            ResourcePreparation(self.buyer, True)

    def test_legacy_missing_contract_preserves_unknown_instead_of_crashing(self):
        selected = self.publish()
        details = self.buyer.resolve(**selected, purpose='personal_use')
        details['usage_contract'] = None
        details['compatibility'] = {'status': 'contract_missing', 'execution_authorized': False,
                                    'next_action': 'choose_declared_resource'}
        with patch.object(self.buyer, 'resolve', return_value=details):
            result = self.helper.check(self.selected(selected))
        self.assertEqual('contract_missing', result['needs'][0]['status'])
        self.assertIsNone(result['estimated_total_cents'])
        self.assertEqual('needs_attention', result['status'])

    def test_cli_refuses_missing_database_and_handles_host_policy_from_foreign_cwd(self):
        selected = self.publish()
        request_file = self.root / 'request.json'
        request_file.write_text(json.dumps(self.selected(selected)), encoding='utf-8')
        policy = self.root / 'policy.json'
        policy.write_text(json.dumps({'capabilities': ['read_markdown'], 'permissions': [],
                                      'dependencies': [], 'extra_budget_cents': 0}), encoding='utf-8')
        script = Path(__file__).resolve().parents[2] / 'tools/market_prepare.py'
        command = [sys.executable, '-I', '-B', str(script), 'check', '--principal', 'buyer',
                   '--input', str(request_file), '--host-policy', str(policy), '--database']
        before = self.db.read_bytes()
        execution = subprocess.run([*command, str(self.db)], cwd=self.root, capture_output=True, timeout=10)
        self.assertEqual(0, execution.returncode, execution.stderr)
        self.assertEqual('prechecked', json.loads(execution.stdout)['status'])
        self.assertEqual(before, self.db.read_bytes())
        missing = self.root / 'missing.sqlite'
        execution = subprocess.run([*command, str(missing)], cwd=self.root, capture_output=True, timeout=10)
        self.assertEqual(1, execution.returncode)
        self.assertFalse(missing.exists())
        missing.touch()
        execution = subprocess.run([*command, str(missing)], cwd=self.root, capture_output=True, timeout=10)
        self.assertEqual(1, execution.returncode)
        self.assertEqual(b'', missing.read_bytes())
        (self.root / 'restore-started.json').write_text('{}', encoding='utf-8')
        execution = subprocess.run([*command, str(self.db)], cwd=self.root, capture_output=True, timeout=10)
        self.assertEqual('recovery_required', json.loads(execution.stdout)['error_code'])
        self.assertEqual(before, self.db.read_bytes())


if __name__ == '__main__':
    unittest.main()
