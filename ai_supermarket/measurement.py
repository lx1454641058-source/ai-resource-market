"""来源：本项目原创。受信宿主的整任务计量，不授权支出、不记录正文或推测token。"""

from contextlib import contextmanager
from contextlib import closing
import hashlib
import math
import sqlite3
import time
import uuid

from .common import MarketError, choice, compact, fields, identifier, integer, parse_json
from .store import Store


class MeasurementStore(Store):
    tables = {'meta', 'runs', 'calls'}
    schema = '''
    CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    INSERT INTO meta VALUES('schema_version','2');
    INSERT INTO meta VALUES('measurement_schema_version','1');
    CREATE TABLE runs(id TEXT PRIMARY KEY,principal TEXT NOT NULL,spec TEXT NOT NULL,
        outcome TEXT NOT NULL DEFAULT 'pending',validator TEXT,evidence_ref TEXT,
        coverage INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL,finished_at REAL);
    CREATE TABLE calls(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),
        task_id TEXT NOT NULL,kind TEXT NOT NULL,reference TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'in_flight',elapsed_ms INTEGER,
        input_tokens INTEGER,output_tokens INTEGER,cached_input_tokens INTEGER,
        cost_micro_cny INTEGER,receipt_ref TEXT UNIQUE,reply_bytes INTEGER,created_at REAL NOT NULL);
    CREATE INDEX run_calls ON calls(run_id);
    '''

    @contextmanager
    def connect(self, write=False):
        with super().connect(write=write) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
            state = db.execute("SELECT value FROM meta WHERE key='recovery_state'").fetchone() if exists else None
            if ((self.path.parent / 'restore-started.json').exists() and state is None
                    or state is not None and state[0] not in ('pending', 'released')):
                raise MarketError('recovery_required', '计量副本尚未建立恢复标记，不能启用。')
            yield db

    def execution_ready(self, db):
        state = db.execute("SELECT value FROM meta WHERE key='recovery_state'").fetchone()
        if state is None:
            return True
        if state[0] != 'released':
            return False
        epoch = db.execute("SELECT value FROM meta WHERE key='recovery_id'").fetchone()
        # Four-ledger recovery writes this sibling itself. A partially committed release
        # cannot enable model callbacks before the market gate also commits.
        market = self.path.parent / 'market.sqlite'
        if not market.is_file() or epoch is None:
            return False
        try:
            with closing(sqlite3.connect(market.as_uri() + '?mode=ro', uri=True, timeout=0.1)) as other:
                meta = dict(other.execute("SELECT key,value FROM meta WHERE key IN ('recovery_state','recovery_id','measurement_required')"))
            return meta == {'recovery_state': 'released', 'recovery_id': epoch[0], 'measurement_required': '1'}
        except sqlite3.Error:
            return False


KINDS = ('search', 'resource', 'model', 'dependency', 'feedback', 'other')
NUMERIC = ('input_tokens', 'output_tokens', 'cached_input_tokens', 'cost_micro_cny')
RUN_MARKER = 'measurement_recovery_run:'
AUDIT_MARKER = 'measurement_recovery_audit:'


def _data_hash(run, calls):
    return hashlib.sha256(compact({'run': dict(run), 'calls': [dict(r) for r in calls]}).encode('utf-8')).hexdigest()


def _timestamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise MarketError('invalid_params', '计量时间需要非负有限数值。')


def restore_measurement(db, recovery_id, snapshot_id):
    """Called only for a NEW restored copy, with the source snapshot already verified."""
    db.execute("INSERT OR REPLACE INTO meta VALUES('recovery_id',?)", (recovery_id,))
    db.execute("INSERT OR REPLACE INTO meta VALUES('recovery_state','pending')")
    for run in db.execute('SELECT id FROM runs').fetchall():
        marker = {'recovery_id': recovery_id, 'snapshot_id': snapshot_id, 'reviewed_hash': None}
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (RUN_MARKER + run['id'], compact(marker)))


