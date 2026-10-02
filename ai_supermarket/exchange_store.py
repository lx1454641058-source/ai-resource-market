"""来源：本项目原创。开放市场独立账本，复用原获取/使用表，不迁移旧数据库。"""

from contextlib import contextmanager

from .common import MarketError
from .store import SCHEMA, Store


EXCHANGE_SCHEMA = '''
INSERT INTO meta VALUES ('exchange_schema_version', '1');
CREATE TABLE submissions (
 resource_id TEXT NOT NULL, version TEXT NOT NULL, seller TEXT NOT NULL,
 metadata TEXT NOT NULL, content TEXT NOT NULL, content_hash TEXT NOT NULL,
 revision INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('draft','pending_review','approved','rejected')),
 review_note TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 PRIMARY KEY(resource_id, version));
CREATE INDEX seller_submissions ON submissions(seller, updated_at);
CREATE TABLE offers (
 resource_id TEXT NOT NULL, version TEXT NOT NULL, price_cents INTEGER NOT NULL CHECK(price_cents>=0),
 revision INTEGER NOT NULL, availability TEXT NOT NULL CHECK(availability IN ('active','withdrawn','suspended')),
 updated_at REAL NOT NULL, PRIMARY KEY(resource_id, version),
 FOREIGN KEY(resource_id, version) REFERENCES submissions(resource_id, version));
CREATE TABLE exchange_receipts (
 actor TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
 response TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(actor, request_id));
CREATE TABLE exchange_audit (
 id TEXT PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
 resource_id TEXT, version TEXT, detail TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX exchange_audit_resource ON exchange_audit(resource_id, version, created_at);
CREATE TABLE quotes (
 id TEXT PRIMARY KEY, buyer TEXT NOT NULL, resource_id TEXT NOT NULL, version TEXT NOT NULL,
 purpose TEXT NOT NULL, content_hash TEXT NOT NULL, offer_revision INTEGER NOT NULL,
 price_cents INTEGER NOT NULL, policy_version TEXT NOT NULL,
 created_at REAL NOT NULL, expires_at REAL NOT NULL);
'''


class ExchangeStore(Store):
    version = '1'
    schema = SCHEMA + EXCHANGE_SCHEMA
    tables = Store.tables | {'submissions', 'offers', 'exchange_receipts', 'exchange_audit', 'quotes'}

    @contextmanager
    def connect(self, write=False):
        with super().connect(write=write) as db:
            # Check every operation, including objects created before recovery was gated.
            has_meta = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
            state = db.execute("SELECT value FROM meta WHERE key='recovery_state'").fetchone() if has_meta else None
            restoring = (self.path.parent / 'restore-started.json').exists()
            if (state and state[0] != 'released') or (restoring and (not state or state[0] != 'released')):
                raise MarketError('recovery_required', '恢复副本尚未完成运营核验，市场未开放。')
            yield db

    def __init__(self, path):
        super().__init__(path)
        with self.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key='exchange_schema_version'").fetchone()
            if not row or row[0] != self.version:
                from .common import MarketError
                raise MarketError('wrong_database', '不支持的开放市场账本版本；不会自动迁移。')
