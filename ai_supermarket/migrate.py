"""来源：本项目原创。只读快照复制升级v1账本；不替换原文件或修改客户端配置。"""

import argparse
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import sys

from .common import MarketError, choice, compact, error_result, identifier
from .exchange_store import EXCHANGE_SCHEMA, ExchangeStore
from .store import LEGACY_SCHEMA, Store


def schema_objects(db):
    return {row['name']: (row['type'], row['tbl_name'], ''.join(row['sql'].split()))
            for row in db.execute("SELECT name,type,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL")}


def copy_upgrade(source, destination):
    """Copy a consistent SQLite snapshot to a new v2 ledger, verifying all copied records.

    Stop callers before switching paths: writes committed after this snapshot remain only
    in the old ledger. This command deliberately never performs the switch itself.
    """
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or destination.exists():
        raise MarketError('destination_exists', '目标必须是尚不存在的新文件；不会覆盖账本。')
    if not source.is_file():
        raise MarketError('not_found', '找不到源账本。')
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=10)) as original, \
            closing(sqlite3.connect(':memory:')) as snapshot:
        source_data_version = original.execute('PRAGMA data_version').fetchone()[0]
        snapshot.row_factory = sqlite3.Row
        original.backup(snapshot)
        if snapshot.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise MarketError('invalid_database', '源账本完整性检查失败。')
        tables = {r[0] for r in snapshot.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables == Store.tables:
            store_type, kind, legacy = Store, 'catalog', LEGACY_SCHEMA
        elif tables == ExchangeStore.tables:
            store_type, kind, legacy = ExchangeStore, 'exchange', LEGACY_SCHEMA + EXCHANGE_SCHEMA
        else:
            raise MarketError('wrong_database', '不是受支持的v1超市账本。')
        with closing(sqlite3.connect(':memory:')) as expected:
            expected.row_factory = sqlite3.Row
            expected.executescript(legacy)
            if schema_objects(snapshot) != schema_objects(expected):
                raise MarketError('wrong_database', '源账本结构不匹配；不执行猜测式迁移。')
        meta = dict(snapshot.execute('SELECT key,value FROM meta'))
        wanted = {'schema_version': '1'}
        if kind == 'exchange':
            wanted['exchange_schema_version'] = '1'
        if meta != wanted or snapshot.execute('PRAGMA foreign_key_check').fetchall():
            raise MarketError('invalid_database', '源版本或关联记录不完整。')
        # The old schema did not enforce principal consistency between these tables.
        if snapshot.execute('''SELECT 1 FROM usage_events u JOIN acquisitions a ON a.id=u.acquisition_id
                WHERE u.principal<>a.principal LIMIT 1''').fetchone():
            raise MarketError('invalid_database', '源账本使用记录归属不一致。')
        feedback = []
        for row in snapshot.execute('SELECT * FROM feedback ORDER BY usage_id'):
            usage = snapshot.execute('SELECT principal FROM usage_events WHERE id=?', (row['usage_id'],)).fetchone()
            if not usage or row['principal'] != usage[0]:
                raise MarketError('invalid_database', '源反馈归属不一致。')
            choice(row['evidence'], ('host_check', 'agent_reported'), 'evidence')
            validator = row['validator'] or ''
            if row['evidence'] == 'host_check':
                identifier(validator, 'validator')
            elif validator:
                raise MarketError('invalid_database', '模型反馈不能冒充程序检查。')
            choice(row['useful'], ('yes', 'no', 'unknown'), 'useful')
            choice(row['check_result'], ('pass', 'fail', 'not_run'), 'check_result')
            if row['failure_code'] is not None:
                from .service import FAILURES
                choice(row['failure_code'], FAILURES, 'failure_code')
            if row['check_result'] == 'pass' and row['failure_code'] is not None:
                raise MarketError('invalid_database', '源反馈通过/失败字段矛盾。')
            fid = 'f_' + hashlib.sha256(compact([row['usage_id'], row['evidence'], validator]).encode()).hexdigest()
            feedback.append((fid, row['usage_id'], row['principal'], row['evidence'], validator, 1,
                             row['useful'], row['check_result'], row['failure_code'], None, None, row['created_at']))
        order = ['acquisitions', 'usage_events', 'task_checks']
        if kind == 'exchange':
            order += ['submissions', 'offers', 'exchange_receipts', 'exchange_audit', 'quotes']
        records = {table: [tuple(r) for r in snapshot.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                   for table in order}
        widths = {table: len(snapshot.execute('PRAGMA table_info(' + table + ')').fetchall()) for table in order}
        # Exclusive creation prevents races and preserves existing files, including empty ones.
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open('xb'):
            pass
        target = store_type(destination)
        with target.connect(write=True) as db:
            for table in order:
                db.executemany('INSERT INTO ' + table + ' VALUES (' + ','.join('?' * widths[table]) + ')', records[table])
            db.executemany('INSERT INTO feedback VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', feedback)
            for table in order:
                copied = [tuple(r) for r in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                if copied != records[table]:
                    raise MarketError('copy_verification_failed', '复制校验失败，原账本未变；不要切换目标。')
            if [tuple(r) for r in db.execute('SELECT * FROM feedback ORDER BY usage_id')] != feedback:
                raise MarketError('copy_verification_failed', '反馈复制校验失败；不要切换目标。')
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchall():
                raise MarketError('copy_verification_failed', '新账本完整性校验失败；不要切换目标。')
            if original.execute('PRAGMA data_version').fetchone()[0] != source_data_version:
                raise MarketError('source_changed', '复制期间源账本有新写入；请停止写入后复制到另一个新文件，不要切换本次目标。')
        return {'status': 'copied', 'kind': kind, 'source_version': 1, 'target_version': 2,
                'destination': str(destination), 'counts': {**{k: len(v) for k, v in records.items()}, 'feedback': len(feedback)},
                'verified': True, 'switched': False,
                'next_action': 'keep_writers_stopped_until_manual_path_switch_and_verification'}


def main(argv=None):
    parser = argparse.ArgumentParser(description='先停止账本写入，再将v1复制升级到新文件；不覆盖或自动切换')
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = copy_upgrade(args.source, args.output)
    except MarketError as exc:
        result = error_result(exc)
    except (OSError, sqlite3.Error):
        result = {'status': 'error', 'error_code': 'unavailable',
                  'message': '文件不可用或复制失败。原文件未被本工具改写；失败目标不可切换使用。'}
    sys.stdout.buffer.write(compact(result).encode('utf-8') + b'\n')
    return 0 if result['status'] == 'copied' else 1


if __name__ == '__main__':
    raise SystemExit(main())