def validate_measurement(db, meta):
    """Read-only semantic validation for snapshots; no model callbacks or inferred values."""
    runs = {r['id']: r for r in db.execute('SELECT * FROM runs')}
    for run in runs.values():
        for key in ('id', 'principal'):
            identifier(run[key], key)
        spec = parse_json(run['spec'])
        fields(spec, ('experiment', 'case', 'arm', 'configuration_hash', 'evidence_mode'))
        for key in ('experiment', 'case'):
            identifier(spec[key], key)
        choice(spec['arm'], ('direct', 'local', 'market'), 'arm')
        choice(spec['evidence_mode'], ('real', 'synthetic'), 'evidence_mode')
        value = spec['configuration_hash']
        if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
            raise MarketError('invalid_database', '计量配置摘要无效。')
        choice(run['outcome'], ('pending', 'pass', 'fail', 'unknown'), 'outcome')
        integer(run['coverage'], 0, 1, 'coverage')
        _timestamp(run['created_at'])
        if run['outcome'] == 'pending':
            if run['coverage'] != 0 or any(run[k] is not None for k in ('validator', 'evidence_ref', 'finished_at')):
                raise MarketError('invalid_database', '未结束任务含无效最终验收。')
        else:
            identifier(run['validator'], 'validator')
            identifier(run['evidence_ref'], 'evidence_ref')
            _timestamp(run['finished_at'])
    for row in db.execute('SELECT * FROM calls'):
        for key in ('id', 'run_id', 'task_id', 'reference'):
            identifier(row[key], key)
        if row['run_id'] not in runs:
            raise MarketError('invalid_database', '调用未关联有效任务。')
        choice(row['kind'], KINDS, 'kind')
        choice(row['state'], ('in_flight', 'succeeded', 'failed', 'unknown'), 'state')
        _timestamp(row['created_at'])
        for key in NUMERIC + ('elapsed_ms', 'reply_bytes'):
            if row[key] is not None:
                integer(row[key], 0, 10**12 if key in ('elapsed_ms', 'reply_bytes') else 10**15, key)
        if (row['state'] == 'in_flight' and (row['elapsed_ms'] is not None or row['reply_bytes'] is not None)
                or row['state'] != 'in_flight' and row['elapsed_ms'] is None):
            raise MarketError('invalid_database', '计量调用状态和耗时不匹配。')
        if row['kind'] != 'model' and any(row[k] not in (None, 0) for k in NUMERIC[:3]):
            raise MarketError('invalid_database', '非模型调用不能包含模型token。')
        if row['cached_input_tokens'] is not None and (row['input_tokens'] is None or row['cached_input_tokens'] > row['input_tokens']):
            raise MarketError('invalid_database', '缓存输入超过总输入或总输入未知。')
        if row['receipt_ref'] is not None:
            identifier(row['receipt_ref'], 'receipt_ref')
        elif any(row[k] is not None for k in NUMERIC):
            raise MarketError('invalid_database', '计量数值缺少回执引用。')
    for key, raw in meta.items():
        if key.startswith(RUN_MARKER):
            run_id = key[len(RUN_MARKER):]
            marker = parse_json(raw)
            fields(marker, ('recovery_id', 'snapshot_id', 'reviewed_hash'))
            identifier(marker['recovery_id'], 'recovery_id')
            if run_id not in runs or marker['recovery_id'] != meta.get('recovery_id'):
                raise MarketError('invalid_database', '恢复任务标记与批次不匹配。')
            identifier(marker['snapshot_id'], 'snapshot_id')
            digest = marker['reviewed_hash']
            if digest is not None and (not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)):
                raise MarketError('invalid_database', '恢复复核摘要无效。')
        elif key.startswith(AUDIT_MARKER):
            identifier(key[len(AUDIT_MARKER):], 'audit_id')
            audit = parse_json(raw)
            fields(audit, ('run_id', 'recovery_id', 'action', 'evidence', 'details', 'created_at'))
            if audit['run_id'] not in runs:
                raise MarketError('invalid_database', '恢复审计缺少任务。')
            identifier(audit['recovery_id'], 'recovery_id')
            choice(audit['action'], ('call_imported', 'call_resolved', 'run_reviewed', 'review_withdrawn'), 'action')
            identifier(audit['evidence'], 'evidence')
            if not isinstance(audit['details'], dict):
                raise MarketError('invalid_database', '恢复审计详情无效。')
            _timestamp(audit['created_at'])


