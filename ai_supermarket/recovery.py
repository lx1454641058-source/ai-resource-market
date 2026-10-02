"""来源：本项目原创。业务与可选计量联合快照、向新目录恢复及受信运营放行。"""

import argparse
from contextlib import ExitStack, closing
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid

from .budget import BudgetStore, MICRO_ACTUAL, POOL_KEY, POOL_AUDIT, read_pool, restore_pool, validate_budget_amounts, validate_pool_metadata
from .common import MarketError, compact, error_result, fields, identifier, integer, parse_json
from .exchange_store import ExchangeStore
from .identity import IdentityStore
from .measurement import MeasurementStore, recovery_metadata_keys, restore_measurement, validate_measurement
from .migrate import schema_objects


STORES = {'market': ExchangeStore, 'identities': IdentityStore, 'budget': BudgetStore}
ALL_STORES = {**STORES, 'measurement': MeasurementStore}
FILENAMES = {kind: kind + '.sqlite' for kind in ALL_STORES}
BASE_META = {'schema_version': '2'}
VERSION_KEYS = {'market': 'exchange_schema_version', 'identities': 'identity_schema_version',
                'budget': 'budget_schema_version', 'measurement': 'measurement_schema_version'}
REVIEW_FIELDS = ('access_review', 'financial_reconciliation', 'delivery_review', 'approval')


def connect_existing(path, *, writable=False, timeout=1.0):
    path = Path(path).resolve()
    if not path.is_file():
        raise MarketError('not_found', '缺少必需账本；检查不会自动初始化。')
    db = sqlite3.connect(path.as_uri() + ('?mode=rw' if writable else '?mode=ro'),
                         uri=True, timeout=timeout)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA trusted_schema=OFF')
    db.execute('PRAGMA foreign_keys=ON')
    return db


