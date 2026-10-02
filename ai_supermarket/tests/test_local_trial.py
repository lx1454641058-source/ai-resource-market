"""来源：本项目原创。本地试用交付、只读结果与故障保护；无模型或外网调用。"""

from contextlib import closing
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_supermarket.common import MarketError
from ai_supermarket.exchange import Exchange
from tools import market_release, market_try


class LocalTrialTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / '试用 space'

    def prepare(self):
        return market_try.prepare(self.directory)

    def use(self):
        buyer = Exchange(self.directory / 'market.sqlite', market_try.BUYER, 'buyer')
        request = json.loads((self.directory / 'acquire.json').read_bytes())
        acquired = buyer.acquire(**request)
        self.assertEqual(acquired, buyer.acquire(**request))
        usage = buyer.read(acquired['acquisition_id'], 'trial-task-1', 'trial-read-1')
        self.assertEqual(usage, buyer.read(acquired['acquisition_id'], 'trial-task-1', 'trial-read-1'))
        return buyer, usage

    def test_preparation_has_no_buyer_activity_and_preserves_existing_directory(self):
        result = self.prepare()
        self.assertEqual(0, result['model_calls'])
        self.assertFalse(result['client_installed'])
        before = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        self.assertEqual({'acquisitions': 0, 'reads': 0, 'feedback_usages': 0},
                         market_try.report(self.directory)['counts'])
        with self.assertRaises(FileExistsError):
            self.prepare()
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.directory.iterdir()})

    def test_real_usage_feedback_corrections_and_read_only_report(self):
        self.prepare()
        buyer, usage = self.use()
        self.assertIn('## 事实', usage['content'])
        self.assertIn('核对负责人：未知', usage['content'])
        buyer.feedback(usage['usage_id'])
        buyer.record_check(usage['usage_id'], 'fixture-sections', True)
        buyer.correct_check(usage['usage_id'], 'fixture-sections', False, 1, 'check_repeated')
        before = (self.directory / 'market.sqlite').read_bytes()
        report = market_try.report(self.directory)
        self.assertEqual({'acquisitions': 1, 'reads': 1, 'feedback_usages': 1}, report['counts'])
        self.assertCountEqual([
            {'evidence': 'agent_reported', 'check': 'not_run', 'count': 1},
            {'evidence': 'host_check', 'check': 'fail', 'count': 1}], report['latest_feedback'])
        self.assertFalse(report['task_quality_verified'])
        self.assertIsNone(report['actual_model_calls'])
        self.assertIsNone(report['token_savings'])
        self.assertEqual(before, (self.directory / 'market.sqlite').read_bytes())

    def test_missing_incomplete_wrong_ledger_and_oversized_marker_are_rejected(self):
        with self.assertRaises(OSError):
            market_try.report(self.directory)
        self.assertFalse(self.directory.exists())
        self.directory.mkdir()
        market_try.write_new(self.directory / 'trial.json', market_try.MARKER)
        with self.assertRaises(sqlite3.Error):
            market_try.report(self.directory)
        self.assertFalse((self.directory / 'market.sqlite').exists())
        with closing(sqlite3.connect(self.directory / 'market.sqlite')) as db, db:
            db.execute('CREATE TABLE unrelated (value TEXT)')
        with self.assertRaises(sqlite3.Error):
            market_try.report(self.directory)
        (self.directory / 'trial.json').write_bytes(b' ' * 4097)
        with self.assertRaises(MarketError) as caught:
            market_try.report(self.directory)
        self.assertEqual('invalid_trial', caught.exception.code)

    def test_snapshot_recovery_and_changed_version_are_rejected(self):
        self.prepare()
        for name in ('.ai-supermarket-snapshot.json', 'restore-started.json'):
            with self.subTest(name=name):
                with patch.object(Path, 'exists', lambda p: p.name == name or p.is_file()):
                    with self.assertRaises(MarketError):
                        market_try.report(self.directory)
        with closing(sqlite3.connect(self.directory / 'market.sqlite')) as db, db:
            db.execute("INSERT INTO meta VALUES ('recovery_state','pending')")
        with self.assertRaises(MarketError):
            market_try.report(self.directory)

    def test_linked_output_is_refused_before_creating_files(self):
        with patch.object(Path, 'is_junction', return_value=True):
            with self.assertRaises(market_release.ReleaseError):
                self.prepare()
        self.assertFalse(self.directory.exists())

    def test_failed_prepare_has_no_completion_marker(self):
        with patch.object(market_try.Exchange, 'review', side_effect=MarketError('fixture_failure', 'fixture')):
            with self.assertRaises(MarketError):
                self.prepare()
        self.assertTrue(self.directory.exists())
        self.assertFalse((self.directory / 'trial.json').exists())
        with self.assertRaises(OSError):
            market_try.report(self.directory)

    def test_extracted_cli_and_generated_configuration_work_from_foreign_directory(self):
        built = market_release.build(self.root / 'build', 'trial-test')
        package = self.root / 'package'
        market_release.extract(built['archive'], built['sha256'], package)
        foreign = self.root / 'unrelated'
        foreign.mkdir()

        def run(*args, success=True):
            execution = subprocess.run([sys.executable, '-I', '-B', *map(str, args)],
                                       cwd=foreign, capture_output=True, timeout=15)
            self.assertEqual(0 if success else 1, execution.returncode, execution.stderr.decode('utf-8'))
            return json.loads(execution.stdout)

        script = package / 'tools/market_try.py'
        self.assertEqual('prepared', run(script, 'prepare', '--output', self.directory)['status'])
        configuration = json.loads((self.directory / 'buyer.mcp.json').read_bytes())
        server = configuration['mcpServers']['ai-supermarket-trial']
        self.assertEqual(sys.executable, server['command'])
        self.assertTrue(Path(server['args'][0]).is_relative_to(package))
        args = server['args'][:-1]
        described = run(*args, 'describe')
        self.assertNotIn('seller_draft', json.dumps(described))
        self.assertNotIn('review_decide', json.dumps(described))
        self.assertIn('market_read', json.dumps(described))
        acquired = run(*args, 'call', 'market_acquire', '--input', self.directory / 'acquire.json')
        market_try.write_new(self.directory / 'read.json', {
            'acquisition_id': acquired['acquisition_id'], 'task_id': 'trial-task-1', 'request_id': 'trial-read-1'})
        usage = run(*args, 'call', 'market_read', '--input', self.directory / 'read.json')
        market_try.write_new(self.directory / 'feedback.json', {'usage_id': usage['usage_id']})
        run(*args, 'call', 'market_feedback', '--input', self.directory / 'feedback.json')
        self.assertEqual(1, run(script, 'report', '--directory', self.directory)['counts']['reads'])
        self.assertEqual('destination_exists', run(script, 'prepare', '--output', self.directory,
                                                  success=False)['error_code'])

    def run_generated_powershell(self):
        shell = shutil.which('pwsh')
        if not shell:
            self.skipTest('PowerShell is not installed; real generated-command check unavailable')
        guide = (self.directory / 'START-HERE.md').read_text('utf-8')
        block = next(block for block in re.findall(r'```powershell\n(.*?)\n```', guide, re.S)
                     if 'Write-NewTrialJson' in block)
        script = self.root / 'generated-trial.ps1'
        script.write_text(block, encoding='utf-8-sig')
        return subprocess.run([shell, '-NoProfile', '-NonInteractive', '-File', str(script)],
                              cwd=self.root, capture_output=True, timeout=30)

    def test_generated_powershell_continues_real_receipts_without_manual_ids(self):
        self.directory = self.root / "试用 O'Brien space"
        self.prepare()
        run = self.run_generated_powershell()
        self.assertEqual(0, run.returncode, run.stderr.decode('utf-8', errors='replace'))
        self.assertEqual({'acquisitions': 1, 'reads': 1, 'feedback_usages': 1}, market_try.report(self.directory)['counts'])
        read = json.loads((self.directory / 'read.json').read_bytes())
        feedback = json.loads((self.directory / 'feedback.json').read_bytes())
        with closing(sqlite3.connect(self.directory / 'market.sqlite')) as db:
            self.assertEqual(read['acquisition_id'], db.execute('SELECT id FROM acquisitions').fetchone()[0])
            self.assertEqual(feedback['usage_id'], db.execute('SELECT id FROM usage_events').fetchone()[0])
        buyer = Exchange(self.directory / 'market.sqlite', market_try.BUYER, 'buyer')
        request = json.loads((self.directory / 'acquire.json').read_bytes())
        state = buyer.status(read['acquisition_id'])
        self.assertEqual('expected_release_v1', state.get('release_binding'))
        self.assertEqual(request['expected_release_digest'], state['release_digest'])
        # Recovery must replay the frozen request, including its binding.
        self.assertEqual(read['acquisition_id'], buyer.acquire(**request)['acquisition_id'])
        self.assertEqual(1, market_try.report(self.directory)['counts']['acquisitions'])
        self.assertEqual('unknown', feedback['useful'])
        self.assertEqual('not_run', feedback['check'])

    def test_generated_powershell_rejects_conditions_changed_since_preparation(self):
        self.prepare()
        original = (self.directory / 'acquire.json').read_bytes()
        # Simulate a changed source ledger in this isolated fixture. Approved
        # versions cannot be edited through the normal seller interface.
        with closing(sqlite3.connect(self.directory / 'market.sqlite')) as db, db:
            metadata = json.loads(db.execute('SELECT metadata FROM submissions').fetchone()[0])
            metadata['license_text'] += '\nChanged after preparation.'
            db.execute('UPDATE submissions SET metadata=?', (json.dumps(metadata),))
        run = self.run_generated_powershell()
        self.assertNotEqual(0, run.returncode)
        self.assertEqual(original, (self.directory / 'acquire.json').read_bytes())
        self.assertFalse((self.directory / 'read.json').exists())
        self.assertFalse((self.directory / 'feedback.json').exists())
        self.assertEqual({'acquisitions': 0, 'reads': 0, 'feedback_usages': 0},
                         market_try.report(self.directory)['counts'])

    def test_generated_powershell_stops_without_overwriting_existing_request(self):
        self.prepare()
        buyer = Exchange(self.directory / 'market.sqlite', market_try.BUYER, 'buyer')
        acquired = buyer.acquire(**json.loads((self.directory / 'acquire.json').read_bytes()))
        # A valid old request must not be executed after creating the new file
        # fails. Invalid JSON would hide unintended continuation at market_read.
        prior = json.dumps({'acquisition_id': acquired['acquisition_id'],
                            'task_id': 'old-task', 'request_id': 'old-read'}).encode()
        (self.directory / 'read.json').write_bytes(prior)
        run = self.run_generated_powershell()
        self.assertNotEqual(0, run.returncode)
        self.assertEqual(prior, (self.directory / 'read.json').read_bytes())
        self.assertFalse((self.directory / 'feedback.json').exists())
        self.assertEqual(0, market_try.report(self.directory)['counts']['reads'])

    def test_generated_powershell_stops_when_acquire_is_not_available(self):
        self.prepare()
        request = json.loads((self.directory / 'acquire.json').read_bytes())
        Exchange(self.directory / 'market.sqlite', market_try.SELLER, 'seller').availability(
            request['id'], request['version'], 1, 'withdrawn', '演练停止获取', 'stop-trial')
        run = self.run_generated_powershell()
        self.assertNotEqual(0, run.returncode)
        self.assertFalse((self.directory / 'read.json').exists())
        self.assertFalse((self.directory / 'feedback.json').exists())
        self.assertEqual({'acquisitions': 0, 'reads': 0, 'feedback_usages': 0}, market_try.report(self.directory)['counts'])

    def test_generated_powershell_does_not_submit_preexisting_feedback(self):
        self.prepare()
        buyer = Exchange(self.directory / 'market.sqlite', market_try.BUYER, 'buyer')
        acquired = buyer.acquire(**json.loads((self.directory / 'acquire.json').read_bytes()))
        old = buyer.read(acquired['acquisition_id'], 'older-task', 'older-read')
        prior = json.dumps({'usage_id': old['usage_id'], 'useful': 'yes', 'check': 'pass'}).encode()
        (self.directory / 'feedback.json').write_bytes(prior)
        run = self.run_generated_powershell()
        self.assertNotEqual(0, run.returncode)
        self.assertEqual(prior, (self.directory / 'feedback.json').read_bytes())
        self.assertEqual(0, market_try.report(self.directory)['counts']['feedback_usages'])


if __name__ == '__main__':
    unittest.main()
