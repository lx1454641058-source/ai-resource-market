"""来源：本项目原创。持久化订单/重放/未知状态验证，不调用支付渠道。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from ai_supermarket.common import MarketError, compact
from ai_supermarket.commission_agreements import record_agreement
from ai_supermarket.payment_orders import PaymentOrders
from ai_supermarket.tests import test_machine_pay_order as fixture


class PaymentOrderTests(unittest.TestCase):
    setUp=fixture.MachinePayOrderTests.setUp
    actor=fixture.MachinePayOrderTests.actor
    draft=fixture.MachinePayOrderTests.draft
    publish=fixture.MachinePayOrderTests.publish
    count=fixture.MachinePayOrderTests.count
    assert_code=fixture.MachinePayOrderTests.assert_code
    quote=fixture.MachinePayOrderTests.quote
    prepare=fixture.MachinePayOrderTests.prepare

    def start(self):
        self.quote()
        self.orders=PaymentOrders(self.buyer,self.merchant)
        return self.orders.prepare(self.quoted['quote_id'],self.digest,'prepare-main')

    def db_orders(self):
        with self.buyer.store.connect() as db:
            return db.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'payment_order:%'").fetchone()[0]

    def test_prepare_replay_and_read_survive_quote_expiry_without_new_order(self):
        record=self.start(); self.now[0]=2000
        again=self.orders.prepare(self.quoted['quote_id'],self.digest,'prepare-main')
        self.assertEqual({**record,'replayed':True},again)
        self.assertEqual(record,self.orders.get(record['order_id']))
        self.assertEqual(1,self.db_orders())
        self.assertEqual(0,self.count('acquisitions'))
        self.assertFalse(record['paid_verified']); self.assertFalse(record['payment_authorized'])

    def test_same_request_cannot_switch_quote_or_merchant(self):
        self.start()
        fresh=self.buyer.quote(self.resource,'1.0.0','personal_use','new-quote')
        self.assert_code('idempotency_conflict',self.orders.prepare,fresh['quote_id'],self.digest,'prepare-main')
        changed=PaymentOrders(self.buyer,{**self.merchant,'seller_id':'changed'})
        self.assert_code('idempotency_conflict',changed.prepare,self.quoted['quote_id'],self.digest,'prepare-main')

    def test_different_request_same_quote_coalesces_to_same_order(self):
        first=self.start()
        second=self.orders.prepare(self.quoted['quote_id'],self.digest,'another-request')
        self.assertEqual(first['order_id'],second['order_id']);self.assertEqual(1,self.db_orders())

    def test_new_quote_for_unresolved_resource_cannot_create_second_order(self):
        record=self.start()
        self.orders.reserve_handoff(record['order_id'],'a'*64)
        self.now[0]=2000
        fresh=self.buyer.quote(self.resource,'1.0.0','personal_use','new')
        self.assert_code('pending_payment',self.orders.prepare,fresh['quote_id'],self.digest,'other')
        self.assertEqual(1,self.db_orders())

    def test_only_unstarted_order_can_cancel_and_new_quote_is_required(self):
        record=self.start(); key=record['order_id']
        cancelled=self.orders.cancel_unstarted(key)
        self.assertEqual(cancelled,self.orders.cancel_unstarted(key))
        self.assert_code('order_closed',self.orders.prepare,self.quoted['quote_id'],self.digest,'new-request')
        fresh=self.buyer.quote(self.resource,'1.0.0','personal_use','fresh')
        replacement=self.orders.prepare(fresh['quote_id'],self.digest,'replacement')
        self.assertNotEqual(key,replacement['order_id'])
        self.assertEqual('cancelled_before_verification',self.orders.get(key)['status'])

    def test_handoff_replay_unknown_effect_and_cancellation_guard(self):
        record=self.start(); key=record['order_id']
        first=self.orders.reserve_handoff(key,'a'*64)
        self.assertTrue(self.orders.reserve_handoff(key,'a'*64)['handoff_replayed'])
        self.assert_code('idempotency_conflict',self.orders.reserve_handoff,key,'b'*64)
        self.assert_code('reconciliation_required',self.orders.cancel_unstarted,key)
        self.assertFalse(first['channel_request_sent_by_this_module'])
        self.assertFalse(first['payment_authorized'])

    def test_first_handoff_checks_current_terms_and_expiry(self):
        record=self.start(); key=record['order_id'];self.now[0]=2000
        self.assert_code('quote_expired',self.orders.reserve_handoff,key,'a'*64)
        self.assertEqual('prepared_not_payable',self.orders.get(key)['status'])

    def test_attempt_is_persisted_before_observation_and_replay_does_not_resend(self):
        record=self.start(); key=record['order_id']
        self.assert_code('attempt_not_found',self.orders.observe,key,'v1','unknown')
        first=self.orders.begin_verification(key,'v1','a'*64)
        self.assertEqual('verification_pending',self.orders.get(key)['status'])
        self.assertTrue(self.orders.begin_verification(key,'v1','a'*64)['attempt_replayed'])
        self.assert_code('idempotency_conflict',self.orders.begin_verification,key,'v1','b'*64)
        self.assert_code('reconciliation_required',self.orders.begin_verification,key,'v2','a'*64)
        self.assertFalse(first['channel_request_sent_by_this_module'])

    def test_expired_handed_off_order_can_be_reconciled_without_repurchase(self):
        record=self.start();key=record['order_id'];self.orders.reserve_handoff(key,'a'*64)
        self.now[0]=2000
        result=self.orders.begin_verification(key,'v1','b'*64)
        self.assertEqual('verification_pending',result['status'])
        self.assertEqual(1,self.db_orders())

    def test_unknown_or_invalid_observation_never_means_unpaid_or_paid(self):
        record=self.start();key=record['order_id']
        for i,outcome in enumerate(('unknown','invalid','unverified_response')):
            attempt='v'+str(i);self.orders.begin_verification(key,attempt,'a'*64)
            result=self.orders.observe(key,attempt,outcome,'b'*64)
            self.assertEqual('reconciliation_required',result['status'])
            self.assertFalse(result['paid_verified'])
            self.assertFalse(result['entitlement_created'])
            self.assertTrue(self.orders.observe(key,attempt,outcome,'b'*64)['observation_replayed'])
            self.assert_code('reconciliation_required',self.orders.cancel_unstarted,key)
        self.assertEqual(0,self.count('acquisitions'))

    def test_fake_paid_status_and_overwriting_observation_refused(self):
        record=self.start();key=record['order_id'];self.orders.begin_verification(key,'v1','a'*64)
        for outcome in ('paid','verified',True):
            self.assert_code('invalid_params',self.orders.observe,key,'v1',outcome,'b'*64)
        self.orders.observe(key,'v1','unknown')
        self.assert_code('idempotency_conflict',self.orders.observe,key,'v1','invalid','b'*64)

    def test_attempt_limit_does_not_replace_original_order(self):
        record=self.start();key=record['order_id']
        for i in range(4):
            self.orders.begin_verification(key,str(i),'a'*64)
            self.orders.observe(key,str(i),'unknown')
        self.assert_code('attempt_limit',self.orders.begin_verification,key,'extra','a'*64)
        self.assertEqual(1,self.db_orders())

    def test_wrong_buyer_role_and_changed_merchant_cannot_access(self):
        record=self.start();key=record['order_id']
        self.assert_code('forbidden',PaymentOrders,self.seller,self.merchant)
        other=PaymentOrders(self.actor('bob','buyer'),self.merchant)
        self.assert_code('order_not_found',other.get,key)
        rebound=PaymentOrders(self.buyer,{**self.merchant,'app_id':'new-app'})
        self.assert_code('order_changed',rebound.get,key)

    def test_short_id_collision_preserves_existing_full_binding(self):
        record=self.start()
        fresh=self.buyer.quote(self.resource,'1.0.0','personal_use','q2')
        self.quoted=fresh; changed=self.prepare()
        changed['channel_identifiers']['out_trade_no']=record['order_id']
        with patch('ai_supermarket.payment_orders.prepare_order',return_value=changed):
            self.assert_code('channel_id_conflict',self.orders.prepare,fresh['quote_id'],self.digest,'collision')
        self.assertEqual(record,self.orders.get(record['order_id']))

    def test_concurrent_preparations_coalesce(self):
        record=self.start()
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies=list(pool.map(lambda n:self.orders.prepare(self.quoted['quote_id'],self.digest,n),['r1','r2']))
        self.assertEqual([record['order_id']]*2,[r['order_id'] for r in replies])
        self.assertEqual(1,self.db_orders())

    def test_mid_transaction_failure_leaves_no_order_or_dangling_index(self):
        self.quote();self.orders=PaymentOrders(self.buyer,self.merchant)
        from ai_supermarket import payment_orders as module
        real=module._put;calls=[]
        def fail(db,key,value):
            calls.append(key)
            if len(calls)==3:raise OSError('fixture interruption')
            return real(db,key,value)
        with patch.object(module,'_put',side_effect=fail):
            with self.assertRaises(OSError):self.orders.prepare(self.quoted['quote_id'],self.digest,'p')
        with self.buyer.store.connect() as db:
            self.assertEqual(0,db.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'payment_%'").fetchone()[0])
        self.orders.prepare(self.quoted['quote_id'],self.digest,'p');self.assertEqual(1,self.db_orders())

    def test_fee_snapshot_retained_and_cross_process_unknown_recovered(self):
        record_agreement(self.buyer.store,'vendor','fixture-agreement','introductory','fixture',starts_on='1970-01-01')
        record=self.start();key=record['order_id']
        self.assertEqual(500,record['prepared']['binding']['fee_policy']['commission_bps'])
        self.orders.begin_verification(key,'v1','a'*64);self.orders.observe(key,'v1','unknown')
        script='''import sys,json
from ai_supermarket.exchange import Exchange
from ai_supermarket.payment_orders import PaymentOrders
orders=PaymentOrders(Exchange(sys.argv[1],'alice'),json.loads(sys.argv[2]))
print(json.dumps(orders.get(sys.argv[3])))
'''
        result=subprocess.run([sys.executable,'-X','utf8','-B','-c',script,str(self.database),json.dumps(self.merchant),key],capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertEqual('reconciliation_required',json.loads(result.stdout)['status'])


if __name__=='__main__':unittest.main()