def validate_api_metadata(db, meta):
    """Recognize only code-owned API records; never mint rights from metadata.

    External quota/operation ledgers and result bytes are outside this bundle.
    Historical records remain valid when keys expire or tasks are later revoked.
    """
    from .api_quota import canonical, checksum
    from .api_product import validate_api
    from .exchange import Exchange
    from .purchase_download import digest

    prefixes = ('api_operation_authority:', 'api_usage:', 'api_client_receipt:')
    found = {}
    for key, raw in meta.items():
        if not isinstance(key, str):
            raise MarketError('invalid_database', '市场元信息键无效。')
        if not key.startswith(prefixes):
            continue
        if not isinstance(raw, str) or len(raw.encode('utf-8')) > 8192:
            raise MarketError('invalid_database', 'API历史记录超过上限。')
        value = parse_json(raw)
        suffix = key.split(':', 1)[1]
        if key.startswith(prefixes[0]):
            digest(suffix)
            fields(value, ('key_id', 'principal', 'root_id', 'task_id', 'provider', 'binding_id', 'operation_id', 'request_sha256'))
            for name in ('principal', 'root_id', 'task_id', 'provider', 'binding_id'):
                identifier(value[name], name)
            if (not isinstance(value['key_id'], str) or not re.fullmatch('ak_[0-9a-f]{32}', value['key_id'])
                    or value['operation_id'] != suffix):
                raise MarketError('invalid_database', 'API原操作编号或凭据编号不符。')
            digest(value['request_sha256'])
        elif key.startswith(prefixes[1]):
            if not re.fullmatch('u_[0-9a-f]{32}', suffix):
                raise MarketError('invalid_database', 'API使用编号无效。')
            fields(value, ('format', 'id', 'version', 'acquisition_id', 'release_digest', 'task_id', 'request_id',
                           'purpose', 'provider', 'operation_id', 'sha256', 'bytes', 'media_type', 'license_sha256', 'expires_at'))
            for name in ('id', 'version', 'acquisition_id', 'task_id', 'request_id', 'provider'):
                identifier(value[name], name)
            for name in ('release_digest', 'operation_id', 'sha256', 'license_sha256'):
                digest(value[name])
            integer(value['bytes'], 1, 65536, 'bytes'); integer(value['expires_at'], 1, 9999999999, 'expires_at')
            if value['format'] != 'market-api-result-v1' or value['media_type'] != 'application/json':
                raise MarketError('invalid_database', 'API交付清单格式无效。')
            usage = db.execute('SELECT * FROM usage_events WHERE id=?', (suffix,)).fetchone()
            acquired = db.execute('SELECT * FROM acquisitions WHERE id=?', (value['acquisition_id'],)).fetchone()
            item = db.execute('SELECT metadata,content_hash FROM submissions WHERE resource_id=? AND version=?',
                              (value['id'], value['version'])).fetchone()
            if (not usage or not acquired or not item or usage['acquisition_id'] != acquired['id']
                    or usage['principal'] != acquired['principal'] or usage['task_id'] != value['task_id']
                    or usage['request_id'] != checksum(compact(['api-call', value['request_id']]))
                    or any(acquired[column] != value[name] for column, name in
                           (('resource_id', 'id'), ('version', 'version'), ('purpose', 'purpose')))
                    or acquired['content_hash'] != item['content_hash']):
                raise MarketError('invalid_database', 'API使用记录与原权益或商品不符。')
            metadata = parse_json(item['metadata'])
            if not isinstance(metadata, dict) or not isinstance(metadata.get('license_text'), str):
                raise MarketError('invalid_database', 'API原商品元数据无效。')
            api = metadata.get('api')
            validate_api(api)
            if (api['provider'] != value['provider'] or value['bytes'] > api['result_max_bytes']
                    or value['license_sha256'] != checksum(metadata['license_text'])
                    or value['expires_at'] != int(acquired['created_at']) + api['validity_seconds']
                    or value['release_digest'] != Exchange._release_digest(db, value['id'], value['version'])):
                raise MarketError('invalid_database', 'API交付清单与原发行不符。')
        else:
            digest(suffix)
            fields(value, ('offer', 'receipt_id', 'acknowledged_at'))
            offer = value['offer']
            fields(offer, ('scope', 'request_id', 'usage_id', 'manifest_sha256', 'input_sha256'))
            scope = offer['scope']; fields(scope, ('principal', 'key_id', 'root_id', 'task_id'))
            for name in ('principal', 'root_id', 'task_id'):
                identifier(scope[name], name)
            for name in ('request_id', 'usage_id'):
                identifier(offer[name], name)
            for name in ('manifest_sha256', 'input_sha256'):
                digest(offer[name])
            stamp = value['acknowledged_at']
            if (not isinstance(scope['key_id'], str) or not re.fullmatch('ak_[0-9a-f]{32}', scope['key_id'])
                    or type(stamp) not in (int, float) or not 0 <= stamp < 10**12
                    or suffix != checksum(canonical([scope, offer['request_id']], 4096))
                    or value['receipt_id'] != 'ar_' + checksum(canonical(offer, 8192))):
                raise MarketError('invalid_database', 'API收货编号、范围或时间不符。')
        found[key] = raw

    # Cross-record checks run after shapes; iteration order must not affect validity.
    for key, raw in found.items():
        value = parse_json(raw)
        if key.startswith(prefixes[1]):
            operation = found.get(prefixes[0] + value['operation_id'])
            if operation is not None:
                original = parse_json(operation)
                usage = db.execute('SELECT principal FROM usage_events WHERE id=?', (key.split(':', 1)[1],)).fetchone()
                if (original['principal'] != usage['principal'] or original['provider'] != value['provider']
                        or original['task_id'] != value['task_id']):
                    raise MarketError('invalid_database', 'API原操作与使用记录范围不符。')
        elif key.startswith(prefixes[2]):
            offer = value['offer']; scope = offer['scope']
            raw_manifest = found.get(prefixes[1] + offer['usage_id'])
            if raw_manifest is None:
                raise MarketError('invalid_database', 'API回执缺少原交付清单。')
            manifest = parse_json(raw_manifest)
            usage = db.execute('SELECT principal FROM usage_events WHERE id=?', (offer['usage_id'],)).fetchone()
            if (offer['manifest_sha256'] != checksum(canonical(manifest, 8192))
                    or offer['request_id'] != manifest['request_id'] or scope['task_id'] != manifest['task_id']
                    or scope['principal'] != usage['principal']):
                raise MarketError('invalid_database', 'API收货声明不属于原交付。')
            operation = found.get(prefixes[0] + manifest['operation_id'])
            if operation is not None and any(parse_json(operation)[name] != scope[name] for name in scope):
                raise MarketError('invalid_database', 'API回执身份或预算不属于原派发。')
    return found


