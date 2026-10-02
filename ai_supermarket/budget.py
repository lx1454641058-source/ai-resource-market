"""来源：本项目原创。主子任务共享授权额度；不是支付账户、余额或渠道付款授权。"""

import argparse
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import sys
import time
import uuid

from .common import MarketError, choice, compact, error_result, fields, identifier, integer, parse_json
from .store import Store


KINDS = ('resource', 'model', 'dependency', 'feedback')
MAX_CENTS = 100_000_000
MICRO_PER_CENT = 10_000
MICRO_TAG = 'micro_cny:v1'
MICRO_ACTUAL = 'budget_micro_actual:'
POOL_KEY = 'budget_pool:v1'
POOL_AUDIT = 'budget_pool_audit:'
MAX_POOL_MICRO = 1_000_000_000_000_000


def validate_pool_config(value):
    fields(value, ('format', 'revision', 'state', 'limit_micro_cny', 'owners', 'authorization_ref'))
    integer(value['format'], 1, 1, 'format')
    integer(value['revision'], 1, MAX_CENTS, 'revision')
    choice(value['state'], ('active', 'paused', 'closed'), 'state')
    integer(value['limit_micro_cny'], 0, MAX_POOL_MICRO, 'limit_micro_cny')
    identifier(value['authorization_ref'], 'authorization_ref')
    if not isinstance(value['owners'], dict) or not 1 <= len(value['owners']) <= 256:
        raise MarketError('invalid_params', '总预算需要1～256个受信归属方额度。')
    for owner, amount in value['owners'].items():
        identifier(owner, 'owner')
        integer(amount, 0, MAX_POOL_MICRO, 'owner_limit_micro_cny')
    return value


def read_pool(db):
    version = db.execute("SELECT value FROM meta WHERE key='budget_schema_version'").fetchone()
    row = db.execute('SELECT value FROM meta WHERE key=?', (POOL_KEY,)).fetchone()
    if not version or version[0] not in ('1', '2') or (version[0] == '2') != bool(row):
        raise MarketError('invalid_database', '总预算配置与账本能力版本不一致。')
    if row is None:
        if db.execute('SELECT 1 FROM meta WHERE key GLOB ? LIMIT 1', (POOL_AUDIT + '*',)).fetchone():
            raise MarketError('invalid_database', '总预算审计缺少当前配置。')
        return None
    try:
        config = validate_pool_config(parse_json(row[0]))
    except MarketError:
        raise MarketError('invalid_database', '总预算配置损坏，不能忽略限制。') from None
    if any(row[0] not in config['owners'] for row in db.execute('SELECT DISTINCT owner FROM budgets')):
        raise MarketError('invalid_database', '总预算未覆盖既有预算归属方。')
    return config


def save_pool(db, config, action, evidence_ref):
    # A capability marker makes old BudgetLedger constructors reject this file.
    # Operators must stop old processes before opting in; existing old objects are not upgraded.
    db.execute("UPDATE meta SET value='2' WHERE key='budget_schema_version'")
    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (POOL_KEY, compact(config)))
    db.execute('INSERT INTO meta VALUES (?,?)', (POOL_AUDIT + str(config['revision']), compact({
        'configuration': config, 'action': action, 'evidence_ref': evidence_ref})))


def validate_pool_metadata(db, meta):
    config = read_pool(db)
    records = {key: raw for key, raw in meta.items() if key.startswith(POOL_AUDIT)}
    if config is None:
        return
    if len(records) != config['revision']:
        raise MarketError('invalid_database', '总预算审计修订不完整。')
    for revision in range(1, config['revision'] + 1):
        raw = records.get(POOL_AUDIT + str(revision))
        if raw is None:
            raise MarketError('invalid_database', '总预算审计修订缺失。')
        value = parse_json(raw)
        fields(value, ('configuration', 'action', 'evidence_ref'))
        state = validate_pool_config(value['configuration'])
        choice(value['action'], ('configured', 'overrun_paused', 'recovery_paused'), 'action')
        identifier(value['evidence_ref'], 'evidence_ref')
        if state['revision'] != revision or (revision == config['revision'] and state != config):
            raise MarketError('invalid_database', '总预算审计与当前配置不一致。')


def restore_pool(db, snapshot_id):
    config = read_pool(db)
    if config is not None:
        save_pool(db, {**config, 'revision': config['revision'] + 1,
                       'state': 'closed' if config['state'] == 'closed' else 'paused'},
                  'recovery_paused', snapshot_id)


