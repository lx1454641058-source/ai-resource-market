"""本项目原创。卖方上传到买方下载的同实例HTTP链路；付款仅为隔离合成证据。"""
import hashlib
import json
from pathlib import Path
import socket
import threading
import time
import unittest
from unittest.mock import patch

from ai_supermarket.common import compact
from ai_supermarket.http_client import RemoteMarket
from ai_supermarket.purchase_download import PurchaseDownloader
from ai_supermarket.remote_task import RemoteTaskResources, RemoteTaskInterface
from ai_supermarket.tests import test_assets as assets
from ai_supermarket.tests import test_resource_bundle_http as bundles
from ai_supermarket.tests.test_asset_prepare import candidate
from ai_supermarket.tests.test_exchange import CHECKS
from tools.market_assets import prepare_candidate


class AssetHandoffHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bundles.ResourceBundleHTTPTests.setUpClass()

    def setUp(self):
        self.source = assets.AssetTests()
        self.source.setUp()
        self.addCleanup(self.source.doCleanups)
        self.fixture = bundles.ResourceBundleHTTPTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        # No product is preinstalled: the only route into this catalogue is HTTP upload.
        self.fixture.install(self.source, 'fixture:asset', assets.POLICY)
        self.f = self.fixture.f
        self.f.app = self.f.make_app(asset_uploads=True)
        import uvicorn
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        self.f.app.hosts = frozenset([f'127.0.0.1:{port}'])
        server = uvicorn.Server(uvicorn.Config(self.f.app, http='h11', ws='none', proxy_headers=False,
                                access_log=False, log_level='critical', lifespan='on'))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        def stop():
            server.should_exit = True
            thread.join(8)
            sock.close()
            self.assertFalse(thread.is_alive())
        self.addCleanup(stop)
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(server.started)
        self.url = f'http://127.0.0.1:{port}'
        self.seller = self.client('fixture', 'seller')
        self.reviewer = self.client('reviewer', 'reviewer')
        credential = self.source.root/'buyer.json'
        credential.write_text(compact({'token': self.f.token}), encoding='utf-8')
        self.buyer = RemoteMarket(self.url, credential)
        self.output = self.source.root/'downloads'
        self.output.mkdir()

    def client(self, principal, role):
        self.f.identities.create_account(principal, role)
        key = self.f.identities.issue_key(principal)
        credential = self.source.root/(principal + '.json')
        credential.write_text(compact({'token': key['token']}), encoding='utf-8')
        return RemoteMarket(self.url, credential)

    def publish_via_http(self, *, audio, price):
        folder = self.source.root/'original'
        folder.mkdir()
        value = candidate(folder, audio)
        path = folder/'candidate.json'
        path.write_text(compact(value), encoding='utf-8')
        request, raw = prepare_candidate('fixture', path, '原创素材验收', '素材', ['素材'], 'upload', price)
        self.assertEqual('draft', self.seller.upload_asset(request, raw)['status'])
        self.assertTrue(self.seller.upload_asset(request, raw)['replayed'])
        search = {'q': '素材', 'purpose': 'personal_use'}
        self.assertEqual('no_match', self.buyer.call('market_search', search)['status'])
        identity = {'id': 'fixture:asset', 'version': '1.0.0'}
        submitted = self.seller.call('seller_submit', {**identity, 'revision': 1, 'request_id': 'submit'})
        self.assertEqual('pending_review', submitted['status'])
        review = {**identity, 'revision': 1, 'decision': 'approve', 'checks': CHECKS,
                  'note': 'Original isolated fixture only; not supplier or professional approval', 'request_id': 'review'}
        self.assertEqual('error', self.seller.call('review_decide', review)['status'])
        self.assertEqual(raw, self.source.reviewer.inspect_asset('fixture:asset', '1.0.0'))
        approved = self.reviewer.call('review_decide', review)
        self.assertNotEqual('error', approved['status'], approved)
        self.assertEqual('fixture:asset', self.buyer.call('market_search', search)['items'][0]['id'])
        detail = self.buyer.call('market_resolve', {**identity, 'purpose': 'personal_use'})
        self.assertEqual(price, detail['price_cents'])
        return raw, request, detail

    def verify_files(self, result, raw, request):
        self.assertEqual('downloaded', result['status'])
        self.assertEqual(raw, Path(result['path']).read_bytes())
        self.assertEqual(request['metadata']['license_text'].encode(), Path(result['license_path']).read_bytes())
        manifest = json.loads(Path(result['manifest_path']).read_bytes())
        self.assertEqual(hashlib.sha256(raw).hexdigest(), manifest['resource']['sha256'])
        self.assertEqual(request['metadata']['rights'], manifest['rights'])
        self.assertFalse(result['task_completed'])
        for path in self.output.rglob('*'):
            if path.is_file():
                self.assertNotIn(self.f.token.encode(), path.read_bytes())
                self.assertNotIn(b'fictional-proof-only', path.read_bytes())

    def test_uploaded_paid_png_download_and_receipt_keep_one_original_purchase(self):
        raw, request, detail = self.publish_via_http(audio=False, price=100)
        downloader = PurchaseDownloader(self.buyer, 'task', 'personal_use', self.output)
        args = ('fixture:asset', '1.0.0', 'buy', detail['release_digest'])
        pending = downloader.resource(*args).result
        self.assertEqual('payment_needed', pending['status'])
        self.assertEqual(0, self.f.count('acquisitions'))
        self.f.order_id = pending['order_id']
        self.f.record = self.f.orders.get(self.f.order_id)
        self.f.body.update(self.f.record['prepared']['channel_identifiers'])
        self.f.client._enabled = True
        with patch.object(self.f.client, '_post', return_value=self.f.signed()) as channel:
            done = downloader.resource(*args, payment_proof=self.f.proof()).result
            self.verify_files(done, raw, request)
            restarted = PurchaseDownloader(self.buyer, 'task', 'personal_use', self.output)
            self.assertEqual(done, restarted.resource(*args).result)
            self.assertTrue(restarted.acknowledge(*args)['server_acknowledged'])
            self.assertTrue(restarted.acknowledge(*args)['server_acknowledged'])
            self.assertEqual(1, channel.call_count)
        self.assertEqual(100, self.f.budget.summary()['spent_cents'])
        self.assertEqual(1, self.f.count('acquisitions'))
        self.assertEqual(1, self.f.count('usage_events'))
        self.assertEqual('paid_delivered_to_host', self.f.orders.get(self.f.order_id)['status'])
        self.f.identities.revoke_key(self.f.issued['key_id'])
        denied = downloader.resource(*args).result
        self.assertEqual('unauthenticated', denied['error_code'])
        self.assertNotIn('path', denied)

    def test_uploaded_free_wav_reaches_existing_model_tool_entry_without_wallet(self):
        raw, request, detail = self.publish_via_http(audio=True, price=0)
        with patch.object(self.f.client, '_post', side_effect=AssertionError('must not contact channel')):
            task = RemoteTaskResources(self.buyer, 'task', '素材', 'personal_use', self.output)
            model = RemoteTaskInterface(task)
            identity = {'id': 'fixture:asset', 'version': '1.0.0'}
            resolved = model.call('market_resolve', identity)
            self.assertEqual(detail['release_digest'], resolved['release_digest'])
            done = model.call('market_use', {**identity, 'request_id': 'buy'})
            self.verify_files(done, raw, request)
            self.assertTrue(model.call('market_receipt', {'request_id': 'buy'})['server_acknowledged'])
        self.assertEqual(0, self.f.budget.summary()['spent_cents'])
        self.assertEqual(1, self.f.count('acquisitions'))
        self.assertEqual(1, self.f.count('usage_events'))


if __name__ == '__main__':
    unittest.main()