def validate_database(db, kind, *, deep=True):
    """Validate code-owned schema before reading content; never execute SQL from a manifest."""
    store_type = ALL_STORES[kind]
    if kind == 'market':
        version = db.execute("SELECT value FROM meta WHERE key='exchange_schema_version'").fetchone()
        if version and version[0] == '2':
            from .assets import AssetStore
            store_type = AssetStore
    with closing(sqlite3.connect(':memory:')) as expected:
        expected.row_factory = sqlite3.Row
        expected.executescript(store_type.schema)
        if schema_objects(db) != schema_objects(expected):
            raise MarketError('wrong_database', '账本结构与当前程序不匹配。')
    meta = dict(db.execute('SELECT key,value FROM meta'))
    base = {**BASE_META, VERSION_KEYS[kind]: '1'}
    if kind == 'market':
        base[VERSION_KEYS[kind]] = store_type.version
        base.update(validate_api_metadata(db, meta))
        from .commerce_recovery import validate_metadata
        base.update(validate_metadata(db, meta))
    if 'recovery_id' in meta:
        identifier(meta['recovery_id'], 'recovery_id')
        base['recovery_id'] = meta['recovery_id']
    if kind in ('market', 'measurement') and 'recovery_state' in meta:
        if meta['recovery_state'] not in ('pending', 'released'):
            raise MarketError('invalid_database', '恢复状态无效。')
        base['recovery_state'] = meta['recovery_state']
    if kind == 'market' and 'measurement_required' in meta:
        if meta['measurement_required'] != '1' or 'recovery_id' not in meta:
            raise MarketError('invalid_database', '计量恢复要求无效。')
        base['measurement_required'] = '1'
    if kind == 'measurement':
        if ('recovery_id' in meta) != ('recovery_state' in meta):
            raise MarketError('invalid_database', '计量恢复批次与状态必须同时存在。')
        for key in recovery_metadata_keys(meta):
            base[key] = meta[key]
    if kind == 'budget':
        from .task_budget_gateway import BINDING_PREFIX
        pool = read_pool(db)
        if pool is not None:
            base['budget_schema_version'] = '2'
        for key in meta:
            if key == POOL_KEY or key.startswith((MICRO_ACTUAL, BINDING_PREFIX, POOL_AUDIT)):
                base[key] = meta[key]
    if meta != base:
        raise MarketError('wrong_database', '账本版本或元信息不匹配。')
    if deep:
        if [r[0] for r in db.execute('PRAGMA integrity_check')] != ['ok'] or db.execute('PRAGMA foreign_key_check').fetchone():
            raise MarketError('invalid_database', '账本完整性或关联校验失败。')
        if kind == 'market':
            if store_type.version == '2':
                from .assets import validate_objects
                validate_objects(db)
            from .exchange import validate_listing
            from .assets import version_hash, get_asset
            for row in db.execute('SELECT resource_id,version,metadata,content,content_hash FROM submissions'):
                metadata = parse_json(row['metadata'])
                validate_listing(metadata, row['content'])
                if 'asset' in metadata:
                    get_asset(db, row['resource_id'], row['version'], metadata)
                if version_hash(metadata, row['content']) != row['content_hash']:
                    raise MarketError('integrity_failed', '商品正文与校验值不一致。')
            if db.execute('''SELECT 1 FROM usage_events u JOIN acquisitions a ON a.id=u.acquisition_id
                WHERE u.principal<>a.principal LIMIT 1''').fetchone() or db.execute('''SELECT 1 FROM feedback f
                JOIN usage_events u ON u.id=f.usage_id WHERE f.principal<>u.principal LIMIT 1''').fetchone():
                raise MarketError('invalid_database', '使用或反馈记录归属不一致。')
        if kind == 'identities':
            from .contracts import HostPolicy
            for row in db.execute('SELECT policy FROM accounts'):
                HostPolicy.from_json(parse_json(row[0]))
        if kind == 'budget' and db.execute('''SELECT 1 FROM reservations r JOIN budget_tasks t
            ON r.root_id=t.root_id AND r.task_id=t.id WHERE r.principal<>t.principal LIMIT 1''').fetchone():
            raise MarketError('invalid_database', '预算任务归属不一致。')
        if kind == 'measurement':
            validate_measurement(db, meta)
        if kind == 'budget':
            validate_budget_amounts(db)
            validate_pool_metadata(db, meta)
            from .task_budget_gateway import validate_bindings
            validate_bindings(db, meta)
    return meta


