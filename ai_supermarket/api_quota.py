"""本项目原创。供应商内部的预付 API 次数账本；不是公共工具或支付凭证。

认证、权益和输入契约由供应商的 authorize 回调核实。execute/lookup 只能是
可信供应商代码；模型不能提供这些回调。未知调用保持占用，不自动重新执行。
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from .common import MarketError, fields, identifier, integer, parse_json
from .origin import _safe_path


MAX_INPUT = 16384
MAX_RESULT = 65536
FORMAT = 'provider-api-quota-v1'


def canonical(value, maximum):
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise MarketError('invalid_json', '需要有界的有效JSON。') from None
    if len(raw) > maximum:
        raise MarketError('payload_too_large', '输入或结果超过本接口限制。')
    return raw.decode('utf-8')


def checksum(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def request_value(value):
    fields(value, ('acquisition_id', 'resource_id', 'version', 'purpose',
                   'release_digest', 'task_id', 'request_id', 'operation', 'input'))
    for name in ('acquisition_id', 'resource_id', 'version', 'task_id', 'request_id', 'operation'):
        identifier(value[name], name)
    if value['purpose'] not in ('personal_use', 'commercial_use'):
        raise MarketError('invalid_params', '用途无效。')
    if not isinstance(value['release_digest'], str) or not re.fullmatch('[0-9a-f]{64}', value['release_digest']):
        raise MarketError('invalid_params', '需要原发行摘要。')
    if not isinstance(value['input'], dict):
        raise MarketError('invalid_params', 'API输入需要JSON对象。')
    return parse_json(canonical(value, MAX_INPUT))


class ProviderQuota:
    """One provider, one private ledger, shared across all of its host processes.

    authorize(principal, request) must independently validate authenticated buyer,
    acquisition/payment lineage, release, purpose, task, operation and input schema.
    It returns a stable provider grant (see integration/api-quota.md). Caller fields
    never establish ownership or quota. Grants cannot change in place or be reset.
    """
    def __init__(self, path, provider, authorize, *, clock=None, require_existing=False):
        self.path = _safe_path(path)
        self.provider = identifier(provider, 'provider')
        if not callable(authorize) or type(require_existing) is not bool:
            raise MarketError('invalid_configuration', '缺少供应商权利核验器。')
        self.authorize = authorize
        self.clock = clock or time.time
        with self._db(create=not require_existing) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and tables != {'configuration', 'grants', 'calls'}:
                raise MarketError('ledger_mismatch', '需要独立供应商额度账本，不能迁移其他数据库。')
            if not tables:
                if require_existing:
                    raise MarketError('ledger_mismatch', '原次数账本缺少表，不能重新初始化。')
                db.execute('CREATE TABLE configuration (format TEXT NOT NULL, provider TEXT NOT NULL)')
                db.execute('INSERT INTO configuration VALUES (?,?)', (FORMAT, self.provider))
                db.execute('CREATE TABLE grants (id TEXT PRIMARY KEY, declaration TEXT NOT NULL)')
                db.execute('''CREATE TABLE calls (
                    id TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES grants(id),
                    principal TEXT NOT NULL, task_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    request TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('reserved','completed','not_executed')),
                    result TEXT, result_sha256 TEXT, evidence_ref TEXT,
                    UNIQUE(principal,task_id,request_id))''')
            self._binding(db)

    def _binding(self, db):
        records = [tuple(row) for row in db.execute('SELECT format,provider FROM configuration')]
        if records != [(FORMAT, self.provider)]:
            raise MarketError('ledger_mismatch', '供应商额度账本绑定不符。')

    @contextmanager
    def _db(self, *, create=False):
        _safe_path(self.path)
        try:
            if not create:
                with self.path.open('rb') as original:
                    if original.read(16) != b'SQLite format 3\x00':
                        raise MarketError('ledger_unavailable', '原次数账本为空或不可识别，停止调用。')
            # rw closes the check/open race; runtime must not manufacture quota.
            db = sqlite3.connect(self.path.as_uri() + ('?mode=rwc' if create else '?mode=rw'),
                                 uri=True, timeout=5, isolation_level=None)
        except (OSError, sqlite3.Error):
            raise MarketError('ledger_unavailable', '原次数账本不可用，保留记录核查，不重建。') from None
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _authorized(self, principal, request):
        identifier(principal, 'authenticated_principal')
        request = request_value(request)
        # Give policy a separate copy so it cannot silently alter the fixed call.
        grant = self.authorize(principal, parse_json(canonical(request, MAX_INPUT)))
        required = ('provider', 'principal', 'acquisition_id', 'resource_id', 'version',
                    'purpose', 'release_digest', 'operation', 'total_units', 'expires_at')
        fields(grant, required)
        for name in ('acquisition_id', 'resource_id', 'version', 'purpose', 'release_digest', 'operation'):
            if grant[name] != request[name]:
                raise MarketError('entitlement_denied', '供应商确认的权益与本次调用不符。')
        if grant['provider'] != self.provider or grant['principal'] != principal:
            raise MarketError('entitlement_denied', '供应商确认的主体不符。')
        integer(grant['total_units'], 1, 1000000, 'total_units')
        integer(grant['expires_at'], 1, 9999999999, 'expires_at')
        now = self.clock()
        if type(now) not in (int, float) or not 0 <= now < grant['expires_at']:
            raise MarketError('entitlement_expired', 'API权益过期或核验时间不可用。')
        declaration = canonical(grant, MAX_INPUT)
        grant_id = checksum(canonical([self.provider, principal, request['acquisition_id']], MAX_INPUT))
        return request, declaration, grant_id

    def _grant(self, db, grant_id, declaration):
        self._binding(db)
        previous = db.execute('SELECT declaration FROM grants WHERE id=?', (grant_id,)).fetchone()
        if previous is not None and previous['declaration'] != declaration:
            raise MarketError('grant_changed', '原API权益条件变化，不能重置或扩大原额度。')

    def _existing(self, db, principal, request):
        row = db.execute('SELECT * FROM calls WHERE principal=? AND task_id=? AND request_id=?',
                         (principal, request['task_id'], request['request_id'])).fetchone()
        if row is not None and row['request'] != canonical(request, MAX_INPUT):
            raise MarketError('idempotency_conflict', '原请求编号已绑定其他输入或权益。')
        return row

    def _report(self, db, row):
        grant = parse_json(db.execute('SELECT declaration FROM grants WHERE id=?', (row['grant_id'],)).fetchone()[0])
        counts = dict(db.execute('SELECT state,COUNT(*) FROM calls WHERE grant_id=? GROUP BY state', (row['grant_id'],)))
        held, used = counts.get('reserved', 0), counts.get('completed', 0)
        result = {'status': row['state'], 'operation_id': row['id'],
                  'reserved_units': held, 'used_units': used,
                  'remaining_units': grant['total_units'] - held - used,
                  'payment_executed': False, 'task_completed': False}
        if row['state'] == 'completed':
            if not isinstance(row['result'], str) or checksum(row['result']) != row['result_sha256']:
                raise MarketError('integrity_failed', '供应商缓存结果损坏，不能重新消费。')
            result.update(result=parse_json(row['result']), result_sha256=row['result_sha256'])
        return result

    def _reserve(self, principal, request):
        request, declaration, grant_id = self._authorized(principal, request)
        with self._db() as db:
            self._grant(db, grant_id, declaration)
            row = self._existing(db, principal, request)
            if row is not None:
                return request, self._report(db, row), False
            grant = parse_json(declaration)
            occupied = db.execute("SELECT COUNT(*) FROM calls WHERE grant_id=? AND state!='not_executed'", (grant_id,)).fetchone()[0]
            if occupied >= grant['total_units']:
                raise MarketError('quota_exhausted', 'API次数已用完或被未知调用占用。')
            db.execute('INSERT OR IGNORE INTO grants VALUES (?,?)', (grant_id, declaration))
            raw = canonical(request, MAX_INPUT)
            operation_id = checksum(canonical([self.provider, principal, request], MAX_INPUT + 1024))
            db.execute('INSERT INTO calls VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL)',
                       (operation_id, grant_id, principal, request['task_id'], request['request_id'], raw, 'reserved'))
            row = self._existing(db, principal, request)
            return request, self._report(db, row), True

    def status(self, principal, request):
        request, declaration, grant_id = self._authorized(principal, request)
        with self._db() as db:
            self._grant(db, grant_id, declaration)
            row = self._existing(db, principal, request)
            if row is None:
                raise MarketError('not_found', '没有该原请求；状态查询不会占用次数。')
            return self._report(db, row)

    def _settle(self, principal, request, state, result, evidence_ref):
        # Recheck access after provider work; revocation leaves the hold intact.
        request, declaration, grant_id = self._authorized(principal, request)
        identifier(evidence_ref, 'provider_evidence_ref')
        raw = canonical(result, MAX_RESULT) if state == 'completed' else None
        with self._db() as db:
            self._grant(db, grant_id, declaration)
            row = self._existing(db, principal, request)
            if row is None:
                raise MarketError('not_found', '无法确认未预留的原调用。')
            if row['state'] != 'reserved':
                if row['state'] != state or row['result'] != raw:
                    raise MarketError('settlement_conflict', '供应商证据与已保存结果冲突。')
                return self._report(db, row)
            db.execute('UPDATE calls SET state=?,result=?,result_sha256=?,evidence_ref=? WHERE id=?',
                       (state, raw, checksum(raw) if raw is not None else None, evidence_ref, row['id']))
            return self._report(db, self._existing(db, principal, request))

    def invoke(self, principal, request, execute):
        """execute(operation_id, operation, input) is invoked once per saved call.

        A process crash between reservation and execution leaves a hold, as does
        an exception/invalid result. No timeout can prove the operation did not run.
        """
        if not callable(execute):
            raise MarketError('invalid_configuration', '需要可信供应商执行器。')
        request, report, fresh = self._reserve(principal, request)
        if not fresh:
            return report
        try:
            result = execute(report['operation_id'], request['operation'],
                             parse_json(canonical(request['input'], MAX_INPUT)))
        except Exception:
            # Do not expose provider credentials or an untrusted exception string.
            return self.status(principal, request)
        return self._settle(principal, request, 'completed', result, 'executor-returned')

    def recover(self, principal, request, lookup):
        """Explicit read-only provider lookup; never redispatch the paid operation.

        not_executed means authoritative final proof plus fencing of pending work;
        provider 'not found', timeout or absence of a result must map to unknown.
        """
        if not callable(lookup):
            raise MarketError('invalid_configuration', '需要可信供应商原调用查询器。')
        request = request_value(request)
        report = self.status(principal, request)
        if report['status'] != 'reserved':
            return report
        try:
            evidence = lookup(report['operation_id'])
        except Exception:
            return self.status(principal, request)
        fields(evidence, ('state',), ('result', 'evidence_ref'))
        if evidence['state'] == 'unknown' and set(evidence) == {'state'}:
            return self.status(principal, request)
        if evidence['state'] == 'completed' and set(evidence) == {'state', 'result', 'evidence_ref'}:
            return self._settle(principal, request, 'completed', evidence['result'], evidence['evidence_ref'])
        if evidence['state'] == 'not_executed' and set(evidence) == {'state', 'evidence_ref'}:
            return self._settle(principal, request, 'not_executed', None, evidence['evidence_ref'])
        raise MarketError('invalid_evidence', '供应商状态不能证明完成或未执行，保留占用。')
