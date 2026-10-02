"""来源：本项目原创。受信运营入口测试；只使用隔离临时账号和文件。"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_supermarket.common import MarketError
from ai_supermarket.identity import Identities
from ai_supermarket.identity_admin import issue_to_file


class AdminTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / 'identity.sqlite'
        self.identities = Identities(self.path)
        self.identities.create_account('alice', 'buyer')

    def test_issue_never_overwrites_and_rejects_before_creating_key(self):
        destination = self.root / 'credential.json'
        destination.write_text('existing-user-file', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            issue_to_file(self.identities, 'alice', destination, 60)
        self.assertEqual('existing-user-file', destination.read_text(encoding='utf-8'))
        with self.identities.store.connect() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM api_keys').fetchone()[0])

    def test_failed_file_sync_revokes_issued_key(self):
        with patch('ai_supermarket.identity_admin.os.fsync', side_effect=OSError('synthetic file failure')):
            with self.assertRaises(OSError):
                issue_to_file(self.identities, 'alice', self.root / 'partial.json', 60)
        with self.identities.store.connect() as db:
            rows = db.execute('SELECT revoked_at FROM api_keys').fetchall()
        self.assertEqual(1, len(rows))
        self.assertIsNotNone(rows[0]['revoked_at'])

    def test_admin_cli_issues_secret_only_to_new_file_and_revoke_works(self):
        output = self.root / 'credential.json'
        command = [sys.executable, '-m', 'ai_supermarket.identity_admin', '--identities', str(self.path)]
        process = subprocess.run(command + ['issue-key', '--principal', 'alice', '--output', str(output)],
                                 capture_output=True, timeout=10)
        self.assertEqual(0, process.returncode)
        self.assertEqual(b'', process.stderr)
        summary = json.loads(process.stdout)
        token = json.loads(output.read_text(encoding='utf-8'))['token']
        self.assertTrue(token.encode('ascii') not in process.stdout)
        self.assertEqual('alice', self.identities.authenticate(token)['principal'])
        revoked = subprocess.run(command + ['revoke-key', '--key-id', summary['key_id']], capture_output=True, timeout=10)
        self.assertEqual(0, revoked.returncode)
        with self.assertRaises(MarketError):
            self.identities.authenticate(token)

    def test_non_init_operation_does_not_silently_create_identity_database(self):
        missing = self.root / 'missing.sqlite'
        process = subprocess.run([sys.executable, '-m', 'ai_supermarket.identity_admin', '--identities', str(missing),
                                  'create-account', '--principal', 'new', '--role', 'buyer'], capture_output=True, timeout=10)
        self.assertEqual(1, process.returncode)
        self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
