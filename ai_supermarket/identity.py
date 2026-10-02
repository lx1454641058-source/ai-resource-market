"""来源：本项目原创。独立服务身份账本；随机API凭据只存摘要，不读取已有账号或密钥。"""

import hashlib
import re
import secrets
import time
import uuid

from .common import MarketError, choice, compact, identifier, integer, parse_json
from .contracts import HostPolicy
from .store import Store


IDENTITY_SCHEMA = '''
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta VALUES ('schema_version','2');
INSERT INTO meta VALUES ('identity_schema_version','1');
CREATE TABLE accounts (
 principal TEXT PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('buyer','seller','reviewer')),
 policy TEXT NOT NULL, disabled INTEGER NOT NULL CHECK(disabled IN (0,1)), created_at REAL NOT NULL);
CREATE TABLE api_keys (
 id TEXT PRIMARY KEY, principal TEXT NOT NULL REFERENCES accounts(principal),
 digest TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL, revoked_at REAL);
CREATE INDEX account_keys ON api_keys(principal);
CREATE TABLE identity_audit (
 id TEXT PRIMARY KEY, principal TEXT NOT NULL, action TEXT NOT NULL,
 key_id TEXT, created_at REAL NOT NULL);
'''
DEFAULT_POLICY = {'capabilities': ['read_markdown'], 'permissions': [], 'dependencies': [], 'extra_budget_cents': 0}
KEY_PATTERN = r'ak_[0-9a-f]{32}\.[A-Za-z0-9_-]{43}'


def new_credential():
    """Only trusted local provisioning code receives this value, never model tools."""
    return 'ak_' + uuid.uuid4().hex + '.' + secrets.token_urlsafe(32)


class IdentityStore(Store):
    schema = IDENTITY_SCHEMA
    tables = {'meta', 'accounts', 'api_keys', 'identity_audit'}