def recovery_metadata_keys(meta):
    return {key for key in meta if key.startswith((RUN_MARKER, AUDIT_MARKER))}


class TaskMeter:
    """One run is one complete root task, including children, retries and failures.

    Only trusted host adapters may call this API. Evidence references are assertions
    made by that host, not authenticated supplier receipts or independent reviews.
    """
    def __init__(self, database, run_id, principal, *, experiment, case, arm,
                 configuration_hash, evidence_mode):
        for key, value in [('run_id', run_id), ('principal', principal), ('experiment', experiment), ('case', case)]:
            identifier(value, key)
        choice(arm, ('direct', 'local', 'market'), 'arm')
        choice(evidence_mode, ('real', 'synthetic'), 'evidence_mode')
        if (not isinstance(configuration_hash, str) or len(configuration_hash) != 64
                or any(c not in '0123456789abcdef' for c in configuration_hash)):
            raise MarketError('invalid_params', 'configuration_hash需要实际冻结配置的SHA-256。')
        self.store, self.run_id, self.principal = MeasurementStore(database), run_id, principal
        spec = compact({'experiment': experiment, 'case': case, 'arm': arm,
                        'configuration_hash': configuration_hash, 'evidence_mode': evidence_mode})
        with self.store.connect(write=True) as db:
            meta = db.execute("SELECT value FROM meta WHERE key='measurement_schema_version'").fetchone()
            if not meta or meta[0] != '1':
                raise MarketError('wrong_database', '不支持的计量账本。')
            old = db.execute('SELECT principal,spec FROM runs WHERE id=?', (run_id,)).fetchone()
            if old and (old['principal'], old['spec']) != (principal, spec):
                raise MarketError('measurement_conflict', '任务编号已绑定其他主体或实验配置。')
            if not old:
                if not self.store.execution_ready(db):
                    raise MarketError('recovery_required', '计量恢复未放行；不能开始新任务。')
                db.execute('INSERT INTO runs(id,principal,spec,created_at) VALUES(?,?,?,?)',
                           (run_id, principal, spec, time.time()))

    def _run(self, db):
        return db.execute('SELECT * FROM runs WHERE id=? AND principal=?', (self.run_id, self.principal)).fetchone()

    @classmethod
    def existing(cls, database, run_id, principal):
        """Open an existing scoped run without asking operators to retype its frozen spec."""
        from .recovery import connect_existing, validate_database
        identifier(run_id, 'run_id')
        identifier(principal, 'principal')
        with closing(connect_existing(database)) as db:
            validate_database(db, 'measurement', deep=False)
            row = db.execute('SELECT spec FROM runs WHERE id=? AND principal=?', (run_id, principal)).fetchone()
            if row is None:
                raise MarketError('not_found', '找不到此主体的计量任务。')
            spec = parse_json(row[0])
            fields(spec, ('experiment', 'case', 'arm', 'configuration_hash', 'evidence_mode'))
        return cls(database, run_id, principal, **spec)

    def begin(self, kind, reference, task_id):
        choice(kind, KINDS, 'kind')
        identifier(reference, 'reference')
        identifier(task_id, 'task_id')
        call_id = 'mc_' + uuid.uuid4().hex
        with self.store.connect(write=True) as db:
            if not self.store.execution_ready(db):
                raise MarketError('recovery_required', '计量恢复未放行；不能执行新调用。')
            if self._recovery(db) is not None:
                raise MarketError('measurement_recovery_frozen', '恢复的旧任务不自动继续执行；核对旧调用，新工作另开任务。')
            if self._run(db)['outcome'] != 'pending':
                raise MarketError('measurement_closed', '任务已结束，不能新增调用；晚到回执可补到原调用。')
            db.execute('INSERT INTO calls(id,run_id,task_id,kind,reference,created_at) VALUES(?,?,?,?,?,?)',
                       (call_id, self.run_id, task_id, kind, reference, time.time()))
        return call_id

    def _call(self, db, call_id):
        identifier(call_id, 'call_id')
        row = db.execute('SELECT * FROM calls WHERE id=? AND run_id=?', (call_id, self.run_id)).fetchone()
        if not row:
            raise MarketError('not_found', '找不到本任务的计量调用。')
        return row

    def end(self, call_id, state, elapsed_ms, reply_bytes=None):
        choice(state, ('succeeded', 'failed', 'unknown'), 'state')
        integer(elapsed_ms, 0, 10**12, 'elapsed_ms')
        if reply_bytes is not None:
            integer(reply_bytes, 0, 10**12, 'reply_bytes')
        with self.store.connect(write=True) as db:
            row = self._call(db, call_id)
            values = (state, elapsed_ms, reply_bytes)
            if row['state'] != 'in_flight':
                if (row['state'], row['elapsed_ms'], row['reply_bytes']) != values:
                    raise MarketError('measurement_conflict', '已记录调用结果不能被覆盖。')
                return
            db.execute('UPDATE calls SET state=?,elapsed_ms=?,reply_bytes=? WHERE id=?', (*values, call_id))

    def receipt(self, call_id, receipt_ref, *, input_tokens=None, output_tokens=None,
                cached_input_tokens=None, cost_micro_cny=None):
        identifier(receipt_ref, 'receipt_ref')
        values = (input_tokens, output_tokens, cached_input_tokens, cost_micro_cny)
        for key, value in zip(NUMERIC, values):
            if value is not None:
                integer(value, 0, 10**15, key)
        with self.store.connect(write=True) as db:
            row = self._call(db, call_id)
            if row['kind'] != 'model' and any(value not in (None, 0) for value in values[:3]):
                raise MarketError('invalid_params', '模型调用必须标为model，避免其token在汇总中遗漏。')
            if row['receipt_ref'] is not None:
                if row['receipt_ref'] != receipt_ref or any(
                        row[k] is not None and value is not None and row[k] != value
                        for k, value in zip(NUMERIC, values)):
                    raise MarketError('measurement_conflict', '回执不可覆盖；请核原始供应商记录。')
                values = tuple(row[k] if value is None else value for k, value in zip(NUMERIC, values))
            if values[2] is not None and (values[0] is None or values[2] > values[0]):
                raise MarketError('invalid_params', '缓存输入属于总输入，不能超过总输入或在总输入未知时填写。')
            if db.execute('SELECT 1 FROM calls WHERE receipt_ref=? AND id<>?', (receipt_ref, call_id)).fetchone():
                raise MarketError('duplicate_receipt', '同一账本的费用回执不能计入两次调用。')
            db.execute('UPDATE calls SET input_tokens=?,output_tokens=?,cached_input_tokens=?,cost_micro_cny=?,receipt_ref=? WHERE id=?',
                       (*values, receipt_ref, call_id))

    def call_records(self, offset=0, limit=100):
        """Trusted recovery/receipt linkage. Contains identifiers and metrics, never request bodies."""
        integer(offset, 0, 10**9, 'offset')
        integer(limit, 1, 100, 'limit')
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM calls WHERE run_id=? ORDER BY created_at,id LIMIT ? OFFSET ?',
                                               (self.run_id, limit, offset))]

    def observe(self, kind, reference, task_id, operation):
        """Record before running a trusted callback. Does not retry or prevent external duplicates."""
        call_id = self.begin(kind, reference, task_id)
        started = time.monotonic()
        try:
            result = operation()
        except BaseException:
            try:
                self.end(call_id, 'unknown', max(0, round((time.monotonic() - started) * 1000)))
            except Exception:
                # A failed write leaves in_flight for recovery; preserve the original operation error.
                pass
            raise
        elapsed = max(0, round((time.monotonic() - started) * 1000))
        # Count a deterministic serialization only when possible. No content is persisted.
        try:
            size = len(compact(result).encode('utf-8'))
        except (TypeError, ValueError, UnicodeError):
            size = None
        status = result.get('status') if isinstance(result, dict) else None
        state = 'failed' if status in ('error', 'unavailable', 'payment_unavailable') else 'succeeded'
        self.end(call_id, state, elapsed, size)
        return result, call_id

    def finish(self, outcome, validator, evidence_ref, *, coverage_complete=False):
        choice(outcome, ('pass', 'fail', 'unknown'), 'outcome')
        identifier(validator, 'validator')
        identifier(evidence_ref, 'evidence_ref')
        if type(coverage_complete) is not bool:
            raise MarketError('invalid_params', 'coverage_complete需要布尔值。')
        with self.store.connect(write=True) as db:
            row = self._run(db)
            values = (outcome, validator, evidence_ref, int(coverage_complete))
            if row['outcome'] != 'pending':
                if (row['outcome'], row['validator'], row['evidence_ref'], row['coverage']) != values:
                    raise MarketError('measurement_conflict', '最终验收记录不能被静默覆盖。')
                return
            if db.execute("SELECT 1 FROM calls WHERE run_id=? AND state='in_flight'", (self.run_id,)).fetchone():
                raise MarketError('measurement_in_flight', '仍有未结束调用；先核对原操作。')
            db.execute('UPDATE runs SET outcome=?,validator=?,evidence_ref=?,coverage=?,finished_at=? WHERE id=?',
                       (*values, time.time(), self.run_id))

    def _recovery(self, db, required=False):
        row = db.execute('SELECT value FROM meta WHERE key=?', (RUN_MARKER + self.run_id,)).fetchone()
        if required and row is None:
            raise MarketError('invalid_state', '此任务不是恢复导入任务。')
        return parse_json(row[0]) if row else None

    def _recovery_audit(self, db, action, evidence_ref, details):
        marker = self._recovery(db, required=True)
        audit = {'run_id': self.run_id, 'recovery_id': marker['recovery_id'], 'action': action,
                 'evidence': evidence_ref, 'details': details, 'created_at': time.time()}
        db.execute('INSERT INTO meta VALUES(?,?)', (AUDIT_MARKER + uuid.uuid4().hex, compact(audit)))

    def recover_call(self, call_id, kind, reference, task_id, created_at, evidence_ref):
        """Trusted operator imports a missing historical call; NEVER runs an operation."""
        for key, value in (('call_id', call_id), ('reference', reference), ('task_id', task_id), ('evidence_ref', evidence_ref)):
            identifier(value, key)
        choice(kind, KINDS, 'kind')
        _timestamp(created_at)
        with self.store.connect(write=True) as db:
            self._recovery(db, required=True)
            old = db.execute('SELECT * FROM calls WHERE id=?', (call_id,)).fetchone()
            if old:
                if (old['run_id'], old['kind'], old['reference'], old['task_id'], old['created_at']) != (
                        self.run_id, kind, reference, task_id, created_at):
                    raise MarketError('measurement_conflict', '原调用编号已绑定不同记录。')
                return
            db.execute('INSERT INTO calls(id,run_id,task_id,kind,reference,created_at) VALUES(?,?,?,?,?,?)',
                       (call_id, self.run_id, task_id, kind, reference, created_at))
            self._recovery_audit(db, 'call_imported', evidence_ref, {'call_id': call_id})

    def resolve_recovered_call(self, call_id, state, elapsed_ms, evidence_ref, reply_bytes=None):
        """Resolve recorded unknown status from external evidence, never by repeating a call."""
        choice(state, ('succeeded', 'failed'), 'state')
        identifier(evidence_ref, 'evidence_ref')
        integer(elapsed_ms, 0, 10**12, 'elapsed_ms')
        if reply_bytes is not None:
            integer(reply_bytes, 0, 10**12, 'reply_bytes')
        with self.store.connect(write=True) as db:
            self._recovery(db, required=True)
            row = self._call(db, call_id)
            values = (state, elapsed_ms, reply_bytes)
            if row['state'] in ('succeeded', 'failed'):
                if (row['state'], row['elapsed_ms'], row['reply_bytes']) != values:
                    raise MarketError('measurement_conflict', '已知调用结果不可覆盖。')
                return
            db.execute('UPDATE calls SET state=?,elapsed_ms=?,reply_bytes=? WHERE id=?', (*values, call_id))
            self._recovery_audit(db, 'call_resolved', evidence_ref,
                                 {'call_id': call_id, 'previous_state': row['state'], 'state': state})

    def review_recovery(self, coverage_ref, receipts_ref, outcome_ref, *, coverage_complete=False):
        """Attest the CURRENT rows after external reconciliation; later edits invalidate it."""
        for name, value in (('coverage_ref', coverage_ref), ('receipts_ref', receipts_ref), ('outcome_ref', outcome_ref)):
            identifier(value, name)
        if coverage_complete is not True:
            raise MarketError('measurement_incomplete', '必须明确核对整个任务覆盖完整，不能仅核对现有条目。')
        with self.store.connect(write=True) as db:
            marker = self._recovery(db, required=True)
            run = self._run(db)
            calls = db.execute('SELECT * FROM calls WHERE run_id=? ORDER BY created_at,id', (self.run_id,)).fetchall()
            if (run['outcome'] == 'pending' or any(r['state'] in ('unknown', 'in_flight')
                    or r['cost_micro_cny'] is None or r['kind'] == 'model' and
                    (r['input_tokens'] is None or r['output_tokens'] is None) for r in calls)):
                raise MarketError('measurement_incomplete', '任务覆盖、最终验收、调用状态或费用未齐全；保留不可比较。')
            digest = _data_hash(run, calls)
            if marker['reviewed_hash'] == digest:
                return {'status': 'reviewed', 'independent_verification': False}
            self._recovery_audit(db, 'run_reviewed', coverage_ref,
                                 {'receipts_ref': receipts_ref, 'outcome_ref': outcome_ref, 'data_hash': digest,
                                  'coverage_complete': True})
            marker['reviewed_hash'] = digest
            db.execute('UPDATE meta SET value=? WHERE key=?', (compact(marker), RUN_MARKER + self.run_id))
        return {'status': 'reviewed', 'independent_verification': False}

    def invalidate_recovery(self, evidence_ref):
        """Withdraw a prior attestation when new external evidence reveals a discrepancy."""
        identifier(evidence_ref, 'evidence_ref')
        with self.store.connect(write=True) as db:
            marker = self._recovery(db, required=True)
            if marker['reviewed_hash'] is not None:
                self._recovery_audit(db, 'review_withdrawn', evidence_ref, {'previous_hash': marker['reviewed_hash']})
                marker['reviewed_hash'] = None
                db.execute('UPDATE meta SET value=? WHERE key=?', (compact(marker), RUN_MARKER + self.run_id))
        return {'status': 'review_withdrawn'}

    def report(self):
        with self.store.connect() as db:
            run = self._run(db)
            calls = db.execute('SELECT * FROM calls WHERE run_id=? ORDER BY created_at,id', (self.run_id,)).fetchall()
            marker = self._recovery(db)
            ledger_ready = self.store.execution_ready(db)
        spec = parse_json(run['spec'])
        model = [r for r in calls if r['kind'] == 'model']
        recovery_review_required = marker is not None and (marker['reviewed_hash'] != _data_hash(run, calls) or not ledger_ready)
        complete = run['outcome'] != 'pending' and (not recovery_review_required if marker is not None else bool(run['coverage']))
        known_cost = sum(r['cost_micro_cny'] for r in calls if r['cost_micro_cny'] is not None)
        cost_complete = complete and all(r['cost_micro_cny'] is not None for r in calls)
        # Each direction has its own coverage: unknown output must not hide known input.
        input_complete = complete and all(r['input_tokens'] is not None for r in model)
        output_complete = complete and all(r['output_tokens'] is not None for r in model)
        return {'run_id': self.run_id, **spec, 'outcome': run['outcome'],
                'recovery_review_required': recovery_review_required,
                'recovery_id': marker['recovery_id'] if marker else None,
                'source_coverage_attested_by_host': bool(run['coverage']),
                'coverage_attested_by_host': complete, 'independent_quality_verified': False,
                'calls': len(calls), 'model_calls': len(model),
                'unknown_calls': sum(r['state'] in ('unknown', 'in_flight') for r in calls),
                'known_cost_micro_cny': known_cost, 'total_cost_micro_cny': known_cost if cost_complete else None,
                'input_tokens': sum(r['input_tokens'] for r in model) if input_complete else None,
                'output_tokens': sum(r['output_tokens'] for r in model) if output_complete else None,
                'known_call_elapsed_ms': sum(r['elapsed_ms'] for r in calls if r['elapsed_ms'] is not None),
                'call_elapsed_ms': sum(r['elapsed_ms'] for r in calls) if complete and all(r['elapsed_ms'] is not None for r in calls) else None,
                'task_elapsed_ms': None if run['finished_at'] is None else max(0, round((run['finished_at'] - run['created_at']) * 1000)),
                'resource_references': sorted({r['reference'] for r in calls if r['kind'] == 'resource'}),
                'savings_proven': False}


