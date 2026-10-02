"""本项目原创。外部版本信任反馈引出的交付中断与精确版本回归。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_supermarket.common import MarketError
from ai_supermarket.exchange import Exchange
from ai_supermarket.tests.test_exchange import CHECKS, CONTENT, listing
from tools.market_bundle_check import check_bundle
from tools.market_deliver import deliver


class VersionRecoveryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.database = self.root / 'market.sqlite'
        self.buyer = Exchange(self.database, 'buyer', 'buyer')
        self.seller = Exchange(self.database, 'vendor', 'seller')
        self.reviewer = Exchange(self.database, 'reviewer', 'reviewer')
        self.publish('1.0.0', CONTENT)
        self.ref = {'id': 'vendor:brief', 'version': '1.0.0'}
        self.request = {'task_id': 'fixed-task', 'purpose': 'personal_use',
            'needs': [{'id': 'brief', 'query': '产品简报', 'kind': 'template', 'required': True}],
            'selections': {'brief': self.ref}}

    def publish(self, version, content):
        self.seller.draft('brief', version, listing(), content, 'draft-' + version)
        self.seller.submit('vendor:brief', version, 1, 'submit-' + version)
        self.reviewer.review('vendor:brief', version, 1, 'approve', CHECKS,
                             '本项目原创工程样例，不是外部审核', 'review-' + version)

    def counts(self):
        with self.buyer.store.connect() as db:
            return tuple(db.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]
                         for name in ('acquisitions', 'usage_events'))

    def assert_delivered(self, result, folder):
        self.assertEqual('delivered', result['status'])
        bundle = json.loads((folder / 'bundle.json').read_bytes())
        row = bundle['resources'][0]
        self.assertEqual('1.0.0', row['version'])
        self.assertEqual(CONTENT.encode('utf-8'), (folder / row['path']).read_bytes())
        checked = check_bundle(self.buyer, folder, result['manifest_sha256'], 'fixed-task', 'personal_use')
        self.assertEqual('verified_for_read', checked['status'])
        return row

    def test_new_version_between_discovery_and_delivery_does_not_replace_selection(self):
        found = self.buyer.search('产品简报', purpose='personal_use')
        self.assertEqual('1.0.0', found['items'][0]['version'])
        self.buyer.resolve(**self.ref, purpose='personal_use')
        self.publish('1.1.0', '# 新版本\n不同正文，不能替代调用方已选版本。\n')
        folder = self.root / 'delivery'
        row = self.assert_delivered(deliver(self.buyer, self.request, folder), folder)
        acquired = self.buyer.status(row['acquisition_id'])
        self.assertEqual('1.0.0', acquired['version'])
        self.assertEqual((1, 1), self.counts())

    def test_approved_version_cannot_change_contract_or_content_via_revision(self):
        metadata = listing()
        metadata['usage_contract']['permissions'] = ['file_read']
        before = self.seller.inspect_submission(**self.ref)
        with self.assertRaises(MarketError) as caught:
            self.seller.revise(**self.ref, revision=1, metadata=metadata,
                               content='# changed', request_id='revise-approved')
        self.assertEqual('invalid_state', caught.exception.code)
        self.assertEqual(before, self.seller.inspect_submission(**self.ref))
        self.assertEqual((0, 0), self.counts())

    def test_acquire_committed_but_reply_lost_recovery_reuses_same_acquisition(self):
        original = self.buyer.acquire
        recorded = []
        def lost_reply(*args, **kwargs):
            recorded.append(original(*args, **kwargs))
            raise TimeoutError('synthetic lost acquisition response after commit')
        failed = self.root / 'failed'
        with patch.object(self.buyer, 'acquire', side_effect=lost_reply):
            result = deliver(self.buyer, self.request, failed)
        self.assertEqual('delivery_incomplete', result['status'])
        self.assertEqual((1, 0), self.counts())
        self.assertFalse((failed / 'bundle.json').exists())
        preserved = (failed / 'failure.json').read_bytes()
        retry = self.root / 'retry'
        row = self.assert_delivered(deliver(self.buyer, self.request, retry), retry)
        self.assertEqual(recorded[0]['acquisition_id'], row['acquisition_id'])
        self.assertEqual((1, 1), self.counts())
        self.assertEqual(preserved, (failed / 'failure.json').read_bytes())

    def test_read_committed_but_reply_lost_recovery_reuses_same_usage(self):
        original = self.buyer.read
        recorded = []
        def lost_reply(*args, **kwargs):
            recorded.append(original(*args, **kwargs))
            raise TimeoutError('synthetic lost read response after commit')
        failed = self.root / 'failed'
        with patch.object(self.buyer, 'read', side_effect=lost_reply):
            result = deliver(self.buyer, self.request, failed)
        self.assertEqual('delivery_incomplete', result['status'])
        self.assertEqual((1, 1), self.counts())
        self.assertFalse((failed / 'bundle.json').exists())
        retry = self.root / 'retry'
        row = self.assert_delivered(deliver(self.buyer, self.request, retry), retry)
        self.assertEqual(recorded[0]['usage_id'], row['usage_id'])
        self.assertEqual((1, 1), self.counts())

    def test_suspension_after_lost_read_blocks_recovery_without_new_usage(self):
        original = self.buyer.read
        def lost_reply(*args, **kwargs):
            original(*args, **kwargs)
            raise TimeoutError('synthetic lost read response after commit')
        with patch.object(self.buyer, 'read', side_effect=lost_reply):
            result = deliver(self.buyer, self.request, self.root / 'failed')
        acquisition = result['resources'][0]['acquisition_id']
        self.reviewer.availability(**self.ref, offer_revision=1, state='suspended',
                                   reason='本地工程隔离样例', request_id='suspend')
        self.assertEqual('unavailable', self.buyer.status(acquisition)['status'])
        retry = self.root / 'retry'
        with patch.object(self.buyer, 'read', side_effect=AssertionError('should not read')):
            result = deliver(self.buyer, self.request, retry)
        self.assertEqual('needs_attention', result['status'])
        self.assertFalse(retry.exists())
        self.assertEqual((1, 1), self.counts())


if __name__ == '__main__':
    unittest.main()
