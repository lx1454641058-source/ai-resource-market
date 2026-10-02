"""来源：本项目原创。实际本地账本、跨进程锁与固定批次恢复验收。"""

from copy import deepcopy
import json
import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from ai_supermarket.common import MarketError, compact
from ai_supermarket.exchange import Exchange
from ai_supermarket.exchange_tools import ExchangeInterface
from ai_supermarket.examples.supplier_job import create_job, run_job, _locked
from ai_supermarket.tests import test_supplier_bridge as bridge_fixture
from ai_supermarket.tests import test_http_live as http_fixture

feed = bridge_fixture.feed


class SupplierJobTests(unittest.TestCase):
    # Reuse only the fixture, not every inherited bridge test.
    setUp = bridge_fixture.SupplierBridgeTests.setUp
    count = bridge_fixture.SupplierBridgeTests.count

    def prepare(self, rows=None):
        self.directory = Path(self.folder.name) / 'batch'
        result = create_job(feed() if rows is None else rows, 'supplier-demo', 'market-local', self.directory)
        self.digest = result['batch_sha256']
        return result

    def run_job(self, client=None, **kwargs):
        return run_job(self.directory, self.digest, 'supplier-demo', 'market-local',
                       self.client if client is None else client, **kwargs)

    def assert_code(self, code, fn):
        with self.assertRaises(MarketError) as caught:
            fn()
        self.assertEqual(code, caught.exception.code)

    def test_freeze_before_send_and_input_changes_do_not_change_saved_batch(self):
        rows = feed()
        original = deepcopy(rows)
        prepared = self.prepare(rows)
        self.assertEqual(0, prepared['market_calls'])
        self.assertEqual(0, self.count('submissions'))
        rows[0]['content'] = 'changed upstream after export'
        result = self.run_job()
        self.assertEqual('submitted', result['status'])
        stored = self.seller.inspect_submission('supplier-demo:' + original[0]['slug'], original[0]['version'])
        self.assertEqual(original[0]['content'], stored['content'])
        self.assertNotIn(original[0]['content'], compact(result))
        self.assertEqual(result, json.loads((self.directory/'attempt-0001.result.json').read_bytes()))

    def test_invalid_batch_never_creates_directory_or_calls_market(self):
        rows = feed()
        rows[-1]['metadata']['price_cents'] = -1
        with self.assertRaises(MarketError):
            self.prepare(rows)
        self.assertFalse(self.directory.exists())
        self.assertEqual(0, self.count('submissions'))

    def test_existing_directory_is_not_overwritten(self):
        self.prepare()
        before = (self.directory/'batch.json').read_bytes()
        with self.assertRaises(FileExistsError):
            create_job(feed(), 'supplier-demo', 'market-local', self.directory)
        self.assertEqual(before, (self.directory/'batch.json').read_bytes())

    def test_wrong_host_connection_or_batch_digest_stops_before_send(self):
        self.prepare()
        for seller, connection, digest in [('other', 'market-local', self.digest),
                ('supplier-demo', 'other', self.digest), ('supplier-demo', 'market-local', '0'*64)]:
            with self.subTest(seller=seller, connection=connection, digest=digest):
                self.assert_code('batch_mismatch', lambda: run_job(
                    self.directory, digest, seller, connection, self.client))
        self.assertEqual(0, self.count('submissions'))

    def test_modified_disk_batch_rejected_against_host_retained_digest(self):
        self.prepare()
        path = self.directory/'batch.json'
        value = json.loads(path.read_bytes())
        value['requests'][0]['content'] += '\nmodified\n'
        path.write_text(compact(value), encoding='utf-8')
        self.assert_code('batch_mismatch', self.run_job)
        self.assertEqual(0, self.count('submissions'))

    def test_lost_commit_response_requires_explicit_recovery_after_reopen(self):
        self.prepare()
        actual = self.client
        class LostResponse:
            def call(self, tool, arguments):
                result = actual.call(tool, arguments)
                if tool == 'seller_submit':
                    raise OSError('private diagnostic')
                return result
        stopped = self.run_job(LostResponse())
        self.assertEqual('stopped', stopped['status'])
        self.assertNotIn('private', compact(stopped))
        first_receipt = (self.directory/'attempt-0001.result.json').read_bytes()
        self.client = ExchangeInterface(Exchange(self.db, 'supplier-demo', 'seller'))
        self.assert_code('recovery_required', self.run_job)
        self.assertEqual(1, self.count('submissions'))
        # Explicitly inspect the actual committed state before original replay.
        state = self.client.call('seller_status', {'id': 'supplier-demo:' + feed()[0]['slug'], 'version': '1.0.0'})
        self.assertEqual('pending_review', state['status'])
        recovered = self.run_job(resume_unknown=True)
        self.assertEqual('submitted', recovered['status'])
        self.assertEqual(2, self.count('submissions'))
        self.assertEqual(first_receipt, (self.directory/'attempt-0001.result.json').read_bytes())
        before = self.count('exchange_receipts')
        self.assertEqual('submitted', self.run_job()['status'])
        self.assertEqual(before, self.count('exchange_receipts'))

    def test_receipt_disk_failure_leaves_unknown_intent_and_preserves_successful_server_records(self):
        self.prepare()
        from ai_supermarket.examples import supplier_job
        write = supplier_job._write
        def fail_result(path, value):
            if path.name.endswith('.result.json'):
                raise OSError('disk full')
            return write(path, value)
        with patch.object(supplier_job, '_write', fail_result), self.assertRaises(OSError):
            self.run_job()
        self.assertEqual(2, self.count('submissions'))
        self.assert_code('recovery_required', self.run_job)
        before = self.count('exchange_receipts')
        self.assertEqual('submitted', self.run_job(resume_unknown=True)['status'])
        self.assertEqual(before, self.count('exchange_receipts'))

    def test_live_other_process_lock_denies_run_then_releases(self):
        self.prepare()
        code = '''
import sys
from ai_supermarket.examples.supplier_job import run_job
from ai_supermarket.common import MarketError
try:
    run_job(sys.argv[1], sys.argv[2], 'supplier-demo', 'market-local', None, resume_unknown=True)
except MarketError as e:
    print(e.code)
'''
        with _locked(self.directory):
            run = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', code,
                                  str(self.directory), self.digest], capture_output=True, timeout=10,
                                 cwd=Path(__file__).resolve().parents[2])
            self.assertEqual(0, run.returncode, run.stderr)
            self.assertEqual('job_busy', run.stdout.decode().strip())
            self.assertEqual(0, self.count('submissions'))
        self.assertEqual('submitted', self.run_job()['status'])

    def test_corrupt_attempt_receipt_does_not_trigger_another_send(self):
        self.prepare()
        self.run_job()
        (self.directory/'attempt-0001.result.json').write_text('[]', encoding='utf-8')
        self.assert_code('invalid_journal', self.run_job)
        self.assertEqual(1, len(list(self.directory.glob('*.started.json'))))