def ceil_cents(amount):
    return (amount + MICRO_PER_CENT - 1) // MICRO_PER_CENT


def reservation_amounts(db, row):
    """Exact internal amounts; cent columns remain conservative compatibility displays."""
    spec = parse_json(row['spec'])
    if not isinstance(spec, list):
        raise MarketError('invalid_database', '预算金额契约无效。')
    micro = len(spec) == 4 and spec[0] == MICRO_TAG
    values = spec[1:] if micro else spec
    if len(values) != 3:
        raise MarketError('invalid_database', '预算金额契约无效。')
    kind, reference, maximum = values
    choice(kind, KINDS, 'kind')
    identifier(reference, 'reference')
    integer(maximum, 0, MAX_CENTS * (MICRO_PER_CENT if micro else 1), 'maximum')
    exact_max = maximum if micro else maximum * MICRO_PER_CENT
    if row['max_cents'] != ceil_cents(exact_max):
        raise MarketError('invalid_database', '预算上限与精确金额不一致。')
    if 'micro_actual' in row.keys():
        raw_actual = row['micro_actual']
    else:
        metadata = db.execute('SELECT value FROM meta WHERE key=?', (MICRO_ACTUAL + row['id'],)).fetchone()
        raw_actual = metadata[0] if metadata else None
    exact_actual = None
    if micro and row['state'] == 'settled':
        if raw_actual is None:
            raise MarketError('invalid_database', '微额费用缺少原始精度记录，不能按零或取整值继续。')
        exact_actual = parse_json(raw_actual)
        integer(exact_actual, 0, MAX_CENTS * MICRO_PER_CENT, 'actual_micro_cny')
        if row['actual_cents'] != ceil_cents(exact_actual):
            raise MarketError('invalid_database', '已结算金额与精确记录不一致。')
    elif raw_actual is not None:
        raise MarketError('invalid_database', '精确费用记录与预留状态不一致。')
    elif row['state'] == 'settled':
        integer(row['actual_cents'], 0, MAX_CENTS, 'actual_cents')
        exact_actual = row['actual_cents'] * MICRO_PER_CENT
    return micro, exact_max, exact_actual


def validate_budget_amounts(db):
    for row in db.execute('SELECT r.*,m.value AS micro_actual FROM reservations r LEFT JOIN meta m ON m.key=? || r.id', (MICRO_ACTUAL,)):
        reservation_amounts(db, row)
    for row in db.execute('SELECT key FROM meta WHERE key LIKE ?', (MICRO_ACTUAL + '%',)):
        if not db.execute('SELECT 1 FROM reservations WHERE id=?', (row[0][len(MICRO_ACTUAL):],)).fetchone():
            raise MarketError('invalid_database', '精确费用缺少关联预留。')
BUDGET_SCHEMA = '''
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta VALUES ('schema_version','2');
INSERT INTO meta VALUES ('budget_schema_version','1');
CREATE TABLE budgets (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, limit_cents INTEGER NOT NULL CHECK(limit_cents>=0),
 state TEXT NOT NULL CHECK(state IN ('active','paused','closed')), revision INTEGER NOT NULL,
 authorization_ref TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE budget_tasks (
 root_id TEXT NOT NULL REFERENCES budgets(id), id TEXT NOT NULL, principal TEXT NOT NULL,
 parent_id TEXT, state TEXT NOT NULL CHECK(state IN ('active','closed')), created_at REAL NOT NULL,
 PRIMARY KEY(root_id,id), FOREIGN KEY(root_id,parent_id) REFERENCES budget_tasks(root_id,id));
CREATE TABLE reservations (
 id TEXT PRIMARY KEY, root_id TEXT NOT NULL, task_id TEXT NOT NULL, principal TEXT NOT NULL,
 request_id TEXT NOT NULL, spec TEXT NOT NULL, max_cents INTEGER NOT NULL CHECK(max_cents>=0),
 state TEXT NOT NULL CHECK(state IN ('reserved','in_flight','unknown','settled','cancelled')),
 actual_cents INTEGER CHECK(actual_cents>=0), evidence_ref TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(root_id,task_id,request_id), FOREIGN KEY(root_id,task_id) REFERENCES budget_tasks(root_id,id),
 CHECK((state='settled' AND actual_cents IS NOT NULL AND evidence_ref IS NOT NULL)
    OR (state<>'settled' AND actual_cents IS NULL)));
CREATE INDEX budget_exposure ON reservations(root_id,state);
CREATE UNIQUE INDEX budget_receipt ON reservations(root_id,evidence_ref) WHERE state='settled';
CREATE TABLE budget_audit (
 id TEXT PRIMARY KEY, root_id TEXT NOT NULL REFERENCES budgets(id), task_id TEXT,
 reservation_id TEXT, action TEXT NOT NULL, detail TEXT NOT NULL, created_at REAL NOT NULL);
'''


