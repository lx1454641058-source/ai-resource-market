"""来源：本项目原创。入驻、重放、跨账本失败和凭据边界的本地验收。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_supermarket.common import MarketError, compact
from ai_supermarket.enrollment import Enrollments
from ai_supermarket.enrollment_admin import issue_to_file
from ai_supermarket.enrollment_client import prepare_credential, RemoteEnrollment
from ai_supermarket.exchange_store import ExchangeStore
from ai_supermarket.http_app import Limits, MarketHTTP
from ai_supermarket.identity import DEFAULT_POLICY, Identities, new_credential
from ai_supermarket.tests import test_http_app


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.now = [1000.0]
        self.identity_path, self.path = self.root / 'identity.sqlite', self.root / 'enroll.sqlite'
        self.identities = Identities(self.identity_path, clock=lambda: self.now[0])
        self.service = Enrollments(self.path, self.identity_path, initialize=True, clock=lambda: self.now[0])

    def claim(self, invite, credential=None):
        return {'invitation': invite['invitation'], 'credential': credential or new_credential()}

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(MarketError) as cm:
            function(*args, **kwargs)
        self.assertEqual(code, cm.exception.code)

    def counts(self):
        with self.identities.store.connect() as db:
            return tuple(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                         for table in ('accounts', 'api_keys', 'identity_audit'))

    def test_invite_fixes_role_and_replay_preserves_key_expiration_and_audit(self):
        for role in ('buyer', 'seller'):
            with self.subTest(role=role):
                invite = self.service.issue(role, ttl_seconds=60)
                value = self.claim(invite)
                first = self.service.claim(value)
                self.now[0] += 120  # Expired invitation may acknowledge an already granted active key.
                second = self.service.claim(value)
                self.assertEqual(first, second)
                identity = self.identities.authenticate(value['credential'])
                self.assertEqual(role, identity['role'])
                self.assertEqual(DEFAULT_POLICY, identity['policy'])
        self.assertEqual((2, 2, 2), self.counts())
        with self.service.store.connect() as db:
            self.assertEqual(2, db.execute("SELECT COUNT(*) FROM enrollment_audit WHERE action='consumed'").fetchone()[0])

    def test_reviewer_policy_principal_and_invalid_credentials_cannot_be_requested(self):
        self.assert_code('invalid_params', self.service.issue, 'reviewer')
        value = self.claim(self.service.issue('seller'))
        for name in ('role', 'principal', 'policy', 'ttl_seconds'):
            self.assert_code('invalid_params', self.service.claim, {**value, name: 'reviewer'})
        for credential in ('', None, 'ak_fake.secret', 'ak_' + 'a' * 32 + '.' + 'x' * 44):
            self.assert_code('invalid_params', self.service.claim, {**value, 'credential': credential})
        self.assertEqual((0, 0, 0), self.counts())

    def test_expired_revoked_and_wrong_invitation_do_not_create_accounts(self):
        expired = self.service.issue('buyer', 60)
        revoked = self.service.issue('seller')
        self.service.revoke(revoked['invitation_id'])
        self.now[0] += 60
        for value in (self.claim(expired), self.claim(revoked),
                      {'invitation': 'inv_' + 'a' * 32 + '.' + 'a' * 43, 'credential': new_credential()},
                      {'invitation': 'fake', 'credential': new_credential()}):
            self.assert_code('enrollment_denied', self.service.claim, value)
        self.assertEqual((0, 0, 0), self.counts())

    def test_different_key_or_same_id_wrong_secret_cannot_take_over_claimed_invitation(self):
        invite = self.service.issue('buyer')
        value = self.claim(invite)
        self.service.claim(value)
        self.assert_code('enrollment_conflict', self.service.claim, self.claim(invite))
        altered = value['credential'][:-1] + ('a' if value['credential'][-1] != 'a' else 'b')
        self.assert_code('enrollment_conflict', self.service.claim, self.claim(invite, altered))
        self.assertEqual((1, 1, 1), self.counts())

    def test_revoked_disabled_or_expired_credentials_never_revive_on_replay(self):
        for action in ('revoke', 'disable', 'expire'):
            with self.subTest(action=action):
                invite = self.service.issue('seller', key_ttl_seconds=60)
                value = self.claim(invite)
                issued = self.service.claim(value)
                if action == 'revoke':
                    self.identities.revoke_key(issued['key_id'])
                elif action == 'disable':
                    self.identities.disable_account(issued['principal'])
                else:
                    self.now[0] += 60
                self.assert_code('enrollment_inactive', self.service.claim, value)
                self.assert_code('unauthenticated', self.identities.authenticate, value['credential'])

    def test_identity_audit_failure_rolls_back_both_account_and_key(self):
        value = self.claim(self.service.issue('buyer'))
        with patch.object(self.service.identities, 'audit', side_effect=sqlite3.OperationalError('test failure')):
            with self.assertRaises(sqlite3.Error):
                self.service.claim(value)
        self.assertEqual((0, 0, 0), self.counts())
        self.assertEqual('enrolled', self.service.claim(value)['status'])

    def test_second_ledger_failure_and_restart_reconcile_original_identity(self):
        invite = self.service.issue('seller')
        value = self.claim(invite)
        with patch.object(self.service, 'audit', side_effect=sqlite3.OperationalError('test failure')):
            with self.assertRaises(sqlite3.Error):
                self.service.claim(value)
        self.assertEqual((1, 1, 1), self.counts())
        self.assertEqual('seller', self.identities.authenticate(value['credential'])['role'])
        self.service = Enrollments(self.path, self.identity_path, clock=lambda: self.now[0])
        self.assert_code('enrollment_conflict', self.service.claim, self.claim(invite))
        self.now[0] += 604800  # Still reconcile the original active key after invitation expires.
        reply = self.service.claim(value)
        self.assertEqual(1000 + 2592000, reply['expires_at'])
        self.assertEqual((1, 1, 1), self.counts())

    def test_concurrent_claims_create_exactly_one_account_and_never_rotate_credential(self):
        invite = self.service.issue('buyer')
        original = self.claim(invite)
        with ThreadPoolExecutor(max_workers=6) as executor:
            replies = list(executor.map(self.service.claim, [original] * 12))
        self.assertTrue(all(reply == replies[0] for reply in replies))
        self.assertEqual((1, 1, 1), self.counts())
        invite = self.service.issue('seller')
        values = [self.claim(invite) for _ in range(6)]
        def submit(value):
            try:
                return self.service.claim(value)['status']
            except MarketError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(submit, values))
        self.assertEqual(1, results.count('enrolled'))
        self.assertEqual(5, results.count('enrollment_conflict'))
        self.assertEqual((2, 2, 2), self.counts())

    def test_identity_recovery_epoch_and_wrong_identity_path_refuse_old_invites(self):
        value = self.claim(self.service.issue('buyer'))
        other = self.root / 'other.sqlite'
        Identities(other)
        self.assert_code('enrollment_recovery_required', Enrollments, self.path, other)
        with self.identities.store.connect(write=True) as db:
            db.execute("INSERT INTO meta VALUES('recovery_id','new-recovery')")
        self.assert_code('enrollment_recovery_required', self.service.claim, value)
        self.assert_code('enrollment_recovery_required', self.service.ready)
        self.assert_code('enrollment_recovery_required', Enrollments, self.path, self.identity_path, initialize=True)
        self.assertEqual((0, 0, 0), self.counts())

    def test_existing_empty_configuration_is_not_initialized_and_probe_does_not_recreate_missing_file(self):
        empty = self.root / 'empty.sqlite'
        empty.touch()
        self.assert_code('wrong_database', Enrollments, empty, self.identity_path)
        self.assertEqual(b'', empty.read_bytes())
        self.assert_code('wrong_database', Enrollments, self.path, empty)
        self.assertEqual(b'', empty.read_bytes())
        self.assert_code('wrong_database', MarketHTTP, self.root / 'empty-market.sqlite', empty,
                         enrollment_database=self.path, allowed_hosts=['market.test:8765'])
        self.assertEqual(b'', empty.read_bytes())
        self.path.rename(self.root / 'moved-enroll.sqlite')
        self.assert_code('not_found', self.service.ready)
        self.assertFalse(self.path.exists())

    def test_real_snapshot_restore_invalidates_old_invites_and_does_not_revive_old_key(self):
        from ai_supermarket.budget import BudgetStore
        from ai_supermarket import recovery
        value = self.claim(self.service.issue('buyer'))
        self.service.claim(value)
        market, budget = self.root / 'market.sqlite', self.root / 'budget.sqlite'
        ExchangeStore(market)
        BudgetStore(budget)
        bundle, restored = self.root / 'snapshot', self.root / 'restored'
        recovery.snapshot_bundle(market, self.identity_path, budget, bundle)
        recovery.restore_bundle(bundle, restored)
        restored_identity = restored / 'identities.sqlite'
        self.assert_code('enrollment_recovery_required', Enrollments, self.path, restored_identity)
        recovered = Identities(restored_identity, clock=lambda: self.now[0])
        self.assert_code('unauthenticated', recovered.authenticate, value['credential'])
        # A fresh admission configuration is explicit and separate; it does not import old invitations.
        fresh = Enrollments(restored / 'new-enroll.sqlite', restored_identity, initialize=True)
        self.assert_code('enrollment_denied', fresh.claim, value)
        app = MarketHTTP(restored / 'market.sqlite', restored_identity, enrollment_database=restored / 'new-enroll.sqlite',
                         allowed_hosts=['market.test:8765'])
        new_claim = self.claim(fresh.issue('seller'))
        self.assert_code('not_ready', app.enroll, new_claim)
        recovery.release_bundle(restored, {key: 'test-only:' + key for key in recovery.REVIEW_FIELDS})
        self.assertEqual('enrolled', app.enroll(new_claim)['status'])
        self.assert_code('unauthenticated', recovered.authenticate, value['credential'])

    def test_recovery_or_expiration_between_checks_and_identity_write_refuses_creation(self):
        value = self.claim(self.service.issue('buyer', 60))
        actual = self.service.identities.enroll
        def expired(*args, **kwargs):
            self.now[0] += 60
            return actual(*args, **kwargs)
        with patch.object(self.service.identities, 'enroll', side_effect=expired):
            self.assert_code('enrollment_denied', self.service.claim, value)
        value = self.claim(self.service.issue('seller'))
        def restored(*args, **kwargs):
            with self.identities.store.connect(write=True) as db:
                db.execute("INSERT INTO meta VALUES('recovery_id','restored-during-claim')")
            return actual(*args, **kwargs)
        with patch.object(self.service.identities, 'enroll', side_effect=restored):
            self.assert_code('enrollment_recovery_required', self.service.claim, value)
        self.assertEqual((0, 0, 0), self.counts())

    def test_existing_operator_account_or_key_cannot_be_reused_for_enrollment(self):
        invite = self.service.issue('buyer')
        principal = 'member_' + invite['invitation_id'][4:]
        self.identities.create_account(principal, 'buyer')
        self.assert_code('enrollment_conflict', self.service.claim, self.claim(invite))
        self.identities.create_account('operator', 'reviewer')
        key = self.identities.issue_key('operator')
        invite = self.service.issue('seller')
        self.assert_code('enrollment_conflict', self.service.claim, self.claim(invite, key['token']))
        self.assertEqual('reviewer', self.identities.authenticate(key['token'])['role'])

    def test_secret_files_never_overwrite_and_ledgers_do_not_store_plaintext(self):
        invite_file, credential_file = self.root / 'invite.json', self.root / 'credential.json'
        issuance = issue_to_file(self.service, 'seller', invite_file)
        prepared = prepare_credential(credential_file)
        before = credential_file.read_bytes()
        with self.assertRaises(FileExistsError):
            prepare_credential(credential_file)
        with self.assertRaises(FileExistsError):
            issue_to_file(self.service, 'buyer', invite_file)
        self.assertEqual(before, credential_file.read_bytes())
        invitation = json.loads(invite_file.read_text())['invitation']
        credential = json.loads(before)['token']
        result = self.service.claim({'invitation': invitation, 'credential': credential})
        public = compact([issuance, prepared, result]).encode('utf-8')
        for secret in (invitation.encode(), credential.encode()):
            self.assertNotIn(secret, public)
            self.assertNotIn(secret, self.path.read_bytes())
            self.assertNotIn(secret, self.identity_path.read_bytes())

    def test_failed_invitation_file_write_revokes_invitation(self):
        with patch('ai_supermarket.enrollment_admin.os.fsync', side_effect=OSError('test write failure')):
            with self.assertRaises(OSError):
                issue_to_file(self.service, 'seller', self.root / 'failed.json')
        with self.service.store.connect() as db:
            row = db.execute('SELECT revoked_at FROM invitations').fetchone()
            self.assertIsNotNone(row['revoked_at'])

    def test_local_client_rejects_missing_files_and_reflected_secret_responses(self):
        client = RemoteEnrollment('http://127.0.0.1:8765', self.root / 'credential.json')
        self.assert_code('credential_unavailable', client.enroll, self.root / 'invite.json')
        prepare_credential(client.credential_file)
        self.assert_code('invitation_unavailable', client.enroll, self.root / 'invite.json')
        issue_to_file(self.service, 'buyer', self.root / 'invite.json')
        reflected = client.credential()
        with patch.object(client, '_send', return_value={'status': 'error', 'error_code': reflected, 'message': reflected}):
            reply = client.enroll(self.root / 'invite.json')
        self.assertNotIn(reflected, compact(reply))
        with patch.object(client, '_send', return_value={'status': 'enrolled', 'token': reflected}):
            self.assert_code('invalid_response', client.enroll, self.root / 'invite.json')

    def test_operator_and_user_cli_keep_credentials_out_of_output(self):
        invitation, credential = self.root / 'cli-invite.json', self.root / 'cli-key.json'
        commands = [
            ['ai_supermarket.enrollment_admin', '--identities', str(self.identity_path), '--enrollments', str(self.path),
             'issue', '--role', 'seller', '--output', str(invitation)],
            ['ai_supermarket.enrollment_client', 'prepare', '--credential-file', str(credential)],
        ]
        outputs = []
        for command in commands:
            run = subprocess.run([sys.executable, '-m', *command], capture_output=True, timeout=10)
            self.assertEqual(0, run.returncode)
            self.assertEqual(b'', run.stderr)
            outputs.append(run.stdout)
        for path, field in ((invitation, 'invitation'), (credential, 'token')):
            secret = json.loads(path.read_text())[field].encode()
            self.assertNotIn(secret, b''.join(outputs))


class EnrollmentHTTPTests(unittest.IsolatedAsyncioTestCase):
    request = test_http_app.HTTPTests.request

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.market, self.identity_path, self.path = (self.root / name for name in ('market.sqlite', 'identity.sqlite', 'enroll.sqlite'))
        ExchangeStore(self.market)
        self.identities = Identities(self.identity_path)
        self.service = Enrollments(self.path, self.identity_path, initialize=True)
        self.app = self.make_app()
        self.value = {'invitation': self.service.issue('seller')['invitation'], 'credential': new_credential()}

    def make_app(self, **limits):
        return MarketHTTP(self.market, self.identity_path, enrollment_database=self.path,
                          allowed_hosts=['market.test:8765'], limits=replace(Limits(), **limits))

    async def enroll(self, value=None, **kwargs):
        return await self.request(self.value if value is None else value, path='/v1/enroll', token=False, **kwargs)

    async def test_optional_route_and_fixed_role_without_bearer_auth(self):
        code, reply, headers = await self.enroll()
        self.assertEqual(200, code)
        self.assertEqual('seller', reply['role'])
        self.assertEqual(b'no-store', headers[b'cache-control'])
        self.assertNotIn(self.value['credential'], compact(reply))
        self.assertEqual(reply, (await self.enroll())[1])
        self.app = MarketHTTP(self.market, self.identity_path, allowed_hosts=['market.test:8765'])
        self.assertEqual(404, (await self.enroll())[0])

    async def test_unknown_fields_invalid_json_and_oversize_never_create_accounts(self):
        self.app = self.make_app(enrollment_peer_per_minute=30)
        for field in ('role', 'principal', 'policy'):
            self.assertEqual(400, (await self.enroll({**self.value, field: 'reviewer'}))[0])
        self.assertEqual(400, (await self.enroll(body=b'{"credential":"x","credential":"y"}'))[0])
        self.assertEqual(413, (await self.enroll(body=b'x' * 4097))[0])
        self.assertEqual(413, (await self.enroll(headers=[(b'content-length', b'4097')]))[0])
        with self.identities.store.connect() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0])

    async def test_peer_rate_limit_rejects_before_reading_new_body(self):
        self.app = self.make_app(enrollment_peer_per_minute=1)
        self.assertEqual(200, (await self.enroll())[0])
        async def unread():
            self.fail('Rate-limited request must not read the credential body')
        code, _, headers = await self.enroll(receive=unread)
        self.assertEqual(429, code)
        self.assertIn(b'retry-after', headers)

    async def test_remote_cleartext_origin_and_compressed_body_refused(self):
        cases = [({'scheme': 'http', 'peer': '198.51.100.5'}, 403),
                 ({'headers': [(b'origin', b'https://other.test')]}, 403),
                 ({'headers': [(b'content-encoding', b'gzip')]}, 415)]
        for kwargs, expected in cases:
            self.assertEqual(expected, (await self.enroll(**kwargs))[0])
        self.assertEqual(200, (await self.enroll(scheme='https', peer='198.51.100.5'))[0])

    async def test_revoke_during_body_read_denies_admission(self):
        async def receive():
            self.service.revoke(self.value['invitation'].split('.')[0])
            return {'type': 'http.request', 'body': compact(self.value).encode('utf-8')}
        code, reply, _ = await self.enroll(receive=receive)
        self.assertEqual((403, 'enrollment_denied'), (code, reply['error_code']))

    async def test_pending_market_recovery_prevents_new_accounts(self):
        with ExchangeStore(self.market).connect(write=True) as db:
            db.execute("INSERT INTO meta VALUES('recovery_state','pending')")
        code, reply, _ = await self.enroll()
        self.assertEqual((503, 'not_ready'), (code, reply['error_code']))
        with self.identities.store.connect() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0])

    async def test_live_epoch_change_marks_configured_enrollment_unready(self):
        with self.identities.store.connect(write=True) as db:
            db.execute("INSERT INTO meta VALUES('recovery_id','new-batch')")
        with ExchangeStore(self.market).connect(write=True) as db:
            db.execute("INSERT INTO meta VALUES('recovery_id','new-batch')")
        self.assertEqual(503, (await self.enroll())[0])
        self.assertEqual((503, {'status': 'not_ready'}),
                         (await self.request(path='/health/ready', method='GET', token=False))[:2])

    async def test_disconnected_request_does_not_consume_invitation(self):
        async def receive():
            return {'type': 'http.disconnect'}
        self.assertEqual(400, (await self.enroll(receive=receive))[0])
        with self.service.store.connect() as db:
            self.assertIsNone(db.execute('SELECT consumed_at FROM invitations').fetchone()[0])
