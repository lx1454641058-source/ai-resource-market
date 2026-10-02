"""本项目原创。素材网络提交及权限/格式边界；全部为隔离原创夹具，无外部上传。"""
import asyncio
from copy import deepcopy
from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from ai_supermarket.asset_upload import encode, decode, describe, MAGIC, MAX_BODY, MEDIA_TYPE, ROUTE
from ai_supermarket.assets import AssetStore, media_info
from ai_supermarket.common import MarketError, compact
from ai_supermarket.exchange import Exchange
from ai_supermarket.http_app import MarketHTTP, Limits
from ai_supermarket.http_client import RemoteMarket
from ai_supermarket.identity import Identities
from ai_supermarket.tests.test_asset_prepare import candidate
from ai_supermarket.tests.test_exchange import CHECKS
from tools.market_assets import prepare_candidate

POLICY = {'capabilities': ['read_markdown', 'receive_asset'], 'permissions': [],
          'dependencies': [], 'extra_budget_cents': 0}

class AssetUploadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.root = Path(folder.name); self.db = self.root / 'market.sqlite'
        AssetStore(self.db)
        self.ids = Identities(self.root / 'identities.sqlite'); self.keys = {}
        for name, role in [('vendor', 'seller'), ('other', 'seller'), ('buyer', 'buyer'), ('reviewer', 'reviewer')]:
            self.ids.create_account(name, role, POLICY if role == 'buyer' else None)
            self.keys[name] = self.ids.issue_key(name)
        self.app = MarketHTTP(self.db, self.root / 'identities.sqlite',
                              allowed_hosts=['market.test'], asset_uploads=True)
        self.req, self.raw = self.fixture()

    def fixture(self, audio=False):
        folder = self.root / ('audio' if audio else 'image'); folder.mkdir()
        value = candidate(folder, audio); value['id'] = 'vendor:asset'
        path = folder / 'candidate.json'; path.write_text(compact(value), encoding='utf-8')
        return prepare_candidate('vendor', path, '素材验证', '原创夹具', ['素材'], 'upload-one', 125)

    def count(self):
        with AssetStore(self.db).connect() as db:
            return tuple(db.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0]
                         for t in ('submissions', 'asset_objects', 'asset_links'))

    async def invoke(self, body=None, *, principal='vendor', path=ROUTE, method='POST', headers=(), receive=None, peer='127.0.0.1'):
        h = [(b'host', b'market.test'), (b'content-type', MEDIA_TYPE.encode())]
        if principal is not None:
            h.append((b'authorization', ('Bearer ' + self.keys[principal]['token']).encode()))
        messages = []
        if receive is None:
            async def receive():
                return {'type': 'http.request', 'body': body if body is not None else encode(self.req, self.raw)}
        async def send(value): messages.append(value)
        await self.app({'type': 'http', 'path': path, 'method': method, 'scheme': 'http',
                        'headers': h + list(headers), 'client': (peer, 1), 'query_string': b''}, receive, send)
        return messages[0]['status'], json.loads(messages[1]['body'])

    async def test_seller_upload_stays_draft_and_exact_replay_does_not_duplicate(self):
        code, result = await self.invoke(); self.assertEqual((200, 'draft'), (code, result['status']))
        code, replay = await self.invoke(); self.assertTrue(replay['replayed'])
        self.assertEqual((1, 1, 1), self.count())
        self.assertEqual('no_match', Exchange(self.db, 'buyer').search('素材')['status'])
        self.assertEqual(self.raw, Exchange(self.db, 'reviewer', 'reviewer').inspect_asset('vendor:asset', '1.0.0'))
        self.assertNotIn('asset_bytes', result)
        changed = deepcopy(self.req); changed['metadata']['price_cents'] = 1
        self.assertEqual('idempotency_conflict', (await self.invoke(encode(changed, self.raw)))[1]['error_code'])
        self.assertEqual((1, 1, 1), self.count())

    async def test_seller_binding_cannot_be_spoofed(self):
        self.assertEqual(403, (await self.invoke(principal='other'))[0])
        self.assertEqual((0, 0, 0), self.count())

    async def test_auth_and_role_checked_before_reading_body(self):
        async def unread(): self.fail('body must not be read')
        for principal, code in [(None, 401), ('buyer', 403), ('reviewer', 403)]:
            self.assertEqual(code, (await self.invoke(principal=principal, receive=unread))[0])

    async def test_revocation_during_body_read_prevents_write(self):
        async def revoke():
            self.ids.revoke_key(self.keys['vendor']['key_id'])
            return {'type': 'http.request', 'body': encode(self.req, self.raw)}
        self.assertEqual(401, (await self.invoke(receive=revoke))[0])
        self.assertEqual((0, 0, 0), self.count())

    async def test_disabled_route_and_buyer_discovery(self):
        code, buyer = await self.invoke(path='/v1/tools', method='GET', principal='buyer')
        self.assertEqual(200, code); self.assertNotIn('asset_upload', buyer)
        code, seller = await self.invoke(path='/v1/tools', method='GET')
        self.assertEqual(describe(), seller['asset_upload'])
        self.app = MarketHTTP(self.db, self.root/'identities.sqlite', allowed_hosts=['market.test'])
        self.assertEqual(404, (await self.invoke())[0])

    async def test_length_content_encoding_and_tls_are_enforced(self):
        for headers, code in [([(b'content-length', str(MAX_BODY + 1).encode())], 413),
                              ([(b'content-encoding', b'gzip')], 415),
                              ([(b'payment-proof', b'not-accepted')], 400),
                              ([(b'content-length', b'1')], 400)]:
            self.assertEqual(code, (await self.invoke(headers=headers))[0])
        self.assertEqual(403, (await self.invoke(peer='192.0.2.1'))[0])
        self.assertEqual((0, 0, 0), self.count())

    async def test_malformed_header_truncation_and_tampered_bytes_never_create_draft(self):
        good = encode(self.req, self.raw)
        for bad in (b'', b'other', MAGIC + (131073).to_bytes(4,'big') + b'x', good[:-1], good + b'x'):
            self.assertGreaterEqual((await self.invoke(bad))[0], 400)
        self.assertEqual((0, 0, 0), self.count())

    async def test_large_wav_uses_bounded_binary_limit_not_json_tool_limit(self):
        request, raw = self.fixture(audio=True)
        data = b'\0' * 200000
        raw = raw[:4] + struct.pack('<I', 36 + len(data)) + raw[8:40] + struct.pack('<I', len(data)) + data
        request['metadata']['asset'].update(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw), media=media_info(raw, 'audio'))
        body = encode(request, raw)
        self.assertGreater(len(body), Limits().body_bytes)
        self.assertEqual(200, (await self.invoke(body))[0])
        self.app.limits = replace(Limits(), asset_body_bytes=100)
        self.assertEqual(413, (await self.invoke(body))[0])

    async def test_paused_market_cannot_receive_assets(self):
        with AssetStore(self.db).connect(write=True) as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('recovery_state','pending')")
        self.assertEqual('recovery_required', (await self.invoke())[1]['error_code'])

    async def test_opt_in_does_not_create_or_migrate_a_database(self):
        missing = self.root/'missing.sqlite'
        with self.assertRaises((MarketError, ValueError)):
            MarketHTTP(missing, self.root/'identities.sqlite', allowed_hosts=['market.test'], asset_uploads=True)
        self.assertFalse(missing.exists())
        old = self.root/'old.sqlite'; Exchange(old, 'vendor', 'seller')
        with self.assertRaises((MarketError, ValueError)):
            MarketHTTP(old, self.root/'identities.sqlite', allowed_hosts=['market.test'], asset_uploads=True)
        self.assertEqual('1', Exchange(old,'vendor','seller').store.version)

    @unittest.skipUnless(importlib.util.find_spec('uvicorn'), 'Requires existing HTTP runtime')
    async def test_real_http_client_upload_submit_and_review(self):
        import uvicorn
        sock = socket.socket(); sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        self.app.hosts = frozenset([f'127.0.0.1:{port}'])
        server = uvicorn.Server(uvicorn.Config(self.app, http='h11', ws='none', proxy_headers=False,
                                  access_log=False, log_level='critical', lifespan='on'))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True); thread.start()
        def stop():
            server.should_exit = True; thread.join(8); sock.close()
            self.assertFalse(thread.is_alive())
        self.addCleanup(stop)
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            await asyncio.sleep(.02)
        self.assertTrue(server.started)
        path = self.root/'seller-key.json'; path.write_text(compact({'token':self.keys['vendor']['token']}))
        client = RemoteMarket(f'http://127.0.0.1:{port}',path)
        result = await asyncio.to_thread(client.upload_asset, self.req, self.raw)
        self.assertEqual('draft', result['status'])
        again = await asyncio.to_thread(client.upload_asset, self.req, self.raw)
        self.assertTrue(again['replayed'])
        repo = Path(__file__).resolve().parents[2]
        cli = await asyncio.to_thread(subprocess.run,
                [sys.executable, '-B', str(repo/'tools/market_assets.py'), 'upload',
                 '--url', client.base_url, '--credential-file', str(path), '--seller', 'vendor',
                 '--input', str(self.root/'image/candidate.json'), '--title', '素材验证',
                 '--summary', '原创夹具', '--keyword', '素材', '--request-id', 'upload-one', '--price-cents', '125'],
                capture_output=True, timeout=15)
        self.assertEqual(0, cli.returncode, 'Host upload CLI failed')
        self.assertTrue(json.loads(cli.stdout)['replayed'])
        result = await asyncio.to_thread(client.call, 'seller_submit',
                 {'id':'vendor:asset','version':'1.0.0','revision':1,'request_id':'submit'})
        self.assertEqual('pending_review', result['status'])
        reviewer = Exchange(self.db,'reviewer','reviewer')
        reviewer.review('vendor:asset','1.0.0',1,'approve',CHECKS,'Original isolated fixture only','approve')
        detail = Exchange(self.db,'buyer').resolve('vendor:asset','1.0.0','personal_use')
        self.assertEqual(125,detail['price_cents']); self.assertEqual(800,detail['fee_policy']['commission_bps'])
        self.assertEqual(self.raw,reviewer.inspect_asset('vendor:asset','1.0.0'))
        await asyncio.to_thread(client.call, 'seller_reprice', {'id':'vendor:asset','version':'1.0.0',
                                'offer_revision':1,'price_cents':0,'request_id':'free-fixture'})
        buyer_path = self.root/'buyer-key.json'
        buyer_path.write_text(compact({'token':self.keys['buyer']['token']}))
        buyer_client = RemoteMarket(client.base_url,buyer_path)
        detail = await asyncio.to_thread(buyer_client.call, 'market_resolve',
                                        {'id':'vendor:asset','version':'1.0.0','purpose':'personal_use'})
        acquired = await asyncio.to_thread(buyer_client.call, 'market_acquire',
                      {'id':'vendor:asset','version':'1.0.0','purpose':'personal_use',
                       'request_id':'acquire','expected_release_digest':detail['release_digest']})
        self.assertEqual('acquired',acquired['status'])
        host = Exchange(self.db,'buyer',host_policy=POLICY)
        delivery = host.read_asset(acquired['acquisition_id'],'fixture-task','read')
        self.assertEqual(self.raw,delivery['asset_bytes'])
