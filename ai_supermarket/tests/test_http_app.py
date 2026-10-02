"""来源：本项目原创。网络边界、身份、限流、断线和取消的本地验收。"""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import unittest

from ai_supermarket.common import compact
from ai_supermarket.http_app import Limits, MarketHTTP
from ai_supermarket.identity import Identities
from ai_supermarket.tests.test_exchange import CONTENT, listing


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.market = self.root / 'market.sqlite'
        self.identities = Identities(self.root / 'identity.sqlite')
        self.identities.create_account('vendor', 'seller')
        self.issued = self.identities.issue_key('vendor')
        self.token = self.issued['token']
        self.now = [0.0]
        self.app = self.make_app()

    def make_app(self, **limits):
        return MarketHTTP(self.market, self.root / 'identity.sqlite', allowed_hosts=['market.test:8765'],
                          limits=replace(Limits(), **limits), clock=lambda: self.now[0])

    def draft(self):
        return {'tool': 'seller_draft', 'arguments': {'slug': 'brief', 'version': '1.0.0',
                'metadata': listing(), 'content': CONTENT, 'request_id': 'draft'}}

    async def test_readiness_missing_market_and_recovery_gate_are_distinct_from_liveness(self):
        from contextlib import closing
        import sqlite3
        status, result, _ = await self.request(path='/health/ready', method='GET', token=False)
        self.assertEqual((503, {'status': 'not_ready'}), (status, result))
        self.assertFalse(self.market.exists())
        await self.request(self.draft())
        status, result, _ = await self.request(path='/health/ready', method='GET', token=False)
        self.assertEqual((200, {'status': 'ready'}), (status, result))
        with closing(sqlite3.connect(self.market)) as db, db:
            db.execute("INSERT INTO meta VALUES ('recovery_state','pending')")
        status, result, _ = await self.request(path='/health/ready', method='GET', token=False)
        self.assertEqual((503, {'status': 'not_ready'}), (status, result))
        status, result, _ = await self.request(self.draft())
        self.assertEqual((503, 'recovery_required'), (status, result['error_code']))
        status, result, _ = await self.request(path='/health/live', method='GET', token=False)
        self.assertEqual((200, 'alive'), (status, result['status']))

    async def request(self, value=None, *, headers=(), token=True, path='/v1/tools/call', method='POST',
                      body=None, receive=None, scheme='http', peer='127.0.0.1'):
        base = [(b'host', b'market.test:8765'), (b'content-type', b'application/json')]
        if token:
            base.append((b'authorization', b'Bearer ' + self.token.encode('ascii')))
        if receive is None:
            async def receive():
                return {'type': 'http.request', 'body': body if body is not None else compact(value).encode('utf-8')}
        scope = {'type': 'http', 'method': method, 'path': path, 'scheme': scheme,
                 'client': (peer, 1234), 'headers': base + list(headers), 'query_string': b''}
        messages = []
        async def send(message):
            messages.append(message)
        await self.app(scope, receive, send)
        return messages[0]['status'], json.loads(messages[1]['body']), dict(messages[0]['headers'])

    async def test_unauthenticated_request_never_reads_body_or_creates_market(self):
        async def unread():
            self.fail('Authentication must precede reading body')
        code, body, headers = await self.request(self.draft(), token=False, receive=unread)
        self.assertEqual(401, code)
        self.assertEqual('unauthenticated', body['error_code'])
        self.assertIn(b'www-authenticate', headers)
        self.assertFalse(self.market.exists())

    async def test_role_binding_and_idempotent_request(self):
        code, first, _ = await self.request(self.draft())
        self.assertEqual(200, code)
        _, second, _ = await self.request(self.draft())
        self.assertEqual(first['id'], second['id'])
        _, denied, _ = await self.request({'tool': 'review_decide', 'arguments': {}})
        self.assertEqual('unknown_tool', denied['error_code'])
        value = self.draft()
        value['arguments']['role'] = 'reviewer'
        code, _, _ = await self.request(value)
        self.assertEqual(422, code)

    async def test_duplicate_headers_origins_and_remote_cleartext_are_refused(self):
        cases = [([(b'host', b'evil.test')], 'http', '127.0.0.1', 400),
                 ([(b'authorization', b'Bearer fake')], 'http', '127.0.0.1', 400),
                 ([(b'origin', b'https://evil.test')], 'http', '127.0.0.1', 403),
                 ([(b'x-forwarded-proto', b'https')], 'http', '198.51.100.2', 403)]
        for headers, scheme, peer, expected in cases:
            with self.subTest(expected=expected):
                code, _, _ = await self.request(self.draft(), headers=headers, scheme=scheme, peer=peer)
                self.assertEqual(expected, code)
        self.assertFalse(self.market.exists())
        code, _, _ = await self.request(method='GET', path='/v1/tools', scheme='https', peer='198.51.100.2')
        self.assertEqual(200, code)

    async def test_invalid_json_size_encoding_and_length_do_not_execute(self):
        for body in (b'{"tool":"seller_draft","tool":"other","arguments":{}}', b'\xff', b'{"x":NaN}'):
            code, _, _ = await self.request(body=body)
            self.assertEqual(400, code)
        self.app = self.make_app(body_bytes=32)
        code, _, _ = await self.request(body=b'x' * 33)
        self.assertEqual(413, code)
        code, _, _ = await self.request(body=b'{}', headers=[(b'content-length', b'3')])
        self.assertEqual(400, code)
        code, _, _ = await self.request(body=b'{}', headers=[(b'content-encoding', b'gzip')])
        self.assertEqual(415, code)
        self.assertFalse(self.market.exists())

    async def test_chunked_oversize_disconnect_and_body_timeout_never_execute(self):
        self.app = self.make_app(body_bytes=20, body_seconds=0.02)
        chunks = iter([{'type': 'http.request', 'body': b'x' * 15, 'more_body': True},
                       {'type': 'http.request', 'body': b'x' * 15}])
        async def chunked():
            return next(chunks)
        self.assertEqual(413, (await self.request(receive=chunked))[0])
        async def disconnected():
            return {'type': 'http.disconnect'}
        self.assertEqual(400, (await self.request(receive=disconnected))[0])
        async def slow():
            await asyncio.sleep(1)
        self.assertEqual(408, (await self.request(receive=slow))[0])
        self.assertFalse(self.market.exists())

    async def test_revocation_during_body_upload_is_seen_before_mutation(self):
        async def receive():
            self.identities.revoke_key(self.issued['key_id'])
            return {'type': 'http.request', 'body': compact(self.draft()).encode('utf-8')}
        code, _, _ = await self.request(receive=receive)
        self.assertEqual(401, code)
        self.assertFalse(self.market.exists())

    async def test_principal_rate_limit_survives_key_rotation(self):
        self.app = self.make_app(principal_per_minute=1)
        self.assertEqual(200, (await self.request(path='/v1/tools', method='GET'))[0])
        self.token = self.identities.issue_key('vendor')['token']
        code, _, headers = await self.request(self.draft())
        self.assertEqual(429, code)
        self.assertEqual(b'60', headers[b'retry-after'])
        self.now[0] = 60.0
        self.assertEqual(200, (await self.request(self.draft()))[0])

    async def test_rate_tracker_fails_closed_at_capacity_and_expires(self):
        self.app = self.make_app(tracked_keys=2)
        code, _, _ = await self.request(self.draft())
        self.assertEqual(429, code)
        self.assertLessEqual(len(self.app.limiter.windows), 2)
        self.assertFalse(self.market.exists())
        self.now[0] = 61.0
        self.assertEqual(200, (await self.request(path='/health/live', method='GET', token=False))[0])

    async def test_slow_body_consumes_slot_and_second_request_is_rejected(self):
        self.app = self.make_app(concurrent=1)
        started, release = asyncio.Event(), asyncio.Event()
        async def receive():
            started.set()
            await release.wait()
            return {'type': 'http.request', 'body': compact(self.draft()).encode('utf-8')}
        pending = asyncio.create_task(self.request(receive=receive))
        await asyncio.wait_for(started.wait(), 2)
        try:
            self.assertEqual(503, (await self.request(self.draft()))[0])
        finally:
            release.set()
        self.assertEqual(200, (await pending)[0])
        self.assertEqual(0, self.app.active)

    async def test_cancelled_request_keeps_slot_until_worker_finishes(self):
        self.app = self.make_app(concurrent=1)
        started, release = threading.Event(), threading.Event()
        original = self.app.gateway.call
        def delayed(*args):
            started.set()
            release.wait(3)
            return original(*args)
        self.app.gateway.call = delayed
        pending = asyncio.create_task(self.request(self.draft()))
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        pending.cancel()
        await asyncio.sleep(0)
        try:
            self.assertEqual(503, (await self.request(self.draft()))[0])
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(0, self.app.active)
        self.assertTrue(self.market.exists())
        # An interrupted response can still correspond to a committed operation; replay stays idempotent.
        self.assertEqual(200, (await self.request(self.draft()))[0])

    async def test_unexpected_exception_does_not_leak_private_details(self):
        def fail(*_):
            raise RuntimeError('sensitive-example-do-not-echo')
        self.app.gateway.call = fail
        code, body, headers = await self.request(self.draft())
        self.assertEqual(500, code)
        self.assertFalse('sensitive-example' in str(body))
        self.assertFalse(self.token in str(body) or self.token in str(headers))
        self.assertEqual(b'no-store', headers[b'cache-control'])

    async def test_health_methods_and_unknown_route(self):
        self.assertEqual(200, (await self.request(path='/health/live', method='GET', token=False))[0])
        self.assertFalse(self.market.exists())
        self.assertEqual(405, (await self.request(path='/v1/tools', method='POST'))[0])
        self.assertEqual(404, (await self.request(path='/admin/issue-key'))[0])


if __name__ == '__main__':
    unittest.main()
