"""来源：本项目原创。开放供给、准入、价格、权益与隔离的业务验收。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from ai_supermarket.commerce import fee_breakdown
from ai_supermarket.common import MarketError, compact
from ai_supermarket.exchange import Exchange
from ai_supermarket.host import TaskResources
from ai_supermarket.store import Store


def listing(price=0):
    return {'title': '测试用产品简报模板', 'summary': '生成可核对目标与风险的产品简报。',
            'kind': 'template', 'keywords': ['产品简报', 'brief'],
            'input': '产品目标与已知事实', 'output': '包含目标与风险的简报', 'check': '目标与风险两节存在',
            'license_name': 'LicenseRef-Test-Original',
            'license_text': '本测试资料由项目原创，仅用于本地工程验收。测试授权不构成对外商业许可。',
            'source_statement': '测试作者原创，无第三方素材。',
            'allowed_uses': ['personal_use', 'commercial_use'], 'price_cents': price,
            'usage_contract': {'version': 1, 'delivery': 'static_markdown', 'capabilities': ['read_markdown'],
                               'permissions': [], 'dependencies': [],
                               'sample': {'input': '制作一个产品简报', 'expected': '目标与风险两节', 'check': '两节非空'}}}


CONTENT = '# 产品简报\n\n## 目标\n填写目标。\n\n## 风险\n填写风险。\n'
CHECKS = {'rights_verified': True, 'sample_checked': True, 'content_checked': True, 'contract_checked': True}


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.database = Path(folder.name) / 'exchange.sqlite'
        self.now = [1000.0]
        self.seller = self.actor('vendor', 'seller')
        self.reviewer = self.actor('operator', 'reviewer')
        self.buyer = self.actor('alice', 'buyer')

    def actor(self, name, role):
        return Exchange(self.database, name, role, clock=lambda: self.now[0])

    def assert_code(self, code, method, *args, **kwargs):
        with self.assertRaises(MarketError) as error:
            method(*args, **kwargs)
        self.assertEqual(code, error.exception.code)

    def draft(self, price=0, version='1.0.0', slug='brief'):
        return self.seller.draft(slug, version, listing(price), CONTENT, 'draft-' + version + '-' + slug)

    def publish(self, price=0, version='1.0.0', slug='brief'):
        draft = self.draft(price, version, slug)
        self.seller.submit(draft['id'], version, 1, 'submit-' + version + '-' + slug)
        self.reviewer.review(draft['id'], version, 1, 'approve', CHECKS,
                             '已在原创测试样例范围核对来源、正文与格式，不是生产签核。', 'review-' + version + '-' + slug)
        return draft['id']

    def count(self, table):
        with self.buyer.store.connect() as db:
            return db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

    def test_seller_to_buyer_real_delivery_and_feedback(self):
        resource = self.publish()
        found = self.buyer.search('产品简报')
        self.assertEqual(resource, found['items'][0]['id'])
        self.assertNotIn('content', found['items'][0])
        detail = self.buyer.resolve(resource, '1.0.0')
        self.assertFalse(detail['training_allowed'])
        self.assertEqual('vendor', detail['seller'])
        acquired = self.buyer.acquire(resource, '1.0.0', 'acquire')
        read = self.buyer.read(acquired['acquisition_id'], 'task', 'read')
        self.assertEqual(CONTENT, read['content'])
        self.assertEqual('accepted', self.buyer.feedback(read['usage_id'], 'yes', 'pass')['status'])
        self.assertTrue(self.buyer.search('产品简报')['items'][0]['owned'])

    def test_draft_pending_and_rejected_never_enter_search(self):
        draft = self.draft()
        self.assertEqual('no_match', self.buyer.search('产品简报')['status'])
        self.seller.submit(draft['id'], '1.0.0', 1, 'submit')
        self.assertEqual('no_match', self.buyer.search('产品简报')['status'])
        self.reviewer.review(draft['id'], '1.0.0', 1, 'reject', {**CHECKS, 'rights_verified': False},
                             '补充来源证据', 'reject')
        self.assertEqual('no_match', self.buyer.search('产品简报')['status'])
        self.assert_code('not_found', self.buyer.resolve, draft['id'], '1.0.0')

    def test_review_requires_separation_evidence_and_current_revision(self):
        draft = self.draft()
        self.seller.submit(draft['id'], '1.0.0', 1, 'submit')
        self.assert_code('forbidden', self.seller.review, draft['id'], '1.0.0', 1, 'approve', CHECKS, '测试', 'r')
        self.assert_code('self_review', self.actor('vendor', 'reviewer').review,
                         draft['id'], '1.0.0', 1, 'approve', CHECKS, '测试', 'r')
        self.assert_code('review_incomplete', self.reviewer.review, draft['id'], '1.0.0', 1,
                         'approve', {**CHECKS, 'sample_checked': False}, '测试', 'r')
        self.assert_code('revision_conflict', self.reviewer.review, draft['id'], '1.0.0', 2,
                         'approve', CHECKS, '测试', 'r')

    def test_rejection_can_be_revised_approved_content_is_immutable(self):
        draft = self.draft()
        self.seller.submit(draft['id'], '1.0.0', 1, 'submit')
        self.reviewer.review(draft['id'], '1.0.0', 1, 'reject', CHECKS, '请补充', 'reject')
        revised = self.seller.revise(draft['id'], '1.0.0', 1, listing(), CONTENT + '\n新增说明', 'revise')
        self.assertEqual(2, revised['revision'])
        self.assert_code('revision_conflict', self.seller.submit, draft['id'], '1.0.0', 1, 'stale')
        self.seller.submit(draft['id'], '1.0.0', 2, 'resubmit')
        self.reviewer.review(draft['id'], '1.0.0', 2, 'approve', CHECKS, '修订已核验', 'approve')
        self.assert_code('invalid_state', self.seller.revise, draft['id'], '1.0.0', 2, listing(), CONTENT, 'rewrite')
        self.assert_code('version_exists', self.seller.draft, 'brief', '1.0.0', listing(), CONTENT, 'new-id')

    def test_other_seller_cannot_inspect_change_or_withdraw(self):
        resource = self.publish()
        other = self.actor('other', 'seller')
        self.assertEqual([], other.list_submissions()['items'])
        self.assert_code('not_found', other.inspect_submission, resource, '1.0.0')
        self.assert_code('not_found', other.reprice, resource, '1.0.0', 1, 99, 'price')
        self.assert_code('not_found', other.availability, resource, '1.0.0', 1, 'withdrawn', '停售', 'withdraw')

    def test_idempotent_mutations_and_conflicting_request_ids(self):
        a = self.draft()
        b = self.draft()
        self.assertTrue(b['replayed'])
        self.assertEqual(a['id'], b['id'])
        self.assertEqual(1, self.count('submissions'))
        self.assertEqual(1, self.count('exchange_audit'))
        self.assert_code('idempotency_conflict', self.seller.draft, 'brief', '1.0.0', listing(100), CONTENT, 'draft-1.0.0-brief')

    def test_concurrent_draft_and_acquire_only_create_one_record(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: self.draft(), range(8)))
        self.assertEqual(1, len({r['id'] for r in replies}))
        self.seller.submit('vendor:brief', '1.0.0', 1, 'submit')
        self.reviewer.review('vendor:brief', '1.0.0', 1, 'approve', CHECKS, '原创检查', 'approve')
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: self.buyer.acquire('vendor:brief', '1.0.0', 'one'), range(8)))
        self.assertEqual(1, len({r['acquisition_id'] for r in replies}))
        self.assertEqual(1, self.count('acquisitions'))

    def test_self_pricing_free_and_paid_not_bound_to_reference(self):
        resource = self.publish(99_999_999)
        detail = self.buyer.resolve(resource, '1.0.0')
        self.assertEqual(99_999_999, detail['price_cents'])
        reference = self.seller.pricing_reference()
        self.assertFalse(reference['mandatory'])
        self.assertIsNone(reference['reference_cents'])
        self.assertEqual(0, reference['stall_fee_cents'])
        self.assertEqual('payment_unavailable', self.buyer.acquire(resource, '1.0.0', 'paid')['status'])
        self.assertEqual(0, self.count('acquisitions'))
        self.seller.reprice(resource, '1.0.0', 1, 0, 'free')
        self.assertEqual('acquired', self.buyer.acquire(resource, '1.0.0', 'free-acquire')['status'])

    def test_quotes_are_bound_expire_and_repricing_invalidates_them(self):
        resource = self.publish()
        quote = self.buyer.quote(resource, '1.0.0', 'commercial_use', 'quote')
        self.assertEqual(0, quote['fee_policy']['platform_cents'])
        other = self.actor('bob', 'buyer')
        self.assert_code('invalid_quote', other.acquire, resource, '1.0.0', 'a', quote_id=quote['quote_id'])
        self.now[0] += 301
        self.assert_code('quote_expired', self.buyer.acquire, resource, '1.0.0', 'a', quote_id=quote['quote_id'])
        fresh = self.buyer.quote(resource, '1.0.0', 'commercial_use', 'fresh')
        self.seller.reprice(resource, '1.0.0', 1, 100, 'price')
        self.assert_code('quote_changed', self.buyer.acquire, resource, '1.0.0', 'a', quote_id=fresh['quote_id'])

    def test_repricing_invalidates_search_cache_but_preserves_owned_rights(self):
        resource = self.publish()
        acquired = self.buyer.acquire(resource, '1.0.0', 'a')
        self.buyer.search('产品简报')
        self.assertTrue(self.buyer.search('产品简报')['cache_hit'])
        self.seller.reprice(resource, '1.0.0', 1, 1000, 'price')
        found = self.buyer.search('产品简报')
        self.assertFalse(found['cache_hit'])
        self.assertEqual(1000, found['items'][0]['price_cents'])
        resolved = self.buyer.resolve(resource, '1.0.0')
        self.assertTrue(resolved['owned'])
        self.assertEqual('read', resolved['next_action'])
        self.assertEqual(acquired['acquisition_id'], resolved['acquisition_id'])
        self.assertEqual(CONTENT, self.buyer.read(acquired['acquisition_id'], 'task', 'read')['content'])
        self.assertEqual('acquired', self.buyer.acquire(resource, '1.0.0', 'reuse')['status'])

    def test_withdrawal_preserves_owned_reading_but_blocks_new_buyers(self):
        resource = self.publish()
        acquired = self.buyer.acquire(resource, '1.0.0', 'a')
        self.buyer.search('产品简报')
        self.seller.availability(resource, '1.0.0', 1, 'withdrawn', '停止新销售', 'withdraw')
        self.assertEqual('no_match', self.buyer.search('产品简报')['status'])
        self.assert_code('not_found', self.actor('bob', 'buyer').acquire, resource, '1.0.0', 'a')
        self.assertEqual(CONTENT, self.buyer.read(acquired['acquisition_id'], 'task', 'read')['content'])
        self.assertEqual('acquired', self.buyer.status(acquired['acquisition_id'])['status'])

    def test_suspension_stops_existing_delivery_and_seller_cannot_override(self):
        resource = self.publish()
        acquired = self.buyer.acquire(resource, '1.0.0', 'a')
        self.reviewer.availability(resource, '1.0.0', 1, 'suspended', '待核查内容', 'suspend')
        self.assert_code('not_found', self.buyer.read, acquired['acquisition_id'], 'task', 'read')
        self.assertEqual('unavailable', self.buyer.status(acquired['acquisition_id'])['status'])
        self.assert_code('forbidden', self.seller.availability, resource, '1.0.0', 2, 'withdrawn', '绕过隔离', 'withdraw')
        self.assert_code('forbidden', self.seller.availability, resource, '1.0.0', 2, 'active', '恢复', 'restore')
        self.reviewer.availability(resource, '1.0.0', 2, 'active', '已核查恢复', 'restore')
        self.assertEqual('acquired', self.buyer.status(acquired['acquisition_id'])['status'])

    def test_unsupported_permissions_licenses_and_bad_content_rejected(self):
        for changes in [{'allowed_uses': ['training']}, {'permissions': ['shell']},
                        {'price_cents': True}, {'price_cents': -1}, {'license_name': 'AGPL-3.0'}]:
            with self.subTest(changes=changes), self.assertRaises(MarketError):
                self.seller.draft('bad', '1.0.0', {**listing(), **changes}, CONTENT, 'bad')
        self.assert_code('invalid_params', self.seller.draft, '../outside', '1.0.0', listing(), CONTENT, 'bad')
        self.assert_code('invalid_params', self.seller.draft, 'big', '1.0.0', listing(), '汉' * 9000, 'big')
        self.assertEqual(0, self.count('submissions'))

    def test_purpose_and_acquisition_ownership_checked(self):
        meta = {**listing(), 'allowed_uses': ['personal_use']}
        self.seller.draft('personal', '1.0.0', meta, CONTENT, 'draft')
        self.seller.submit('vendor:personal', '1.0.0', 1, 'submit')
        self.reviewer.review('vendor:personal', '1.0.0', 1, 'approve', CHECKS, '已核验个人用途', 'review')
        self.assertEqual('no_match', self.buyer.search('产品简报')['status'])
        self.assert_code('license_denied', self.buyer.acquire, 'vendor:personal', '1.0.0', 'a')
        acquired = self.buyer.acquire('vendor:personal', '1.0.0', 'a', purpose='personal_use')
        self.assert_code('not_found', self.actor('bob', 'buyer').read, acquired['acquisition_id'], 't', 'r')

    def test_new_version_does_not_rewrite_old_content(self):
        resource = self.publish()
        original = self.buyer.acquire(resource, '1.0.0', 'a')
        self.publish(version='2.0.0')
        self.assertEqual(CONTENT, self.buyer.read(original['acquisition_id'], 't', 'r')['content'])
        self.assertEqual(['2.0.0'], [i['version'] for i in self.buyer.search('产品简报')['items']])

    def test_many_old_versions_do_not_starve_other_products(self):
        for version in ['1.0.0', '2.0.0', '3.0.0', '4.0.0', '5.0.0', '10.0.0']:
            self.publish(version=version)
        self.publish(slug='another')
        found = self.buyer.search('产品简报')['items']
        self.assertEqual(2, len(found))
        self.assertEqual('10.0.0', next(i for i in found if i['id'] == 'vendor:brief')['version'])

    def test_host_carries_requested_purpose_from_search_to_acquire(self):
        resource = self.publish()
        host = TaskResources(self.buyer, 'task-personal', '产品简报', purpose='personal_use')
        self.assertEqual('ok', host.candidates['status'])
        self.assertEqual(CONTENT, host.use(resource, '1.0.0', 'use')['content'])
        with self.buyer.store.connect() as db:
            self.assertEqual('personal_use', db.execute('SELECT purpose FROM acquisitions').fetchone()[0])

    def test_reviewer_cannot_restore_their_own_seller_product(self):
        resource = self.publish()
        self.reviewer.availability(resource, '1.0.0', 1, 'suspended', '待核查', 'suspend')
        self.assert_code('self_review', self.actor('vendor', 'reviewer').availability,
                         resource, '1.0.0', 2, 'active', '自行恢复', 'restore')

    def test_stale_concurrent_offer_updates_do_not_overwrite(self):
        resource = self.publish()
        self.seller.reprice(resource, '1.0.0', 1, 500, 'first')
        self.assert_code('revision_conflict', self.seller.reprice, resource, '1.0.0', 1, 600, 'stale')
        self.assertEqual(500, self.buyer.resolve(resource, '1.0.0')['price_cents'])

    def test_database_types_are_not_migrated_or_confused(self):
        self.assert_code('wrong_database', Store, self.database)
        legacy = self.database.with_name('legacy.sqlite')
        Store(legacy)
        self.assert_code('wrong_database', Exchange, legacy, 'a')
        Store(legacy)

    def test_audit_and_listing_do_not_include_content(self):
        self.publish()
        listed = self.seller.list_submissions(limit=1)
        self.assertNotIn(CONTENT, compact(listed))
        with self.buyer.store.connect() as db:
            audit = [dict(row) for row in db.execute('SELECT * FROM exchange_audit')]
        self.assertNotIn(CONTENT, compact(audit))


class FeeTests(unittest.TestCase):
    def test_exact_eight_percent_with_conservation_and_small_amount_rounding(self):
        for paid in [0, 1, 4, 5, 6, 9, 10, 99, 1000, 100_000_000]:
            fee = fee_breakdown(paid)
            self.assertEqual(paid, fee['platform_cents'] + fee['seller_before_other_fees_cents'])
        self.assertEqual(80, fee_breakdown(1000)['platform_cents'])
        self.assertEqual(920, fee_breakdown(1000)['seller_before_other_fees_cents'])

    def test_cumulative_partial_refunds_and_full_refund(self):
        amounts = [fee_breakdown(1000, r)['platform_cents'] for r in [0, 101, 302, 999, 1000]]
        returned = sum(a - b for a, b in zip(amounts, amounts[1:]))
        self.assertEqual(80, returned)
        self.assertEqual(0, fee_breakdown(1000, 1000)['net_cents'])
        for paid, refund in [(1, 2), (True, 0), (-1, 0), (10, -1)]:
            with self.assertRaises(MarketError):
                fee_breakdown(paid, refund)


if __name__ == '__main__':
    unittest.main()