def digest_file(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def write_new(path, value):
    with Path(path).open('x', encoding='utf-8') as handle:
        handle.write(compact(value) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def paths_for(directory, *, include_measurement=False):
    directory = Path(directory).resolve()
    return {kind: directory / FILENAMES[kind] for kind in (ALL_STORES if include_measurement else STORES)}


def copy_db(source, destination, deadline):
    # Never copy raw SQLite/WAL files. A separate reader can back up under our writer reservation.
    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise MarketError('operation_timeout', '快照超过期限；不使用未完成目标。')
    with Path(destination).open('xb'):
        pass
    with closing(connect_existing(source)) as src, closing(sqlite3.connect(destination)) as target:
        src.backup(target, pages=256, progress=progress, sleep=0.01)
        # Seal the NEW copy as one file, even when the source uses a WAL journal.
        target.execute('PRAGMA journal_mode=DELETE')
    with Path(destination).open('r+b') as handle:
        os.fsync(handle.fileno())


def lock_databases(stack, paths, deadline):
    """Fixed path order prevents competing bundle operations from locking in opposite order."""
    if len({str(Path(p).resolve()).casefold() for p in paths.values()}) != len(paths):
        raise MarketError('invalid_params', '每类账本必须使用不同文件。')
    connections = {}
    for kind in sorted(paths, key=lambda k: str(paths[k]).casefold()):
        db = stack.enter_context(closing(connect_existing(paths[kind], writable=True)))
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        db.execute('BEGIN IMMEDIATE')
        connections[kind] = db
    return connections


def snapshot_bundle(market, identities, budget, output, *, measurement=None):
    """Reserve every included writer first. Does not stop external supplier work."""
    paths = {k: Path(p).resolve() for k, p in zip(STORES, (market, identities, budget))}
    if measurement is not None:
        paths['measurement'] = Path(measurement).resolve()
    output = Path(output).resolve()
    if output.exists():
        raise MarketError('destination_exists', '目标必须是新目录；不覆盖或合并。')
    started = time.time()
    snapshot_id = 'snap_' + uuid.uuid4().hex
    deadline = time.monotonic() + 30
    with ExitStack() as stack:
        locked = lock_databases(stack, paths, deadline)
        recovery_ids = set()
        for kind, db in locked.items():
            meta = validate_database(db, kind)
            recovery_ids.add(meta.get('recovery_id'))
            if kind == 'market' and meta.get('measurement_required') == '1' and measurement is None:
                raise MarketError('measurement_required', '此服务恢复范围包含计量，快照不能省略计量库。')
        if len(recovery_ids) != 1:
            raise MarketError('recovery_mismatch', '源账本来自不同恢复批次。')
        _business_links(locked)
        output.mkdir(parents=True, exist_ok=False)
        write_new(output / '.ai-supermarket-snapshot.json', {'snapshot_id': snapshot_id})
        # manifest.json is written last; a partial directory never represents a valid snapshot.
        records = {}
        for kind, destination in paths_for(output, include_measurement=measurement is not None).items():
            copy_db(paths[kind], destination, deadline)
            with closing(connect_existing(destination)) as db:
                validate_database(db, kind)
            records[kind] = {'file': FILENAMES[kind], 'sha256': digest_file(destination),
                             'bytes': destination.stat().st_size}
        manifest = {'format': 2 if measurement is not None else 1, 'snapshot_id': snapshot_id,
                    'started_at': started, 'completed_at': time.time(), 'files': records,
                    'scope': 'exchange_identity_budget_measurement' if measurement is not None else 'exchange_identity_budget',
                    'external_effects_reconciled': False}
        write_new(output / 'manifest.json', manifest)
    return {'status': 'snapshotted', 'snapshot_id': manifest['snapshot_id'], 'verified': True,
            'external_effects_reconciled': False, 'switched': False}


def read_manifest(directory):
    path = Path(directory) / 'manifest.json'
    with path.open('rb') as handle:
        raw = handle.read(16385)
    if len(raw) > 16384:
        raise MarketError('invalid_manifest', '快照清单过大。')
    value = parse_json(raw)
    fields(value, ('format', 'snapshot_id', 'started_at', 'completed_at', 'files', 'scope',
                   'external_effects_reconciled'))
    scopes = {1: 'exchange_identity_budget', 2: 'exchange_identity_budget_measurement'}
    if type(value['format']) is not int or value['format'] not in scopes or value['scope'] != scopes[value['format']]:
        raise MarketError('invalid_manifest', '不支持的快照格式。')
    if (value['external_effects_reconciled'] is not False or
            any(type(value[key]) not in (int, float) or value[key] < 0
                for key in ('started_at', 'completed_at')) or value['completed_at'] < value['started_at']):
        raise MarketError('invalid_manifest', '快照时间或外部核验声明无效。')
    identifier(value['snapshot_id'], 'snapshot_id')
    with (Path(directory) / '.ai-supermarket-snapshot.json').open('rb') as handle:
        marker = parse_json(handle.read(4097))
    fields(marker, ('snapshot_id',))
    if marker['snapshot_id'] != value['snapshot_id']:
        raise MarketError('invalid_manifest', '快照标记与清单不匹配。')
    fields(value['files'], ALL_STORES if value['format'] == 2 else STORES)
    if value['format'] == 1 and (Path(directory) / FILENAMES['measurement']).exists():
        raise MarketError('invalid_manifest', '目录含未纳入清单的计量库，不能静默忽略。')
    for kind, row in value['files'].items():
        fields(row, ('file', 'sha256', 'bytes'))
        # Fixed local filenames: a manifest cannot redirect the reader outside the bundle.
        if (row['file'] != FILENAMES[kind] or type(row['bytes']) is not int or row['bytes'] < 1
                or not isinstance(row['sha256'], str) or len(row['sha256']) != 64
                or any(c not in '0123456789abcdef' for c in row['sha256'])):
            raise MarketError('invalid_manifest', '快照文件描述不合法。')
    return value


def verify_bundle(directory, *, require_measurement=False):
    directory = Path(directory).resolve()
    manifest = read_manifest(directory)
    if require_measurement and manifest['format'] != 2:
        raise MarketError('measurement_required', '本次恢复要求包含计量账本。')
    recovery_ids = set()
    for kind, path in paths_for(directory, include_measurement=manifest['format'] == 2).items():
        if path.is_symlink() or path.parent != directory:
            raise MarketError('invalid_manifest', '快照文件不能使用符号链接。')
        row = manifest['files'][kind]
        if path.stat().st_size != row['bytes'] or digest_file(path) != row['sha256']:
            raise MarketError('integrity_failed', '快照文件与清单不一致。')
        # Sealed snapshots are standalone files; WAL/journal additions are not part of their hash.
        if any(Path(str(path) + suffix).exists() for suffix in ('-wal', '-journal')):
            raise MarketError('invalid_manifest', '快照含清单外写入日志，不能恢复。')
        with closing(connect_existing(path)) as db:
            meta = validate_database(db, kind)
            recovery_ids.add(meta.get('recovery_id'))
            if kind == 'market' and meta.get('measurement_required') == '1' and manifest['format'] != 2:
                raise MarketError('measurement_required', '服务要求的计量库未包含在快照中。')
    if len(recovery_ids) != 1:
        raise MarketError('recovery_mismatch', '快照账本来自不同恢复批次。')
    _purchase_links_at(paths_for(directory, include_measurement=manifest['format'] == 2))
    return {'status': 'verified', 'snapshot_id': manifest['snapshot_id'], 'files': len(manifest['files']),
            'authenticity_proven': False, 'external_effects_reconciled': False}


def restore_bundle(source, output, *, require_measurement=False):
    """Restore to new files, revoke saved keys, pause budgets and gate the market until review."""
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists():
        raise MarketError('destination_exists', '恢复目标必须是新目录。')
    verified = verify_bundle(source, require_measurement=require_measurement)
    manifest = read_manifest(source)
    output.mkdir(parents=True, exist_ok=False)
    with_measurement = manifest['format'] == 2
    paths = paths_for(output, include_measurement=with_measurement)
    recovery_id = 'restore_' + uuid.uuid4().hex
    # A failed copy is never silently reused or activated.
    write_new(output / 'restore-started.json', {'snapshot_id': verified['snapshot_id'], 'recovery_id': recovery_id})
    for kind, target in paths.items():
        with (source / FILENAMES[kind]).open('rb') as src, target.open('xb') as dst:
            while chunk := src.read(1024 * 1024):
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        if digest_file(target) != manifest['files'][kind]['sha256']:
            raise MarketError('integrity_failed', '复制期间快照变化；不要启用恢复目标。')
    now = time.time()
    # Install the market gate before changing any identity or budget state.
    with closing(connect_existing(paths['market'], writable=True)) as db, db:
        validate_database(db, 'market')
        db.execute("INSERT OR REPLACE INTO meta VALUES ('recovery_state','pending')")
        db.execute("INSERT OR REPLACE INTO meta VALUES ('recovery_id',?)", (recovery_id,))
        if with_measurement:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('measurement_required','1')")
    with closing(connect_existing(paths['identities'], writable=True)) as db, db:
        validate_database(db, 'identities')
        db.execute("INSERT OR REPLACE INTO meta VALUES ('recovery_id',?)", (recovery_id,))
        for row in db.execute('SELECT id,principal FROM api_keys WHERE revoked_at IS NULL').fetchall():
            db.execute('UPDATE api_keys SET revoked_at=? WHERE id=?', (now, row['id']))
            db.execute('INSERT INTO identity_audit VALUES (?,?,?,?,?)',
                       ('ie_' + uuid.uuid4().hex, row['principal'], 'recovery_key_revoked', row['id'], now))
    with closing(connect_existing(paths['budget'], writable=True)) as db, db:
        validate_database(db, 'budget')
        db.execute("INSERT OR REPLACE INTO meta VALUES ('recovery_id',?)", (recovery_id,))
        for row in db.execute("SELECT id FROM budgets WHERE state='active'").fetchall():
            db.execute("UPDATE budgets SET state='paused',revision=revision+1 WHERE id=?", (row['id'],))
            db.execute('INSERT INTO budget_audit VALUES (?,?,?,?,?,?,?)',
                       ('be_' + uuid.uuid4().hex, row['id'], None, None, 'recovery_paused',
                        compact({'snapshot_id': verified['snapshot_id']}), now))
        restore_pool(db, verified['snapshot_id'])
    if with_measurement:
        with closing(connect_existing(paths['measurement'], writable=True)) as db, db:
            validate_database(db, 'measurement')
            restore_measurement(db, recovery_id, verified['snapshot_id'])
    for kind, path in paths.items():
        with closing(connect_existing(path)) as db:
            validate_database(db, kind)
    _purchase_links_at(paths)
    record = {'status': 'restored_quarantined', 'snapshot_id': verified['snapshot_id'], 'recovery_id': recovery_id,
              'old_keys_revoked': True, 'budgets_paused': True, 'market_released': False,
              'switched': False, 'external_effects_reconciled': False}
    if with_measurement:
        record['measurement_included'] = True
    write_new(output / 'restore-complete.json', record)
    return record


def release_bundle(directory, evidence):
    """Trusted local operator attests external review; references are not independent proof."""
    fields(evidence, REVIEW_FIELDS, ('measurement_reconciliation',))
    for key in evidence:
        identifier(evidence[key], key)
    directory = Path(directory).resolve()
    if not (directory / 'restore-complete.json').is_file():
        raise MarketError('incomplete_restore', '恢复未完成，不能放行。')
    with (directory / 'restore-complete.json').open('rb') as handle:
        raw = handle.read(16385)
    if len(raw) > 16384:
        raise MarketError('incomplete_restore', '恢复记录过大。')
    record = parse_json(raw)
    fields(record, ('status', 'snapshot_id', 'recovery_id', 'old_keys_revoked', 'budgets_paused',
                    'market_released', 'switched', 'external_effects_reconciled'), ('measurement_included',))
    with_measurement = record.get('measurement_included') is True
    if 'measurement_included' in record and not with_measurement:
        raise MarketError('incomplete_restore', '计量恢复范围无效。')
    fields(evidence, (*REVIEW_FIELDS, 'measurement_reconciliation') if with_measurement else REVIEW_FIELDS)
    if not with_measurement and (directory / FILENAMES['measurement']).exists():
        raise MarketError('incomplete_restore', '恢复记录遗漏计量库，不能放行。')
    identifier(record['recovery_id'], 'recovery_id')
    if (record['status'] != 'restored_quarantined' or record['old_keys_revoked'] is not True
            or record['budgets_paused'] is not True or record['market_released'] is not False
            or record['switched'] is not False or record['external_effects_reconciled'] is not False):
        raise MarketError('incomplete_restore', '恢复记录不完整。')
    paths, deadline = paths_for(directory, include_measurement=with_measurement), time.monotonic() + 30
    with ExitStack() as stack:
        connections = lock_databases(stack, paths, deadline)
        for kind, db in connections.items():
            if validate_database(db, kind).get('recovery_id') != record['recovery_id']:
                raise MarketError('recovery_mismatch', '账本不是同一恢复批次，不能放行。')
        _business_links(connections)
        market = connections['market']
        required = market.execute("SELECT value FROM meta WHERE key='measurement_required'").fetchone()
        if bool(required) != with_measurement:
            raise MarketError('incomplete_restore', '恢复完成记录与市场要求范围不一致。')
        state = market.execute("SELECT value FROM meta WHERE key='recovery_state'").fetchone()
        if not state or state[0] != 'pending':
            raise MarketError('invalid_state', '仅待复核恢复副本可以放行。')
        market.execute('INSERT INTO exchange_audit VALUES (?,?,?,?,?,?,?)',
                       ('ea_' + uuid.uuid4().hex, 'trusted-recovery-operator', 'recovery_released', None, None,
                        compact(evidence), time.time()))
        if with_measurement:
            measurement_db = connections['measurement']
            measurement_state = measurement_db.execute("SELECT value FROM meta WHERE key='recovery_state'").fetchone()
            if not measurement_state or measurement_state[0] not in ('pending', 'released'):
                raise MarketError('incomplete_restore', '计量恢复状态无效。')
            # Keep market pending if this commit fails; partial release is retryable.
            measurement_db.execute("UPDATE meta SET value='released' WHERE key='recovery_state'")
            measurement_db.commit()
        market.execute("UPDATE meta SET value='released' WHERE key='recovery_state'")
        market.commit()
    return {'status': 'released', 'old_keys_restored': False, 'budgets_resumed': False,
            'switched': False, 'independent_review_proven': False}


def _business_links(connections):
    from .purchase_recovery import validate_budget_links
    from .delivery_recovery import validate_account_links
    validate_budget_links(connections['market'], connections.get('budget'))
    validate_account_links(connections['market'], connections.get('identities'), connections.get('budget'))


def _purchase_links_at(paths, deadline=None):
    with ExitStack() as stack:
        connections = {}
        for kind in ('market', 'budget', 'identities'):
            if kind not in paths: continue
            db = stack.enter_context(closing(connect_existing(paths[kind], timeout=0.1)))
            if deadline is not None:
                db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            connections[kind] = db
        _business_links(connections)


def readiness(market, identities, budget=None, measurement=None):
    """Bounded non-mutating connectivity/schema probe, not a production certification."""
    paths = {'market': market, 'identities': identities}
    if budget is not None:
        paths['budget'] = budget
    if measurement is not None:
        paths['measurement'] = measurement
    deadline = time.monotonic() + 2
    try:
        recovery_ids = set()
        for kind, path in paths.items():
            if (Path(path).resolve().parent / '.ai-supermarket-snapshot.json').exists():
                return {'status': 'not_ready'}
            with closing(connect_existing(path, timeout=0.1)) as db:
                db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                meta = validate_database(db, kind, deep=False)
                if kind == 'market' and meta.get('measurement_required') == '1' and measurement is None:
                    return {'status': 'not_ready'}
                recovery_ids.add(meta.get('recovery_id'))
                if (meta.get('recovery_state') == 'pending' or
                        (kind == 'market' and (Path(path).resolve().parent / 'restore-started.json').exists()
                         and meta.get('recovery_state') != 'released')):
                    return {'status': 'not_ready'}
                # Exercise every table without publishing counts, paths, content or identities.
                for table in ALL_STORES[kind].tables:
                    db.execute('SELECT 1 FROM ' + table + ' LIMIT 1').fetchone()
        _purchase_links_at(paths, deadline)
        return {'status': 'ready' if len(recovery_ids) == 1 else 'not_ready'}
    except (MarketError, OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, OverflowError):
        return {'status': 'not_ready'}


def main(argv=None):
    parser = argparse.ArgumentParser(description='AI超市业务与可选计量联合快照；不自动切换生产')
    commands = parser.add_subparsers(dest='command', required=True)
    backup = commands.add_parser('snapshot')
    for field in ('market', 'identities', 'budget', 'output'):
        backup.add_argument('--' + field, type=Path, required=True)
    backup.add_argument('--measurement', type=Path, help='计量账本；指定后生成四库格式2快照')
    for command in ('verify', 'restore', 'release'):
        sub = commands.add_parser(command)
        sub.add_argument('--bundle', type=Path, required=True)
        if command in ('verify', 'restore'):
            sub.add_argument('--require-measurement', action='store_true', help='拒绝没有计量库的旧三库快照')
        if command == 'restore':
            sub.add_argument('--output', type=Path, required=True)
        if command == 'release':
            sub.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'snapshot':
            result = snapshot_bundle(args.market, args.identities, args.budget, args.output, measurement=args.measurement)
        elif args.command == 'verify':
            result = verify_bundle(args.bundle, require_measurement=args.require_measurement)
        elif args.command == 'restore':
            result = restore_bundle(args.bundle, args.output, require_measurement=args.require_measurement)
        else:
            with args.evidence.open('rb') as handle:
                raw = handle.read(16385)
            if len(raw) > 16384:
                raise MarketError('invalid_params', '复核依据文件过大。')
            result = release_bundle(args.bundle, parse_json(raw))
    except MarketError as exc:
        result = error_result(exc)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        result = {'status': 'error', 'error_code': 'unavailable',
                  'message': '快照或恢复未完成；不使用失败目标。原始路径、资料和凭据不输出。'}
    sys.stdout.buffer.write(compact(result).encode('utf-8') + b'\n')
    return 1 if result['status'] == 'error' else 0


if __name__ == '__main__':
    raise SystemExit(main())
