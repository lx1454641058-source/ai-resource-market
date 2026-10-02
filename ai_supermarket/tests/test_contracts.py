"""来源：本项目原创。商品契约、宿主能力/权限/费用与旧权益验收。"""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from ai_supermarket.common import MarketError, compact
from ai_supermarket.exchange import Exchange
from ai_supermarket.exchange_tools import ExchangeInterface
from ai_supermarket.host import TaskResources
from ai_supermarket.tests.test_exchange import listing, CONTENT, CHECKS


class ContractTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'market.sqlite'
        self.seller = Exchange(self.path, 'vendor', 'seller')
        self.reviewer = Exchange(self.path, 'operator', 'reviewer')
        self.buyer = Exchange(self.path, 'alice')

    def assert_code(self, code, method, *args, **kwargs):
        with self.assertRaises(MarketError) as cm:
            method(*args, **kwargs)
        self.assertEqual(code, cm.exception.code)

    def publish(self, metadata=None):
        self.seller.draft('brief', '1.0.0', metadata or listing(), CONTENT, 'draft')
        self.seller.submit('vendor:brief', '1.0.0', 1, 'submit')
        self.reviewer.review('vendor:brief', '1.0.0', 1, 'approve', CHECKS, '测试样例及契约已核对', 'review')
        return 'vendor:brief'

    def policy(self, **changes):
        base = {'capabilities': ['read_markdown'], 'permissions': [], 'dependencies': [], 'extra_budget_cents': 0}
        return {**base, **changes}

    def count(self):
        with self.buyer.store.connect() as db:
            return db.execute('SELECT COUNT(*) FROM acquisitions').fetchone()[0]

    def test_missing_contract_can_be_saved_but_cannot_be_submitted(self):
        meta = listing()
        del meta['usage_contract']
        self.seller.draft('brief', '1.0.0', meta, CONTENT, 'draft')
        self.assert_code('contract_required', self.seller.submit, 'vendor:brief', '1.0.0', 1, 'submit')
        self.assertEqual('draft', self.seller.inspect_submission('vendor:brief', '1.0.0')['status'])
        self.seller.revise('vendor:brief', '1.0.0', 1, listing(), CONTENT, 'revise')
        self.assertEqual('pending_review', self.seller.submit('vendor:brief', '1.0.0', 2, 'submit-fixed')['status'])

    def test_contract_and_sample_review_required_and_published_version_immutable(self):
        self.seller.draft('brief', '1.0.0', listing(), CONTENT, 'draft')
        self.seller.submit('vendor:brief', '1.0.0', 1, 'submit')
        self.assert_code('review_incomplete', self.reviewer.review, 'vendor:brief', '1.0.0', 1, 'approve',
                         {**CHECKS, 'contract_checked': False}, '尚未核对兼容契约', 'review')
        self.reviewer.review('vendor:brief', '1.0.0', 1, 'approve', CHECKS, '模拟检查完成', 'review-ok')
        changed = listing()
        changed['usage_contract']['permissions'] = ['network']
        self.assert_code('invalid_state', self.seller.revise, 'vendor:brief', '1.0.0', 1, changed, CONTENT, 'revise')

    def test_ready_static_content_does_not_authorize_execution(self):
        resource = self.publish()
        details = self.buyer.resolve(resource, '1.0.0')
        self.assertEqual('ready', details['compatibility']['status'])
        self.assertFalse(details['compatibility']['execution_authorized'])
        self.assertEqual('requested_not_granted', details['permission_semantics'])
        self.assertEqual(0, details['compatibility']['extra_max_cents'])
        host = TaskResources(self.buyer, 'task', '产品简报')
        self.assertEqual('read', host.use(resource, '1.0.0', 'use')['status'])

    def test_capability_and_permission_block_new_acquisition(self):
        meta = listing()
        meta['usage_contract']['capabilities'].append('web_search')
        meta['usage_contract']['permissions'] = ['network']
        resource = self.publish(meta)
        self.assert_code('incompatible', self.buyer.acquire, resource, '1.0.0', 'buy')
        capable = Exchange(self.path, 'alice', host_policy=self.policy(capabilities=['read_markdown', 'web_search']))
        self.assert_code('permission_required', capable.acquire, resource, '1.0.0', 'buy')
        self.assertEqual(0, self.count())
        permitted = Exchange(self.path, 'alice', host_policy=self.policy(
            capabilities=['read_markdown', 'web_search'], permissions=['network']))
        self.assertEqual('acquired', permitted.acquire(resource, '1.0.0', 'buy')['status'])
        # Revoked host permissions block use even though the bytes were previously acquired.
        host = TaskResources(self.buyer, 'task', '产品简报')
        self.assertEqual('unavailable', host.use(resource, '1.0.0', 'again')['status'])
        self.assertEqual(1, self.count())

    def test_dependency_presence_and_total_extra_budget_are_separate_checks(self):
        meta = listing()
        meta['usage_contract']['dependencies'] = [
            {'id': 'tool-a:1', 'license_name': 'MIT', 'max_cost_cents': 60},
            {'id': 'tool-b:1', 'license_name': 'MIT', 'max_cost_cents': 60}]
        resource = self.publish(meta)
        self.assert_code('dependency_missing', self.buyer.acquire, resource, '1.0.0', 'buy')
        low = Exchange(self.path, 'alice', host_policy=self.policy(dependencies=['tool-a:1', 'tool-b:1'], extra_budget_cents=100))
        self.assert_code('extra_budget_exceeded', low.acquire, resource, '1.0.0', 'buy')
        enough = Exchange(self.path, 'alice', host_policy=self.policy(dependencies=['tool-a:1', 'tool-b:1'], extra_budget_cents=120))
        self.assertEqual(120, enough.resolve(resource, '1.0.0')['compatibility']['extra_max_cents'])
        self.assertEqual('acquired', enough.acquire(resource, '1.0.0', 'buy')['status'])
        self.assertEqual(1, self.count())

    def test_unknown_dependency_price_is_not_zero_or_overridden_by_budget(self):
        meta = listing()
        meta['usage_contract']['dependencies'] = [{'id': 'tool:1', 'license_name': 'MIT', 'max_cost_cents': None}]
        resource = self.publish(meta)
        buyer = Exchange(self.path, 'alice', host_policy=self.policy(dependencies=['tool:1'], extra_budget_cents=100_000_000))
        self.assertIsNone(buyer.resolve(resource, '1.0.0')['compatibility']['extra_max_cents'])
        self.assert_code('cost_unknown', buyer.acquire, resource, '1.0.0', 'buy')
        self.assertEqual(0, self.count())

    def test_owned_withdrawn_version_passes_preflight_but_suspension_blocks_it(self):
        resource = self.publish()
        self.buyer.acquire(resource, '1.0.0', 'buy')
        self.seller.availability(resource, '1.0.0', 1, 'withdrawn', '停止新销售', 'withdraw')
        host = TaskResources(self.buyer, 'task', '产品简报')
        self.assertEqual('read', host.use(resource, '1.0.0', 'reuse')['status'])
        bob = Exchange(self.path, 'bob')
        self.assert_code('not_found', bob.check_compatibility, resource, '1.0.0')
        self.reviewer.availability(resource, '1.0.0', 2, 'suspended', '风险隔离', 'suspend')
        self.assert_code('not_found', host.use, resource, '1.0.0', 'reuse-2')

    def test_legacy_contract_missing_is_explicit_and_does_not_erase_owned_bytes(self):
        resource = self.publish()
        acquired = self.buyer.acquire(resource, '1.0.0', 'buy')
        # Emulate an already approved pre-contract row; no application mutation allows this edit.
        meta = listing()
        del meta['usage_contract']
        with self.buyer.store.connect(write=True) as db:
            db.execute('UPDATE submissions SET metadata=?', (compact(meta),))
        self.assertEqual('contract_missing', self.buyer.resolve(resource, '1.0.0')['compatibility']['status'])
        self.assert_code('contract_missing', Exchange(self.path, 'bob').acquire, resource, '1.0.0', 'buy')
        self.assertEqual('read', self.buyer.read(acquired['acquisition_id'], 'manual', 'read')['status'])
        self.assertEqual('unavailable', TaskResources(self.buyer, 'task', '产品简报').use(resource, '1.0.0', 'use')['status'])

    def test_model_cannot_inject_host_policy_or_invalid_contract_fields(self):
        resource = self.publish()
        interface = ExchangeInterface(self.buyer)
        reply = interface.call('market_acquire', {'id': resource, 'version': '1.0.0', 'request_id': 'buy',
                                                 'host_policy': self.policy(permissions=['network'])})
        self.assertEqual('invalid_params', reply['error_code'])
        seller = ExchangeInterface(self.seller)
        cases = [('version', True), ('version', 2), ('delivery', 'shell'), ('capabilities', []),
                 ('permissions', ['admin']), ('capabilities', ['read_markdown', 'read_markdown']),
                 ('sample', {'input': 'x', 'expected': 'y'}), ('dependencies', [{}])]
        for field, value in cases:
            meta = deepcopy(listing())
            meta['usage_contract'][field] = value
            reply = seller.call('seller_draft', {'slug': 'bad', 'version': '1.0.0', 'metadata': meta,
                                                'content': CONTENT, 'request_id': 'bad'})
            self.assertEqual('error', reply['status'], (field, value))

    def test_unapproved_license_and_duplicate_dependency_rejected(self):
        meta = listing()
        dependency = {'id': 'tool:1', 'license_name': 'GPL-3.0', 'max_cost_cents': 0}
        meta['usage_contract']['dependencies'] = [dependency]
        self.assert_code('unsupported_license', self.seller.draft, 'brief', '1.0.0', meta, CONTENT, 'draft')
        dependency['license_name'] = 'MIT'
        meta['usage_contract']['dependencies'].append(deepcopy(dependency))
        self.assert_code('invalid_params', self.seller.draft, 'brief', '1.0.0', meta, CONTENT, 'draft')


if __name__ == '__main__':
    unittest.main()
