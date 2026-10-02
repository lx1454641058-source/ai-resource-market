"""来源：本项目原创。临时密钥、虚构授权和隔离预算，无付款或对外发布。"""
import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from ai_supermarket.alipay_transport import RSA2RequestSigner
from ai_supermarket.budget import BudgetLedger
from ai_supermarket.common import MarketError
from ai_supermarket.payment_bills import PurchaseBilling, authorize_purchase_task, revoke_purchase_task
from ai_supermarket.payment_fulfillment import PaymentFulfillment
from ai_supermarket.payment_orders import _get, _put
from ai_supermarket.tests import test_payment_fulfillment as fixture


class PaymentBillTests(unittest.TestCase):
    actor=fixture.PaymentFulfillmentTests.actor
    draft=fixture.PaymentFulfillmentTests.draft
    publish=fixture.PaymentFulfillmentTests.publish
    count=fixture.PaymentFulfillmentTests.count
    assert_code=fixture.PaymentFulfillmentTests.assert_code
    quote=fixture.PaymentFulfillmentTests.quote
    prepare=fixture.PaymentFulfillmentTests.prepare
    start=fixture.PaymentFulfillmentTests.start
    signed=fixture.PaymentFulfillmentTests.signed
    configure=fixture.PaymentFulfillmentTests.configure
    update_meta=fixture.PaymentFulfillmentTests.update_meta
    seed_evidence=fixture.PaymentFulfillmentTests.seed_evidence
    confirm=fixture.PaymentFulfillmentTests.confirm

    @classmethod
    def setUpClass(cls):
        fixture.PaymentFulfillmentTests.setUpClass.__func__(cls)
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        cls.bill_key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        cls.sign_pem=cls.bill_key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption())

    def setUp(self):
        fixture.PaymentFulfillmentTests.setUp(self)
        self.signer=RSA2RequestSigner(self.sign_pem)
        self.policy['signing_public_key_sha256']=self.signer.public_key_sha256
        self.fulfillment=PaymentFulfillment(self.orders,self.policy,enabled=True)
        self.ledger=BudgetLedger(self.database.parent/'budget.sqlite',clock=lambda:self.now[0])
        self.ledger.create('root','fixture-owner',300,'fictional-budget-consent')
        self.ledger.grant_task('root','task','alice')
        self.budget=self.ledger.scope('root','task','alice')
        self.authorize()
        self.biller=self.make_biller()

    def authorize(self, mandate='mandate', **kwargs):
        args={'consent_reference':'fictional-purchase-consent','allowed_sellers':['vendor'],
              'allowed_purposes':['personal_use'],'max_order_cents':100,'expires_at':1200}
        args.update(kwargs)
        return authorize_purchase_task(self.orders,self.budget,mandate,**args)

    def make_biller(self,**kwargs):
        args={'seller_name':'原创测试卖家','enabled':True};args.update(kwargs)
        return PurchaseBilling(self.orders,self.budget,self.signer,self.policy,**args)

    def issue(self,order=None,mandate='mandate'):
        return self.biller.issue(order or self.order_id,mandate)

    def new_order(self,slug):
        resource=self.publish(price=100,slug=slug)
        detail=self.buyer.resolve(resource,'1.0.0','personal_use')
        quote=self.buyer.quote(resource,'1.0.0','personal_use','q-'+slug)
        return self.orders.prepare(quote['quote_id'],detail['release_digest'],'p-'+slug)['order_id']

    def test_bill_signature_exact_original_order_and_shared_budget(self):
        result=self.issue()
        self.assertEqual(402,result['http_status'])
        payload=json.loads(base64.urlsafe_b64decode(result['headers']['Payment-Needed']))
        protocol,method=payload['protocol'],payload['method']
        self.assertEqual(self.order_id,protocol['out_trade_no']);self.assertEqual('1.00',protocol['amount'])
        self.assertEqual('1970-01-01T08:20:00+08:00',protocol['pay_before'])
        signed={k:protocol[k] for k in ('amount','currency','out_trade_no','pay_before','resource_id')}
        signed.update({k:method[k] for k in ('goods_name','seller_id','service_id')})
        raw='&'.join(k+'='+signed[k] for k in sorted(signed)).encode('utf-8')
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        self.bill_key.public_key().verify(base64.b64decode(protocol['seller_signature']),raw,padding.PKCS1v15(),hashes.SHA256())
        summary=self.budget.summary()
        self.assertEqual((0,100,200),(summary['spent_cents'],summary['held_cents'],summary['available_cents']))
        self.assertEqual('in_flight',self.budget.status(result['reservation_id'])['state'])
        self.assertFalse(result['payment_sent']);self.assertFalse(result['wallet_payment_authorized'])
        self.assertTrue(result['purchase_authorized_by_host'])
        self.assertEqual('handoff_reserved',self.orders.get(self.order_id)['status'])

    def test_disabled_and_missing_purchase_consent_cannot_expose_bill(self):
        self.assert_code('billing_disabled',self.make_biller(enabled=False).issue,self.order_id,'mandate')
        self.assert_code('purchase_authorization_missing',self.issue,mandate='missing')
        self.assertEqual(0,self.budget.summary()['held_cents'])
        self.assertIsNone(self.orders.get(self.order_id)['handoff_sha256'])

    def test_replay_keeps_exact_bill_and_one_reservation(self):
        first=self.issue();self.biller=self.make_biller();again=self.issue()
        self.assertEqual(first['headers'],again['headers']);self.assertTrue(again['replayed'])
        self.assertEqual(first['reservation_id'],again['reservation_id'])
        self.assertEqual(100,self.budget.summary()['held_cents'])

    def test_wrong_seller_purpose_or_single_order_limit_denied(self):
        for name,changes in [('seller',{'allowed_sellers':['other']}),('purpose',{'allowed_purposes':['commercial_use']}),
                             ('limit',{'max_order_cents':99})]:
            self.authorize(name,**changes)
            self.assert_code('purchase_not_authorized',self.issue,mandate=name)
        self.assertEqual(0,self.budget.summary()['held_cents'])

    def test_revoke_stops_new_handoff_without_releasing_old_exposure(self):
        issued=self.issue();revoke_purchase_task(self.orders,'mandate')
        self.assertEqual('revoked',self.authorize()['state'])
        self.assert_code('purchase_authorization_revoked',self.issue)
        self.assert_code('invalid_state',self.budget.cancel,issued['reservation_id'])
        self.assertEqual(100,self.budget.summary()['held_cents'])
        self.assertFalse(self.biller.history(self.order_id)['new_payment_authorized'])

    def test_expired_authorization_and_quote_do_not_reissue(self):
        issued=self.issue();self.now[0]=1200
        self.assert_code('purchase_authorization_expired',self.issue)
        self.assertEqual('in_flight',self.budget.status(issued['reservation_id'])['state'])
        other=self.new_order('expired')
        self.authorize('long',expires_at=2000)
        self.now[0]=1600
        self.assert_code('quote_expired',self.issue,other,'long')

    def test_budget_other_costs_and_pause_prevent_handoff(self):
        other=self.budget.reserve('model','model','fixture-model',201)
        self.assert_code('budget_exceeded',self.issue)
        self.budget.cancel(other['reservation_id'])
        self.ledger.set_state('root','paused',1)
        self.assert_code('budget_inactive',self.issue)
        self.assertEqual(0,self.budget.summary()['held_cents'])

    def test_two_orders_compete_for_one_shared_remaining_budget(self):
        self.budget.reserve('model','model','fixture-model',200)
        other=self.new_order('second')
        def run(order):
            try:return self.issue(order)['status']
            except MarketError as error:return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,[self.order_id,other]))
        self.assertCountEqual(['payment_needed','budget_exceeded'],results)
        self.assertEqual(300,self.budget.summary()['held_cents'])

    def test_same_order_concurrency_reuses_bill(self):
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:self.issue(),range(2)))
        self.assertEqual(results[0]['headers'],results[1]['headers'])
        self.assertEqual(100,self.budget.summary()['held_cents'])

    def test_cannot_switch_authorization_or_task_after_issuing(self):
        self.issue();self.authorize('other')
        self.assert_code('idempotency_conflict',self.issue,mandate='other')
        self.ledger.grant_task('root','another','alice')
        task=self.ledger.scope('root','another','alice')
        authorize_purchase_task(self.orders,task,'other-task',consent_reference='fixture',allowed_sellers=['vendor'],
                                allowed_purposes=['personal_use'],max_order_cents=100,expires_at=1200)
        biller=PurchaseBilling(self.orders,task,self.signer,self.policy,seller_name='原创测试卖家',enabled=True)
        self.assert_code('idempotency_conflict',biller.issue,self.order_id,'other-task')
        self.assertEqual(100,self.budget.summary()['held_cents'])

    def test_failure_after_reservation_before_stage_reuses_original_hold(self):
        with patch.object(self.biller,'_build',side_effect=OSError('fixture interruption')):
            with self.assertRaises(OSError):self.issue()
        self.assertEqual(100,self.budget.summary()['held_cents'])
        self.assertIsNone(self.orders.get(self.order_id)['handoff_sha256'])
        self.issue();self.assertEqual(100,self.budget.summary()['held_cents'])

    def test_failure_after_begin_before_handoff_recovers_same_staged_bill(self):
        from ai_supermarket import payment_bills as module
        original=module._put
        def fail(db,key,value):
            if key=='payment_order:'+self.order_id:raise OSError('fixture interruption')
            return original(db,key,value)
        with patch.object(module,'_put',side_effect=fail):
            with self.assertRaises(OSError):self.issue()
        history=self.biller.history(self.order_id)
        self.assertEqual('staged',history['state']);self.assertEqual('in_flight',history['reservation']['state'])
        self.assertIsNone(self.orders.get(self.order_id)['handoff_sha256'])
        self.issue();self.assertEqual(100,self.budget.summary()['held_cents'])

    def test_cancelled_order_is_never_resurrected(self):
        self.orders.cancel_unstarted(self.order_id)
        self.assert_code('order_closed',self.issue)
        self.assertEqual(0,self.budget.summary()['held_cents'])

    def test_tampered_bill_or_budget_reference_refused(self):
        self.issue()
        with self.buyer.store.connect(write=True) as db:
            bill=_get(db,'payment_bill:'+self.order_id);bill['header']='changed';_put(db,'payment_bill:'+self.order_id,bill)
        self.assert_code('bill_corrupt',self.issue)
        self.assertEqual(100,self.budget.summary()['held_cents'])

    def test_unknown_budget_or_existing_verification_blocks_new_bill(self):
        issued=self.issue();self.budget.mark_unknown(issued['reservation_id'])
        self.assert_code('reconciliation_required',self.issue)
        self.orders.begin_verification(self.order_id,'unknown-verification','a'*64)
        self.assert_code('order_closed',self.issue)
        self.assertEqual(100,self.budget.summary()['held_cents'])

    def test_confirmed_payment_settles_original_cost_once(self):
        self.issue();self.seed_evidence();self.confirm()
        result=self.biller.settle_budget(self.order_id)
        self.assertEqual(100,result['reservation']['actual_cents'])
        self.assertEqual(result,self.biller.settle_budget(self.order_id))
        self.assertEqual((100,0),(self.budget.summary()['spent_cents'],self.budget.summary()['held_cents']))
        self.assert_code('already_paid',self.issue)

    def test_settlement_market_failure_recovers_without_double_accounting(self):
        self.issue();self.seed_evidence();self.confirm()
        with patch('ai_supermarket.payment_bills._put',side_effect=OSError('fixture interruption')):
            with self.assertRaises(OSError):self.biller.settle_budget(self.order_id)
        self.assertEqual(100,self.budget.summary()['spent_cents'])
        self.biller.settle_budget(self.order_id)
        self.assertEqual('budget_settled',self.biller.history(self.order_id)['state'])
        self.assertEqual(100,self.budget.summary()['spent_cents'])

    def test_unconfirmed_payment_cannot_be_settled(self):
        self.issue();self.assert_code('payment_not_confirmed',self.biller.settle_budget,self.order_id)
        self.assertEqual(0,self.budget.summary()['spent_cents'])

    def test_bill_payment_accounting_entitlement_and_delivery_are_linked(self):
        issued=self.issue();self.seed_evidence();self.confirm()
        settled=self.biller.settle_budget(self.order_id)
        granted=self.fulfillment.grant(self.order_id)
        read=self.buyer.read(granted['acquisition_id'],'task','read')
        delivered=self.fulfillment.record_delivery(self.order_id,'task','read')
        self.assertEqual(issued['reservation_id'],settled['reservation']['reservation_id'])
        self.assertEqual(read['usage_id'],delivered['usage_id'])
        self.assertEqual(1,self.count('acquisitions'))
        self.assertFalse(delivered['channel_fulfillment_confirmed'])
        self.assertEqual(100,self.budget.summary()['spent_cents'])

    def test_staged_cancellation_is_checked_before_budget_begin(self):
        from ai_supermarket import payment_bills as module
        original=module._put
        def fail(db,key,value):
            if key=='payment_order:'+self.order_id:raise OSError('fixture interruption')
            return original(db,key,value)
        with patch.object(module,'_put',side_effect=fail):
            with self.assertRaises(OSError):self.issue()
        self.orders.cancel_unstarted(self.order_id)
        self.assert_code('order_closed',self.issue)
        self.assertIsNone(self.orders.get(self.order_id)['handoff_sha256'])
        self.assertEqual(100,self.budget.summary()['held_cents'])

    def test_wrong_budget_reservation_cannot_be_used_to_settle_bill(self):
        self.issue();self.seed_evidence();self.confirm()
        other=self.budget.reserve('other','resource','other-resource',1)
        self.budget.begin(other['reservation_id'])
        with self.buyer.store.connect(write=True) as db:
            bill=_get(db,'payment_bill:'+self.order_id)
            bill['reservation_id']=other['reservation_id'];_put(db,'payment_bill:'+self.order_id,bill)
        self.assert_code('bill_corrupt',self.biller.settle_budget,self.order_id)
        self.assertEqual(0,self.budget.summary()['spent_cents'])

    def test_no_implicit_amount_unit_and_wrong_signing_key_refused(self):
        changed=deepcopy(self.policy);changed['signing_public_key_sha256']='0'*64
        self.assert_code('payment_policy_mismatch',PurchaseBilling,self.orders,self.budget,self.signer,changed,seller_name='fixture')
        changed=deepcopy(self.policy);changed['amount_unit']='CNY_minor'
        biller=PurchaseBilling(self.orders,self.budget,self.signer,changed,seller_name='fixture',enabled=True)
        issued=biller.issue(self.order_id,'mandate')
        payload=json.loads(base64.urlsafe_b64decode(issued['headers']['Payment-Needed']))
        self.assertEqual('100',payload['protocol']['amount'])


if __name__=='__main__':unittest.main()
