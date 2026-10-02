"""本项目原创。固定货架的发现、交付、隔离与失败保护。"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_supermarket.catalog import Catalog
from ai_supermarket.common import MarketError
from ai_supermarket.exchange import Exchange
from tools import market_stock as stock


class StockTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.output = self.root / 'stock'

    def test_full_stock_and_six_task_searches_deliver_original_bytes(self):
        result = stock.prepare(self.output)
        self.assertEqual(32, result['resources'])
        self.assertEqual({'skill': 14, 'template': 18}, result['kinds'])
        buyer = Exchange(Path(result['database']), stock.BUYER, 'buyer')
        with buyer.store.connect() as db:
            self.assertEqual(32, db.execute('SELECT COUNT(*) FROM submissions').fetchone()[0])
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM acquisitions').fetchone()[0])
        catalog = Catalog()
        for query, slug in [('数据清洗', 'csv-cleaning'), ('程序报错', 'bug-triage'),
                            ('网站文案', 'web-content-structure'), ('视频策划', 'video-preproduction'),
                            ('陌生领域', 'domain-onboarding'), ('客服', 'customer-support-triage')]:
            with self.subTest(query=query):
                resource = stock.SELLER + ':' + slug
                found = buyer.search(query, kind='skill', purpose='personal_use')
                self.assertIn(resource, [item['id'] for item in found['items']])
                self.assertLessEqual(len(found['items']), 3)
                self.assertNotIn('content', found['items'][0])
                resolved = buyer.resolve(resource, '1.0.0', 'personal_use')
                self.assertFalse(resolved['training_allowed'])
                acquired = buyer.acquire(resource, '1.0.0', 'get-' + slug, purpose='personal_use')
                read = buyer.read(acquired['acquisition_id'], 'engineering-check', 'read-' + slug)
                self.assertEqual(catalog.read(catalog.get(slug, '1.0.0')), read['content'])
                self.assertEqual(read['usage_id'], buyer.read(acquired['acquisition_id'],
                                 'engineering-check', 'read-' + slug)['usage_id'])
                self.assertEqual([], buyer.search(query, purpose='commercial_use')['items'])
        inventory = json.loads((self.output / 'inventory.json').read_bytes())
        self.assertFalse(inventory['independent_review'])
        self.assertEqual(0, inventory['external_sellers'])
        config = json.loads((self.output / 'buyer.mcp.json').read_bytes())
        self.assertFalse(config['installed'])
        self.assertEqual(['--role', 'buyer', 'mcp'], config['mcpServers']['ai-supermarket-stock']['args'][-3:])

    def test_existing_directory_is_never_changed(self):
        self.output.mkdir()
        sentinel = self.output / 'market.sqlite'
        sentinel.write_bytes(b'untouched-existing-instance')
        with self.assertRaises(MarketError) as caught:
            stock.prepare(self.output)
        self.assertEqual('output_exists', caught.exception.code)
        self.assertEqual(b'untouched-existing-instance', sentinel.read_bytes())
        self.assertEqual([sentinel], list(self.output.iterdir()))

    def test_invalid_body_fails_before_creating_database(self):
        with patch.object(Catalog, 'read', side_effect=MarketError('integrity_failed', 'test')):
            with self.assertRaises(MarketError):
                stock.prepare(self.output)
        self.assertFalse(self.output.exists())

    def test_interruption_keeps_evidence_without_ready_marker(self):
        with patch.object(Exchange, 'submit', side_effect=OSError('test interruption')):
            with self.assertRaises(OSError):
                stock.prepare(self.output)
        self.assertTrue((self.output / 'setup-started.json').exists())
        self.assertTrue((self.output / 'market.sqlite').exists())
        self.assertFalse((self.output / 'stock-ready.json').exists())

    def test_cli_runs_from_unrelated_working_directory(self):
        process = subprocess.run([sys.executable, '-I', '-B', str(stock.ROOT / 'tools/market_stock.py'),
                                  '--output', str(self.output)], cwd=self.root,
                                 capture_output=True, timeout=30)
        self.assertEqual(0, process.returncode, process.stderr.decode('utf-8', errors='replace'))
        self.assertEqual('stock_ready', json.loads(process.stdout)['status'])


if __name__ == '__main__':
    unittest.main()
