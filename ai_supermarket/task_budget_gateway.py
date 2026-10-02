"""来源：本项目原创。远程静态资料交付的凭据任务绑定和共享预算门；不执行依赖。"""

import hashlib
from pathlib import Path
import re
import sqlite3

from .budget import BudgetLedger, TaskBudget
from .common import MarketError, compact, error_result, fields, identifier, parse_json
from .exchange import Exchange
from .exchange_tools import ExchangeInterface, validate
from .gateway import AuthenticatedGateway


BINDING_PREFIX = 'budget_remote_key:'


def validate_bindings(db, meta):
    for key, raw in meta.items():
        if not key.startswith(BINDING_PREFIX):
            continue
        if not re.fullmatch(r'ak_[0-9a-f]{32}', key[len(BINDING_PREFIX):]):
            raise MarketError('invalid_database', '任务绑定凭据编号无效。')
        value = parse_json(raw)
        fields(value, ('root_id', 'task_id', 'principal', 'identity_binding', 'authorization_ref', 'state'))
        for name in ('root_id', 'task_id', 'principal', 'authorization_ref'):
            identifier(value[name], name)
        if (value['state'] not in ('active', 'revoked')
                or not isinstance(value['identity_binding'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', value['identity_binding'])):
            raise MarketError('invalid_database', '任务绑定状态或身份位置摘要无效。')
        row = db.execute('SELECT principal FROM budget_tasks WHERE root_id=? AND id=?',
                         (value['root_id'], value['task_id'])).fetchone()
        if not row or row['principal'] != value['principal']:
            raise MarketError('invalid_database', '任务绑定与预算授权不一致。')


class TaskBudgetGateway(AuthenticatedGateway):
    """One credential is bound to one authorized task; binding is trusted local administration."""
    def __init__(self, market_database, identity_database, budget_database, clock=None):
        # Do not initialize missing/empty operator dependencies because enforcement was enabled.
        from .recovery import connect_existing, validate_database
        from contextlib import closing
        for path, kind in ((identity_database, 'identities'), (budget_database, 'budget')):
            with closing(connect_existing(path)) as db:
                validate_database(db, kind, deep=False)
        super().__init__(market_database, identity_database, clock=clock)
        self.budgets = BudgetLedger(budget_database, clock=clock)
        self.identity_binding = hashlib.sha256(str(Path(identity_database).resolve()).casefold().encode()).hexdigest()

    def bind_key(self, key_id, root_id, task_id, principal, authorization_ref):
        """Local operator only. Never takes a secret token and never issues credentials."""
        if not isinstance(key_id, str) or not re.fullmatch(r'ak_[0-9a-f]{32}', key_id):
            raise MarketError('invalid_params', '需要凭据编号，不接受凭据秘密。')
        for name, value in (('root_id', root_id), ('task_id', task_id), ('principal', principal),
                            ('authorization_ref', authorization_ref)):
            identifier(value, name)
        value = {'root_id': root_id, 'task_id': task_id, 'principal': principal,
                 'identity_binding': self.identity_binding, 'authorization_ref': authorization_ref, 'state': 'active'}
        with self.budgets.store.connect(write=True) as db:
            self.budgets._task(db, root_id, task_id, principal, active=True)
            with self.identities.store.connect() as identities:
                key = identities.execute('''SELECT k.principal,k.revoked_at,k.expires_at,a.role,a.disabled
                    FROM api_keys k JOIN accounts a ON a.principal=k.principal WHERE k.id=?''', (key_id,)).fetchone()
            if (not key or key['principal'] != principal or key['role'] != 'buyer' or key['disabled']
                    or key['revoked_at'] is not None or key['expires_at'] <= self.identities.clock()):
                raise MarketError('task_binding_denied', '凭据不属于可用买方主体。')
            old = db.execute('SELECT value FROM meta WHERE key=?', (BINDING_PREFIX + key_id,)).fetchone()
            if old:
                if parse_json(old[0]) != value:
                    raise MarketError('task_binding_conflict', '凭据已绑定其他任务或已撤销；不能改绑或复活。')
            else:
                db.execute('INSERT INTO meta VALUES(?,?)', (BINDING_PREFIX + key_id, compact(value)))
                self.budgets._audit(db, root_id, 'remote_key_bound', {'key_id': key_id,
                    'principal': principal, 'authorization_ref': authorization_ref}, task_id)
        return {'status': 'bound', 'key_id': key_id, 'task_id': task_id, 'payment_authorized': False}

    def revoke_binding(self, key_id):
        identifier(key_id, 'key_id')
        with self.budgets.store.connect(write=True) as db:
            row = db.execute('SELECT value FROM meta WHERE key=?', (BINDING_PREFIX + key_id,)).fetchone()
            if not row:
                raise MarketError('not_found', '任务绑定不存在。')
            value = parse_json(row[0])
            if value['state'] != 'revoked':
                value['state'] = 'revoked'
                db.execute('UPDATE meta SET value=? WHERE key=?', (compact(value), BINDING_PREFIX + key_id))
                self.budgets._audit(db, value['root_id'], 'remote_key_unbound', {'key_id': key_id}, value['task_id'])
        return {'status': 'revoked', 'key_id': key_id, 'credential_revoked': False}

    def inspect_binding(self, key_id, after_reservation_id=None):
        """Trusted operator view, bounded pages; never authenticates or exposes a token."""
        identifier(key_id, 'key_id')
        if after_reservation_id is not None:
            identifier(after_reservation_id, 'after_reservation_id')
        with self.budgets.store.connect() as db:
            key = BINDING_PREFIX + key_id
            row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
            if not row:
                raise MarketError('not_found', '任务绑定不存在。')
            validate_bindings(db, {key: row[0]})
            binding = parse_json(row[0])
            records = db.execute('''SELECT * FROM reservations
                WHERE root_id=? AND task_id=? AND id>? ORDER BY id LIMIT 101''',
                (binding['root_id'], binding['task_id'], after_reservation_id or '')).fetchall()
            return {'status': 'inspected', 'key_id': key_id,
                    'binding': {name: binding[name] for name in ('root_id', 'task_id', 'principal', 'state')},
                    'identity_location_matches': binding['identity_binding'] == self.identity_binding,
                    'credential_validity_checked': False,
                    'budget': self.budgets._summary(db, binding['root_id']),
                    'reservations': [TaskBudget._view(db, record) for record in records[:100]],
                    'next_cursor': records[99]['id'] if len(records) > 100 else None}

    def _binding(self, db, identity):
        row = db.execute('SELECT value FROM meta WHERE key=?', (BINDING_PREFIX + identity['key_id'],)).fetchone()
        if not row:
            raise MarketError('task_budget_required', '此凭据尚未绑定获准任务，请由受信运营者处理。')
        value = parse_json(row[0])
        validate_bindings(db, {BINDING_PREFIX + identity['key_id']: row[0]})
        if (value['state'] != 'active' or value['principal'] != identity['principal']
                or value['identity_binding'] != self.identity_binding):
            raise MarketError('task_binding_denied', '任务绑定已失效或不属于此身份位置。')
        return value

    def tool_list(self, token):
        identity = self.identities.authenticate(token)
        result = super().tool_list(token)
        if identity['role'] == 'buyer':
            result['task_budget_required'] = True
            result['instructions'] += ' 远程read需要运营预先绑定此凭据与任务；任务编号不能自行更换。返回预算只表示依赖费用预留，不授权或执行依赖。'
            with self.budgets.store.connect() as db:
                try:
                    binding = self._binding(db, identity)
                except MarketError as exc:
                    if exc.code not in ('task_budget_required', 'task_binding_denied'):
                        raise
                else:
                    result['bound_task_id'] = binding['task_id']
                    for tool in result['tools']:
                        if tool['name'] == 'market_read':
                            tool['inputSchema']['properties']['task_id']['enum'] = [binding['task_id']]
        return result

    def call(self, token, tool, arguments):
        if tool != 'market_read':
            return super().call(token, tool, arguments)
        try:
            identity = self.identities.authenticate(token)
            interface = ExchangeInterface(Exchange(self.database, identity['principal'], identity['role'],
                                                    clock=self.clock, host_policy=identity['policy']))
            if tool not in interface.specs:
                raise MarketError('unknown_tool', '此角色未提供该工具。')
            _, properties, required, _, _ = interface.specs[tool]
            validate(arguments, {'type': 'object', 'properties': properties, 'required': required})
            return self._read(token, identity, interface.market, arguments)
        except MarketError as exc:
            return error_result(exc)
        except (OSError, sqlite3.Error):
            return {'status': 'error', 'error_code': 'unavailable', 'message': '交付或预算状态未知；保留原任务及请求编号核验。'}

    def _read(self, token, identity, market, arguments):
        with self.budgets.store.connect() as db:
            binding = self._binding(db, identity)
        if arguments['task_id'] != binding['task_id']:
            raise MarketError('budget_scope_mismatch', '请求任务与凭据授权任务不一致。')
        scope = self.budgets.scope(binding['root_id'], binding['task_id'], identity['principal'])
        with market.store.connect() as db:
            acquired = market._owned(db, arguments['acquisition_id'])
        # Re-check declared compatibility even for previously acquired rights.
        compatibility = market.check_compatibility(acquired['resource_id'], acquired['version'], acquired['purpose'])
        if compatibility['status'] != 'ready':
            raise MarketError(compatibility['status'], '商品当前使用条件未满足，未交付正文。')
        reservation = None
        if compatibility['extra_max_cents']:
            reference = hashlib.sha256(compact([acquired['resource_id'], acquired['version'], acquired['purpose']]).encode()).hexdigest()
            reservation = scope.reserve('dependency:' + reference, 'dependency', reference, compatibility['extra_max_cents'])
        # Hold the budget writer lock across admission and delivery: a concurrent pause,
        # close or binding revocation cannot slip between this check and the market read.
        with self.budgets.store.connect(write=True) as db:
            fresh = self.identities.authenticate(token)
            if self._binding(db, fresh) != binding:
                raise MarketError('task_binding_conflict', '任务绑定已改变，未交付正文。')
            scope._bound(db, active=True)
            if reservation:
                reservation = scope._view(db, scope._get(db, reservation['reservation_id']))
                if reservation['state'] not in ('reserved', 'settled'):
                    raise MarketError('budget_operation_' + reservation['state'], '先核对原依赖操作，未交付正文。')
            result = market.read(**arguments)
        result['task_budget'] = {'task_id': binding['task_id'], 'dependency_reservation': reservation,
                                 'execution_authorized': False, 'payment_authorized': False}
        return result
