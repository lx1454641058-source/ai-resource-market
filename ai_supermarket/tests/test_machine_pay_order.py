"""来源：本项目原创。真实报价账本的付款前绑定检查；不模拟支付宝成功。"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

from ai_supermarket.common import MarketError, compact
from ai_supermarket.contracts import HostPolicy
from ai_supermarket.examples.machine_pay_order import prepare_order, recheck_order
from ai_supermarket.tests import test_exchange as fixture


class MachinePayOrderTests(unittest.TestCase):
    setUp = fixture.ExchangeTests.setUp
    actor = fixture.ExchangeTests.actor
    draft = fixture.ExchangeTests.draft
    publish = fixture.ExchangeTests.publish
    count = fixture.ExchangeTests.count
    assert_code = fixture.ExchangeTests.assert_code

    def quote(self, price=100, purpose='personal_use', slug='brief'):
        self.resource = self.publish(price=price, slug=slug)
        detail = self.buyer.resolve(self.resource, '1.0.0', purpose)
        self.digest = detail['release_digest']
        self.quoted = self.buyer.quote(self.resource, '1.0.0', purpose, 'quote-main')
        self.merchant = {'market_id': 'test-market', 'market_seller': 'vendor',
            'seller_id': 'fixture-seller', 'app_id': 'fixture-app', 'service_id': 'fixture-service',
            'environment': 'sandbox'}
        return self.prepare()

    def prepare(self):
        return prepare_order(self.buyer, self.quoted['quote_id'], self.digest, self.merchant)

    def test_freezes_actual_quote_without_amount_guess_payment_or_database_mutation(self):
        prepared = self.quote()
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        again = self.prepare()
        self.assertEqual(prepared, again)
        self.assertEqual(100, prepared['binding']['price_cents'])
        self.assertIsNone(prepared['channel_amount'])
        self.assertIsNone(prepared['payment_needed_header'])
        self.assertFalse(prepared['payment_authorized'])
        self.assertEqual(0, self.count('acquisitions'))
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).hexdigest())
        paid = self.buyer.acquire(self.resource, '1.0.0', 'still-blocked', 'personal_use',
                                  self.quoted['quote_id'], self.digest)
        self.assertEqual('payment_unavailable', paid['status'])

    def test_short_channel_ids_do_not_truncate_long_market_resource(self):
        prepared = self.quote(slug='a'*48)
        self.assertGreater(len(self.resource), 32)
        self.assertEqual(self.resource, prepared['binding']['resource'])
        for value in prepared['channel_identifiers'].values():
            self.assertEqual(32, len(value))
        self.assertNotEqual(*prepared['channel_identifiers'].values())

    def test_buyer_and_role_binding(self):
        self.quote()
        self.assert_code('quote_not_found', prepare_order, self.actor('bob', 'buyer'),
                         self.quoted['quote_id'], self.digest, self.merchant)
        self.assert_code('forbidden', prepare_order, self.seller,
                         self.quoted['quote_id'], self.digest, self.merchant)

    def test_quote_expiration_boundary_and_clock_rollback(self):
        self.quote()
        for time in (999, 1300, 1301):
            self.now[0] = time
            self.assert_code('quote_expired', self.prepare)

    def test_free_resource_does_not_prepare_payment(self):
        self.assert_code('invalid_params', self.quote, 0)

    def test_seller_mapping_mismatch(self):
        self.quote()
        self.merchant['market_seller'] = 'another-vendor'
        self.assert_code('seller_mismatch', self.prepare)

    def test_price_change_requires_new_quote_not_silent_new_amount(self):
        self.quote()
        self.seller.reprice(self.resource, '1.0.0', 1, 200, 'new-price')
        self.assert_code('quote_changed', self.prepare)

    def test_withdrawal_or_suspension_prevents_new_order(self):
        self.quote()
        self.seller.availability(self.resource, '1.0.0', 1, 'withdrawn', 'fixture', 'withdraw')
        with self.assertRaises(MarketError):
            self.prepare()

    def test_wrong_original_release_digest(self):
        self.quote()
        self.digest = '0'*64
        self.assert_code('release_changed', self.prepare)

    def test_same_quote_cannot_rebind_channel_account_or_environment(self):
        original = self.quote()
        for field in ('market_id', 'seller_id', 'app_id', 'service_id', 'environment'):
            changed = dict(self.merchant)
            changed[field] = 'production' if field == 'environment' else 'other'
            self.assert_code('order_changed', recheck_order, self.buyer, original, changed)

    def test_tampered_preparation_amount_or_authorization_rejected(self):
        original = self.quote()
        for field in ('price', 'authorized', 'channel_id'):
            value = deepcopy(original)
            if field == 'price':
                value['binding']['price_cents'] = 1
            elif field == 'authorized':
                value['payment_authorized'] = True
            else:
                value['channel_identifiers']['out_trade_no'] = 'x'*32
            self.assert_code('order_changed', recheck_order, self.buyer, value, self.merchant)

    def test_existing_rights_reused_after_free_resource_repriced(self):
        self.resource = self.publish(price=0)
        self.buyer.acquire(self.resource, '1.0.0', 'free-first', 'personal_use')
        self.seller.reprice(self.resource, '1.0.0', 1, 100, 'charge-new-buyers')
        self.digest = self.buyer.resolve(self.resource, '1.0.0', 'personal_use')['release_digest']
        self.quoted = self.buyer.quote(self.resource, '1.0.0', 'personal_use', 'new-quote')
        self.merchant = {'market_id': 'test-market', 'market_seller': 'vendor', 'seller_id': 'test',
                         'app_id': 'test', 'service_id': 'test', 'environment': 'sandbox'}
        self.assert_code('already_owned', self.prepare)

    def test_host_capability_required_before_preparing_payment(self):
        self.quote()
        self.buyer.host_policy = HostPolicy(capabilities=frozenset())
        self.assert_code('incompatible', self.prepare)

    def test_json_key_order_does_not_change_recovered_identity(self):
        prepared = self.quote()
        retained = json.loads(json.dumps(prepared, sort_keys=True))
        merchant = dict(reversed(list(self.merchant.items())))
        self.assertEqual(prepared, recheck_order(self.buyer, retained, merchant))

    def test_new_process_reproduces_same_order_identifiers(self):
        prepared = self.quote()
        path = self.database.parent/'prepared.json'
        path.write_text(compact(prepared), encoding='utf-8')
        code = '''
import json,sys
from pathlib import Path
from ai_supermarket.common import compact
from ai_supermarket.exchange import Exchange
from ai_supermarket.examples.machine_pay_order import recheck_order
p=json.loads(Path(sys.argv[2]).read_bytes())
m=Exchange(sys.argv[1],'alice','buyer',clock=lambda:1001)
print(compact(recheck_order(m,p,p['binding']['merchant'])['channel_identifiers']))
'''
        run = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', code, str(self.database), str(path)],
                             cwd=Path(__file__).resolve().parents[2], capture_output=True, timeout=10)
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual(prepared['channel_identifiers'], json.loads(run.stdout))


if __name__ == '__main__':
    unittest.main()
