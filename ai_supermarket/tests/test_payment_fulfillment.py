"""来源：本项目原创。隔离账本和合成渠道观察；不证明真实支付或商户资格。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from ai_supermarket.alipay_proof import AlipayProofVerifier, METHOD, request_digest
from ai_supermarket.alipay_transport import GATEWAYS
from ai_supermarket.commission_agreements import record_agreement
from ai_supermarket.common import MarketError
from ai_supermarket.payment_orders import PaymentOrders, _get, _put
from ai_supermarket.payment_fulfillment import PaymentFulfillment, bind_payment_environment
from ai_supermarket.tests import test_alipay_proof as fixture
from ai_supermarket.tests import test_exchange as exchange_fixture
from ai_supermarket.tests import test_origin as origin_fixture


class PaymentFulfillmentTests(unittest.TestCase):
    actor=fixture.AlipayProofTests.actor
    draft=fixture.AlipayProofTests.draft
    publish=fixture.AlipayProofTests.publish
    count=fixture.AlipayProofTests.count
    assert_code=fixture.AlipayProofTests.assert_code
    quote=fixture.AlipayProofTests.quote
    prepare=fixture.AlipayProofTests.prepare
    start=fixture.AlipayProofTests.start
    signed=fixture.AlipayProofTests.signed

    @classmethod
    def setUpClass(cls):fixture.AlipayProofTests.setUpClass.__func__(cls)

    def setUp(self):
        fixture.AlipayProofTests.setUp(self)
        self.configure()

    def configure(self):
        # This is an isolated synthetic ledger, not an operator migration example.
        self.update_meta('payment_environment','sandbox')
        self.policy={'merchant':self.merchant,'qualification_reference':'synthetic-fixture-not-real',
                     'signing_public_key_sha256':'b'*64,'verification_public_key_sha256':self.verifier.key_sha256,
                     'amount_unit':'CNY_major','policy_reference':'fixture-only','max_evidence_age_seconds':300}
        self.fulfillment=PaymentFulfillment(self.orders,self.policy,enabled=True)

    def seed_evidence(self, changes=None):
        # Deliberately synthetic transport metadata in an isolated fixture database.
        # No production API bypass switch exists in PaymentFulfillment.
        if self.orders.get(self.order_id)['handoff_sha256'] is None:
            self.orders.reserve_handoff(self.order_id,'a'*64)
        digest=request_digest(self.request,self.merchant)
        self.orders.begin_verification(self.order_id,'v1',digest)
        raw=self.signed()
        self.orders.record_signed_response(self.order_id,'v1',raw,self.request,self.verifier)
        transport={'endpoint':GATEWAYS[self.merchant['environment']], 'method':METHOD, 'outcome':'signature_and_order_match',
                   'https_response_received':True,'loopback_test':False,'request_sha256':digest,
                   'response_sha256':hashlib.sha256(raw).hexdigest(),'wire_sha256':'c'*64,
                   'issued_at':self.now[0],'received_at':self.now[0],
                   **{k:self.policy[k] for k in ('signing_public_key_sha256','verification_public_key_sha256','amount_unit','policy_reference')}}
        transport.update(changes or {})
        self.update_meta('payment_transport:'+self.order_id+':v1',transport)

    def update_meta(self,key,value):
        with self.buyer.store.connect(write=True) as db:_put(db,key,value)

    def confirm(self):return self.fulfillment.confirm(self.order_id,'v1')
    def grant(self):return self.fulfillment.grant(self.order_id)

    def test_confirmation_grant_read_and_actual_usage_acknowledgment(self):
        self.seed_evidence();confirmed=self.confirm()
        self.assertTrue(confirmed['paid_verified']);self.assertFalse(confirmed['entitlement_created'])
        granted=self.grant()
        self.assertEqual('verified_by_host',granted['payment'])
        self.assertEqual('acquired',self.buyer.status(granted['acquisition_id'])['status'])
        self.assert_code('delivery_not_recorded',self.fulfillment.record_delivery,self.order_id,'task','read')
        read=self.buyer.read(granted['acquisition_id'],'task','read')
        self.assertEqual(exchange_fixture.CONTENT,read['content'])
        done=self.fulfillment.record_delivery(self.order_id,'task','read')
        self.assertEqual(read['usage_id'],done['usage_id'])
        self.assertFalse(done['channel_fulfillment_confirmed']);self.assertFalse(done['task_completed'])
        self.assertEqual('paid_delivered_to_host',self.orders.get(self.order_id)['status'])
        self.assertTrue(self.fulfillment.record_delivery(self.order_id,'task','read')['replayed'])
        self.assertEqual(1,self.count('acquisitions'))

    def test_disabled_missing_evidence_and_unconfirmed_grant_are_refused(self):
        disabled=PaymentFulfillment(self.orders,self.policy)
        self.assert_code('fulfillment_disabled',disabled.confirm,self.order_id,'v1')
        self.assert_code('handoff_required',self.confirm)
        self.orders.reserve_handoff(self.order_id,'a'*64)
        self.assert_code('payment_evidence_missing',self.confirm)
        self.assert_code('payment_not_confirmed',self.grant)
        self.assertEqual(0,self.count('acquisitions'))

    def test_loopback_unknown_and_wrong_endpoint_never_confirm(self):
        for changes in ({'loopback_test':True},{'https_response_received':False},
                        {'outcome':'reserved'},{'endpoint':'https://example.com/gateway.do'}):
            with self.subTest(changes=changes):
                self.seed_evidence(changes)
                self.assert_code('untrusted_payment_evidence',self.confirm)
        self.assertFalse(self.orders.get(self.order_id)['paid_verified'])

    def test_key_amount_policy_and_request_hash_must_match(self):
        for changes,code in (({'signing_public_key_sha256':'d'*64},'payment_policy_mismatch'),
                             ({'verification_public_key_sha256':'d'*64},'payment_policy_mismatch'),
                             ({'amount_unit':'CNY_minor'},'payment_policy_mismatch'),
                             ({'policy_reference':'other'},'payment_policy_mismatch'),
                             ({'response_sha256':'d'*64},'payment_binding_mismatch'),
                             ({'request_sha256':'d'*64},'payment_binding_mismatch')):
            self.seed_evidence(changes);self.assert_code(code,self.confirm)

    def test_stale_future_missing_timestamp_and_trade_conflict_refused(self):
        for changes in ({'issued_at':600,'received_at':600},{'issued_at':1001,'received_at':1001},
                        {'issued_at':1000,'received_at':999}):
            self.seed_evidence(changes);self.assert_code('payment_evidence_expired',self.confirm)
        self.seed_evidence({'received_at':None});self.assert_code('invalid_clock',self.confirm)
        self.seed_evidence()
        from ai_supermarket.payment_orders import _hash
        self.update_meta('payment_signed_trade:'+_hash(['sandbox','fixture-trade']),'other-order')
        self.assert_code('trade_conflict',self.confirm)

    def test_replay_and_concurrent_grant_produce_one_entitlement(self):
        self.seed_evidence();first=self.confirm()
        self.assertTrue(self.confirm()['confirmation_replayed'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies=list(pool.map(lambda _:self.grant(),range(2)))
        self.assertEqual(1,self.count('acquisitions'))
        self.assertEqual(replies[0]['acquisition_id'],replies[1]['acquisition_id'])
        self.assertEqual(1,sum(reply['replayed'] for reply in replies))
        self.now[0]=5000
        self.assertEqual(first['confirmation'],self.confirm()['confirmation'])
        self.assertTrue(self.grant()['replayed'])

    def test_paid_state_cannot_be_downgraded_or_cancelled(self):
        self.seed_evidence();self.confirm()
        self.assert_code('order_closed',self.orders.cancel_unstarted,self.order_id)
        self.assert_code('order_closed',self.orders.observe,self.order_id,'v1','unknown')
        self.assert_code('order_closed',self.orders.begin_verification,self.order_id,'v2','a'*64)
        self.assertTrue(self.orders.begin_verification(self.order_id,'v1',request_digest(self.request,self.merchant))['paid_verified'])
        self.assertTrue(self.orders.record_signed_response(self.order_id,'v1',self.signed(),self.request,self.verifier)['paid_verified'])

    def test_reprice_expiry_and_withdrawal_do_not_rewrite_paid_original(self):
        self.orders.reserve_handoff(self.order_id,'a'*64)
        self.now[0]=2000;self.seed_evidence()
        self.seller.reprice(self.resource,'1.0.0',1,250,'new-price')
        self.seller.availability(self.resource,'1.0.0',2,'withdrawn','fixture','withdraw')
        confirmed=self.confirm();granted=self.grant()
        self.assertEqual(100,confirmed['confirmation']['payload']['fees']['net_cents'])
        self.assertEqual(exchange_fixture.CONTENT,self.buyer.read(granted['acquisition_id'],'task','read')['content'])

    def test_suspension_retains_payment_and_reactivation_can_finish(self):
        self.seed_evidence();self.confirm()
        self.reviewer.availability(self.resource,'1.0.0',1,'suspended','fixture','suspend')
        self.assert_code('not_found',self.grant)
        self.assertTrue(self.orders.get(self.order_id)['paid_verified'])
        self.assertFalse(self.orders.get(self.order_id)['entitlement_created'])
        self.reviewer.availability(self.resource,'1.0.0',2,'active','fixture','restore')
        self.grant();self.assertEqual(1,self.count('acquisitions'))

    def test_changed_content_or_license_cannot_substitute_paid_version(self):
        self.seed_evidence();self.confirm()
        with self.buyer.store.connect(write=True) as db:
            db.execute('UPDATE submissions SET content=? WHERE resource_id=?',('changed',self.resource))
        self.assert_code('integrity_failed',self.grant)
        self.assertTrue(self.orders.get(self.order_id)['paid_verified']);self.assertEqual(0,self.count('acquisitions'))

    def test_existing_free_entitlement_requires_reconciliation_instead_of_duplicate(self):
        self.seed_evidence();self.confirm()
        self.seller.reprice(self.resource,'1.0.0',1,0,'free-now')
        self.buyer.acquire(self.resource,'1.0.0','free-acquire','personal_use',expected_release_digest=self.digest)
        self.assert_code('already_owned_reconciliation',self.grant)
        self.assertEqual(1,self.count('acquisitions'))

    def test_grant_transaction_failure_retains_confirmation_without_partial_right(self):
        self.seed_evidence();self.confirm()
        from ai_supermarket import payment_fulfillment as module
        original=module._put;calls=[]
        def fail(db,key,value):
            calls.append(key)
            if len(calls)==2:raise OSError('fixture interruption')
            return original(db,key,value)
        with patch.object(module,'_put',side_effect=fail):
            with self.assertRaises(OSError):self.grant()
        self.assertEqual(0,self.count('acquisitions'))
        self.assertEqual('paid_entitlement_pending',self.orders.get(self.order_id)['status'])
        with self.buyer.store.connect() as db:
            self.assertEqual(0,db.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'paid_entitlement:%'").fetchone()[0])
        self.grant()

    def test_lost_paid_lineage_blocks_reads_and_acquire_alias(self):
        self.seed_evidence();self.confirm();granted=self.grant()
        self.update_meta('paid_entitlement:'+granted['acquisition_id'],{})
        self.assert_code('integrity_failed',self.buyer.read,granted['acquisition_id'],'task','read')
        self.assert_code('integrity_failed',self.buyer.acquire,self.resource,'1.0.0','alias','personal_use',expected_release_digest=self.digest)
        self.assertEqual('unavailable',self.buyer.status(granted['acquisition_id'])['status'])
        self.assertEqual(1,self.count('acquisitions'))

    def test_other_buyer_cannot_confirm_grant_or_acknowledge_delivery(self):
        self.seed_evidence();self.confirm();self.grant()
        other=PaymentFulfillment(PaymentOrders(self.actor('bob','buyer'),self.merchant),self.policy,enabled=True)
        self.assert_code('order_not_found',other.confirm,self.order_id,'v1')
        self.assert_code('order_not_found',other.grant,self.order_id)
        self.assert_code('order_not_found',other.record_delivery,self.order_id,'task','read')

    def test_confirmed_fee_snapshot_survives_supplier_agreement_change(self):
        # Start an independent paid resource after recording a five-percent agreement.
        record_agreement(self.buyer.store,'vendor','fixture-terms','introductory','fixture',starts_on='1970-01-01')
        resource=self.publish(price=100,slug='discount')
        detail=self.buyer.resolve(resource,'1.0.0','personal_use')
        quote=self.buyer.quote(resource,'1.0.0','personal_use','discount-q')
        self.record=self.orders.prepare(quote['quote_id'],detail['release_digest'],'discount-p')
        self.order_id=self.record['order_id'];self.body.update(self.record['prepared']['channel_identifiers'])
        self.seed_evidence();self.confirm()
        record_agreement(self.buyer.store,'vendor','new-terms','standard','fixture',expected_revision=1)
        self.grant()
        confirmed=self.orders.get(self.order_id)['confirmation']['payload']
        self.assertEqual(500,confirmed['fees']['commission_bps'])

    def test_new_process_recovers_and_grants_without_requery(self):
        self.seed_evidence();self.confirm()
        script='''import sys,json
from ai_supermarket.exchange import Exchange
from ai_supermarket.payment_orders import PaymentOrders
from ai_supermarket.payment_fulfillment import PaymentFulfillment
p=json.loads(sys.argv[2]);o=PaymentOrders(Exchange(sys.argv[1],'alice'),p['merchant'])
print(json.dumps(PaymentFulfillment(o,p,enabled=True).grant(sys.argv[3])))
'''
        result=subprocess.run([sys.executable,'-X','utf8','-B','-c',script,str(self.database),json.dumps(self.policy),self.order_id],capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertEqual('acquired',json.loads(result.stdout)['status'])
        self.assertEqual(1,self.count('acquisitions'))

    def test_reacquire_reuses_paid_right_and_binds_new_request(self):
        self.seed_evidence();self.confirm();granted=self.grant()
        first=self.buyer.acquire(self.resource,'1.0.0','reuse','personal_use',expected_release_digest=self.digest)
        again=self.buyer.acquire(self.resource,'1.0.0','reuse','personal_use',expected_release_digest=self.digest)
        self.assertEqual(first,again);self.assertEqual(granted['acquisition_id'],first['acquisition_id'])
        other=self.publish(slug='other')
        self.assert_code('idempotency_conflict',self.buyer.acquire,other,'1.0.0','reuse','personal_use')
        self.assertEqual(1,self.count('acquisitions'))
        self.update_meta('paid_entitlement:'+granted['acquisition_id'],{})
        self.assert_code('integrity_failed',self.buyer.acquire,self.resource,'1.0.0','reuse','personal_use',expected_release_digest=self.digest)

    def test_corrupted_confirmation_blocks_reads_and_status(self):
        self.seed_evidence();self.confirm();granted=self.grant()
        with self.buyer.store.connect(write=True) as db:
            record=_get(db,'payment_order:'+self.order_id)
            record['confirmation']['sha256']='0'*64
            _put(db,'payment_order:'+self.order_id,record)
        self.assert_code('integrity_failed',self.buyer.read,granted['acquisition_id'],'t','r')
        self.assertEqual('unavailable',self.buyer.status(granted['acquisition_id'])['status'])

    def test_delivery_cannot_be_claimed_using_other_resources_usage(self):
        self.seed_evidence();self.confirm();self.grant()
        other=self.publish(slug='other')
        acquired=self.buyer.acquire(other,'1.0.0','other','personal_use')
        self.buyer.read(acquired['acquisition_id'],'task','read-other')
        self.assert_code('delivery_not_recorded',self.fulfillment.record_delivery,self.order_id,'task','read-other')

    def test_host_compatibility_failure_preserves_payment_until_restored(self):
        self.seed_evidence();self.confirm()
        from ai_supermarket.contracts import HostPolicy
        original=self.buyer.host_policy
        self.buyer.host_policy=HostPolicy(capabilities=frozenset())
        with self.assertRaises(MarketError):self.grant()
        self.assertEqual('paid_entitlement_pending',self.orders.get(self.order_id)['status'])
        self.buyer.host_policy=original;self.grant()

    def test_confirmation_write_failure_cannot_mark_paid(self):
        self.seed_evidence()
        with patch('ai_supermarket.payment_fulfillment._put',side_effect=OSError('fixture interruption')):
            with self.assertRaises(OSError):self.confirm()
        self.assertFalse(self.orders.get(self.order_id)['paid_verified'])
        self.assertEqual(0,self.count('acquisitions'))
        self.confirm()

    def test_environment_cannot_switch_or_bind_existing_unbound_orders(self):
        self.assertTrue(bind_payment_environment(self.buyer.store,'sandbox')['replayed'])
        self.assert_code('payment_environment_mismatch',bind_payment_environment,self.buyer.store,'production')
        wrong=PaymentOrders(self.buyer,{**self.merchant,'environment':'production'})
        self.assert_code('payment_environment_mismatch',wrong.prepare,self.quoted['quote_id'],self.digest,'wrong-environment')
        self.assert_code('payment_environment_mismatch',wrong.get,self.order_id)
        with self.buyer.store.connect(write=True) as db:
            db.execute("UPDATE meta SET value='null' WHERE key='payment_environment'")
        self.assert_code('payment_environment_unbound',bind_payment_environment,self.buyer.store,'sandbox')

    def test_mismatched_environment_blocks_confirmation_and_read(self):
        self.seed_evidence();self.update_meta('payment_environment','production')
        self.assert_code('payment_environment_mismatch',self.confirm)
        self.update_meta('payment_environment','sandbox');self.confirm();granted=self.grant()
        self.update_meta('payment_environment','production')
        self.assert_code('integrity_failed',self.buyer.read,granted['acquisition_id'],'t','r')

    def test_clean_ledger_can_bind_environment_before_payment_preparation(self):
        from ai_supermarket.exchange import Exchange
        clean=Exchange(self.database.parent/'separate.sqlite','alice')
        self.assertFalse(bind_payment_environment(clean.store,'production')['replayed'])
        self.assertTrue(bind_payment_environment(clean.store,'production')['replayed'])
        self.assert_code('payment_environment_mismatch',bind_payment_environment,clean.store,'sandbox')

    def test_task_resource_host_reads_paid_right_without_repurchase(self):
        self.seed_evidence();self.confirm();granted=self.grant()
        from ai_supermarket.host import TaskResources
        host=TaskResources(self.buyer,'task','产品简报',purpose='personal_use')
        read=host.use(self.resource,'1.0.0','use')
        self.assertEqual(exchange_fixture.CONTENT,read['content'])
        self.assertEqual(1,self.count('acquisitions'))
        self.assertEqual(granted['acquisition_id'],self.buyer.resolve(self.resource,'1.0.0','personal_use')['acquisition_id'])

    def test_paid_original_site_failure_recovery_and_cached_delivery(self):
        origin=origin_fixture.OriginTests();origin.setUp();self.addCleanup(origin.doCleanups)
        origin.buyer.clock=lambda:self.now[0]
        resource=origin.publish(price=100)
        self.buyer=origin.buyer;self.merchant={**self.merchant,'environment':'sandbox'}
        self.orders=PaymentOrders(self.buyer,self.merchant)
        detail=self.buyer.resolve(resource,'1.0.0','personal_use')
        quote=self.buyer.quote(resource,'1.0.0','personal_use','paid-origin-q')
        self.record=self.orders.prepare(quote['quote_id'],detail['release_digest'],'paid-origin-p')
        self.order_id=self.record['order_id'];self.body.update(self.record['prepared']['channel_identifiers'])
        self.verifier=AlipayProofVerifier(self.pem,self.merchant,amount_unit='CNY_major',policy_reference='fixture-only')
        self.configure();self.seed_evidence();self.confirm();granted=self.grant()
        origin.mode='error'
        with self.assertRaises(MarketError):origin.deliver(granted)
        self.assert_code('delivery_not_recorded',self.fulfillment.record_delivery,self.order_id,'task','delivery')
        self.assertTrue(self.orders.get(self.order_id)['entitlement_created'])
        origin.mode='ok';delivered=origin.deliver(granted)
        self.assertEqual(origin_fixture.RAW,Path(delivered['path']).read_bytes())
        self.fulfillment.record_delivery(self.order_id,'task','delivery')
        count=len(origin.requests);origin.mode='error'
        self.assertTrue(origin.deliver(granted)['cache_hit']);self.assertEqual(count,len(origin.requests))
        self.assertEqual(1,self.count('acquisitions'))


if __name__=='__main__':unittest.main()