@unittest.skipUnless(importlib.util.find_spec('uvicorn'), '需要已有HTTP测试运行时')
class RemoteSupplierJobTests(unittest.TestCase):
    setUp = http_fixture.LiveHTTPTests.setUp
    stop = http_fixture.LiveHTTPTests.stop
    client = http_fixture.LiveHTTPTests.client

    def test_original_batch_recovered_by_new_process_over_real_http(self):
        directory = self.root/'saved-job'
        prepared = create_job(feed(), 'vendor', 'market-loopback', directory)
        client = self.client('vendor')
        class LostResponse:
            def call(self, tool, arguments):
                result = client.call(tool, arguments)
                if tool == 'seller_submit':
                    raise OSError('lost response after actual HTTP commit')
                return result
        first = run_job(directory, prepared['batch_sha256'], 'vendor', 'market-loopback', LostResponse())
        self.assertEqual('stopped', first['status'])
        status = client.call('seller_status', {'id': 'vendor:' + feed()[0]['slug'], 'version': '1.0.0'})
        self.assertEqual('pending_review', status['status'])
        code = '''
import sys
from ai_supermarket.common import compact
from ai_supermarket.examples.supplier_job import run_job
from ai_supermarket.http_client import RemoteMarket
result = run_job(sys.argv[1], sys.argv[2], 'vendor', 'market-loopback',
                 RemoteMarket(sys.argv[3], sys.argv[4]), resume_unknown=True)
print(compact(result))
'''
        child = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', code,
                    str(directory), prepared['batch_sha256'], self.url, str(self.credentials['vendor'])],
                    capture_output=True, timeout=15, cwd=Path(__file__).resolve().parents[2])
        self.assertEqual(0, child.returncode, child.stderr)
        result = json.loads(child.stdout)
        self.assertEqual('submitted', result['status'])
        self.assertEqual(2, result['attempt'])
        listings = client.call('seller_list', {})
        self.assertEqual(2, len(listings['items']))
        self.assertEqual(['pending_review']*2, [row['state'] for row in listings['items']])
        self.assertEqual('no_match', self.client('alice').call('market_search', {'q': '事实清单'})['status'])


if __name__ == '__main__':
    unittest.main()
