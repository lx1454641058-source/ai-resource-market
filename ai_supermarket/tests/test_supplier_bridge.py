"""来源：本项目原创。供应商批量接入示例的失败、恢复与交付边界。"""

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

from ai_supermarket.common import MarketError, compact
from ai_supermarket.exchange import Exchange
from ai_supermarket.exchange_tools import ExchangeInterface
from ai_supermarket.examples.supplier_bridge import fetch_batch, prepare_batch, submit_batch
from tools.market_seller import sample_request


def feed():
    first = sample_request()
    second = deepcopy(first)
    second.update(slug='paid-note', request_id='paid-note-draft-1')
    second['metadata']['price_cents'] = 100
    return [first, second]


class SupplierBridgeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = Path(self.folder.name) / 'market.sqlite'
        self.seller = Exchange(self.db, 'supplier-demo', 'seller')
        self.client = ExchangeInterface(self.seller)
        self.buyer = Exchange(self.db, 'buyer-demo', 'buyer')
        self.reviewer = Exchange(self.db, 'review-demo', 'reviewer')

    def count(self, table):
        with self.seller.store.connect() as db:
            return db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

    def approve(self, row):
        return self.reviewer.review('supplier-demo:' + row['slug'], row['version'], 1, 'approve',
            {key: True for key in ('rights_verified', 'sample_checked', 'content_checked', 'contract_checked')},
            '仅审核本项目原创工程样例，不代表独立或商业审核。', 'approve-' + row['slug'])

    def server(self, data, status=200, content_type='application/json', location=None):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                if location:
                    self.send_header('Location', location)
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}/export.json'

    def test_whole_batch_preflight_before_writes(self):
        rows = feed()
        rows[1]['metadata']['price_cents'] = True
        with self.assertRaises(MarketError):
            submit_batch(rows, 'supplier-demo', self.client)
        self.assertEqual(0, self.count('submissions'))

    def test_pending_invisible_and_compact_receipt(self):
        rows = feed()
        before = deepcopy(rows)
        result = submit_batch(rows, 'supplier-demo', self.client)
        self.assertEqual('submitted', result['status'])
        self.assertEqual(['pending_review'] * 2, [item['state'] for item in result['items']])
        self.assertEqual('no_match', self.buyer.search('事实清单', purpose='personal_use')['status'])
        self.assertNotIn('content', compact(result))
        self.assertNotIn(rows[0]['content'], compact(result))
        self.assertEqual(before, rows)

    def test_repeat_after_approval_reports_current_state_without_duplicates(self):
        rows = feed()
        submit_batch(rows, 'supplier-demo', self.client)
        self.approve(rows[0])
        before = self.count('exchange_receipts')
        result = submit_batch(rows, 'supplier-demo', self.client)
        self.assertEqual(['approved', 'pending_review'], [item['state'] for item in result['items']])
        self.assertEqual(2, self.count('submissions'))
        self.assertEqual(before, self.count('exchange_receipts'))

    def test_unknown_after_server_commit_stops_then_original_replay_recovers(self):
        client = self.client
        class LostResponse:
            calls = 0
            def call(self, tool, arguments):
                self.calls += 1
                response = client.call(tool, arguments)
                if tool == 'seller_submit':
                    raise OSError('private server detail must not be exposed')
                return response
        interrupted = LostResponse()
        result = submit_batch(feed(), 'supplier-demo', interrupted)
        self.assertEqual('stopped', result['status'])
        self.assertEqual(2, interrupted.calls)
        self.assertEqual(1, self.count('submissions'))
        self.assertNotIn('private server', compact(result))
        recovered = submit_batch(feed(), 'supplier-demo', self.client)
        self.assertEqual('submitted', recovered['status'])
        self.assertEqual(2, self.count('submissions'))

    def test_changed_original_request_does_not_overwrite(self):
        rows = feed()
        submit_batch(rows, 'supplier-demo', self.client)
        rows[0]['content'] += '\nchanged\n'
        result = submit_batch(rows, 'supplier-demo', self.client)
        self.assertEqual('stopped', result['status'])
        self.assertEqual('seller_draft', result['stage'])
        stored = self.seller.inspect_submission('supplier-demo:' + rows[0]['slug'], rows[0]['version'])
        self.assertEqual(feed()[0]['content'], stored['content'])

    def test_reject_duplicate_requests_resources_and_extra_commands(self):
        for change in ('request', 'resource', 'command'):
            rows = feed()
            if change == 'request':
                rows[1]['request_id'] = rows[0]['request_id']
            elif change == 'resource':
                rows[1]['slug'] = rows[0]['slug']
            else:
                rows[1]['tool'] = 'review_decide'
            with self.subTest(change=change), self.assertRaises(MarketError):
                prepare_batch(rows, 'supplier-demo')

    def test_wrong_host_seller_binding_cannot_submit(self):
        result = submit_batch(feed(), 'different-seller', self.client)
        self.assertEqual('stopped', result['status'])
        with self.seller.store.connect() as db:
            self.assertEqual(['draft'], [row[0] for row in db.execute('SELECT state FROM submissions')])

    def test_real_http_export_then_free_delivery_paid_blocked(self):
        rows = fetch_batch(self.server(compact(feed()).encode('utf-8')))
        self.assertEqual('submitted', submit_batch(rows, 'supplier-demo', self.client)['status'])
        for row in rows:
            self.approve(row)
        for row in rows:
            resource = 'supplier-demo:' + row['slug']
            detail = self.buyer.resolve(resource, row['version'], 'personal_use')
            acquired = self.buyer.acquire(resource, row['version'], 'get-' + row['slug'],
                purpose='personal_use', expected_release_digest=detail['release_digest'])
            if row['metadata']['price_cents']:
                self.assertEqual('payment_unavailable', acquired['status'])
                quote = self.buyer.quote(resource, row['version'], 'personal_use', 'quote-paid')
                self.assertEqual(100, quote['price_cents'])
                self.assertEqual(8, quote['fee_policy']['platform_cents'])
                self.assertFalse(quote['real_payments'])
            else:
                delivered = self.buyer.read(acquired['acquisition_id'], 'task-demo', 'read-demo')
                self.assertEqual(row['content'], delivered['content'])
        self.assertEqual(1, self.count('acquisitions'))

    def test_supplier_http_error_redirect_and_html_are_not_imported(self):
        for status, content_type in ((302, 'application/json'), (500, 'application/json'), (200, 'text/html')):
            with self.subTest(status=status, content_type=content_type):
                url = self.server(b'[]', status, content_type, 'http://127.0.0.1:1/never')
                with self.assertRaises(MarketError):
                    fetch_batch(url)
        self.assertEqual(0, self.count('submissions'))

    def test_oversize_duplicate_json_fields_and_empty_feed(self):
        with self.assertRaises(MarketError):
            fetch_batch(self.server(b' ' * 1_048_577))
        with self.assertRaises(MarketError):
            fetch_batch(self.server(b'[{"x":1,"x":2}]'))
        for value in ([], feed() * 9, {'items': feed()}):
            with self.assertRaises(MarketError):
                prepare_batch(value, 'supplier-demo')

    def test_invalid_endpoints_and_timeouts(self):
        for url in ('http://example.com/export', 'https://u:p@example.com/export',
                    'https://example.com/export?secret=1', 'file:///tmp/feed',
                    'https://example.com/export#x', 'http://localhost/export', 'https://example.com:0/export'):
            with self.subTest(url=url), self.assertRaises(MarketError):
                fetch_batch(url)
        for timeout in (True, 0, 31, float('nan'), float('inf')):
            with self.subTest(timeout=timeout), self.assertRaises(MarketError):
                fetch_batch('https://example.com/export', timeout)


if __name__ == '__main__':
    unittest.main()
