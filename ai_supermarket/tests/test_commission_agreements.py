"""来源：本项目原创。真实隔离账本中的协议、报价与订单准备，不付款。"""
from datetime import datetime
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from ai_supermarket._commission import _fee_amounts
from ai_supermarket.commission_agreements import (
    AGREEMENT_POLICY, BUSINESS_ZONE, record_agreement, quote_fee, _prefix)
from ai_supermarket.common import MarketError, compact
from ai_supermarket.examples.machine_pay_order import prepare_order, recheck_order
from ai_supermarket.exchange_tools import ExchangeInterface
from ai_supermarket.tests import test_exchange as fixture


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=BUSINESS_ZONE).timestamp()


class AgreementTests(unittest.TestCase):
    setUp = fixture.ExchangeTests.setUp
    actor = fixture.ExchangeTests.actor
    draft = fixture.ExchangeTests.draft
    publish = fixture.ExchangeTests.publish
    count = fixture.ExchangeTests.count
    assert_code = fixture.ExchangeTests.assert_code

    def agreement(self, tier='introductory', **args):
        # Preserve original contract fixtures to exercise legacy compatibility.
        options = {'terms_version': 1}
        if tier == 'introductory': options['starts_on'] = '2026-09-23'
        options.update(args)
        return record_agreement(self.buyer.store, 'vendor', 'fixture-agreement', tier,
                                'private-review:fixture-only', **options)

    def quote(self, request='q'):
        return self.buyer.quote(self.resource, '1.0.0', 'personal_use', request)

    def start(self):
        self.now[0] = stamp('2026-09-23T12:00:00')
        self.resource = self.publish(price=10001)
        self.digest = self.buyer.resolve(self.resource, '1.0.0', 'personal_use')['release_digest']
        self.merchant = {'market_id': 'fixture', 'market_seller': 'vendor',
            'seller_id': 'fixture-seller', 'app_id': 'fixture-app', 'service_id': 'fixture-service',
            'environment': 'sandbox'}

    def prepare(self, quoted):
        return prepare_order(self.buyer, quoted['quote_id'], self.digest, self.merchant)

    def test_agreement_used_by_details_quote_and_order_preparation(self):
        self.start(); self.agreement()
        detail = self.buyer.resolve(self.resource, '1.0.0', 'personal_use')
        quoted = self.quote()
        self.assertEqual(500, detail['fee_policy']['commission_bps'])
        self.assertEqual(detail['fee_policy'], quoted['fee_policy'])
        prepared = self.prepare(quoted)
        self.assertEqual(quoted['fee_policy'], prepared['binding']['fee_policy'])
        self.assertEqual(500, prepared['binding']['fee_policy']['platform_cents'])
        self.assertEqual('machine-pay-order-preparation-v2', prepared['binding']['format'])
        self.assertNotIn('private-review', compact([detail, quoted, prepared]))
        self.assertFalse(prepared['payment_authorized'])
        self.assertEqual('payment_unavailable', self.buyer.acquire(self.resource,'1.0.0','a',
            'personal_use',quoted['quote_id'],self.digest)['status'])
        self.assertEqual(0, self.count('acquisitions'))

    def test_revision_invalidates_unconfirmed_order_but_history_keeps_original_rate(self):
        self.start(); self.agreement(); quoted = self.quote(); original = self.prepare(quoted)
        self.agreement('negotiated', expected_revision=1, starts_on='2026-09-01',
                       ends_on='2027-09-01', negotiated_bps=300)
        self.assert_code('policy_changed', self.prepare, quoted)
        self.assert_code('policy_changed', recheck_order, self.buyer, original, self.merchant)
        self.assertEqual({**quoted, 'replayed': True}, self.quote())
        self.assertEqual(300, self.quote('new')['fee_policy']['commission_bps'])
        with self.buyer.store.connect() as db:
            row = db.execute('SELECT * FROM quotes WHERE id=?',(quoted['quote_id'],)).fetchone()
            old = quote_fee(db,row,'vendor')
        self.assertEqual(500,old['commission_bps'])
        self.assertEqual((5000,250,4750), _fee_amounts(10001,5001,old['commission_bps']))

    def test_expiry_shortens_quote_and_new_quote_returns_to_standard(self):
        self.start(); self.agreement()
        self.now[0] = stamp('2027-03-22T23:59:30')
        quoted = self.quote()
        self.assertEqual(stamp('2027-03-23T00:00:00'),quoted['expires_at'])
        self.assertEqual(500,quoted['fee_policy']['commission_bps'])
        self.now[0] = quoted['expires_at']
        self.assert_code('quote_expired', self.prepare, quoted)
        self.assertEqual(1000,self.quote('after')['fee_policy']['commission_bps'])

    def test_future_discount_start_also_bounds_standard_quote(self):
        self.start(); self.agreement()
        self.now[0] = stamp('2026-09-22T23:59:59')
        quoted = self.quote()
        self.assertEqual(1000,quoted['fee_policy']['commission_bps'])
        self.assertEqual(self.now[0]+1,quoted['expires_at'])
        self.now[0] += 1
        self.assertEqual(500,self.quote('started')['fee_policy']['commission_bps'])

    def test_new_agreement_invalidates_legacy_unconfirmed_quote(self):
        self.start(); quoted = self.quote(); self.prepare(quoted)
        self.agreement()
        self.assert_code('policy_changed', self.prepare, quoted)
        self.assert_code('policy_changed',self.buyer.acquire,self.resource,'1.0.0','a',
                         'personal_use',quoted['quote_id'],self.digest)

    def test_revision_replay_does_not_rewind_and_conflict_is_refused(self):
        self.start(); first = self.agreement()
        self.assertEqual(first,self.agreement())
        self.agreement('standard',expected_revision=1)
        self.assertEqual(first,self.agreement())
        self.assertEqual(2,self.quote()['fee_policy']['agreement']['revision'])
        self.assert_code('agreement_conflict',self.agreement,starts_on='2026-10-01')
        self.assert_code('revision_conflict',self.agreement,expected_revision=3)

    def test_invalid_terms_leave_no_records(self):
        for args in [{'starts_on':'tomorrow'},{'starts_on':20260923},
                     {'starts_on':'20260923'},{'negotiated_bps':300}]:
            with self.subTest(args=args), self.assertRaises(MarketError):
                self.agreement(**args)
        self.assert_code('invalid_params',self.agreement,'negotiated',starts_on='2026-01-01',
                         ends_on='2027-01-01',negotiated_bps=501)
        with self.buyer.store.connect() as db:
            self.assertEqual(0,db.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'commission_agreement:%'").fetchone()[0])

    def test_corrupted_quote_snapshot_stops_order_preparation(self):
        self.start(); self.agreement(); quoted = self.quote()
        with self.buyer.store.connect(write=True) as db:
            row=db.execute('SELECT value FROM meta WHERE key=?',('quote_fee:'+quoted['quote_id'],)).fetchone()
            saved=json.loads(row[0]); saved['platform_cents']=1
            db.execute('UPDATE meta SET value=? WHERE key=?',(compact(saved),'quote_fee:'+quoted['quote_id']))
        self.assert_code('agreement_corrupt',self.prepare,quoted)

    def test_missing_snapshot_never_defaults_to_ten_percent(self):
        self.start(); self.agreement(); quoted=self.quote()
        with self.buyer.store.connect(write=True) as db:
            db.execute('DELETE FROM meta WHERE key=?',('quote_fee:'+quoted['quote_id'],))
        self.assert_code('agreement_corrupt',self.prepare,quoted)

    def test_json_key_order_does_not_change_agreement_or_order(self):
        self.start(); self.agreement(); quoted=self.quote(); prepared=self.prepare(quoted)
        with self.buyer.store.connect(write=True) as db:
            for row in db.execute("SELECT key,value FROM meta WHERE key LIKE 'commission_agreement:%' OR key LIKE 'quote_fee:%'").fetchall():
                db.execute('UPDATE meta SET value=? WHERE key=?',
                           (json.dumps(json.loads(row['value']),sort_keys=True),row['key']))
        self.assertEqual(prepared,self.prepare(quoted))

    def test_corrupted_or_missing_agreement_pointer_refused(self):
        self.start(); self.agreement()
        with self.buyer.store.connect(write=True) as db:
            db.execute('DELETE FROM meta WHERE key=?',(_prefix('vendor')+'current',))
        self.assert_code('agreement_corrupt',self.quote)

    def test_quote_and_fee_snapshot_transaction_roll_back_together(self):
        self.start(); self.agreement()
        with patch('ai_supermarket.exchange.freeze_quote_fee', side_effect=OSError('fixture interruption')):
            with self.assertRaises(OSError): self.quote()
        self.assertEqual(0,self.count('quotes'))
        self.assertEqual(500,self.quote()['fee_policy']['commission_bps'])

    def test_protocol_cannot_register_or_override_agreement(self):
        self.start()
        for market in [self.buyer,self.seller,self.reviewer]:
            names=[entry['name'] for entry in ExchangeInterface(market).tool_list()]
            self.assertFalse(any('agreement' in name for name in names))
        result=ExchangeInterface(self.buyer).call('market_quote',{'id':self.resource,'version':'1.0.0',
            'purpose':'personal_use','request_id':'q','commission_bps':300})
        self.assertEqual('error',result['status'])
        self.assertEqual(0,self.count('quotes'))

    def test_other_supplier_agreement_does_not_change_quote(self):
        self.start()
        record_agreement(self.buyer.store,'other','other-contract','introductory','fixture',starts_on='2026-09-23')
        self.assertEqual(800,self.quote()['fee_policy']['commission_bps'])

    def test_new_terms_expire_after_ninety_days_and_freeze_original_fees(self):
        self.start()
        record = record_agreement(self.buyer.store, 'vendor', 'v2-intro', 'introductory',
                                  'fixture-only', starts_on='2026-09-30')
        self.assertEqual(2, record['terms_version'])
        self.now[0] = stamp('2026-12-28T23:59:30')
        old = self.quote('last-discount')
        self.assertEqual(500, old['fee_policy']['commission_bps'])
        self.assertEqual(stamp('2026-12-29T00:00:00'), old['expires_at'])
        self.now[0] = old['expires_at']
        self.assert_code('quote_expired', self.prepare, old)
        new = self.quote('new-standard')
        self.assertEqual(800, new['fee_policy']['commission_bps'])
        self.assertEqual(800, self.prepare(new)['binding']['fee_policy']['platform_cents'])
        with self.buyer.store.connect() as db:
            row = db.execute('SELECT * FROM quotes WHERE id=?', (old['quote_id'],)).fetchone()
            self.assertEqual(500, quote_fee(db, row, 'vendor')['commission_bps'])

    def test_legacy_quote_retained_but_cannot_start_new_payment_after_policy_change(self):
        from ai_supermarket.commerce import fee_breakdown, LEGACY_POLICY_VERSION
        self.start()
        with patch('ai_supermarket.exchange.fee_snapshot',
                   return_value=fee_breakdown(10001, policy_version=LEGACY_POLICY_VERSION)):
            old = self.quote('legacy')
        with self.buyer.store.connect() as db:
            row = db.execute('SELECT * FROM quotes WHERE id=?', (old['quote_id'],)).fetchone()
            self.assertEqual(1000, quote_fee(db, row, 'vendor')['commission_bps'])
        self.assert_code('policy_changed', self.prepare, old)
        self.assertEqual(800, self.quote('current')['fee_policy']['commission_bps'])

    def test_legacy_agreement_replay_and_upgrade_do_not_rewrite_history(self):
        self.start(); legacy = self.agreement('standard'); old = self.quote('old')
        self.assertNotIn('terms_version', legacy)
        record_agreement(self.buyer.store, 'vendor', 'new-standard', 'standard',
                         'fixture-only', expected_revision=1)
        self.assertEqual(legacy, self.agreement('standard'))
        self.assertEqual(800, self.quote('new')['fee_policy']['commission_bps'])
        with self.buyer.store.connect() as db:
            row = db.execute('SELECT * FROM quotes WHERE id=?', (old['quote_id'],)).fetchone()
            self.assertEqual(1000, quote_fee(db, row, 'vendor')['commission_bps'])

    def test_new_negotiated_terms_return_to_eight_percent(self):
        self.start()
        record_agreement(self.buyer.store, 'vendor', 'new-negotiated', 'negotiated',
                         'fixture-only', starts_on='2026-09-23', ends_on='2026-10-01',
                         negotiated_bps=375)
        self.assertEqual(375, self.quote('during')['fee_policy']['commission_bps'])
        self.now[0] = stamp('2026-10-01T00:00:00')
        self.assertEqual(800, self.quote('after')['fee_policy']['commission_bps'])

    def test_cross_process_original_fee_survives_reopen(self):
        self.start(); self.agreement(); quoted=self.quote()
        script = '''import json,sys
from ai_supermarket.exchange import Exchange
from ai_supermarket.commission_agreements import quote_fee
m=Exchange(sys.argv[1],'alice')
with m.store.connect() as db:
 row=db.execute('SELECT * FROM quotes WHERE id=?',(sys.argv[2],)).fetchone()
 print(json.dumps(quote_fee(db,row,'vendor')))
'''
        result=subprocess.run([sys.executable,'-X','utf8','-B','-c',script,str(self.database),quoted['quote_id']],
                              capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertEqual(quoted['fee_policy'],json.loads(result.stdout))


if __name__=='__main__':
    unittest.main()
