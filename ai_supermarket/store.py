"""来源：本项目原创。独立本地SQLite记录，不接触现有钱包账本。"""

from contextlib import contextmanager
from pathlib import Path
import sqlite3

from .common import MarketError


LEGACY_SCHEMA = '''
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta VALUES ('schema_version', '1');
CREATE TABLE acquisitions (
 id TEXT PRIMARY KEY, principal TEXT NOT NULL, request_id TEXT NOT NULL,
 resource_id TEXT NOT NULL, version TEXT NOT NULL, purpose TEXT NOT NULL,
 content_hash TEXT NOT NULL, created_at REAL NOT NULL,
 UNIQUE(principal, request_id));
CREATE TABLE usage_events (
 id TEXT PRIMARY KEY, acquisition_id TEXT NOT NULL REFERENCES acquisitions(id),
 principal TEXT NOT NULL, task_id TEXT NOT NULL, request_id TEXT NOT NULL,
 created_at REAL NOT NULL, UNIQUE(principal, task_id, request_id));
CREATE TABLE feedback (
 usage_id TEXT PRIMARY KEY REFERENCES usage_events(id), principal TEXT NOT NULL,
 useful TEXT NOT NULL, check_result TEXT NOT NULL, failure_code TEXT,
 evidence TEXT NOT NULL, validator TEXT, created_at REAL NOT NULL);
CREATE TABLE task_checks (
 principal TEXT NOT NULL, task_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
 checks INTEGER NOT NULL, cache_hits INTEGER NOT NULL,
 PRIMARY KEY(principal, task_id));
'''

# New ledgers use version 2. Existing files are never migrated on open.
FEEDBACK_SCHEMA = '''CREATE TABLE feedback (
 id TEXT PRIMARY KEY, usage_id TEXT NOT NULL REFERENCES usage_events(id),
 principal TEXT NOT NULL, evidence TEXT NOT NULL CHECK(evidence IN ('agent_reported','host_check')),
 validator TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 useful TEXT NOT NULL CHECK(useful IN ('yes','no','unknown')),
 check_result TEXT NOT NULL CHECK(check_result IN ('pass','fail','not_run')),
 failure_code TEXT, supersedes TEXT REFERENCES feedback(id), reason_code TEXT,
 created_at REAL NOT NULL,
 UNIQUE(usage_id,evidence,validator,revision),
 CHECK((evidence='agent_reported' AND validator='') OR (evidence='host_check' AND validator<>'')),
 CHECK((revision=1 AND supersedes IS NULL AND reason_code IS NULL)
    OR (revision>1 AND supersedes IS NOT NULL AND reason_code IS NOT NULL)))'''
_old_feedback = LEGACY_SCHEMA[LEGACY_SCHEMA.index('CREATE TABLE feedback'):LEGACY_SCHEMA.index('CREATE TABLE task_checks')].strip().rstrip(';')
SCHEMA = LEGACY_SCHEMA.replace("('schema_version', '1')", "('schema_version', '2')").replace(_old_feedback, FEEDBACK_SCHEMA)


class Store:
    schema = SCHEMA
    tables = {'meta', 'acquisitions', 'usage_events', 'feedback', 'task_checks'}

    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Existing ledgers need validation, not a writer reservation on every request.
        # Keep schema and version reads in one snapshot; retain subclass recovery gates.
        with self.connect() as db:
            db.execute('BEGIN')
            if self._initialized(db):
                return
        # Another process may initialize the empty file after the read transaction ends.
        # Recheck while holding the write lock before creating any table.
        with self.connect(write=True) as db:
            if not self._initialized(db):
                for statement in self.schema.split(';'):
                    if statement.strip():
                        db.execute(statement)
                self._initialized(db)

    def _initialized(self, db):
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables:
            return False
        if tables != self.tables:
            raise MarketError('wrong_database', '不是AI超市账本；不会迁移或覆盖其他数据库。')
        version = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if version and version[0] == '1':
            raise MarketError('upgrade_required', '账本为v1；请用ai_supermarket.migrate复制升级到新文件，原文件不会自动修改。')
        if not version or version[0] != '2':
            raise MarketError('wrong_database', '不支持的AI超市账本版本。')
        return True

    @contextmanager
    def connect(self, write=False):
        if (self.path.parent / '.ai-supermarket-snapshot.json').exists():
            raise MarketError('snapshot_read_only', '快照不能直接作为运行账本；请先恢复到新目录并核验。')
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
