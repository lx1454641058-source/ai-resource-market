"""来源：本项目原创。资源使用前核验，不新增读取或评价。"""

import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from ai_supermarket.common import MarketError
from ai_supermarket.exchange import Exchange
from ai_supermarket.tests import test_delivery
from tools import market_bundle_check as checker


class BundleCheckTests(unittest.TestCase):
    def setUp(self):
        test_delivery.DeliveryTests.setUp(self)
        test_delivery.DeliveryTests.add(self)
        self.receipt = test_delivery.DeliveryTests.run_delivery(self)

    def check(self, **kwargs):
        return checker.check_bundle(kwargs.get('market', self.buyer), self.output,
                                    kwargs.get('sha', self.receipt['manifest_sha256']),
                                    kwargs.get('task', 'task-1'), kwargs.get('purpose', 'personal_use'))

    def code(self, expected, **kwargs):
        with self.assertRaises(MarketError) as caught:
            self.check(**kwargs)
        self.assertEqual(expected, caught.exception.code)

    def change_manifest(self, mutate):
        path = self.output / 'bundle.json'
        value = json.loads(path.read_bytes())
        mutate(value)
        raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
        path.write_bytes(raw)
        return checker.digest(raw)

    def test_live_success_is_readonly_and_compact(self):
        before = self.database.read_bytes()
        files = {p.name: p.read_bytes() for p in self.output.iterdir()}
        result = self.check()
        self.assertEqual('verified_for_read', result['status'])
        self.assertFalse(result['task_completed'])
        self.assertFalse(result['rights_independently_verified'])
        self.assertEqual(1, len(result['files']))
        self.assertNotIn('content', result)
        self.assertEqual(before, self.database.read_bytes())
        self.assertEqual(files, {p.name: p.read_bytes() for p in self.output.iterdir()})

    def test_wrong_task_principal_or_purpose(self):
        self.code('context_mismatch', task='other-task')
        self.code('context_mismatch', purpose='commercial_use')
        self.code('context_mismatch', market=Exchange(self.database, 'other'))
        sha = self.change_manifest(lambda value: value.update(principal='other'))
        self.code('receipt_mismatch', sha=sha, market=Exchange(self.database, 'other'))

    def test_manifest_and_body_tampering(self):
        self.code('expected_digest_required', sha='bad')
        self.code('manifest_digest_mismatch', sha='0' * 64)
        (self.output / 'resource-01.md').write_text('tampered', encoding='utf-8')
        self.code('content_digest_mismatch')

    def test_suspended_is_rejected_but_owned_withdrawn_is_supported(self):
        self.seller.availability('seller:brief', '1.0.0', 1, 'withdrawn', 'fixture', 'withdraw')
        self.assertEqual('verified_for_read', self.check()['status'])
        self.reviewer.availability('seller:brief', '1.0.0', 2, 'suspended', 'fixture', 'suspend')
        self.code('resource_unavailable')

    def test_license_and_missing_required_cannot_be_forged_even_with_new_digest(self):
        sha = self.change_manifest(lambda value: value['resources'][0]['conditions'].update(license_text='unlimited'))
        self.code('conditions_mismatch', sha=sha)
        def repair_then_break(value):
            value['resources'][0]['conditions']['license_text'] = test_delivery.listing()['license_text']
            value['needs'][0]['status'] = 'skipped'
        sha = self.change_manifest(repair_then_break)
        self.code('missing_required_resource', sha=sha)

    def test_failed_bundle_and_unsafe_paths(self):
        sha = self.change_manifest(lambda value: value['resources'][0].update(path='../outside.md'))
        self.code('invalid_bundle', sha=sha)
        (self.output / 'failure.json').write_text('{}', encoding='utf-8')
        self.code('delivery_incomplete')
        with patch.object(Path, 'is_junction', return_value=True):
            with self.assertRaises(checker.ReleaseError):
                self.check()

    def test_recovery_gates_existing_object(self):
        with self.buyer.store.connect(write=True) as db:
            db.execute("INSERT INTO meta(key,value) VALUES ('recovery_state','pending')")
        self.code('recovery_required')

    def test_isolated_cli_and_no_missing_database_initialization(self):
        args = [sys.executable, '-I', '-B', str(Path(checker.__file__).resolve()),
                '--principal', 'buyer', '--directory', str(self.output),
                '--expected-sha256', self.receipt['manifest_sha256'], '--task-id', 'task-1',
                '--purpose', 'personal_use', '--database']
        missing = self.root / 'missing.sqlite'
        run = subprocess.run([*args, str(missing)], cwd=self.root, capture_output=True, timeout=20)
        self.assertEqual(1, run.returncode)
        self.assertFalse(missing.exists())
        before = self.database.read_bytes()
        run = subprocess.run([*args, str(self.database)], cwd=self.root, capture_output=True, timeout=20)
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual('verified_for_read', json.loads(run.stdout)['status'])
        self.assertEqual(before, self.database.read_bytes())


if __name__ == '__main__':
    unittest.main()
