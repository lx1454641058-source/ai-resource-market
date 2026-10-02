"""来源：本项目原创。隔离宿主采购演练；不调用真实钱包、支付宝或模型。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from ai_supermarket.alipay_transport import AlipayVerificationClient
from ai_supermarket.common import MarketError
from ai_supermarket.payment_bills import revoke_purchase_task
from ai_supermarket.payment_orders import _get, _put
from ai_supermarket.purchase_host import PurchasingTaskResources
from ai_supermarket.tests import test_payment_bills as fixture


class PurchaseHostTests(unittest.TestCase):
    actor=fixture.PaymentBillTests.actor
    draft=fixture.PaymentBillTests.draft
    publish=fixture.PaymentBillTests.publish
    count=fixture.PaymentBillTests.count
    assert_code=fixture.PaymentBillTests.assert_code
    quote=fixture.PaymentBillTests.quote
    prepare=fixture.PaymentBillTests.prepare
    start=fixture.PaymentBillTests.start
    signed=fixture.PaymentBillTests.signed
    configure=fixture.PaymentBillTests.configure
    update_meta=fixture.PaymentBillTests.update_meta
    seed_evidence=fixture.PaymentBillTests.seed_evidence
    confirm=fixture.PaymentBillTests.confirm
    authorize=fixture.PaymentBillTests.authorize
    make_biller=fixture.PaymentBillTests.make_biller

    @classmethod
    def setUpClass(cls):fixture.PaymentBillTests.setUpClass.__func__(cls)

    def setUp(self):
        fixture.PaymentBillTests.setUp(self)
        self.orders.cancel_unstarted(self.order_id)
        self.client=AlipayVerificationClient(self.orders,self.verifier,self.signer,clock=lambda:self.now[0])
        self.host=self.make_host()

    def make_host(self, **kwargs):
        args={'mandate_id':'mandate'};args.update(kwargs)
        return PurchasingTaskResources(self.biller,self.fulfillment,self.client,'需要原创模板',**args)

    def use(self, request='buy'):
        result=self.host.use(self.resource,'1.0.0',request)
        if result.get('order_id'):
            self.order_id=result['order_id'];self.record=self.orders.get(self.order_id)
            self.body.update(self.record['prepared']['channel_identifiers'])
        return result

    def test_task_start_search_then_paid_bill_with_no_wallet_or_query(self):
        with patch.object(self.client,'_post',side_effect=AssertionError('must not query')):
            first=self.use()
            self.assertEqual(402,first['http_status'])
            again=self.use()
        self.assertIn('items',self.host.candidates)
        self.assertEqual(first['order_id'],again['order_id'])
        self.assertNotIn('headers',again)
        self.assertEqual('host_payment_handoff',again['next_action'])
        self.assertEqual(100,self.budget.summary()['held_cents'])
        self.assertEqual(0,self.count('acquisitions'))

    def test_recreated_host_recovers_exact_bill_only_on_explicit_handoff(self):
        issued=self.use();self.host=self.make_host()
        self.assertNotIn('headers',self.host.resume('buy'))
        recovered=self.host.handoff('buy')
        self.assertEqual(issued['headers'],recovered['headers'])
        self.assertEqual(issued['reservation_id'],recovered['reservation_id'])

    def test_saved_signed_evidence_to_content_and_one_expense(self):
        self.use();self.seed_evidence()
        with patch.object(self.client,'_post',side_effect=AssertionError('resume must not query')):
            result=self.host.resume('buy')
            again=self.make_host().resume('buy')
        self.assertEqual('read',result['status']);self.assertIn('content',result)
        self.assertEqual(result['usage_id'],again['usage_id'])
        self.assertEqual(1,self.count('acquisitions'))
        self.assertEqual(100,self.budget.summary()['spent_cents'])
        self.assertEqual(0,self.budget.summary()['held_cents'])
        self.assertFalse(result['task_completed'])
        self.assertFalse(result['delivery_receipt']['channel_fulfillment_confirmed'])

    def test_query_adapter_connected_but_transport_response_is_synthetic(self):
        self.use()
        self.client._enabled=True
        with patch.object(self.client,'_post',return_value=self.signed()) as post:
            result=self.host.verify_payment('buy','v1',self.request)
            again=self.host.verify_payment('buy','v1',self.request)
        self.assertEqual(1,post.call_count)
        self.assertTrue(result['paid_verified']);self.assertEqual(result['usage_id'],again['usage_id'])
        with self.buyer.store.connect() as db:
            raw=' '.join(row[0] for row in db.execute('SELECT value FROM meta'))
        self.assertNotIn('fictional-proof-only',raw)

    def test_unknown_payment_preserves_original_order_without_query_retry(self):
        self.use();self.client._enabled=True
        with patch.object(self.client,'_post',side_effect=TimeoutError) as post:
            result=self.host.verify_payment('buy','v1',self.request)
            again=self.host.resume('buy')
            self.use()
        self.assertEqual(1,post.call_count)
        self.assertFalse(result['paid_verified']);self.assertEqual(result,again)
        self.assertEqual(100,self.budget.summary()['held_cents'])
        self.assertEqual(0,self.count('acquisitions'))
        self.assert_code('order_closed',self.host.handoff,'buy')

    def test_missing_or_disabled_authority_never_hands_out_bill(self):
        revoke_purchase_task(self.orders,'mandate')
        self.assert_code('purchase_authorization_revoked',self.use)
        state=self.host.status('buy')
        self.assertEqual('prepared_not_payable',state['order_status'])
        self.assertEqual(0,self.budget.summary()['held_cents'])

    def test_changed_resource_and_mandate_cannot_repurpose_request(self):
        self.use();other=self.publish(price=100,slug='other')
        self.assert_code('idempotency_conflict',self.host.use,other,'1.0.0','buy')
        self.authorize('other')
        self.assert_code('purchase_scope_mismatch',self.make_host(mandate_id='other').resume,'buy')

    def test_preparation_interruption_uses_original_request(self):
        with patch.object(self.orders,'prepare',side_effect=OSError('fixture interruption')):
            with self.assertRaises(OSError):self.use()
        self.assertEqual('preparation_pending',self.host.resume('buy')['order_status'])
        self.assertEqual(0,self.budget.summary()['held_cents'])
        issued=self.host.handoff('buy')
        self.assertEqual(402,issued['http_status'])
        self.assertEqual(100,self.budget.summary()['held_cents'])

    def test_order_committed_before_flow_update_can_recover(self):
        from ai_supermarket import purchase_host as module
        original=module._put
        def fail(db,key,value):
            if key.startswith('purchase_host_order:'):raise OSError('fixture interruption')
            return original(db,key,value)
        with patch.object(module,'_put',side_effect=fail):
            with self.assertRaises(OSError):self.use()
        with self.buyer.store.connect() as db:
            before=db.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'payment_order:%'").fetchone()[0]
        self.host.handoff('buy')
        with self.buyer.store.connect() as db:
            after=db.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'payment_order:%'").fetchone()[0]
        self.assertEqual(before,after)

    def test_delivery_failure_after_payment_keeps_expense_and_entitlement(self):
        self.use();self.seed_evidence()
        with patch.object(self.buyer,'read',side_effect=OSError('fixture read failure')):
            with self.assertRaises(OSError):self.host.resume('buy')
        self.assertTrue(self.orders.get(self.order_id)['paid_verified'])
        self.assertEqual(100,self.budget.summary()['spent_cents'])
        self.assertEqual(1,self.count('acquisitions'))
        self.assertEqual('read',self.make_host().resume('buy')['status'])

    def test_receipt_failure_after_read_recovers_original_usage(self):
        self.use();self.seed_evidence()
        with patch.object(self.fulfillment,'record_delivery',side_effect=OSError('fixture receipt failure')):
            with self.assertRaises(OSError):self.host.resume('buy')
        result=self.host.resume('buy')
        self.assertEqual(result['usage_id'],result['delivery_receipt']['usage_id'])
        self.assertEqual(1,self.count('usage_events'))

    def test_concurrent_use_has_one_bill_and_one_hold(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.use(),range(2)))
        self.assertEqual(1,sum('headers' in r for r in results))
        self.assertEqual(100,self.budget.summary()['held_cents'])

    def test_confirmation_rejects_test_transport_even_with_valid_signature(self):
        self.use();self.seed_evidence({'loopback_test':True})
        self.assert_code('untrusted_payment_evidence',self.host.resume,'buy')
        self.assertEqual(0,self.count('acquisitions'))

    def test_fake_delivery_cannot_create_receipt(self):
        self.use();self.seed_evidence();self.confirm();self.fulfillment.grant(self.order_id)
        self.assert_code('delivery_not_recorded',self.host.acknowledge_delivery,'buy')

    def test_host_configuration_mismatch_rejected_before_search(self):
        other=deepcopy(self.policy);other['policy_reference']='other'
        from ai_supermarket.payment_fulfillment import PaymentFulfillment
        wrong=PaymentFulfillment(self.orders,other,enabled=True)
        self.assert_code('purchase_host_mismatch',PurchasingTaskResources,self.biller,wrong,self.client,
                         'goal',mandate_id='mandate')

    def test_free_resource_needs_no_bill_or_purchase_mandate(self):
        free=self.publish(price=0,slug='free')
        revoke_purchase_task(self.orders,'mandate')
        result=self.host.use(free,'1.0.0','free-use')
        self.assertEqual('read',result['status'])
        self.assertNotIn('headers',result)
        self.assertEqual(0,self.budget.summary()['held_cents'])
        self.assert_code('purchase_not_found',self.host.status,'free-use')

    def test_paused_task_records_confirmed_cost_but_does_not_deliver(self):
        self.use();self.seed_evidence()
        self.ledger.set_state('root','paused',1)
        with self.assertRaises(MarketError):self.host.resume('buy')
        self.assertTrue(self.orders.get(self.order_id)['paid_verified'])
        self.assertEqual(100,self.budget.summary()['spent_cents'])
        self.assertEqual(0,self.count('acquisitions'))
        self.ledger.set_state('root','active',2)
        self.assertEqual('read',self.host.resume('buy')['status'])

    def test_known_purchase_and_dependency_costs_share_the_cap(self):
        from ai_supermarket.tests.test_exchange import listing, CONTENT, CHECKS
        from ai_supermarket.contracts import HostPolicy
        meta=listing(100)
        meta['usage_contract']['dependencies']=[{'id':'tool:1','license_name':'MIT','max_cost_cents':80}]
        draft=self.seller.draft('costly','1.0.0',meta,CONTENT,'costly-draft')
        self.seller.submit(draft['id'],'1.0.0',1,'costly-submit')
        self.reviewer.review(draft['id'],'1.0.0',1,'approve',CHECKS,'原创本地测试','costly-review')
        self.buyer.host_policy=HostPolicy(dependencies=frozenset({'tool:1'}),extra_budget_cents=100)
        self.budget.reserve('other-work','model','model-cost',150)
        self.assert_code('budget_exceeded',self.host.use,draft['id'],'1.0.0','costly')
        self.assertEqual(230,self.budget.summary()['held_cents'])
        state=self.host.status('costly')
        self.assertEqual('prepared_not_payable',state['order_status'])
        self.assertIsNone(self.orders.get(state['order_id'])['handoff_sha256'])

    def test_paid_origin_failure_and_real_http_recovery_through_same_host(self):
        from pathlib import Path
        from ai_supermarket.tests import test_origin as origin_fixture
        from ai_supermarket.budget import BudgetLedger
        from ai_supermarket.payment_orders import PaymentOrders
        from ai_supermarket.payment_fulfillment import PaymentFulfillment
        origin=origin_fixture.OriginTests();origin.setUp();self.addCleanup(origin.doCleanups)
        origin.buyer.clock=lambda:self.now[0]
        self.buyer=origin.buyer;self.resource=origin.publish(price=100)
        self.orders=PaymentOrders(self.buyer,self.merchant)
        self.configure();self.policy['signing_public_key_sha256']=self.signer.public_key_sha256
        self.fulfillment=PaymentFulfillment(self.orders,self.policy,enabled=True)
        self.ledger=BudgetLedger(origin.root/'budget.sqlite',clock=lambda:self.now[0])
        self.ledger.create('root','fixture-owner',300,'fictional-budget-consent')
        self.ledger.grant_task('root','task','buyer');self.budget=self.ledger.scope('root','task','buyer')
        self.authorize();self.biller=self.make_biller()
        self.client=AlipayVerificationClient(self.orders,self.verifier,self.signer,clock=lambda:self.now[0])
        kwargs={'origin_clients':{'provider-demo':origin.client},'output_root':origin.root}
        self.host=self.make_host(**kwargs);self.use();self.seed_evidence()
        origin.mode='error'
        self.assert_code('origin_unconfirmed',self.host.resume,'buy')
        self.assertEqual(100,self.budget.summary()['spent_cents'])
        self.assertEqual(1,self.count('acquisitions'))
        origin.mode='ok';result=self.make_host(**kwargs).resume('buy')
        self.assertEqual(origin_fixture.RAW,Path(result['path']).read_bytes())
        self.assertEqual(result['usage_id'],result['delivery_receipt']['usage_id'])
        requests=len(origin.requests);origin.mode='error'
        again=self.host.resume('buy')
        self.assertTrue(again['cache_hit']);self.assertEqual(requests,len(origin.requests))
        self.assertEqual('delivered_to_host',self.host.status('buy')['status'])


if __name__=='__main__':unittest.main()
