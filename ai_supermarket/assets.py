"""来源：本项目原创。受限本地素材存储、版本绑定和取入校验，不提供公网文件传输。"""

import hashlib
from contextlib import closing
from pathlib import Path
import sqlite3

from .common import MarketError, compact, fields, parse_json
from .exchange_store import ExchangeStore


ASSET_SCHEMA = '''
CREATE TABLE asset_objects (
 sha256 TEXT PRIMARY KEY, body BLOB NOT NULL, size INTEGER NOT NULL CHECK(size>0 AND size<=2097152),
 CHECK(length(body)=size));
CREATE TABLE asset_links (
 resource_id TEXT NOT NULL, version TEXT NOT NULL, sha256 TEXT NOT NULL REFERENCES asset_objects(sha256),
 PRIMARY KEY(resource_id,version),
 FOREIGN KEY(resource_id,version) REFERENCES submissions(resource_id,version));
'''
MAX_STORE_BYTES = 64 * 1024 * 1024


class AssetStore(ExchangeStore):
    version = '2'
    schema = ExchangeStore.schema.replace("('exchange_schema_version', '1')", "('exchange_schema_version', '2')") + ASSET_SCHEMA
    tables = ExchangeStore.tables | {'asset_objects', 'asset_links'}


def open_store(path):
    # New ordinary markets stay on schema 1. Asset schema creation/migration is
    # explicit; opening an existing market never upgrades its structure.
    path = Path(path)
    if path.is_file() and path.stat().st_size:
        with closing(sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
                row = db.execute("SELECT value FROM meta WHERE key='exchange_schema_version'").fetchone()
                if row and row[0] == '2':
                    return AssetStore(path)
    return ExchangeStore(path)


def version_hash(metadata, content):
    payload = compact([metadata, content]) if metadata['kind'] in ('image', 'audio', 'api') or 'origin' in metadata else content
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def media_info(raw, kind):
    from tools.market_asset_prepare import MAX_ASSET, png_info, wav_info
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_ASSET:
        raise MarketError('invalid_asset', '素材必须是受限大小的原始字节。')
    return png_info(raw) if kind == 'image' else wav_info(raw)


def validate_asset_metadata(metadata):
    from .common import choice, integer, text
    import re
    descriptor = metadata.get('asset')
    fields(descriptor, ('sha256', 'bytes', 'media_type', 'media'))
    if not isinstance(descriptor['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', descriptor['sha256']):
        raise MarketError('invalid_asset', '素材摘要无效。')
    integer(descriptor['bytes'], 1, 2097152, 'asset.bytes')
    choice(descriptor['media_type'], ('image/png',) if metadata['kind'] == 'image' else ('audio/wav',), 'media_type')
    rights = metadata.get('rights')
    fields(rights, ('rights_holder', 'attribution', 'modification', 'source_redistribution', 'training', 'restrictions'))
    text(rights['rights_holder'], 'rights_holder', 1200)
    text(rights['restrictions'], 'restrictions', 1200)
    for key in ('modification', 'source_redistribution', 'training'):
        choice(rights[key], ('allowed', 'forbidden', 'unknown'), key)
    fields(rights['attribution'], ('required', 'text'))
    attr = rights['attribution']
    if type(attr['required']) is not bool or (not attr['required'] and attr['text'] != ''):
        raise MarketError('invalid_asset', '署名条件无效。')
    if attr['required']:
        text(attr['text'], 'attribution.text', 1200)
    contract = metadata.get('usage_contract')
    if not contract or contract['version'] != 2 or contract['delivery'] != 'static_asset':
        raise MarketError('invalid_asset', '素材需要v2静态素材契约。')


def verify_bytes(metadata, raw):
    descriptor = metadata['asset']
    info = media_info(raw, metadata['kind'])
    if (descriptor['sha256'] != hashlib.sha256(raw).hexdigest() or descriptor['bytes'] != len(raw)
            or descriptor['media'] != info):
        raise MarketError('integrity_failed', '素材字节与描述不一致。')


def get_asset(db, id, version, metadata):
    row = db.execute('''SELECT a.body FROM asset_links l JOIN asset_objects a ON a.sha256=l.sha256
                        WHERE l.resource_id=? AND l.version=? AND l.sha256=?''',
                     (id, version, metadata['asset']['sha256'])).fetchone()
    if not row:
        raise MarketError('asset_unavailable', '找不到此版本绑定的素材。')
    raw = bytes(row[0])
    verify_bytes(metadata, raw)
    return raw


def put_asset(db, id, version, metadata, raw):
    verify_bytes(metadata, raw)
    sha = metadata['asset']['sha256']
    old = db.execute('SELECT body FROM asset_objects WHERE sha256=?', (sha,)).fetchone()
    if old:
        if bytes(old[0]) != raw:
            raise MarketError('integrity_failed', '已有素材对象损坏。')
    else:
        used = db.execute('SELECT COALESCE(SUM(size),0) FROM asset_objects').fetchone()[0]
        if used + len(raw) > MAX_STORE_BYTES:
            raise MarketError('asset_capacity', '本地素材库达到64MiB候选上限。')
        db.execute('INSERT INTO asset_objects VALUES (?,?,?)', (sha, raw, len(raw)))
    db.execute('INSERT INTO asset_links VALUES (?,?,?)', (id, version, sha))


def validate_objects(db):
    total = 0
    for row in db.execute('SELECT sha256,body,size FROM asset_objects'):
        raw = bytes(row['body'])
        total += len(raw)
        if not 1 <= len(raw) <= 2097152 or len(raw) != row['size'] or hashlib.sha256(raw).hexdigest() != row['sha256']:
            raise MarketError('integrity_failed', '素材对象损坏。')
    if total > MAX_STORE_BYTES:
        raise MarketError('asset_capacity', '素材对象总量超过候选上限。')
    for row in db.execute('''SELECT s.metadata,l.sha256 FROM asset_links l
                            JOIN submissions s USING(resource_id,version)'''):
        meta = parse_json(row['metadata'])
        if meta.get('asset', {}).get('sha256') != row['sha256']:
            raise MarketError('integrity_failed', '素材关联与版本声明不一致。')
