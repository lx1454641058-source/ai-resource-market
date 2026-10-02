"""来源：本项目原创。内存生成测试密钥，无真实渠道或付款。"""
import base64
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from ai_supermarket.alipay_proof import AlipayProofVerifier, RESPONSE_KEY, request_digest
from ai_supermarket.common import MarketError
from ai_supermarket.payment_orders import PaymentOrders
from ai_supermarket.tests import test_payment_orders as fixture


class AlipayProofTests(unittest.TestCase):
    actor=fixture.PaymentOrderTests.actor
    draft=fixture.PaymentOrderTests.draft
    publish=fixture.PaymentOrderTests.publish
    count=fixture.PaymentOrderTests.count
    assert_code=fixture.PaymentOrderTests.assert_code
    quote=fixture.PaymentOrderTests.quote
    prepare=fixture.PaymentOrderTests.prepare
    start=fixture.PaymentOrderTests.start

    @classmethod
    def setUpClass(cls):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        cls.key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        cls.pem=cls.key.public_key().public_bytes(serialization.Encoding.PEM,
                                                serialization.PublicFormat.SubjectPublicKeyInfo)

    def setUp(self):
        fixture.PaymentOrderTests.setUp(self)
        self.record=self.start(); self.order_id=self.record['order_id']
        self.request={'trade_no':'fixture-trade','payment_proof':'fictional-proof-only'}
        self.verifier=AlipayProofVerifier(self.pem,self.merchant,amount_unit='CNY_major',policy_reference='fixture-only')
        self.body={'code':'10000','active':True,'amount':'1.00','trade_no':'fixture-trade',
                   **self.record['prepared']['channel_identifiers']}

    def signed(self, body=None, pretty=False):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        content=json.dumps(self.body if body is None else body,ensure_ascii=True,
                           indent=2 if pretty else None).encode()
        signature=base64.b64encode(self.key.sign(content,padding.PKCS1v15(),hashes.SHA256())).decode()
        return ('{"sign":'+json.dumps(signature)+',"'+RESPONSE_KEY+'":').encode()+content+b',"sign_type":"RSA2"}'

    def check(self, raw=None, **kwargs):
        return self.verifier.verify(self.signed() if raw is None else raw,
                                   kwargs.get('prepared',self.record['prepared']),
                                   kwargs.get('request',self.request))

    def begin(self, request=None):
        self.orders.begin_verification(self.order_id,'v1',request_digest(request or self.request,self.merchant))

    def record_response(self, raw=None, request=None):
        return self.orders.record_signed_response(self.order_id,'v1',self.signed() if raw is None else raw,
                                                 request or self.request,self.verifier)

    def test_original_whitespace_escapes_and_nested_key_names(self):
        self.body['msg']='湫 "sign" '+RESPONSE_KEY
        self.body['detail']={'sign':'not a signature','text':'} , { :'}
        result=self.check(self.signed(pretty=True))
        self.assertEqual(100,result['amount_cents'])
        for flag in ('paid_verified','entitlement_created','response_freshness_verified','merchant_relationship_verified'):
            self.assertFalse(result[flag])
        self.assertNotIn('fictional-proof-only',json.dumps(result))

    def test_tampering_and_wrong_key_rejected(self):
        self.assert_code('invalid_signature',self.check,self.signed().replace(b'1.00',b'2.00'))
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        other=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        pem=other.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
        verifier=AlipayProofVerifier(pem,self.merchant,amount_unit='CNY_major',policy_reference='fixture')
        self.assert_code('invalid_signature',verifier.verify,self.signed(),self.record['prepared'],self.request)

    def test_malformed_envelopes_are_rejected(self):
        raw=self.signed()
        cases=[raw.replace(b'RSA2',b'RSA'),raw[:-1]+b',"sign":"duplicate"}',
               raw.replace(b'"active": true',b'"active": true,"active": false'),
               b'[]',b'{}',b'\xff',b' '*65537,raw.replace(b'"sign":"',b'"sign":"!')]
        for value in cases:
            with self.subTest(value=value[:30]),self.assertRaises(MarketError): self.check(value)

    def test_business_failure_and_nonboolean_active_rejected(self):
        for changes in ({'code':'40004'},{'sub_code':'FAIL'},{'active':False},{'active':'true'},{'active':1}):
            with self.subTest(changes=changes):
                self.assert_code('payment_not_verified',self.check,self.signed({**self.body,**changes}))

    def test_all_order_and_optional_merchant_fields_match(self):
        for name in ('trade_no','resource_id','out_trade_no','currency','seller_id','app_id'):
            with self.subTest(name=name):
                self.assert_code('payment_binding_mismatch',self.check,self.signed({**self.body,name:'wrong'}))

    def test_amount_encoding_and_exact_value(self):
        for amount in (1,1.0,'1e0','1.001','01.00','NaN','-1',' 1.00'):
            with self.subTest(amount=amount):
                self.assert_code('invalid_channel_amount',self.check,self.signed({**self.body,'amount':amount}))
        self.assert_code('payment_amount_mismatch',self.check,self.signed({**self.body,'amount':'2.00'}))
        verifier=AlipayProofVerifier(self.pem,self.merchant,amount_unit='CNY_minor',policy_reference='fixture')
        raw=self.signed({**self.body,'amount':'100'})
        self.assertEqual(100,verifier.verify(raw,self.record['prepared'],self.request)['amount_cents'])
        self.assert_code('payment_amount_mismatch',self.check,raw)

    def test_prepared_binding_and_identifiers_cannot_change(self):
        for field in ('binding_sha256','channel_identifiers'):
            changed=deepcopy(self.record['prepared']);changed[field]='wrong'
            self.assert_code('order_changed',self.check,prepared=changed)
        changed=deepcopy(self.record['prepared']);changed['binding']['price_cents']=200
        self.assert_code('order_changed',self.check,prepared=changed)

    def test_bad_key_and_missing_crypto_fail_closed(self):
        self.assert_code('invalid_verification_key',AlipayProofVerifier,b'bad',self.merchant,
                         amount_unit='CNY_major',policy_reference='fixture')
        import builtins
        original=builtins.__import__
        def blocked(name,*args,**kwargs):
            if name.startswith('cryptography'):raise ImportError('fixture')
            return original(name,*args,**kwargs)
        with patch('builtins.__import__',side_effect=blocked):
            self.assert_code('payment_dependency_missing',AlipayProofVerifier,self.pem,self.merchant,
                             amount_unit='CNY_major',policy_reference='fixture')

    def test_request_digest_binds_proof_session_and_merchant(self):
        digest=request_digest(self.request,self.merchant)
        for changes in ({'payment_proof':'other'},{'client_session':'session'},{'trade_no':'other'}):
            self.assertNotEqual(digest,request_digest({**self.request,**changes},self.merchant))
        self.assertNotEqual(digest,request_digest(self.request,{**self.merchant,'app_id':'other'}))
        self.assertEqual(digest,request_digest(dict(reversed(list(self.request.items()))),self.merchant))

    def test_retained_evidence_replay_and_no_payment_or_entitlement(self):
        self.begin();result=self.record_response()
        self.assertEqual('signature_checked_reconciliation_required',result['status'])
        self.assertEqual('signed_response_match',result['attempts'][0]['outcome'])
        self.assertEqual(result['verification'],self.orders.response_evidence(self.order_id,'v1'))
        self.assertTrue(self.record_response()['verification_replayed'])
        self.assertFalse(result['paid_verified']);self.assertEqual(0,self.count('acquisitions'))
        paid=self.buyer.acquire(self.resource,'1.0.0','still-blocked','personal_use',self.quoted['quote_id'],self.digest)
        self.assertEqual('payment_unavailable',paid['status'])
        with self.buyer.store.connect() as db:
            values=''.join(row[0] for row in db.execute("SELECT value FROM meta WHERE key LIKE 'payment_%'"))
        self.assertNotIn('fictional-proof-only',values)

    def test_unrecorded_or_different_request_cannot_attach_response(self):
        self.assert_code('verification_request_mismatch',self.record_response)
        self.begin({'trade_no':'fixture-trade','payment_proof':'different'})
        self.assert_code('verification_request_mismatch',self.record_response)
        self.assert_code('verification_not_found',self.orders.response_evidence,self.order_id,'v1')

    def test_late_valid_response_preserves_unknown_observation(self):
        self.begin();self.orders.observe(self.order_id,'v1','unknown')
        result=self.record_response()
        self.assertEqual('unknown',result['attempts'][0]['outcome'])
        self.assertFalse(result['paid_verified'])
        self.assert_code('reconciliation_required',self.orders.cancel_unstarted,self.order_id)

    def test_cannot_replace_evidence_with_different_signed_bytes(self):
        self.begin();self.record_response()
        self.assert_code('idempotency_conflict',self.record_response,self.signed(pretty=True))

    def test_trade_cannot_attach_to_second_order(self):
        self.begin();self.record_response()
        resource=self.publish(price=100,slug='another')
        detail=self.buyer.resolve(resource,'1.0.0','personal_use')
        quote=self.buyer.quote(resource,'1.0.0','personal_use','q2')
        second=self.orders.prepare(quote['quote_id'],detail['release_digest'],'p2')
        self.orders.begin_verification(second['order_id'],'v2',request_digest(self.request,self.merchant))
        raw=self.signed({**self.body,**second['prepared']['channel_identifiers']})
        self.assert_code('trade_conflict',self.orders.record_signed_response,second['order_id'],'v2',raw,self.request,self.verifier)
        self.assert_code('verification_not_found',self.orders.response_evidence,second['order_id'],'v2')

    def test_evidence_transaction_failure_rolls_back(self):
        self.begin()
        from ai_supermarket import payment_orders as module
        original=module._put; calls=[]
        def fail(db,key,value):
            calls.append(key)
            if len(calls)==2:raise OSError('fixture interruption')
            return original(db,key,value)
        with patch.object(module,'_put',side_effect=fail):
            with self.assertRaises(OSError):self.record_response()
        self.assert_code('verification_not_found',self.orders.response_evidence,self.order_id,'v1')
        self.assertEqual('verification_pending',self.orders.get(self.order_id)['status'])
        self.record_response()


if __name__=='__main__':unittest.main()
