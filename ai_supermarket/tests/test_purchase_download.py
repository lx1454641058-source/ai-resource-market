"""来源：本项目原创。下载器隔离验证；伪凭据及合成资源，不调用真实钱包。"""
from copy import deepcopy
from email.message import Message
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError
import zipfile

from ai_supermarket.common import MarketError, compact
from ai_supermarket.http_client import RemoteMarket
from ai_supermarket.purchase_download import PurchaseDownloader, validate_download, MAX_ZIP, _save


EXPECTED={'id':'fixture:table','version':'1.0.0','task_id':'task','request_id':'read',
          'purpose':'personal_use','release_digest':'a'*64}
RESOURCE='名称,观察\n纸舟,待验证\n'.encode()
LICENSE='原创合成工程测试文件；只用于隔离测试。'.encode()
def sha(raw):return hashlib.sha256(raw).hexdigest()


def parts():
    manifest={'format':'market-resource-zip-v1',**EXPECTED,'acquisition_id':'ap_fixture','usage_id':'u_fixture',
        'resource':{'path':'resource.csv','media_type':'text/csv','sha256':sha(RESOURCE),'size':len(RESOURCE)},
        'license':{'path':'LICENSE.txt','name':'Fixture-License','sha256':sha(LICENSE),'size':len(LICENSE)},
        'source_statement':'本项目原创测试数据','task_completed':False,'execution_authorized':False,
        'client_receipt_confirmed':False}
    return {'resource.csv':RESOURCE,'LICENSE.txt':LICENSE,'manifest.json':compact(manifest).encode()}


def pack(contents=None,*,compression=zipfile.ZIP_STORED,attributes=0o100644):
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as archive:
        for name,raw in (contents or parts()).items():
            info=zipfile.ZipInfo(name,(1980,1,1,0,0,0));info.create_system=3
            info.external_attr=attributes<<16;info.compress_type=compression
            archive.writestr(info,raw)
    return output.getvalue()


class BundleValidationTests(unittest.TestCase):
    def bad(self,raw,expected=None):
        with self.assertRaises(MarketError):validate_download(raw,sha(raw),expected or EXPECTED)

    def test_valid_original_resource_license_and_scope(self):
        raw=pack();manifest,contents=validate_download(raw,sha(raw),EXPECTED)
        self.assertEqual(RESOURCE,contents['resource.csv']);self.assertEqual(LICENSE,contents['LICENSE.txt'])
        self.assertEqual(EXPECTED['release_digest'],manifest['release_digest'])

    def test_task_request_purpose_and_release_mismatch(self):
        raw=pack()
        for key in EXPECTED:
            with self.subTest(key=key):self.bad(raw,{**EXPECTED,key:'different'})

    def test_payload_digest_length_and_false_completion_claims(self):
        for change in ('digest','size','license','completion','unknown','duplicate'):
            content=parts();manifest=json.loads(content['manifest.json'])
            if change=='digest':manifest['resource']['sha256']='b'*64
            if change=='size':manifest['resource']['size']=True
            if change=='license':content['LICENSE.txt']=b'changed'
            if change=='completion':manifest['task_completed']=True
            if change=='unknown':manifest['unreviewed']='extra'
            content['manifest.json']=compact(manifest).encode()
            if change=='duplicate':content['manifest.json']=b'{"id":"one","id":"two"}'
            with self.subTest(change=change):self.bad(pack(content))

    def test_archive_traversal_extra_symlink_compression_and_trailing_data(self):
        content=parts();content['../outside']=b'x';self.bad(pack(content))
        content=parts();content['extra']=b'x';self.bad(pack(content))
        self.bad(pack(attributes=0o120777));self.bad(pack(compression=zipfile.ZIP_DEFLATED))
        self.bad(pack()+b'hidden');self.bad(pack()[:-1]);self.bad(b'x'*(MAX_ZIP+1))

    def test_outer_digest_required_and_member_path_bound_to_media(self):
        with self.assertRaises(MarketError):validate_download(pack(),'0'*64,EXPECTED)
        content=parts();manifest=json.loads(content['manifest.json']);manifest['resource']['path']='../outside'
        content['manifest.json']=compact(manifest).encode();self.bad(pack(content))

    def test_invalid_rights_or_attribution_do_not_pass_schema_check(self):
        for rights in (None,{}, {'rights_holder':'fixture','attribution':{'required':'false','text':''},
                                'modification':'allowed','source_redistribution':'forbidden',
                                'training':'forbidden','restrictions':'fixture'}):
            content=parts();manifest=json.loads(content['manifest.json']);manifest['rights']=rights
            content['manifest.json']=compact(manifest).encode();self.bad(pack(content))


