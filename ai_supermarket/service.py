"""来源：本项目原创。搜索、资料获取和轻量反馈；无模型、网络或支付调用。"""

from collections import OrderedDict
import hashlib
import time
import uuid

from .catalog import Catalog, DEFAULT_CATALOG, KINDS, PURPOSES
from .common import MarketError, choice, compact, identifier, integer, text
from .store import Store


FAILURES = ('incompatible', 'bad_output', 'delivery_failed', 'timeout', 'license_mismatch', 'unknown')


class Market:
    kinds = KINDS
    purposes = PURPOSES
    default_purpose = 'project_local'

    def __init__(self, database, principal='local-agent', catalog=DEFAULT_CATALOG, clock=None):
        self.principal = identifier(principal, 'principal')
        self.catalog_path = catalog
        self.clock = clock or time.time
        Catalog(catalog)  # Validate the catalog before creating a local ledger.
        self.store = Store(database)
        self.cache = OrderedDict()

    def _catalog(self):
        # Refresh metadata on each operation: suspended versions must never survive a cache hit.
        return Catalog(self.catalog_path)

    def _search_catalog(self):
        return self._catalog()

    def search(self, q, kind=None, limit=3, purpose='project_local'):
        catalog = self._search_catalog()
        # Validate even if an identical key might already be cached.
        text(q, 'q', 512)
        integer(limit, 1, 5, 'limit')
        choice(purpose, self.purposes, 'purpose')
        if kind is not None:
            choice(kind, self.kinds, 'kind')
        key = hashlib.sha256(compact([self.principal, q, kind, limit, purpose, catalog.revision]).encode()).hexdigest()
        now = self.clock()
        cached = self.cache.get(key)
        hit = bool(cached and now - cached[0] < 600)
        if hit:
            found = cached[1]
            self.cache.move_to_end(key)
        else:
            found = catalog.search(q, kind, limit, purpose)
            self.cache[key] = now, found
            if len(self.cache) > 128:
                self.cache.popitem(last=False)
        items = []
        with self.store.connect() as db:
            for item in found:
                owned = db.execute('SELECT 1 FROM acquisitions WHERE principal=? AND resource_id=? AND version=? AND purpose=? AND content_hash=? LIMIT 1',
                                   (self.principal, item['id'], item['version'], purpose, item['sha256'])).fetchone()
                items.append({'id': item['id'], 'version': item['version'], 'kind': item['kind'],
                              'summary': item['summary'], 'price_cents': item['price_cents'],
                              'currency': item['currency'], 'owned': bool(owned), 'license': item['license']})
        return {'status': 'ok' if items else 'no_match', 'items': items, 'cache_hit': hit,
                'next_action': 'resolve' if items else 'use_existing_capabilities'}

    def resolve(self, id, version, purpose='project_local'):
        item = self._catalog().get(id, version, purpose)
        return self._resolved(item)

    def _resolved(self, item):
        id, version = item['id'], item['version']
        with self.store.connect() as db:
            stats = db.execute('''SELECT f.evidence, NULLIF(f.validator,'') AS validator,
                f.check_result, COUNT(DISTINCT f.usage_id) AS n FROM feedback f
                JOIN usage_events u ON u.id=f.usage_id JOIN acquisitions a ON a.id=u.acquisition_id
                WHERE f.principal=? AND a.resource_id=? AND a.version=? AND a.content_hash=?
                AND NOT EXISTS (SELECT 1 FROM feedback newer WHERE newer.usage_id=f.usage_id
                    AND newer.evidence=f.evidence AND newer.validator=f.validator AND newer.revision>f.revision)
                GROUP BY f.evidence, f.validator, f.check_result
                ORDER BY f.evidence, f.validator, f.check_result''', (self.principal, id, version, item['sha256'])).fetchall()
        return {'status': 'resolved', 'id': id, 'version': version, 'kind': item['kind'],
                'input': item['input'], 'output': item['output'], 'check': item['check'],
                'format': item['format'], 'permissions': item['permissions'],
                'license': item['license'], 'allowed_uses': item['allowed_uses'],
                'price_cents': item['price_cents'], 'currency': item['currency'],
                'quality': {'scope': 'local_principal', 'independently_verified': False,
                            'observations': [dict(row) for row in stats]},
                'next_action': 'acquire' if item['price_cents'] == 0 else 'payment_unavailable'}

    def acquire(self, id, version, request_id, purpose='project_local'):
        identifier(request_id, 'request_id')
        catalog = self._catalog()
        item = catalog.get(id, version, purpose)
        catalog.read(item)  # Verify actual delivery bytes before recording an entitlement.
        with self.store.connect(write=True) as db:
            old = db.execute('SELECT * FROM acquisitions WHERE principal=? AND request_id=?',
                             (self.principal, request_id)).fetchone()
            if old:
                if (old['resource_id'], old['version'], old['purpose'], old['content_hash']) != (id, version, purpose, item['sha256']):
                    raise MarketError('idempotency_conflict', '同一请求编号不能更换资源、版本或用途。')
                return self._delivery(old)
            # Reuse existing same-version rights without charging again; the first release has no payment adapter.
            owned = db.execute('SELECT * FROM acquisitions WHERE principal=? AND resource_id=? AND version=? AND purpose=? AND content_hash=? LIMIT 1',
                               (self.principal, id, version, purpose, item['sha256'])).fetchone()
            if item['price_cents'] > 0 and not owned:
                return {'status': 'payment_unavailable', 'id': id, 'version': version,
                        'price_cents': item['price_cents'], 'currency': 'CNY', 'next_action': 'choose_free_resource'}
            acquisition_id = 'a_' + uuid.uuid4().hex
            db.execute('INSERT INTO acquisitions VALUES (?,?,?,?,?,?,?,?)',
                       (acquisition_id, self.principal, request_id, id, version, purpose, item['sha256'], self.clock()))
            row = db.execute('SELECT * FROM acquisitions WHERE id=?', (acquisition_id,)).fetchone()
            return self._delivery(row)

    @staticmethod
    def _delivery(row):
        return {'status': 'acquired', 'acquisition_id': row['id'], 'id': row['resource_id'],
                'version': row['version'], 'content_hash': row['content_hash'],
                'payment': 'not_required', 'next_action': 'read'}

    def _owned(self, db, acquisition_id):
        identifier(acquisition_id, 'acquisition_id')
        row = db.execute('SELECT * FROM acquisitions WHERE principal=? AND id=?',
                         (self.principal, acquisition_id)).fetchone()
        if not row:
            raise MarketError('not_found', '找不到本主体的获取记录。')
        return row

    def status(self, acquisition_id):
        with self.store.connect() as db:
            row = self._owned(db, acquisition_id)
        try:
            catalog = self._catalog()
            item = catalog.get(row['resource_id'], row['version'], row['purpose'])
            if item['sha256'] != row['content_hash']:
                raise MarketError('version_changed', '相同版本内容已变化。')
            catalog.read(item)
        except MarketError as exc:
            return {'status': 'unavailable', 'acquisition_id': acquisition_id,
                    'error_code': exc.code, 'next_action': 'stop'}
        return self._delivery(row)

    def read(self, acquisition_id, task_id, request_id):
        identifier(task_id, 'task_id')
        identifier(request_id, 'request_id')
        with self.store.connect(write=True) as db:
            acquired = self._owned(db, acquisition_id)
            catalog = self._catalog()
            item = catalog.get(acquired['resource_id'], acquired['version'], acquired['purpose'])
            if item['sha256'] != acquired['content_hash']:
                raise MarketError('version_changed', '相同版本已变化，请由宿主核对。')
            content = catalog.read(item)
            old = db.execute('SELECT * FROM usage_events WHERE principal=? AND task_id=? AND request_id=?',
                             (self.principal, task_id, request_id)).fetchone()
            if old and old['acquisition_id'] != acquisition_id:
                raise MarketError('idempotency_conflict', '读取请求编号已用于其他资源。')
            usage_id = old['id'] if old else 'u_' + uuid.uuid4().hex
            if not old:
                db.execute('INSERT INTO usage_events VALUES (?,?,?,?,?,?)',
                           (usage_id, acquisition_id, self.principal, task_id, request_id, self.clock()))
        return {'status': 'read', 'usage_id': usage_id, 'format': item['format'], 'content': content,
                'license': item['license'], 'evidence': 'delivery_only'}

    def feedback(self, usage_id, useful='unknown', check='not_run', failure_code=None):
        return self._feedback(usage_id, useful, check, failure_code, 'agent_reported', None)

    def record_check(self, usage_id, validator, passed):
        """Trusted Python host only; never exposed as a model tool or proof of general quality."""
        identifier(validator, 'validator')
        if type(passed) is not bool:
            raise MarketError('invalid_params', 'passed必须是布尔值。')
        return self._feedback(usage_id, 'unknown', 'pass' if passed else 'fail',
                              None if passed else 'bad_output', 'host_check', validator)

    def correct_feedback(self, usage_id, expected_revision, reason_code,
                         useful='unknown', check='not_run', failure_code=None):
        """Trusted host correction of this principal's AI report; original evidence is retained."""
        return self._feedback(usage_id, useful, check, failure_code, 'agent_reported', None,
                              expected_revision, reason_code)

    def correct_check(self, usage_id, validator, passed, expected_revision, reason_code):
        """Trusted host only; correction cannot turn a local check into independent review."""
        identifier(validator, 'validator')
        if type(passed) is not bool:
            raise MarketError('invalid_params', 'passed必须是布尔值。')
        return self._feedback(usage_id, 'unknown', 'pass' if passed else 'fail',
                              None if passed else 'bad_output', 'host_check', validator,
                              expected_revision, reason_code)

    def feedback_history(self, usage_id):
        """Host inspection only; no other principal's usage or task content is exposed."""
        identifier(usage_id, 'usage_id')
        with self.store.connect() as db:
            if not db.execute('SELECT 1 FROM usage_events WHERE id=? AND principal=?',
                              (usage_id, self.principal)).fetchone():
                raise MarketError('not_found', '找不到本主体的使用记录。')
            rows = db.execute('''SELECT id,evidence,NULLIF(validator,'') AS validator,revision,
                useful,check_result,failure_code,supersedes,reason_code,created_at
                FROM feedback WHERE usage_id=? AND principal=?
                ORDER BY evidence,validator,revision''', (usage_id, self.principal)).fetchall()
        return {'status': 'ok', 'usage_id': usage_id, 'events': [dict(row) for row in rows]}

    def _feedback(self, usage_id, useful, check, failure_code, evidence, validator,
                  expected_revision=None, reason_code=None):
        identifier(usage_id, 'usage_id')
        choice(useful, ('yes', 'no', 'unknown'), 'useful')
        choice(check, ('pass', 'fail', 'not_run'), 'check')
        if failure_code is not None:
            choice(failure_code, FAILURES, 'failure_code')
        if check == 'pass' and failure_code is not None:
            raise MarketError('invalid_params', '通过检查不能同时报告失败类型。')
        correction = expected_revision is not None
        if correction:
            integer(expected_revision, 1, 1_000_000, 'expected_revision')
            choice(reason_code, ('check_repeated', 'report_corrected', 'appeal_resolved'), 'reason_code')
        validator = validator or ''
        revision = expected_revision + 1 if correction else 1
        with self.store.connect(write=True) as db:
            event = db.execute('SELECT 1 FROM usage_events WHERE id=? AND principal=?',
                               (usage_id, self.principal)).fetchone()
            if not event:
                raise MarketError('not_found', '找不到本主体的使用记录。')
            old = db.execute('''SELECT * FROM feedback WHERE usage_id=? AND evidence=?
                AND validator=? AND revision=?''', (usage_id, evidence, validator, revision)).fetchone()
            values = useful, check, failure_code, reason_code
            if old:
                if tuple(old[key] for key in ('useful', 'check_result', 'failure_code', 'reason_code')) != values:
                    raise MarketError('feedback_conflict', '该来源和修订号已有不同反馈；请核对历史。')
                return {'status': 'duplicate', 'usage_id': usage_id}
            previous = db.execute('''SELECT id,revision FROM feedback WHERE usage_id=? AND evidence=?
                AND validator=? ORDER BY revision DESC LIMIT 1''', (usage_id, evidence, validator)).fetchone()
            if correction and (not previous or previous['revision'] != expected_revision):
                raise MarketError('revision_conflict', '反馈修订号不匹配；先读取本主体的反馈历史。')
            db.execute('INSERT INTO feedback VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('f_' + uuid.uuid4().hex, usage_id, self.principal, evidence, validator, revision,
                        useful, check, failure_code, previous['id'] if correction else None, reason_code, self.clock()))
        return {'status': 'accepted', 'usage_id': usage_id}

    def begin_task(self, task_id, q, kind=None, purpose=None):
        """Host calls this at task entry; raw task text is not saved in SQLite."""
        identifier(task_id, 'task_id')
        purpose = self.default_purpose if purpose is None else purpose
        result = self.search(q, kind, purpose=purpose)
        fingerprint = hashlib.sha256(compact([q, kind, purpose]).encode()).hexdigest()
        with self.store.connect(write=True) as db:
            db.execute('''INSERT INTO task_checks VALUES (?,?,?,?,?)
                ON CONFLICT(principal,task_id) DO UPDATE SET fingerprint=excluded.fingerprint,
                checks=task_checks.checks+1, cache_hits=task_checks.cache_hits+excluded.cache_hits''',
                       (self.principal, task_id, fingerprint, 1, int(result['cache_hit'])))
        return result