def compare_runs(baseline, candidate):
    """One paired diagnostic only; never infer statistical significance or broad value."""
    for key in ('experiment', 'case', 'configuration_hash', 'evidence_mode'):
        if baseline[key] != candidate[key]:
            raise MarketError('incomparable_runs', '任务、配置或证据类型不同，不能合并比较。')
    if baseline['arm'] == candidate['arm'] or 'market' != candidate['arm']:
        raise MarketError('incomparable_runs', '需要direct/local对照与market实验。')
    ready = all(r['outcome'] == 'pass' and r['coverage_attested_by_host'] and
                r['total_cost_micro_cny'] is not None and r['input_tokens'] is not None and
                r['output_tokens'] is not None and r['unknown_calls'] == 0 for r in (baseline, candidate))
    return {'status': 'paired_diagnostic' if ready else 'insufficient_evidence',
            'evidence_mode': baseline['evidence_mode'], 'savings_proven': False,
            'cost_delta_micro_cny': candidate['total_cost_micro_cny'] - baseline['total_cost_micro_cny'] if ready else None,
            'token_delta': candidate['input_tokens'] + candidate['output_tokens'] - baseline['input_tokens'] - baseline['output_tokens'] if ready else None,
            'notice': '单对任务诊断，不证明总体收益；正差值表示超市组使用更多。'}


