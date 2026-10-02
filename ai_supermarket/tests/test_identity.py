"""来源：本项目原创。凭据身份绑定、撤销和市场角色隔离；只创建临时测试凭据。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from ai_supermarket.common import MarketError
from ai_supermarket.exchange import Exchange
from ai_supermarket.gateway import AuthenticatedGateway
from ai_supermarket.identity import Identities
from ai_supermarket.store import Store
from ai_supermarket.tests.test_exchange import listing, CONTENT, CHECKS


class IdentityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / 'identity.sqlite'
        self.market = self.root / 'exchange.sqlite'
        self.now = [1000.0]
        self.identities = Identities(self.path, clock=lambda: self.now[0])
        self.gateway = AuthenticatedGateway(self.market, self.path, clock=lambda: self.now[0])

    def token(self, principal, role, policy=None, ttl=60):
        self.identities.create_account(principal, role, policy)
        return self.identities.issue_key(principal, ttl)

    def assert_code(self, code, method, *args):
        with self.assertRaises(MarketError) as cm:
            method(*args)
        self.assertEqual(code, cm.exception.code)

    def test_credentials_survive_restart_and_do_not_appear_in_database_or_reply(self):
        issued = self.token('alice', 'buyer')
        auth = Identities(self.path, clock=lambda: self.now[0]).authenticate(issued['token'])
        self.assertEqual('alice', auth['principal'])
        self.assertEqual('buyer', auth['role'])
        with self.identities.store.connect() as db:
            dump = '\n'.join(db.iterdump())
        self.assertTrue(issued['token'] not in dump)
        self.assertTrue(issued['token'].split('.')[1] not in dump)
        tools = self.gateway.tool_list(issued['token'])
        self.assertEqual(7, len(tools['tools']))
        self.assertTrue('token' not in auth and 'digest' not in auth)

    def test_wrong_missing_expired_and_revoked_tokens_have_same_denial(self):
        issued = self.token('alice', 'buyer')
        bad_tokens = ['', 'alice', 'reviewer', None, issued['token'] + 'x', issued['token'][:-1] + '!']
        for token in bad_tokens:
            reply = self.gateway.call(token, 'market_search', {'q': '产品简报'})
            self.assertEqual('unauthenticated', reply['error_code'])
        self.assertFalse(self.market.exists())
        self.now[0] = 1060.0
        self.assert_code('unauthenticated', self.identities.authenticate, issued['token'])
        self.now[0] = 1001.0
        self.identities.revoke_key(issued['key_id'])
        self.assert_code('unauthenticated', self.identities.authenticate, issued['token'])

    def test_same_public_key_id_with_different_secret_never_authenticates(self):
        first = self.token('alice', 'buyer')
        second = self.token('bob', 'seller')
        swapped = first['key_id'] + '.' + second['token'].split('.')[1]
        self.assert_code('unauthenticated', self.identities.authenticate, swapped)
        self.assertEqual('bob', self.identities.authenticate(second['token'])['principal'])

    def test_disable_revokes_all_keys_and_duplicate_create_cannot_reenable(self):
        first = self.token('alice', 'buyer')
        second = self.identities.issue_key('alice')
        self.identities.disable_account('alice')
        self.identities.create_account('alice', 'buyer')
        for issued in (first, second):
            self.assert_code('unauthenticated', self.identities.authenticate, issued['token'])
        self.assert_code('account_unavailable', self.identities.issue_key, 'alice')

    def test_key_rotation_does_not_disable_other_active_key(self):
        first = self.token('alice', 'buyer')
        second = self.identities.issue_key('alice')
        self.identities.revoke_key(first['key_id'])
        self.identities.revoke_key(first['key_id'])
        self.assertEqual('alice', self.identities.authenticate(second['token'])['principal'])
        with self.identities.store.connect() as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM identity_audit WHERE action='key_revoked'").fetchone()[0])

    def test_duplicate_account_cannot_change_role_or_host_policy(self):
        self.token('alice', 'buyer')
        self.assert_code('account_conflict', self.identities.create_account, 'alice', 'reviewer')
        policy = {'capabilities': ['read_markdown'], 'permissions': ['network'], 'dependencies': [], 'extra_budget_cents': 0}
        self.assert_code('account_conflict', self.identities.create_account, 'alice', 'buyer', policy)
        self.assert_code('invalid_params', self.identities.issue_key, 'alice', True)
        self.assert_code('invalid_params', self.identities.issue_key, 'alice', 0)

    def test_concurrent_account_creation_does_not_create_multiple_principals(self):
        def create(_):
            return Identities(self.path).create_account('same', 'buyer')['status']
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(create, range(8)))
        self.assertEqual(1, results.count('created'))
        self.assertEqual(7, results.count('existing'))

    def test_gateway_seller_review_buyer_flow_and_other_seller_denied(self):
        seller = self.token('vendor', 'seller')['token']
        other = self.token('other', 'seller')['token']
        reviewer = self.token('operator', 'reviewer')['token']
        buyer = self.token('alice', 'buyer')['token']
        args = {'slug': 'brief', 'version': '1.0.0', 'metadata': listing(), 'content': CONTENT, 'request_id': 'draft'}
        ref = {'id': self.gateway.call(seller, 'seller_draft', args)['id'], 'version': '1.0.0'}
        self.assertEqual('not_found', self.gateway.call(other, 'seller_status', ref)['error_code'])
        self.assertEqual('pending_review', self.gateway.call(seller, 'seller_submit', {**ref, 'revision': 1, 'request_id': 'submit'})['status'])
        review = {**ref, 'revision': 1, 'decision': 'approve', 'checks': CHECKS, 'note': '本地测试检查', 'request_id': 'review'}
        self.assertEqual('unknown_tool', self.gateway.call(seller, 'review_decide', review)['error_code'])
        self.assertEqual('approved', self.gateway.call(reviewer, 'review_decide', review)['status'])
        acquired = self.gateway.call(buyer, 'market_acquire', {**ref, 'request_id': 'buy'})
        self.assertEqual('acquired', acquired['status'])
        self.assertEqual('read', self.gateway.call(buyer, 'market_read', {'acquisition_id': acquired['acquisition_id'],
                            'task_id': 'task', 'request_id': 'read'})['status'])
        self.identities.disable_account('alice')
        self.assertEqual('unauthenticated', self.gateway.call(buyer, 'market_status', {
            'acquisition_id': acquired['acquisition_id']})['error_code'])

    def test_gateway_does_not_accept_identity_or_issuer_tools_from_model(self):
        token = self.token('alice', 'buyer')['token']
        for field in ('principal', 'role', 'host_policy'):
            reply = self.gateway.call(token, 'market_search', {'q': '产品简报', field: 'reviewer'})
            self.assertEqual('invalid_params', reply['error_code'])
        for method in ('issue_key', 'create_account', 'disable_account', 'revoke_key'):
            self.assertEqual('unknown_tool', self.gateway.call(token, method, {})['error_code'])

    def test_identity_store_and_business_store_never_reuse_each_other(self):
        before = self.path.read_bytes()
        self.assert_code('wrong_database', Store, self.path)
        self.assert_code('wrong_database', Exchange, self.path, 'alice')
        self.assertEqual(before, self.path.read_bytes())
        Exchange(self.market, 'alice')
        self.assert_code('wrong_database', Identities, self.market)


if __name__ == '__main__':
    unittest.main()