class Identities:
    """Provisioning methods are trusted-operator APIs; never expose them as model tools."""
    def __init__(self, path, clock=None):
        self.store = IdentityStore(path)
        self.clock = clock or time.time
        with self.store.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key='identity_schema_version'").fetchone()
            if not row or row[0] != '1':
                raise MarketError('wrong_database', '身份账本版本不受支持。')

    def audit(self, db, principal, action, key_id=None):
        db.execute('INSERT INTO identity_audit VALUES (?,?,?,?,?)',
                   ('ie_' + uuid.uuid4().hex, principal, action, key_id, self.clock()))

    def create_account(self, principal, role, policy=None):
        identifier(principal, 'principal')
        choice(role, ('buyer', 'seller', 'reviewer'), 'role')
        policy = DEFAULT_POLICY if policy is None else policy
        HostPolicy.from_json(policy)
        serialized = compact(policy)
        with self.store.connect(write=True) as db:
            old = db.execute('SELECT role,policy FROM accounts WHERE principal=?', (principal,)).fetchone()
            if old:
                if (old['role'], old['policy']) != (role, serialized):
                    raise MarketError('account_conflict', '主体已存在，不能通过重复注册改变角色或权限。')
                return {'status': 'existing', 'principal': principal, 'role': role}
            db.execute('INSERT INTO accounts VALUES (?,?,?,?,?)', (principal, role, serialized, 0, self.clock()))
            self.audit(db, principal, 'account_created')
        return {'status': 'created', 'principal': principal, 'role': role}

    def issue_key(self, principal, ttl_seconds=2_592_000):
        identifier(principal, 'principal')
        integer(ttl_seconds, 60, 7_776_000, 'ttl_seconds')
        key_id = 'ak_' + uuid.uuid4().hex
        token = key_id + '.' + secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode('ascii')).hexdigest()
        now = self.clock()
        with self.store.connect(write=True) as db:
            row = db.execute('SELECT disabled FROM accounts WHERE principal=?', (principal,)).fetchone()
            if not row or row['disabled']:
                raise MarketError('account_unavailable', '主体不可签发新凭据。')
            db.execute('INSERT INTO api_keys VALUES (?,?,?,?,?,NULL)', (key_id, principal, digest, now, now + ttl_seconds))
            self.audit(db, principal, 'key_issued', key_id)
        # Only the trusted issuer receives the raw token. Never include it in logs or tool results.
        return {'key_id': key_id, 'token': token, 'expires_at': now + ttl_seconds}

    def enroll(self, invitation_id, role, credential, *, expected_epoch, admission_expires_at, ttl_seconds):
        """Trusted invitation service only: create account and key in ONE identity transaction.

        The invitation's fixed principal makes recovery after a second-ledger commit failure
        idempotent. A different credential can never attach to an existing account.
        """
        if not isinstance(invitation_id, str) or not re.fullmatch(r'inv_[0-9a-f]{32}', invitation_id):
            raise MarketError('invalid_params', '入驻编号无效。')
        choice(role, ('buyer', 'seller'), 'role')
        if not isinstance(credential, str) or not re.fullmatch(KEY_PATTERN, credential):
            raise MarketError('invalid_params', '本地凭据格式无效。')
        if not isinstance(expected_epoch, str) or type(admission_expires_at) not in (int, float):
            raise MarketError('invalid_params', '入驻恢复条件无效。')
        integer(ttl_seconds, 60, 7_776_000, 'ttl_seconds')
        principal = 'member_' + invitation_id[4:]
        key_id = credential.split('.', 1)[0]
        digest = hashlib.sha256(credential.encode('ascii')).hexdigest()
        policy = compact(DEFAULT_POLICY)
        with self.store.connect(write=True) as db:
            epoch = db.execute("SELECT value FROM meta WHERE key='recovery_id'").fetchone()
            if (epoch[0] if epoch else '') != expected_epoch:
                raise MarketError('enrollment_recovery_required', '身份恢复批次变化；须重新核验入驻配置。')
            account = db.execute('SELECT role,policy,disabled FROM accounts WHERE principal=?', (principal,)).fetchone()
            key = db.execute('SELECT * FROM api_keys WHERE id=?', (key_id,)).fetchone()
            now = self.clock()
            if account or key:
                marker = db.execute("SELECT 1 FROM identity_audit WHERE principal=? AND action='enrollment_created' AND key_id=?",
                                    (principal, key_id)).fetchone()
                if (not account or not key or not marker or key['principal'] != principal
                        or not secrets.compare_digest(key['digest'], digest)
                        or account['role'] != role or account['policy'] != policy):
                    raise MarketError('enrollment_conflict', '邀请已绑定其他凭据，不能重复开户或更换角色。')
                if account['disabled'] or key['revoked_at'] is not None or key['expires_at'] <= now:
                    raise MarketError('enrollment_inactive', '已有入驻凭据失效；重试不会恢复权限或延长有效期。')
                expires_at = key['expires_at']
            else:
                if not admission_expires_at > now:
                    raise MarketError('enrollment_denied', '邀请无效、已撤销或已过期。')
                expires_at = now + ttl_seconds
                db.execute('INSERT INTO accounts VALUES (?,?,?,?,?)', (principal, role, policy, 0, now))
                db.execute('INSERT INTO api_keys VALUES (?,?,?,?,?,NULL)',
                           (key_id, principal, digest, now, expires_at))
                self.audit(db, principal, 'enrollment_created', key_id)
        return {'status': 'enrolled', 'principal': principal, 'role': role,
                'key_id': key_id, 'expires_at': expires_at}

    def authenticate(self, token):
        denied = MarketError('unauthenticated', '凭据无效、过期或已撤销。')
        if not isinstance(token, str) or not re.fullmatch(KEY_PATTERN, token):
            raise denied
        key_id = token.split('.', 1)[0]
        digest = hashlib.sha256(token.encode('ascii')).hexdigest()
        with self.store.connect() as db:
            row = db.execute('''SELECT k.digest,k.expires_at,k.revoked_at,a.principal,a.role,a.policy,a.disabled
                FROM api_keys k JOIN accounts a ON a.principal=k.principal WHERE k.id=?''', (key_id,)).fetchone()
        valid_digest = secrets.compare_digest(digest, row['digest'] if row else '0' * 64)
        if (not row or not valid_digest or row['disabled'] or row['revoked_at'] is not None
                or row['expires_at'] <= self.clock()):
            raise denied
        policy = parse_json(row['policy'])
        HostPolicy.from_json(policy)
        return {'principal': row['principal'], 'role': row['role'], 'policy': policy, 'key_id': key_id}

    def revoke_key(self, key_id):
        identifier(key_id, 'key_id')
        with self.store.connect(write=True) as db:
            row = db.execute('SELECT principal,revoked_at FROM api_keys WHERE id=?', (key_id,)).fetchone()
            if not row:
                raise MarketError('not_found', '找不到此凭据记录。')
            if row['revoked_at'] is None:
                db.execute('UPDATE api_keys SET revoked_at=? WHERE id=?', (self.clock(), key_id))
                self.audit(db, row['principal'], 'key_revoked', key_id)
        return {'status': 'revoked', 'key_id': key_id}

    def disable_account(self, principal):
        identifier(principal, 'principal')
        with self.store.connect(write=True) as db:
            row = db.execute('SELECT disabled FROM accounts WHERE principal=?', (principal,)).fetchone()
            if not row:
                raise MarketError('not_found', '找不到此主体。')
            if not row['disabled']:
                db.execute('UPDATE accounts SET disabled=1 WHERE principal=?', (principal,))
                # All old credentials remain revoked even if future account recovery is added.
                db.execute('UPDATE api_keys SET revoked_at=? WHERE principal=? AND revoked_at IS NULL',
                           (self.clock(), principal))
                self.audit(db, principal, 'account_disabled')
        return {'status': 'disabled', 'principal': principal}
