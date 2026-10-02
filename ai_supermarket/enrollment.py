"""来源：本项目原创。限定角色的邀请入驻；凭据仅保存摘要，无公开身份管理权限。"""

import hashlib
from contextlib import closing
import os
from pathlib import Path
import re
import secrets
import time
import uuid

from .common import MarketError, choice, fields, integer
from .identity import Identities, KEY_PATTERN
from .store import Store


INVITE_PATTERN = r'inv_[0-9a-f]{32}\.[A-Za-z0-9_-]{43}'


class EnrollmentStore(Store):
    tables = {'meta', 'invitations', 'enrollment_audit'}
    schema = '''
    CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    INSERT INTO meta VALUES('schema_version','2');
    INSERT INTO meta VALUES('enrollment_schema_version','1');
    CREATE TABLE invitations(id TEXT PRIMARY KEY,digest TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('buyer','seller')),created_at REAL NOT NULL,
        expires_at REAL NOT NULL,key_ttl INTEGER NOT NULL,revoked_at REAL,
        key_id TEXT,consumed_at REAL);
    CREATE TABLE enrollment_audit(id TEXT PRIMARY KEY,invitation_id TEXT NOT NULL,
        action TEXT NOT NULL,created_at REAL NOT NULL);
    '''


class Enrollments:
    """Init/issue/revoke belong to the local operator. Only claim is exposed over HTTP."""
    def __init__(self, path, identities, *, initialize=False, clock=None):
        self.clock = clock or time.time
        if not Path(identities).is_file() or (not initialize and not Path(path).is_file()):
            raise MarketError('enrollment_not_initialized', '先由运营初始化身份及入驻账本。')
        if Path(path).resolve() == Path(identities).resolve():
            raise MarketError('wrong_database', '入驻账本必须与身份账本分开。')
        # Loading a server configuration must not initialize an accidentally empty ledger.
        from .recovery import connect_existing, validate_database
        with closing(connect_existing(identities, timeout=0.1)) as db:
            validate_database(db, 'identities', deep=False)
        if not initialize:
            with closing(connect_existing(path, timeout=0.1)) as db:
                self.validate_store(db)
        self.identities = Identities(identities, clock=self.clock)
        self.identity_path_hash = hashlib.sha256(os.path.normcase(str(Path(identities).resolve())).encode('utf-8')).hexdigest()
        epoch = self.identity_epoch()
        self.store = EnrollmentStore(path)
        with self.store.connect(write=True) as db:
            version = db.execute("SELECT value FROM meta WHERE key='enrollment_schema_version'").fetchone()
            if not version or version[0] != '1':
                raise MarketError('wrong_database', '入驻账本版本不受支持。')
            binding = db.execute("SELECT value FROM meta WHERE key='identity_path_hash'").fetchone()
            if binding is None and initialize:
                db.execute("INSERT INTO meta VALUES('identity_path_hash',?)", (self.identity_path_hash,))
                db.execute("INSERT INTO meta VALUES('identity_recovery_id',?)", (epoch,))
            self.check_binding(db, epoch)

    def identity_epoch(self):
        from .recovery import connect_existing
        with closing(connect_existing(self.identities.store.path, timeout=0.1)) as db:
            row = db.execute("SELECT value FROM meta WHERE key='recovery_id'").fetchone()
        return row[0] if row else ''

    @staticmethod
    def validate_store(db):
        import sqlite3
        from .migrate import schema_objects
        with closing(sqlite3.connect(':memory:')) as expected:
            expected.row_factory = sqlite3.Row
            expected.executescript(EnrollmentStore.schema)
            if schema_objects(db) != schema_objects(expected):
                raise MarketError('wrong_database', '入驻账本未初始化或结构不匹配。')
        meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('schema_version','enrollment_schema_version')"))
        if meta != {'schema_version': '2', 'enrollment_schema_version': '1'}:
            raise MarketError('wrong_database', '入驻账本版本不受支持。')

    def check_binding(self, db, epoch):
        values = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('identity_path_hash','identity_recovery_id')"))
        if values.get('identity_path_hash') != self.identity_path_hash or values.get('identity_recovery_id') != epoch:
            raise MarketError('enrollment_recovery_required', '身份位置或恢复批次变化；旧邀请不可继续使用。')

    def ready(self):
        from .recovery import connect_existing
        if any((path.parent / '.ai-supermarket-snapshot.json').exists()
               for path in (self.store.path, self.identities.store.path)):
            raise MarketError('snapshot_read_only', '快照不能作为入驻服务账本。')
        with closing(connect_existing(self.store.path, timeout=0.1)) as db:
            deadline = time.monotonic() + 1
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            self.validate_store(db)
            self.check_binding(db, self.identity_epoch())

    def audit(self, db, invitation_id, action):
        db.execute('INSERT INTO enrollment_audit VALUES(?,?,?,?)',
                   ('ea_' + uuid.uuid4().hex, invitation_id, action, self.clock()))

    def issue(self, role, ttl_seconds=86_400, key_ttl_seconds=2_592_000):
        choice(role, ('buyer', 'seller'), 'role')
        integer(ttl_seconds, 60, 604_800, 'ttl_seconds')
        integer(key_ttl_seconds, 60, 7_776_000, 'key_ttl_seconds')
        invitation_id = 'inv_' + uuid.uuid4().hex
        invitation = invitation_id + '.' + secrets.token_urlsafe(32)
        digest = hashlib.sha256(invitation.encode('ascii')).hexdigest()
        with self.store.connect(write=True) as db:
            self.check_binding(db, self.identity_epoch())
            now = self.clock()
            db.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,NULL,NULL,NULL)',
                       (invitation_id, digest, role, now, now + ttl_seconds, key_ttl_seconds))
            self.audit(db, invitation_id, 'issued')
        # Trusted issuer writes this secret to a NEW file, never stdout/model context.
        return {'invitation_id': invitation_id, 'invitation': invitation, 'role': role,
                'expires_at': now + ttl_seconds}

    def revoke(self, invitation_id):
        if not isinstance(invitation_id, str) or not re.fullmatch(r'inv_[0-9a-f]{32}', invitation_id):
            raise MarketError('invalid_params', '邀请编号无效。')
        with self.store.connect(write=True) as db:
            self.check_binding(db, self.identity_epoch())
            row = db.execute('SELECT revoked_at FROM invitations WHERE id=?', (invitation_id,)).fetchone()
            if not row:
                raise MarketError('not_found', '找不到邀请记录。')
            if row['revoked_at'] is None:
                db.execute('UPDATE invitations SET revoked_at=? WHERE id=?', (self.clock(), invitation_id))
                self.audit(db, invitation_id, 'revoked')
        # Revoking admission is separate from disabling an already issued account.
        return {'status': 'revoked', 'invitation_id': invitation_id}

    def claim(self, value):
        fields(value, ('invitation', 'credential'))
        invitation, credential = value['invitation'], value['credential']
        if not isinstance(invitation, str) or not re.fullmatch(INVITE_PATTERN, invitation):
            raise MarketError('enrollment_denied', '邀请无效、已撤销或已过期。')
        if not isinstance(credential, str) or not re.fullmatch(KEY_PATTERN, credential):
            raise MarketError('invalid_params', '本地凭据格式无效。')
        invitation_id = invitation.split('.', 1)[0]
        digest = hashlib.sha256(invitation.encode('ascii')).hexdigest()
        # Lock order is always enrollment -> identity. If identity commits but this
        # transaction fails, its fixed principal/key pair is the durable replay guard.
        with self.store.connect(write=True) as db:
            epoch = self.identity_epoch()
            self.check_binding(db, epoch)
            row = db.execute('SELECT * FROM invitations WHERE id=?', (invitation_id,)).fetchone()
            valid = secrets.compare_digest(digest, row['digest'] if row else '0' * 64)
            if not row or not valid or row['revoked_at'] is not None:
                raise MarketError('enrollment_denied', '邀请无效、已撤销或已过期。')
            key_id = credential.split('.', 1)[0]
            if row['key_id'] is not None and row['key_id'] != key_id:
                raise MarketError('enrollment_conflict', '邀请已绑定其他凭据，不能重复开户。')
            result = self.identities.enroll(invitation_id, row['role'], credential,
                                           expected_epoch=epoch, admission_expires_at=row['expires_at'],
                                           ttl_seconds=row['key_ttl'])
            if row['consumed_at'] is None:
                db.execute('UPDATE invitations SET key_id=?,consumed_at=? WHERE id=?',
                           (key_id, self.clock(), invitation_id))
                self.audit(db, invitation_id, 'consumed')
        return result