class BudgetStore(Store):
    schema = BUDGET_SCHEMA
    tables = {'meta', 'budgets', 'budget_tasks', 'reservations', 'budget_audit'}

    @contextmanager
    def connect(self, write=False):
        with super().connect(write=write) as db:
            if (self.path.parent / 'restore-started.json').exists():
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
                epoch = db.execute("SELECT value FROM meta WHERE key='recovery_id'").fetchone() if exists else None
                if epoch is None:
                    raise MarketError('recovery_required', '预算副本尚未完成恢复标记，不能使用。')
            yield db

    def execution_ready(self, db):
        epoch = db.execute("SELECT value FROM meta WHERE key='recovery_id'").fetchone()
        if epoch is None:
            return True
        market = self.path.parent / 'market.sqlite'
        if not market.is_file():
            return False
        try:
            with closing(sqlite3.connect(market.as_uri() + '?mode=ro', uri=True, timeout=0.1)) as other:
                meta = dict(other.execute("SELECT key,value FROM meta WHERE key IN ('recovery_state','recovery_id')"))
            return meta == {'recovery_state': 'released', 'recovery_id': epoch[0]}
        except sqlite3.Error:
            return False


class BudgetLedger:
    """Trusted host/operator API. Never register these methods as model tools."""
    def __init__(self, path, clock=None):
        self.store = BudgetStore(path)
        self.clock = clock or time.time
        with self.store.connect() as db:
            db.execute('BEGIN')
            row = db.execute("SELECT value FROM meta WHERE key='budget_schema_version'").fetchone()
            if not row or row[0] not in ('1', '2'):
                raise MarketError('wrong_database', '预算账本版本不受支持。')
            read_pool(db)

    def _pool_totals(self, db, config):
        totals = {owner: {'spent_micro_cny': 0, 'held_micro_cny': 0} for owner in config['owners']}
        for row in db.execute('''SELECT r.*,b.owner,m.value AS micro_actual FROM reservations r
                JOIN budgets b ON b.id=r.root_id LEFT JOIN meta m ON m.key=? || r.id''', (MICRO_ACTUAL,)):
            _, maximum, actual = reservation_amounts(db, row)
            if row['owner'] not in totals:
                raise MarketError('invalid_database', '费用归属未纳入总预算。')
            if row['state'] == 'settled':
                totals[row['owner']]['spent_micro_cny'] += actual
            elif row['state'] in ('reserved', 'in_flight', 'unknown'):
                totals[row['owner']]['held_micro_cny'] += maximum
        return totals

    @staticmethod
    def _pool_amount_view(limit, spent, held, active):
        return {'limit_micro_cny': limit, 'spent_micro_cny': spent, 'held_micro_cny': held,
                'available_micro_cny': max(0, limit - spent - held) if active else 0,
                'over_budget_micro_cny': max(0, spent + held - limit)}

    def _pool_summary(self, db, config):
        totals = self._pool_totals(db, config)
        active = config['state'] == 'active'
        owners = {}
        for owner, amounts in totals.items():
            owners[owner] = self._pool_amount_view(config['owners'][owner], amounts['spent_micro_cny'],
                                                   amounts['held_micro_cny'], active)
        spent = sum(value['spent_micro_cny'] for value in totals.values())
        held = sum(value['held_micro_cny'] for value in totals.values())
        return {'status': 'configured', 'state': config['state'], 'revision': config['revision'],
                'currency': 'CNY', 'authorization_ref': config['authorization_ref'],
                'payment_authorized': False, 'scope': 'same_ledger_cumulative', 'owners': owners,
                'global': self._pool_amount_view(config['limit_micro_cny'], spent, held, active)}

    def pool_summary(self):
        """Trusted operator only; does not expose other owners through buyer tools."""
        with self.store.connect() as db:
            # One read snapshot keeps configuration and totals on the same revision.
            db.execute('BEGIN')
            config = read_pool(db)
            return self._pool_summary(db, config) if config else {'status': 'not_configured', 'payment_authorized': False}

    def configure_pool(self, limit_micro_cny, owners, state, authorization_ref, expected_revision):
        """Explicit opt-in/change. No disable/reset path; historical exposure always remains."""
        integer(expected_revision, 0, MAX_CENTS - 1, 'expected_revision')
        candidate = validate_pool_config({'format': 1, 'revision': expected_revision + 1, 'state': state,
                        'limit_micro_cny': limit_micro_cny, 'owners': owners, 'authorization_ref': authorization_ref})
        with self.store.connect(write=True) as db:
            previous = read_pool(db)
            if (previous['revision'] if previous else 0) != expected_revision:
                raise MarketError('revision_conflict', '总预算配置已改变，先查当前修订。')
            if previous and previous['state'] == 'closed':
                raise MarketError('budget_inactive', '已关闭总预算不能重开或重置。')
            if not self.store.execution_ready(db):
                raise MarketError('recovery_required', '恢复未放行，不能修改总预算配置。')
            if any(row[0] not in owners for row in db.execute('SELECT DISTINCT owner FROM budgets')):
                raise MarketError('budget_scope_mismatch', '新配置必须覆盖全部既有预算归属方。')
            summary = self._pool_summary(db, candidate)
            if state == 'active' and (summary['global']['over_budget_micro_cny'] or
                                      any(v['over_budget_micro_cny'] for v in summary['owners'].values())):
                raise MarketError('budget_exceeded', '已有费用及未知预留超过候选额度，不能启用。')
            save_pool(db, candidate, 'configured', authorization_ref)
            return summary

    def _pool_admit_owner(self, db, owner, extra=0):
        config = read_pool(db)
        if config is None:
            return
        if config['state'] != 'active':
            raise MarketError('budget_inactive', '账本总预算已暂停或关闭。')
        if owner not in config['owners']:
            raise MarketError('budget_scope_mismatch', '归属方尚未纳入获准总预算。')
        totals = self._pool_totals(db, config)
        own = totals[owner]
        global_exposure = sum(v['spent_micro_cny'] + v['held_micro_cny'] for v in totals.values())
        if (global_exposure + extra > config['limit_micro_cny'] or
                own['spent_micro_cny'] + own['held_micro_cny'] + extra > config['owners'][owner]):
            raise MarketError('budget_exceeded', '账本或归属方总额度不足，含其他根任务的费用和未知预留。')

    def _pool_after_settlement(self, db, evidence_ref):
        config = read_pool(db)
        if config and config['state'] == 'active':
            summary = self._pool_summary(db, config)
            if summary['global']['over_budget_micro_cny']:
                save_pool(db, {**config, 'revision': config['revision'] + 1, 'state': 'paused'},
                          'overrun_paused', evidence_ref)

    def _audit(self, db, root_id, action, detail, task_id=None, reservation_id=None):
        db.execute('INSERT INTO budget_audit VALUES (?,?,?,?,?,?,?)',
                   ('be_' + uuid.uuid4().hex, root_id, task_id, reservation_id, action, compact(detail), self.clock()))

    def _root(self, db, root_id):
        row = db.execute('SELECT * FROM budgets WHERE id=?', (root_id,)).fetchone()
        if not row:
            raise MarketError('not_found', '找不到授权预算。')
        return row

    def _summary(self, db, root_id):
        root = self._root(db, root_id)
        spent, held = 0, 0
        for row in db.execute('''SELECT r.*,m.value AS micro_actual FROM reservations r
                LEFT JOIN meta m ON m.key=? || r.id WHERE root_id=?''', (MICRO_ACTUAL, root_id)):
            _, maximum, actual = reservation_amounts(db, row)
            if row['state'] == 'settled':
                spent += actual
            elif row['state'] in ('reserved', 'in_flight', 'unknown'):
                held += maximum
        limit = root['limit_cents'] * MICRO_PER_CENT
        available = limit - spent - held
        return {'root_id': root_id, 'state': root['state'], 'revision': root['revision'], 'currency': 'CNY',
                'limit_cents': root['limit_cents'], 'spent_cents': ceil_cents(spent), 'held_cents': ceil_cents(held),
                'available_cents': max(0, available) // MICRO_PER_CENT if root['state'] == 'active' else 0,
                'over_budget_cents': ceil_cents(max(0, -available)), 'payment_authorized': False,
                'limit_micro_cny': limit, 'spent_micro_cny': spent, 'held_micro_cny': held,
                'available_micro_cny': max(0, available) if root['state'] == 'active' else 0,
                'over_budget_micro_cny': max(0, -available)}

    def create(self, root_id, owner, limit_cents, authorization_ref):
        for name, value in [('root_id', root_id), ('owner', owner), ('authorization_ref', authorization_ref)]:
            identifier(value, name)
        integer(limit_cents, 0, MAX_CENTS, 'limit_cents')
        with self.store.connect(write=True) as db:
            old = db.execute('SELECT * FROM budgets WHERE id=?', (root_id,)).fetchone()
            if old:
                if (old['owner'], old['limit_cents'], old['authorization_ref']) != (owner, limit_cents, authorization_ref):
                    raise MarketError('budget_conflict', '预算已存在；不能通过重复创建更改授权。')
                return self._summary(db, root_id)
            if not self.store.execution_ready(db):
                raise MarketError('recovery_required', '恢复未放行，不能建立新授权预算。')
            self._pool_admit_owner(db, owner)
            db.execute('INSERT INTO budgets VALUES (?,?,?,?,?,?,?)',
                       (root_id, owner, limit_cents, 'active', 1, authorization_ref, self.clock()))
            self._audit(db, root_id, 'created', {'owner': owner, 'limit_cents': limit_cents, 'authorization_ref': authorization_ref})
            return self._summary(db, root_id)

    def grant_task(self, root_id, task_id, principal, parent_id=None):
        for name, value in [('root_id', root_id), ('task_id', task_id), ('principal', principal)]:
            identifier(value, name)
        if parent_id is not None:
            identifier(parent_id, 'parent_id')
        with self.store.connect(write=True) as db:
            self._root(db, root_id)
            old = db.execute('SELECT * FROM budget_tasks WHERE root_id=? AND id=?', (root_id, task_id)).fetchone()
            if old:
                if (old['principal'], old['parent_id']) != (principal, parent_id):
                    raise MarketError('budget_conflict', '任务归属和父任务不能通过重复授权改变。')
                return {'root_id': root_id, 'task_id': task_id, 'state': old['state']}
            if self._root(db, root_id)['state'] != 'active':
                raise MarketError('budget_inactive', '此预算不接受新任务。')
            if not self.store.execution_ready(db):
                raise MarketError('recovery_required', '恢复未放行，不能授权新任务。')
            self._pool_admit_owner(db, self._root(db, root_id)['owner'])
            if parent_id is not None:
                self._task(db, root_id, parent_id, active=True, max_depth=63)
            db.execute('INSERT INTO budget_tasks VALUES (?,?,?,?,?,?)',
                       (root_id, task_id, principal, parent_id, 'active', self.clock()))
            self._audit(db, root_id, 'task_granted', {'principal': principal, 'parent_id': parent_id}, task_id)
            return {'root_id': root_id, 'task_id': task_id, 'state': 'active'}

    def _task(self, db, root_id, task_id, principal=None, active=False, max_depth=64):
        task = db.execute('SELECT * FROM budget_tasks WHERE root_id=? AND id=?', (root_id, task_id)).fetchone()
        if not task or (principal is not None and task['principal'] != principal):
            raise MarketError('not_found', '该主体没有此预算任务。')
        if active:
            if self._root(db, root_id)['state'] != 'active':
                raise MarketError('budget_inactive', '预算已暂停或关闭。')
            if not self.store.execution_ready(db):
                raise MarketError('recovery_required', '恢复未放行，不能开始新的支出。')
            self._pool_admit_owner(db, self._root(db, root_id)['owner'])
            current, seen = task, set()
            while current:
                if current['id'] in seen or len(seen) >= max_depth:
                    raise MarketError('budget_conflict', '任务层级无效或过深。')
                seen.add(current['id'])
                if current['state'] != 'active':
                    raise MarketError('budget_inactive', '任务或父任务已关闭。')
                current = (db.execute('SELECT * FROM budget_tasks WHERE root_id=? AND id=?',
                           (root_id, current['parent_id'])).fetchone() if current['parent_id'] else None)
        return task

    def set_state(self, root_id, state, expected_revision):
        choice(state, ('active', 'paused', 'closed'), 'state')
        integer(expected_revision, 1, MAX_CENTS, 'expected_revision')
        with self.store.connect(write=True) as db:
            root = self._root(db, root_id)
            if root['revision'] != expected_revision:
                raise MarketError('revision_conflict', '预算状态已变化。')
            if root['state'] == 'closed' and state != 'closed':
                raise MarketError('budget_inactive', '已关闭预算不可重新启用。')
            if state == 'active' and self._summary(db, root_id)['over_budget_cents']:
                raise MarketError('budget_exceeded', '已有超额支出，不能重新开放消费。')
            if state == 'active' and not self.store.execution_ready(db):
                raise MarketError('recovery_required', '恢复未放行，不能重新开放预算。')
            if state == 'active':
                self._pool_admit_owner(db, root['owner'])
            if state != root['state']:
                db.execute('UPDATE budgets SET state=?,revision=revision+1 WHERE id=?', (state, root_id))
                self._audit(db, root_id, 'state_changed', {'state': state})
            return self._summary(db, root_id)

    def close_task(self, root_id, task_id):
        with self.store.connect(write=True) as db:
            task = self._task(db, root_id, task_id)
            if task['state'] != 'closed':
                db.execute("UPDATE budget_tasks SET state='closed' WHERE root_id=? AND id=?", (root_id, task_id))
                self._audit(db, root_id, 'task_closed', {}, task_id)
            return {'root_id': root_id, 'task_id': task_id, 'state': 'closed'}

    def scope(self, root_id, task_id, principal):
        for name, value in [('root_id', root_id), ('task_id', task_id), ('principal', principal)]:
            identifier(value, name)
        with self.store.connect() as db:
            self._task(db, root_id, task_id, principal)
        return TaskBudget(self, root_id, task_id, principal)


