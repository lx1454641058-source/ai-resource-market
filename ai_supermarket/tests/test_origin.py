"""项目原创。真实本机原站交付；模型、支付宝及真实商家不在本测试范围。"""
from copy import deepcopy
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import tempfile
import threading
import time
import unittest

from ai_supermarket.common import MarketError, compact, parse_json
from ai_supermarket.contracts import HostPolicy, validate_contract
from ai_supermarket.exchange import Exchange
from ai_supermarket.exchange_tools import ExchangeInterface
from ai_supermarket.origin import OriginClient, deliver_origin, validate_origin, verify_origin_request
from ai_supermarket.tests.test_exchange import listing, CHECKS
from ai_supermarket.tests.test_asset_prepare import png, wav


RAW = '编号,观察状态\n001,未知\n'.encode('utf-8')
POLICY = {'capabilities': ['receive_origin'], 'permissions': ['network', 'file_write'],
          'dependencies': [], 'extra_budget_cents': 0}
MANIFEST = '# 原站资料\n\n这是商品说明；原始表格保存在供应商服务器。\n'


class OriginInputTests(unittest.TestCase):
    def test_invalid_permissions_are_business_errors_not_type_errors(self):
        contract = {'version': 3, 'delivery': 'origin_file', 'capabilities': ['receive_origin'],
                    'permissions': [], 'dependencies': [],
                    'sample': {'input': 'x', 'expected': 'y', 'check': 'z'}}
        for value in (None, 12, [['network']], {'network': True}, 'network', ['network']):
            with self.subTest(value=value), self.assertRaises(MarketError) as caught:
                validate_contract({**contract, 'permissions': value})
            self.assertEqual('invalid_params', caught.exception.code)

    def test_non_ascii_and_malformed_signatures_never_reach_entitlement_check(self):
        calls = []
        for signature in ('签' * 64, 'f' * 63, 'F' * 64, None, ['x']):
            with self.subTest(signature=signature), self.assertRaises(MarketError) as caught:
                verify_origin_request(b'{}', signature, b'x' * 32, lambda r: calls.append(r))
            self.assertEqual('forbidden', caught.exception.code)
        self.assertEqual([], calls)


class OriginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'market.sqlite'
        self.seller = Exchange(self.db, 'vendor', 'seller')
        self.reviewer = Exchange(self.db, 'reviewer', 'reviewer')
        self.buyer = Exchange(self.db, 'buyer', host_policy=POLICY)
        self.key = secrets.token_bytes(32)
        self.requests = []
        self.mode = 'ok'
        self.orders = {}
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                signature = hmac.new(fixture.key, raw, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(self.headers.get('X-Market-Signature', ''), signature):
                    self.send_response(403); self.end_headers(); return
                def check_entitlement(data):
                    try:
                        with fixture.buyer.store.connect() as db:
                            acquired = fixture.buyer._owned(db, data['acquisition_id'])
                            return (acquired['principal'] == data['buyer'] and acquired['resource_id'] == data['id']
                                    and acquired['version'] == data['version'] and acquired['purpose'] == data['purpose'])
                    except MarketError:
                        return False
                try:
                    data, fingerprint = verify_origin_request(raw, self.headers.get('X-Market-Signature', ''),
                                                               fixture.key, check_entitlement)
                except MarketError:
                    self.send_response(403); self.end_headers(); return
                fixture.requests.append(data)
                if (data['buyer'] != 'buyer' or data['purpose'] != 'personal_use'
                        or data['origin']['resource_key'] != 'table-original'
                        or data['origin']['provider'] != 'provider-demo'
                        or not data['issued_at'] <= time.time() <= data['expires_at']):
                    self.send_response(403); self.end_headers(); return
                if fixture.mode == 'redirect':
                    self.send_response(302); self.send_header('Location', '/never'); self.end_headers(); return
                if fixture.mode == 'error':
                    self.send_response(503); self.end_headers(); return
                old = fixture.orders.get(data['order_id'])
                if old is not None and old != fingerprint:
                    self.send_response(409); self.end_headers(); return
                fixture.orders[data['order_id']] = fingerprint
                self.send_response(200)
                self.send_header('Content-Type', 'text/html' if fixture.mode == 'html' else data['origin']['media_type'])
                self.end_headers()
                body = fixture.body
                self.wfile.write(body + b'bad' if fixture.mode == 'corrupt' else body)
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = OriginClient('provider-demo', f'http://127.0.0.1:{self.server.server_port}/fulfill', self.key, 'vendor-connection-1')
        self.body = RAW

    def metadata(self, price=0):
        meta = listing(price)
        meta['title'] = '原站观察记录表'
        meta['keywords'] = ['观察记录']
        meta['allowed_uses'] = ['personal_use']
        meta['usage_contract'].update(version=3, delivery='origin_file', capabilities=['receive_origin'],
                                      permissions=['network', 'file_write'])
        meta['origin'] = {'provider': 'provider-demo', 'resource_key': 'table-original',
            'sha256': hashlib.sha256(RAW).hexdigest(), 'size': len(RAW), 'media_type': 'text/csv'}
        return meta

    def publish(self, price=0, slug='table', version='1.0.0', meta=None):
        meta = meta or self.metadata(price)
        seller = ExchangeInterface(self.seller)
        draft = seller.call('seller_draft', {'slug': slug, 'version': version, 'metadata': meta,
            'content': MANIFEST, 'request_id': 'draft-' + slug + version})
        self.assertEqual('draft', draft['status'], draft)
        seller.call('seller_submit', {'id': draft['id'], 'version': version, 'revision': 1,
                                     'request_id': 'submit-' + slug + version})
        self.reviewer.review(draft['id'], version, 1, 'approve', CHECKS,
            '仅原创数据与原站交付工程检查，不是商业或独立审核。', 'review-' + slug + version)
        return draft['id']

    def acquire(self, resource='vendor:table', request='acquire', buyer=None):
        buyer = buyer or self.buyer
        detail = buyer.resolve(resource, '1.0.0', 'personal_use')
        return buyer.acquire(resource, '1.0.0', request, purpose='personal_use',
                             expected_release_digest=detail['release_digest'])

    def deliver(self, acquired, client=None, folder='output', request='delivery'):
        return deliver_origin(self.buyer, acquired['acquisition_id'], 'task', request,
                              client or self.client, self.root / folder)

    def usage_count(self):
        with self.buyer.store.connect() as db:
            return db.execute('SELECT COUNT(*) FROM usage_events').fetchone()[0]

    def test_original_file_not_in_market_then_actual_http_delivery(self):
        self.publish()
        found = self.buyer.search('观察记录', purpose='personal_use')
        self.assertEqual(1, len(found['items']))
        self.assertNotIn('content', found['items'][0])
        with self.buyer.store.connect() as db:
            self.assertEqual(MANIFEST, db.execute('SELECT content FROM submissions').fetchone()[0])
        acquired = self.acquire()
        read = self.buyer.read(acquired['acquisition_id'], 'task', 'plain-read')
        self.assertEqual('host_transfer_required', read['status'])
        self.assertEqual(0, self.usage_count())
        result = self.deliver(acquired)
        self.assertEqual(RAW, Path(result['path']).read_bytes())
        self.assertEqual(self.metadata()['license_text'], Path(result['license_path']).read_text(encoding='utf-8'))
        self.assertNotIn('content', result)
        self.assertEqual(1, self.usage_count())
        self.assertEqual(1, len(self.requests))

    def test_reopen_reuses_file_and_original_usage_without_network(self):
        self.publish()
        acquired = self.acquire()
        first = self.deliver(acquired)
        self.buyer = Exchange(self.db, 'buyer', host_policy=POLICY)
        self.mode = 'error'
        second = self.deliver(acquired)
        self.assertTrue(second['cache_hit'])
        self.assertEqual(first['usage_id'], second['usage_id'])
        self.assertEqual(first['order_id'], second['order_id'])
        self.assertEqual(1, len(self.requests))

    def test_paid_item_never_fetches_or_records_usage(self):
        self.publish(100)
        acquired = self.acquire()
        self.assertEqual('payment_unavailable', acquired['status'])
        with self.assertRaises(MarketError):
            deliver_origin(self.buyer, 'invented-paid', 'task', 'delivery', self.client, self.root/'output')
        self.assertEqual([], self.requests)
        self.assertEqual(0, self.usage_count())

    def test_missing_policy_blocks_before_origin_request(self):
        self.publish()
        acquired = self.acquire()
        self.buyer.host_policy = HostPolicy()
        with self.assertRaises(MarketError):
            self.deliver(acquired)
        self.assertEqual([], self.requests)

    def test_cross_buyer_cannot_read_another_entitlement(self):
        self.publish()
        acquired = self.acquire()
        self.buyer = Exchange(self.db, 'other-buyer', host_policy=POLICY)
        with self.assertRaises(MarketError):
            self.deliver(acquired)
        self.assertEqual([], self.requests)

    def test_suspended_resource_blocks_cached_read(self):
        self.publish()
        acquired = self.acquire()
        self.deliver(acquired)
        self.reviewer.availability('vendor:table', '1.0.0', 1, 'suspended', '工程演练隔离', 'isolate')
        with self.assertRaises(MarketError):
            self.deliver(acquired)
        self.assertEqual(1, len(self.requests))

    def test_suspension_during_network_does_not_commit_success(self):
        self.publish()
        acquired = self.acquire()
        fetch = self.client.fetch
        def isolate(request, origin):
            raw = fetch(request, origin)
            self.reviewer.availability('vendor:table', '1.0.0', 1, 'suspended', '工程演练隔离', 'isolate')
            return raw
        self.client.fetch = isolate
        with self.assertRaises(MarketError):
            self.deliver(acquired)
        self.assertFalse((self.root/'output/resource.csv').exists())
        self.assertEqual(0, self.usage_count())

    def test_provider_errors_and_corrupt_bytes_never_count_as_delivery(self):
        self.publish()
        acquired = self.acquire()
        for mode in ('redirect', 'error', 'html', 'corrupt'):
            self.mode = mode
            with self.subTest(mode=mode), self.assertRaises(MarketError):
                self.deliver(acquired, folder=mode)
            self.assertFalse((self.root/mode/'resource.csv').exists())
        self.assertEqual(0, self.usage_count())
        self.assertEqual(4, len(self.requests))

    def test_unknown_response_stops_and_explicit_original_retry_reuses_order(self):
        self.publish()
        acquired = self.acquire()
        fetch = self.client.fetch
        def lose(request, origin):
            fetch(request, origin)
            raise MarketError('origin_unconfirmed', '测试丢失响应')
        self.client.fetch = lose
        with self.assertRaises(MarketError):
            self.deliver(acquired)
        self.assertEqual(1, len(self.requests))
        self.assertEqual(0, self.usage_count())
        self.client.fetch = fetch
        self.deliver(acquired)
        self.assertEqual(2, len(self.requests))
        self.assertEqual(1, len(self.orders))
        self.assertEqual(1, self.usage_count())

    def test_local_partial_file_and_foreign_directory_not_overwritten(self):
        self.publish()
        acquired = self.acquire()
        result = self.deliver(acquired)
        target = Path(result['path'])
        target.write_bytes(b'partial')
        with self.assertRaises(MarketError):
            self.deliver(acquired)
        self.assertEqual(b'partial', target.read_bytes())
        foreign = self.root/'foreign'; foreign.mkdir()
        with self.assertRaises(MarketError):
            self.deliver(acquired, folder='foreign')
        self.assertEqual(1, len(self.requests))

    def test_other_connection_cannot_reuse_output(self):
        self.publish()
        acquired = self.acquire()
        self.deliver(acquired)
        client = OriginClient('provider-demo', self.client.endpoint, self.key, 'another-connection')
        with self.assertRaises(MarketError):
            self.deliver(acquired, client)

    def test_same_request_for_other_acquisition_blocks_before_network(self):
        self.publish()
        self.publish(slug='other')
        self.deliver(self.acquire())
        with self.assertRaises(MarketError):
            self.deliver(self.acquire('vendor:other', 'acquire-other'), folder='different')
        self.assertEqual(1, len(self.requests))

    def test_content_or_origin_metadata_tamper_blocks_delivery(self):
        self.publish()
        acquired = self.acquire()
        with self.seller.store.connect(write=True) as db:
            row = db.execute('SELECT metadata FROM submissions').fetchone()
            meta = parse_json(row[0]); meta['origin']['resource_key'] = 'different'
            db.execute('UPDATE submissions SET metadata=?', (compact(meta),))
        with self.assertRaises(MarketError):
            self.deliver(acquired)
        self.assertEqual([], self.requests)

    def test_unbound_acquisition_cannot_download(self):
        self.publish()
        acquired = self.buyer.acquire('vendor:table', '1.0.0', 'legacy', purpose='personal_use')
        with self.assertRaises(MarketError):
            self.deliver(acquired)
        self.assertEqual([], self.requests)

    def test_provider_identity_and_credential_are_host_bound(self):
        self.publish()
        acquired = self.acquire()
        for provider, key in (('different', self.key), ('provider-demo', b'x'*32)):
            client = OriginClient(provider, self.client.endpoint, key, 'bad-config')
            with self.assertRaises(MarketError):
                self.deliver(acquired, client, folder=provider)
        self.assertEqual(0, self.usage_count())

    def test_invalid_description_and_endpoint_rejected(self):
        for name, value in [('sha256', 'x'*64), ('size', True), ('size', 2_097_153),
                            ('provider', '../evil'), ('media_type', 'text/html')]:
            origin = deepcopy(self.metadata()['origin']); origin[name] = value
            with self.subTest(name=name, value=value), self.assertRaises(MarketError):
                validate_origin(origin)
        for endpoint in ('http://example.com/file', 'https://u:p@example.com', 'https://example.com/?key=x'):
            with self.assertRaises(MarketError):
                OriginClient('vendor', endpoint, self.key, 'binding')

    def test_remote_media_type_mismatch_is_rejected(self):
        meta = self.metadata()
        meta['kind'] = 'image'
        with self.assertRaises(MarketError):
            self.seller.draft('bad', '1.0.0', meta, MANIFEST, 'bad')

    def test_revision_preserves_remote_binding(self):
        meta = self.metadata()
        self.seller.draft('revision', '1.0.0', meta, MANIFEST, 'draft')
        revised = self.seller.revise('vendor:revision', '1.0.0', 1, meta, MANIFEST + '\n修订说明。', 'revise')
        self.assertEqual(2, revised['revision'])
        self.seller.submit('vendor:revision', '1.0.0', 2, 'submit')
        approved = self.reviewer.review('vendor:revision', '1.0.0', 2, 'approve', CHECKS, '原创样例核验', 'review')
        self.assertEqual('approved', approved['status'])
        acquired = self.acquire('vendor:revision')
        self.assertEqual('delivered', self.deliver(acquired)['status'])

    def test_images_audio_pdf_and_markdown_transfer_without_market_blobs(self):
        for index, (kind, mime, body) in enumerate((
                ('image', 'image/png', png()), ('audio', 'audio/wav', wav()),
                ('template', 'application/pdf', b'%PDF-1.4\n% original transport fixture\n'),
                ('skill', 'text/markdown', b'# Original fixture\n'))):
            self.body = body
            meta = self.metadata()
            meta['kind'] = kind
            meta['origin'].update(media_type=mime, size=len(body), sha256=hashlib.sha256(body).hexdigest())
            resource = self.publish(slug='media-' + str(index), meta=meta)
            acquired = self.acquire(resource, 'acquire-' + str(index))
            result = self.deliver(acquired, folder='media-' + str(index), request='media-' + str(index))
            self.assertEqual(body, Path(result['path']).read_bytes())
        with self.buyer.store.connect() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='asset_objects'").fetchone())

    def test_raw_blob_import_cannot_mix_remote_contract(self):
        meta = self.metadata()
        meta['kind'] = 'image'
        meta['origin']['media_type'] = 'image/png'
        with self.assertRaises(MarketError):
            self.seller.draft_asset('mixed', '1.0.0', meta, MANIFEST, b'anything', 'mixed')

    def test_provider_rejects_signed_but_nonexistent_entitlement(self):
        self.publish()
        self.deliver(self.acquire())
        request = deepcopy(self.requests[0])
        request['acquisition_id'] = 'invented'
        with self.assertRaises(MarketError):
            self.client.fetch(request, request['origin'])
        self.assertEqual(1, len(self.orders))

    def test_provider_rejects_expired_signed_request(self):
        self.publish()
        self.deliver(self.acquire())
        request = deepcopy(self.requests[0])
        request.update(issued_at=1, expires_at=61)
        with self.assertRaises(MarketError):
            self.client.fetch(request, request['origin'])

    def test_provider_rejects_changed_payload_under_original_order(self):
        self.publish()
        self.deliver(self.acquire())
        request = deepcopy(self.requests[0])
        request['task_id'] = 'different-task'
        with self.assertRaises(MarketError):
            self.client.fetch(request, request['origin'])
        self.assertEqual(1, len(self.orders))


if __name__ == '__main__':
    unittest.main()