def summarize_runs(reports):
    """Aggregate one cohort; failed tasks stay in the numerator, not the qualified count."""
    if not isinstance(reports, list) or not reports:
        raise MarketError('invalid_params', '需要至少一个任务报告。')
    if len({r['run_id'] for r in reports}) != len(reports):
        raise MarketError('measurement_conflict', '同一任务不能重复加入汇总。')
    first = reports[0]
    for report in reports:
        if any(report[key] != first[key] for key in ('experiment', 'arm', 'configuration_hash', 'evidence_mode')):
            raise MarketError('incomparable_runs', '只能汇总同一实验、组别、配置和证据类型。')
    complete = all(r['coverage_attested_by_host'] and r['total_cost_micro_cny'] is not None
                   and r['outcome'] in ('pass', 'fail', 'unknown') and r['unknown_calls'] == 0 for r in reports)
    qualified = sum(r['outcome'] == 'pass' for r in reports)
    total = sum(r['total_cost_micro_cny'] for r in reports) if complete else None
    return {'tasks': len(reports), 'qualified_tasks': qualified, 'evidence_mode': first['evidence_mode'],
            'total_cost_micro_cny': total,
            # Exact numerator/denominator avoids pretending rounded floating costs are accounting entries.
            'cost_per_qualified_task': {'numerator_micro_cny': total, 'denominator_tasks': qualified}
                                      if complete and qualified else None,
            'savings_proven': False, 'independent_quality_verified': False}