class TaskBudget:
    """A trusted host fixes this binding; child tasks share root exposure, never copy its limit."""
    def __init__(self, ledger, root_id, task_id, principal):
        self.ledger, self.root_id, self.task_id, self.principal = ledger, root_id, task_id, principal

    def _bound(self, db, active=False):
        return self.ledger._task(db, self.root_id, self.task_id, self.principal, active=active)

    def _get(self, db, reservation_id):
        identifier(reservation_id, 'reservation_id')
        self._bound(db)
        row = db.execute('SELECT * FROM reservations WHERE id=? AND root_id=? AND task_id=? AND principal=?',
                         (reservation_id, self.root_id, self.task_id, self.principal)).fetchone()
        if not row:
            raise MarketError('not_found', '找不到此任务的预留记录。')
        return row

    @staticmethod
    def _view(db, row):
        micro, maximum, actual = reservation_amounts(db, row)
        return {'reservation_id': row['id'], 'state': row['state'], 'max_cents': row['max_cents'],
                'actual_cents': row['actual_cents'], 'evidence_ref': row['evidence_ref'],
                'payment_authorized': False, 'amount_unit': 'micro_cny' if micro else 'cents',
                'max_micro_cny': maximum, 'actual_micro_cny': actual}

    def summary(self):
        with self.ledger.store.connect() as db:
            self._bound(db)
            return self.ledger._summary(db, self.root_id)

    def status(self, reservation_id):
        with self.ledger.store.connect() as db:
            return self._view(db, self._get(db, reservation_id))

    def reserve(self, request_id, kind, reference, max_cents):
        return self._reserve(request_id, kind, reference, max_cents, micro=False)

    def reserve_micro(self, request_id, kind, reference, max_micro_cny):
        return self._reserve(request_id, kind, reference, max_micro_cny, micro=True)

    def _reserve(self, request_id, kind, reference, maximum, *, micro):
        identifier(request_id, 'request_id')
        identifier(reference, 'reference')
        choice(kind, KINDS, 'kind')
        integer(maximum, 0, MAX_CENTS * (MICRO_PER_CENT if micro else 1), 'maximum')
        exact = maximum if micro else maximum * MICRO_PER_CENT
        max_cents = ceil_cents(exact)
        spec = compact(([MICRO_TAG] if micro else []) + [kind, reference, maximum])
        with self.ledger.store.connect(write=True) as db:
            self._bound(db)
            old = db.execute('SELECT * FROM reservations WHERE root_id=? AND task_id=? AND request_id=?',
                             (self.root_id, self.task_id, request_id)).fetchone()
            if old:
                if old['spec'] != spec or old['principal'] != self.principal:
                    raise MarketError('idempotency_conflict', '同一预留请求不能更换对象或费用。')
                return self._view(db, old)
            self._bound(db, active=True)
            summary = self.ledger._summary(db, self.root_id)
            if exact > summary['available_micro_cny']:
                raise MarketError('budget_exceeded', '主任务剩余额度不足，包含其他子任务已花费及预留。')
            self.ledger._pool_admit_owner(db, self.ledger._root(db, self.root_id)['owner'], extra=exact)
            reservation_id = 'br_' + uuid.uuid4().hex
            now = self.ledger.clock()
            db.execute('INSERT INTO reservations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (reservation_id, self.root_id, self.task_id, self.principal, request_id, spec, max_cents,
                        'reserved', None, None, now, now))
            self.ledger._audit(db, self.root_id, 'reserved', {'kind': kind, 'reference': reference, 'max_cents': max_cents,
                               'amount_unit': 'micro_cny' if micro else 'cents', 'max_micro_cny': exact},
                               self.task_id, reservation_id)
            return self._view(db, self._get(db, reservation_id))

    def begin(self, reservation_id):
        with self.ledger.store.connect(write=True) as db:
            row = self._get(db, reservation_id)
            if row['state'] != 'reserved':
                return {**self._view(db, row), 'execute': False}
            self._bound(db, active=True)
            db.execute("UPDATE reservations SET state='in_flight',updated_at=? WHERE id=?", (self.ledger.clock(), reservation_id))
            self.ledger._audit(db, self.root_id, 'started', {}, self.task_id, reservation_id)
            return {**self._view(db, self._get(db, reservation_id)), 'execute': True}

    def mark_unknown(self, reservation_id):
        with self.ledger.store.connect(write=True) as db:
            row = self._get(db, reservation_id)
            if row['state'] == 'in_flight':
                db.execute("UPDATE reservations SET state='unknown',updated_at=? WHERE id=?", (self.ledger.clock(), reservation_id))
                self.ledger._audit(db, self.root_id, 'unknown', {}, self.task_id, reservation_id)
            return self._view(db, self._get(db, reservation_id))

    def cancel(self, reservation_id):
        return self._cancel(reservation_id, None)

    def confirm_no_charge(self, reservation_id, evidence_ref):
        identifier(evidence_ref, 'evidence_ref')
        return self._cancel(reservation_id, evidence_ref)

    def _cancel(self, reservation_id, evidence_ref):
        with self.ledger.store.connect(write=True) as db:
            row = self._get(db, reservation_id)
            if row['state'] == 'cancelled' and row['evidence_ref'] == evidence_ref:
                return self._view(db, row)
            eligible = ('reserved',) if evidence_ref is None else ('in_flight', 'unknown')
            if row['state'] not in eligible:
                raise MarketError('invalid_state', '仅未开始的预留可直接取消；未知结果须有无扣费核验依据。')
            db.execute("UPDATE reservations SET state='cancelled',evidence_ref=?,updated_at=? WHERE id=?",
                       (evidence_ref, self.ledger.clock(), reservation_id))
            self.ledger._audit(db, self.root_id, 'cancelled', {'evidence_ref': evidence_ref}, self.task_id, reservation_id)
            return self._view(db, self._get(db, reservation_id))

    def settle(self, reservation_id, actual_cents, evidence_ref):
        return self._settle(reservation_id, actual_cents, evidence_ref, micro=False)

    def settle_micro(self, reservation_id, actual_micro_cny, evidence_ref):
        return self._settle(reservation_id, actual_micro_cny, evidence_ref, micro=True)

    def _settle(self, reservation_id, amount, evidence_ref, *, micro):
        integer(amount, 0, MAX_CENTS * (MICRO_PER_CENT if micro else 1), 'actual_amount')
        exact = amount if micro else amount * MICRO_PER_CENT
        actual_cents = ceil_cents(exact)
        identifier(evidence_ref, 'evidence_ref')
        with self.ledger.store.connect(write=True) as db:
            row = self._get(db, reservation_id)
            stored_micro, maximum, old_actual = reservation_amounts(db, row)
            if stored_micro != micro:
                raise MarketError('amount_unit_mismatch', '必须使用与预留相同的金额单位核销。')
            if row['state'] == 'settled':
                if (old_actual, row['evidence_ref']) != (exact, evidence_ref):
                    raise MarketError('idempotency_conflict', '不能覆盖已记录费用或其依据。')
                return self._view(db, row)
            if row['state'] not in ('in_flight', 'unknown'):
                raise MarketError('invalid_state', '只有已开始或未知的操作可以核销。')
            if db.execute("SELECT 1 FROM reservations WHERE root_id=? AND evidence_ref=? AND state='settled'",
                          (self.root_id, evidence_ref)).fetchone():
                raise MarketError('evidence_conflict', '同一费用凭据不能核销多个预留。')
            db.execute("UPDATE reservations SET state='settled',actual_cents=?,evidence_ref=?,updated_at=? WHERE id=?",
                       (actual_cents, evidence_ref, self.ledger.clock(), reservation_id))
            if micro:
                db.execute('INSERT INTO meta VALUES(?,?)', (MICRO_ACTUAL + reservation_id, compact(exact)))
            overrun = exact > maximum
            if overrun:
                # Do not hide real provider overcharges to keep a green budget invariant.
                db.execute("""UPDATE budgets SET state=CASE WHEN state='closed' THEN 'closed' ELSE 'paused' END,
                           revision=revision+1 WHERE id=?""", (self.root_id,))
            self.ledger._pool_after_settlement(db, evidence_ref)
            self.ledger._audit(db, self.root_id, 'settled', {'actual_cents': actual_cents,
                'actual_micro_cny': exact, 'amount_unit': 'micro_cny' if micro else 'cents',
                'evidence_ref': evidence_ref, 'reservation_overrun': overrun}, self.task_id, reservation_id)
            return self._view(db, self._get(db, reservation_id))

    def run(self, request_id, kind, reference, max_cents, operation, receipt):
        """Host-only callbacks: operation receives stable reservation ID; receipt returns (cents, evidence ID)."""
        reserved = self.reserve(request_id, kind, reference, max_cents)
        return self.run_reserved(reserved['reservation_id'], operation, receipt)

    def run_reserved(self, reservation_id, operation, receipt):
        """Execute a trusted host operation at most once; never execute scripts from product content."""
        return self._run_reserved(reservation_id, operation, receipt, micro=False)

    def run_micro(self, request_id, kind, reference, max_micro_cny, operation, receipt):
        reserved = self.reserve_micro(request_id, kind, reference, max_micro_cny)
        return self.run_reserved_micro(reserved['reservation_id'], operation, receipt)

    def run_reserved_micro(self, reservation_id, operation, receipt):
        return self._run_reserved(reservation_id, operation, receipt, micro=True)

    def _run_reserved(self, reservation_id, operation, receipt, *, micro):
        # Detect wrong units BEFORE claiming/executing. Settlement must never guess units.
        if self.status(reservation_id)['amount_unit'] != ('micro_cny' if micro else 'cents'):
            raise MarketError('amount_unit_mismatch', '调用包装与预留的金额单位不一致。')
        claim = self.begin(reservation_id)
        if not claim['execute']:
            return {'status': 'not_executed', 'budget': claim}
        try:
            result = operation(reservation_id)
            amount, evidence = receipt(result)
            settled = self._settle(reservation_id, amount, evidence, micro=micro)
        except BaseException:
            try:
                self.mark_unknown(reservation_id)
            except Exception:
                # A storage outage must not replace the original provider failure.
                # The durable in_flight record still holds the full reservation.
                pass
            raise
        return {'status': 'completed', 'budget': settled, 'result': result}