class Response(io.BytesIO):
    def __init__(self,raw=None,code=200,headers=None):
        raw=pack() if raw is None else raw
        super().__init__(raw);self.code=code;self.headers=Message()
        for name,value in (headers or [('Content-Type','application/zip'),('Content-Length',str(len(raw))),
                                      ('X-Bundle-SHA256',sha(raw))]):self.headers.add_header(name,value)


class DownloadClientTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.creds=self.root/'credential.json';self.token='ak_'+'1'*32+'.'+'x'*43
        self.creds.write_text(compact({'token':self.token}),encoding='utf-8')
        self.remote=RemoteMarket('https://market.example',self.creds)
        self.output=self.root/'downloads';self.output.mkdir()
        self.client=PurchaseDownloader(self.remote,'task','personal_use',self.output)
        self.remote.opener=Mock();self.remote.opener.open.side_effect=lambda *a,**k:Response()

    def fetch(self,**kwargs):
        return self.client.resource(EXPECTED['id'],EXPECTED['version'],EXPECTED['request_id'],
                                    EXPECTED['release_digest'],**kwargs)

    def test_download_replay_and_recreated_client_preserve_original_files(self):
        first=self.fetch().result
        self.assertEqual(RESOURCE,Path(first['path']).read_bytes());self.assertEqual(LICENSE,Path(first['license_path']).read_bytes())
        receipt=json.loads(Path(first['receipt_path']).read_bytes());self.assertTrue(receipt['local_files_verified'])
        self.assertFalse(receipt['server_acknowledged']);self.assertFalse(first['task_completed'])
        before={p.name:p.read_bytes() for p in Path(first['path']).parent.iterdir()}
        self.client=PurchaseDownloader(self.remote,'task','personal_use',self.output)
        self.assertEqual(first,self.fetch().result)
        self.assertEqual(before,{p.name:p.read_bytes() for p in Path(first['path']).parent.iterdir()})
        self.assertNotIn(self.token,compact(first));self.assertNotIn(self.token,str(before))
        self.assertEqual(2,self.remote.opener.open.call_count)

    def test_bill_is_separate_from_model_summary_and_release_checked(self):
        result={'status':'payment_needed','request_id':'read','release_digest':'a'*64,'order_id':'order'}
        def response():return Response(compact(result).encode(),402,[('Content-Type','application/json'),('Payment-Needed','ZmljdGlvbmFs')])
        self.remote.opener.open.side_effect=lambda *a,**k:response()
        first=self.fetch();self.assertEqual('ZmljdGlvbmFs',first.payment_needed)
        self.assertNotIn(first.payment_needed,repr(first));self.assertNotIn('headers',first.result)
        result['release_digest']='b'*64
        with self.assertRaises(MarketError):self.fetch()
        self.assertEqual(['intent.json'],[p.name for p in next(self.output.iterdir()).iterdir()])

    def test_changed_resource_same_request_fails_before_network(self):
        self.fetch();calls=self.remote.opener.open.call_count
        with self.assertRaises(MarketError) as caught:self.client.resource('other','1.0.0','read','a'*64)
        self.assertEqual('idempotency_conflict',caught.exception.code)
        self.assertEqual(calls,self.remote.opener.open.call_count)

    def test_network_error_is_unknown_no_retry_or_saved_resource(self):
        self.remote.opener.open.side_effect=URLError('fictional network secret')
        with self.assertRaises(MarketError) as caught:self.fetch()
        self.assertEqual('transport_unknown',caught.exception.code);self.assertNotIn('secret',str(caught.exception))
        self.assertEqual(1,self.remote.opener.open.call_count)
        self.assertEqual(['intent.json'],[p.name for p in next(self.output.iterdir()).iterdir()])
        self.remote.opener.open.side_effect=lambda *a,**k:Response()
        self.assertEqual('downloaded',self.fetch().result['status'])

    def test_invalid_archive_or_length_never_creates_resource(self):
        for response in [Response(pack()[:-1]),Response(headers=[('Content-Type','application/zip'),('Content-Length','1'),('X-Bundle-SHA256',sha(pack()))]),
                         Response(headers=[('Content-Type','application/zip'),('X-Bundle-SHA256',sha(pack())),('X-Bundle-SHA256',sha(pack()))])]:
            self.remote.opener.open.side_effect=lambda *a,**k:response
            with self.assertRaises(MarketError):self.fetch()
            self.assertEqual(['intent.json'],[p.name for p in next(self.output.iterdir()).iterdir()])

    def test_local_interruption_recovers_only_missing_files(self):
        original=_save
        def interrupted(path,raw):
            if Path(path).name=='LICENSE.txt':raise OSError('fixture write interruption')
            return original(path,raw)
        with patch('ai_supermarket.purchase_download._save',side_effect=interrupted):
            with self.assertRaises(MarketError) as caught:self.fetch()
            self.assertEqual('download_not_saved',caught.exception.code)
        folder=next(self.output.iterdir());self.assertFalse((folder/'receipt.json').exists())
        self.assertEqual(RESOURCE,(folder/'resource.csv').read_bytes())
        self.assertEqual('downloaded',self.fetch().result['status'])

    def test_existing_modified_file_is_not_replaced_or_reported_success(self):
        first=self.fetch().result;target=Path(first['path']);target.write_bytes(b'user-edited')
        with self.assertRaises(MarketError) as caught:self.fetch()
        self.assertEqual('output_conflict',caught.exception.code);self.assertEqual(b'user-edited',target.read_bytes())

    def test_endpoint_change_and_link_paths_are_refused(self):
        self.remote.base_url='https://other.example'
        with self.assertRaises(MarketError):self.fetch()
        self.assertEqual(0,self.remote.opener.open.call_count)
        with patch.object(Path,'is_symlink',return_value=True):
            with self.assertRaises(MarketError):PurchaseDownloader(self.remote,'task','personal_use',self.output)

    def test_redirect_and_inconsistent_json_do_not_return_wallet_bill(self):
        for code,result,extra in [(302,{},[]),(200,{'status':'payment_needed'},[]),
                                   (202,{'status':'purchase_pending'},[('Payment-Needed','YQ==')]),
                                   (500,{'status':'read'},[])]:
            response=Response(compact(result).encode(),code,[('Content-Type','application/json')]+extra)
            self.remote.opener.open.side_effect=lambda *a,**k:response
            with self.assertRaises(MarketError):self.fetch()

    def test_pending_status_preserves_original_request_without_wallet_action(self):
        response=Response(compact({'status':'purchase_pending'}).encode(),202,[('Content-Type','application/json')])
        self.remote.opener.open.side_effect=lambda *a,**k:response
        result=self.fetch();self.assertIsNone(result.payment_needed)
        self.assertEqual('purchase_pending',result.result['status']);self.assertEqual(1,self.remote.opener.open.call_count)

    def test_atomic_save_concurrent_equal_files_and_failed_commit(self):
        from concurrent.futures import ThreadPoolExecutor
        target=self.root/'complete.csv'
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: _save(target,RESOURCE),range(8)))
        self.assertEqual(RESOURCE,target.read_bytes())
        with self.assertRaises(MarketError):_save(target,b'different')
        self.assertEqual(RESOURCE,target.read_bytes())
        failed=self.root/'failed.csv'
        with patch('ai_supermarket.purchase_download.os.link',side_effect=OSError('fixture commit failure')):
            with self.assertRaises(OSError):_save(failed,RESOURCE)
        self.assertFalse(failed.exists());self.assertEqual([],list(self.root.glob('.download-*')))


class LiveDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ai_supermarket.tests.test_resource_bundle_http import ResourceBundleHTTPTests
        ResourceBundleHTTPTests.setUpClass()

    def setUp(self):
        from ai_supermarket.tests.test_resource_bundle_http import ResourceBundleHTTPTests
        self.fixture=ResourceBundleHTTPTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)

    def connect(self,asset=False):
        import socket
        import threading
        import time
        import uvicorn
        source=self.fixture.asset(audio=True) if asset else self.fixture.origin()
        f=self.fixture.f;self.f=f;self.source=source
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        f.app.hosts=frozenset({f'127.0.0.1:{port}'})
        server=uvicorn.Server(uvicorn.Config(f.app,http='h11',ws='none',proxy_headers=False,
            access_log=False,log_level='critical',lifespan='on',timeout_graceful_shutdown=3))
        thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
        def stop():
            server.should_exit=True;thread.join(5);sock.close();self.assertFalse(thread.is_alive())
        self.addCleanup(stop)
        deadline=time.monotonic()+5
        while not server.started and thread.is_alive() and time.monotonic()<deadline:time.sleep(.01)
        self.assertTrue(server.started)
        credential=source.root/'test-credential.json'
        credential.write_text(compact({'token':f.token}),encoding='utf-8')
        output=source.root/'client-files';output.mkdir()
        self.remote=RemoteMarket(f'http://127.0.0.1:{port}',credential)
        self.downloader=PurchaseDownloader(self.remote,'task','personal_use',output)
        self.args=(f.resource,'1.0.0','buy',f.buyer.resolve(f.resource,'1.0.0','personal_use')['release_digest'])

    def test_real_client_paid_origin_receipt_restart_and_no_duplicate_payment(self):
        from ai_supermarket.tests.test_origin import RAW
        self.connect();f=self.f
        first=self.downloader.resource(*self.args)
        self.assertEqual('payment_needed',first.result['status']);self.assertTrue(first.payment_needed)
        self.assertNotIn(first.payment_needed,repr(first))
        f.order_id=first.result['order_id'];f.record=f.orders.get(f.order_id)
        f.body.update(f.record['prepared']['channel_identifiers']);f.client._enabled=True
        with patch.object(f.client,'_post',return_value=f.signed()) as channel:
            done=self.downloader.resource(*self.args,payment_proof=f.proof()).result
            self.assertEqual(1,channel.call_count)
        self.assertEqual(RAW,Path(done['path']).read_bytes())
        self.assertEqual(self.source.metadata()['license_text'].encode(),Path(done['license_path']).read_bytes())
        self.source.mode='error';calls=len(self.source.requests)
        restarted=PurchaseDownloader(self.remote,'task','personal_use',self.downloader.root)
        self.assertEqual(done,restarted.resource(*self.args).result)
        self.assertEqual(calls,len(self.source.requests));self.assertEqual(100,f.budget.summary()['spent_cents'])
        self.assertEqual(1,f.count('acquisitions'));self.assertFalse(done['server_acknowledged'])
        for path in self.downloader.root.rglob('*'):
            if path.is_file():
                raw=path.read_bytes();self.assertNotIn(f.token.encode(),raw)
                self.assertNotIn(b'fictional-proof-only',raw)

    def test_real_client_free_wav_with_license_and_revoked_identity_stops_replay(self):
        self.connect(asset=True)
        done=self.downloader.resource(*self.args).result
        self.assertEqual('.wav',Path(done['path']).suffix)
        self.assertIn('rights',json.loads(Path(done['manifest_path']).read_bytes()))
        self.assertEqual(0,self.f.budget.summary()['spent_cents'])
        self.f.identities.revoke_key(self.f.issued['key_id'])
        denied=self.downloader.resource(*self.args).result
        self.assertEqual('unauthenticated',denied['error_code']);self.assertNotIn('path',denied)


if __name__=='__main__':unittest.main()