def main(argv=None):
    parser = argparse.ArgumentParser(description='受信运营者管理同账本累计总预算；不批准付款')
    parser.add_argument('--database', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('pool-inspect')
    configure = commands.add_parser('pool-configure')
    configure.add_argument('--input', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        # Operator typos must not create a new empty ledger that hides past spending.
        if not args.database.is_file() or args.database.stat().st_size == 0:
            raise MarketError('not_found', '需指定已有非空预算账本，不会自动新建。')
        values = None
        if args.command == 'pool-configure':
            with args.input.open('rb') as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise MarketError('invalid_params', '总预算配置文件超过64KiB。')
            values = parse_json(raw)
            fields(values, ('limit_micro_cny', 'owners', 'state', 'authorization_ref', 'expected_revision'))
        ledger = BudgetLedger(args.database)
        result = ledger.configure_pool(**values) if values is not None else ledger.pool_summary()
    except MarketError as exc:
        result = error_result(exc)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        result = {'status': 'error', 'error_code': 'unavailable',
                  'message': '总预算管理未完成，核对账本与原请求状态，勿重置历史费用。'}
    sys.stdout.buffer.write(compact(result).encode('utf-8') + b'\n')
    return int(result.get('status') == 'error')


if __name__ == '__main__':
    raise SystemExit(main())
