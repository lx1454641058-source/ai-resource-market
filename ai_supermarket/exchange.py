"""来源：本项目原创。卖方准入与买方交付闭环；当前身份由受信宿主注入。"""

from collections import OrderedDict
import hashlib
import json
import re
import time
import uuid

from .catalog import Catalog
from .commerce import MAX_PRICE_CENTS
from .commission_agreements import fee_snapshot, freeze_quote_fee, quote_fee
from .common import MarketError, choice, compact, fields, identifier, integer, parse_json, text
from .exchange_store import ExchangeStore
from .service import Market
from .contracts import HostPolicy, preflight, validate_contract
from .origin import validate_origin
from .api_product import validate_api
from .assets import open_store, version_hash, validate_asset_metadata, get_asset, put_asset


USES = ('personal_use', 'commercial_use')
KINDS = ('skill', 'template', 'image', 'audio', 'api')
ROLES = ('buyer', 'seller', 'reviewer')
META_FIELDS = ('title', 'summary', 'kind', 'keywords', 'input', 'output', 'check',
               'license_name', 'license_text', 'source_statement', 'allowed_uses', 'price_cents')
CHECKS = ('rights_verified', 'sample_checked', 'content_checked', 'contract_checked')


def _release_manifest_digest(resource_id, version, content_hash, metadata):
    """Shared existing release format; price/availability remain separate state."""
    manifest = {'schema': 'market-release-v1', 'id': resource_id, 'version': version,
                'content_hash': content_hash,
                'metadata': {key: value for key, value in metadata.items() if key != 'price_cents'}}
    raw = json.dumps(manifest, ensure_ascii=False, allow_nan=False,
                     sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def document(value, name, limit=24000):
    if not isinstance(value, str) or not value.strip():
        raise MarketError('invalid_params', f'{name}需要非空正文。')
    try:
        raw = value.encode('utf-8')
    except UnicodeError as exc:
        raise MarketError('invalid_params', f'{name}需要有效UTF-8文本。') from exc
    if len(raw) > limit or any(ord(c) < 32 and c not in '\n\r\t' for c in value):
        raise MarketError('invalid_params', f'{name}超过大小限制或含控制字符。')
    return value


def validate_listing(metadata, content):
    fields(metadata, META_FIELDS, ('usage_contract', 'asset', 'rights', 'origin', 'api'))
    if 'usage_contract' in metadata:
        validate_contract(metadata['usage_contract'])
    for name, limit in [('title', 64), ('summary', 180), ('input', 400), ('output', 400),
                        ('check', 400), ('license_name', 100), ('source_statement', 1200)]:
        text(metadata[name], name, limit)
    document(metadata['license_text'], 'license_text', 8000)
    # Custom licenses need explicit reviewer evidence; matching this check is not legal approval.
    if re.search(r'\b(?:a?gpl)(?:\b|[-v0-9])', metadata['license_name'], re.I):
        raise MarketError('unsupported_license', '本项目不引入GPL/AGPL许可商品。')
    choice(metadata['kind'], KINDS, 'kind')
    if metadata['kind'] == 'api' or 'api' in metadata:
        if (metadata['kind'] != 'api' or 'api' not in metadata
                or any(key in metadata for key in ('origin','asset','rights'))
                or metadata.get('usage_contract', {}).get('version') != 4):
            raise MarketError('invalid_params', 'API商品需独立api描述和v4契约，不能混用文件字段。')
        validate_api(metadata['api'])
    elif 'origin' in metadata:
        validate_origin(metadata['origin'])
        if 'asset' in metadata or 'rights' in metadata or metadata.get('usage_contract', {}).get('version') != 3:
            raise MarketError('invalid_params', '原站描述必须使用v3契约，不得混用本地素材字段。')
        expected = {'image': ('image/png',), 'audio': ('audio/wav',),
                    'skill': ('text/markdown',), 'template': ('text/markdown', 'text/csv', 'application/pdf')}
        if metadata['origin']['media_type'] not in expected[metadata['kind']]:
            raise MarketError('invalid_params', '商品类型和原站文件格式不一致。')
    elif metadata['kind'] in ('image', 'audio'):
        validate_asset_metadata(metadata)
    elif 'asset' in metadata or 'rights' in metadata or metadata.get('usage_contract', {}).get('version', 1) != 1:
        raise MarketError('invalid_params', '文本商品不能携带素材字段或素材契约。')
    integer(metadata['price_cents'], 0, MAX_PRICE_CENTS, 'price_cents')
    tags = metadata['keywords']
    if not isinstance(tags, list) or not 1 <= len(tags) <= 30:
        raise MarketError('invalid_params', 'keywords需要1～30项。')
    for tag in tags:
        text(tag, 'keyword', 64)
    uses = metadata['allowed_uses']
    if not isinstance(uses, list) or not uses or len(uses) > len(USES):
        raise MarketError('invalid_params', '需要明确允许的用途。')
    for use in uses:
        choice(use, USES, 'allowed_uses')
    if len(set(uses)) != len(uses):
        raise MarketError('invalid_params', '用途不可重复。')
    document(content, 'content')


def validate_draft_reference(principal, slug, version):
    """Shared by actual drafts and offline preparation; does not authenticate a seller."""
    identifier(principal, 'principal')
    if not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', slug):
        raise MarketError('invalid_params', 'slug需要1～48位小写字母、数字或横线。')
    if not isinstance(version, str) or not re.fullmatch(r'\d{1,6}\.\d{1,6}\.\d{1,6}', version):
        raise MarketError('invalid_params', '版本格式为数字.数字.数字。')
    return identifier(principal + ':' + slug, 'resource_id')


class PublishedCatalog(Catalog):
    """Snapshot from reviewed immutable content; withdrawn items can serve existing rights."""
    purposes = USES
    kinds = KINDS

    def __init__(self, db, allow_existing=False, *, include_content=True):
        self.allow_existing = allow_existing
        self.include_content = include_content
        self.items = {}
        # Search needs metadata and hashes, not every approved document. Keep
        # body-bearing snapshots for acquisition/read integrity verification.
        content_column = ', s.content' if include_content else ''
        rows = db.execute('''SELECT s.resource_id, s.version, s.seller, s.metadata, s.content_hash'''
            + content_column + ''', o.price_cents, o.revision AS offer_revision, o.availability
            FROM submissions s JOIN offers o USING(resource_id,version) WHERE s.state='approved'
            ORDER BY s.resource_id,s.version''').fetchall()
        for row in rows:
            meta = parse_json(row['metadata'])
            item = {**meta, 'id': row['resource_id'], 'version': row['version'], 'seller': row['seller'],
                    'price_cents': row['price_cents'], 'currency': 'CNY', 'sha256': row['content_hash'],
                    'license': meta['license_name'], 'format': 'api' if 'api' in meta else ('origin' if 'origin' in meta else ('asset' if 'asset' in meta else 'markdown')),
                    '_version_metadata': meta,
                    'permissions': meta.get('usage_contract', {}).get('permissions', []),
                    'state': {'active': 'active', 'withdrawn': 'retired', 'suspended': 'suspended'}[row['availability']],
                    'availability': row['availability'], 'offer_revision': row['offer_revision']}
            if include_content:
                item['content'] = row['content']
            self.items[item['id'], item['version']] = item
        # Include offers and statuses: any review, withdrawal or reprice invalidates search cache.
        self.revision = hashlib.sha256(compact([
            [i['id'], i['version'], i['sha256'], i['offer_revision'], i['availability']]
            for i in self.items.values()]).encode()).hexdigest()

    def get(self, resource_id, version, purpose='commercial_use'):
        identifier(resource_id, 'id')
        identifier(version, 'version')
        choice(purpose, self.purposes, 'purpose')
        item = self.items.get((resource_id, version))
        allowed = ('active', 'withdrawn') if self.allow_existing else ('active',)
        if not item or item['availability'] not in allowed:
            raise MarketError('not_found', '商品不可用或不存在。')
        if purpose not in item['allowed_uses']:
            raise MarketError('license_denied', '未获准此用途。')
        return item

    def read(self, item):
        if not self.include_content:
            raise MarketError('invalid_state', '搜索快照没有正文；请通过获取和读取入口加载。')
        content = document(item['content'], 'content')
        if version_hash(item['_version_metadata'], content) != item['sha256']:
            raise MarketError('integrity_failed', '已审核内容与校验值不一致。')
        return content

    def search(self, query, kind=None, limit=3, purpose='commercial_use'):
        # One eligible latest version per product prevents older versions consuming all slots.
        text(query, 'q', 512)
        integer(limit, 1, 5, 'limit')
        choice(purpose, self.purposes, 'purpose')
        if kind is not None:
            choice(kind, KINDS, 'kind')
        latest = {}
        for item in self.items.values():
            if item['state'] == 'active' and purpose in item['allowed_uses'] and (kind is None or item['kind'] == kind):
                previous = latest.get(item['id'])
                if previous is None or tuple(map(int, item['version'].split('.'))) > tuple(map(int, previous['version'].split('.'))):
                    latest[item['id']] = item
        # Rank the reduced catalog, rather than filtering the first five old versions after ranking.
        original = self.items
        try:
            self.items = {(item['id'], item['version']): item for item in latest.values()}
            return super().search(query, kind, limit, purpose)
        finally:
            self.items = original


class Exchange(Market):
    kinds = KINDS
    purposes = USES
    default_purpose = 'commercial_use'

    def __init__(self, database, principal, role='buyer', clock=None, host_policy=None):
        self.principal = identifier(principal, 'principal')
        self.role = choice(role, ROLES, 'role')
        self.host_policy = HostPolicy() if host_policy is None else HostPolicy.from_json(host_policy)
        self.clock = clock or time.time
        self.store = open_store(database)
        self.cache = OrderedDict()

    def _require(self, role):
        if self.role != role:
            raise MarketError('forbidden', '此入口没有该角色权限。')

    def api_receipts(self,id,version,after=0,limit=20):
        from .supplier_receipts import api_receipts
        return api_receipts(self,id,version,after,limit)

    def _catalog(self):
        with self.store.connect() as db:
            return PublishedCatalog(db, allow_existing=True)

    def _search_catalog(self):
        with self.store.connect() as db:
            return PublishedCatalog(db, include_content=False)

    @staticmethod
    def _release_digest(db, resource_id, version):
        row = db.execute('SELECT metadata,content_hash FROM submissions WHERE resource_id=? AND version=?',
                         (resource_id, version)).fetchone()
        if row is None:
            raise MarketError('not_found', '找不到目标版本。')
        metadata = parse_json(row['metadata'])
        # Pricing and availability are mutable offer state, checked separately.
        # Sort object keys, preserve array order and exact Unicode/text values.
        return _release_manifest_digest(resource_id, version, row['content_hash'], metadata)

    def _check_release_binding(self, db, acquired):
        # Pinned IDs are marked so removing their audit binding cannot downgrade
        # them to legacy rights. IDs remain opaque to callers.
        if not acquired['id'].startswith('ap_'):
            return None
        audit = db.execute('SELECT * FROM exchange_audit WHERE id=?',
                           ('e_' + acquired['id'],)).fetchone()
        if (not audit or audit['actor'] != acquired['principal'] or audit['action'] != 'acquire'
                or audit['resource_id'] != acquired['resource_id'] or audit['version'] != acquired['version']):
            raise MarketError('integrity_failed', '发行绑定记录缺失或不匹配，停止读取。')
        detail = parse_json(audit['detail'])
        digest = detail.get('release_digest') if isinstance(detail, dict) else None
        if (not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)
                or detail.get('acquisition_id') != acquired['id'] or detail.get('purpose') != acquired['purpose']):
            raise MarketError('integrity_failed', '发行绑定记录无效，停止读取。')
        if digest != self._release_digest(db, acquired['resource_id'], acquired['version']):
            raise MarketError('release_changed', '版本正文或使用条件已变化，停止读取。')
        if acquired['id'].startswith('ap_pay_'):
            from .payment_fulfillment import check_paid_acquisition
            check_paid_acquisition(db, acquired)
        return digest

    def _owned(self, db, acquisition_id):
        acquired = super()._owned(db, acquisition_id)
        self._check_release_binding(db, acquired)
        return acquired

    def _release_delivery(self, db, acquired):
        result = self._delivery(acquired)
        digest = self._check_release_binding(db, acquired)
        if digest is not None:
            result.update(release_digest=digest, release_binding='expected_release_v1')
        return result

    def _delivery(self, row):
        result = super()._delivery(row)
        if row['id'].startswith('ap_pay_'):
            result['payment'] = 'verified_by_host'
        return result

    def _mutation(self, action, request_id, payload, change):
        identifier(request_id, 'request_id')
        fingerprint = hashlib.sha256(compact([action, payload]).encode()).hexdigest()
        with self.store.connect(write=True) as db:
            previous = db.execute('SELECT * FROM exchange_receipts WHERE actor=? AND request_id=?',
                                  (self.principal, request_id)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise MarketError('idempotency_conflict', '该请求编号已用于不同操作或参数。')
                return {**parse_json(previous['response']), 'replayed': True}
            reply, detail = change(db)
            db.execute('INSERT INTO exchange_receipts VALUES (?,?,?,?,?)',
                       (self.principal, request_id, fingerprint, compact(reply), self.clock()))
            db.execute('INSERT INTO exchange_audit VALUES (?,?,?,?,?,?,?)',
                       ('e_' + uuid.uuid4().hex, self.principal, action, reply.get('id'), reply.get('version'),
                        compact(detail), self.clock()))
            return reply

    def _submission(self, db, resource_id, version, own=True):
        identifier(resource_id, 'id')
        identifier(version, 'version')
        row = db.execute('SELECT * FROM submissions WHERE resource_id=? AND version=?',
                         (resource_id, version)).fetchone()
        if not row or (own and row['seller'] != self.principal):
            raise MarketError('not_found', '找不到本主体的商品。')
        return row

    @staticmethod
    def _submission_reply(row, include_content=False):
        result = {'status': row['state'], 'id': row['resource_id'], 'version': row['version'],
                  'revision': row['revision'], 'review_note': row['review_note'],
                  'metadata': parse_json(row['metadata']), 'content_hash': row['content_hash']}
        if include_content:
            result['content'] = row['content']
        return result

    def draft(self, slug, version, metadata, content, request_id):
        self._require('seller')
        resource_id = validate_draft_reference(self.principal, slug, version)
        validate_listing(metadata, content)
        if metadata['kind'] in ('image', 'audio') and 'origin' not in metadata:
            raise MarketError('host_transfer_required', '素材必须由可信本地导入入口绑定原始字节。')

        def change(db):
            if db.execute('SELECT 1 FROM submissions WHERE resource_id=? AND version=?', (resource_id, version)).fetchone():
                raise MarketError('version_exists', '该版本已存在；草稿用修订操作，已批准内容需新版本。')
            digest = version_hash(metadata, content)
            db.execute('INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (resource_id, version, self.principal, compact(metadata), content, digest,
                        1, 'draft', None, self.clock(), self.clock()))
            return ({'status': 'draft', 'id': resource_id, 'version': version, 'revision': 1}, {'revision': 1})

        return self._mutation('draft', request_id, [slug, version, metadata, content], change)

    def draft_asset(self, slug, version, metadata, content, raw, request_id):
        """Local importer or authenticated host upload; binary stays out of model arguments."""
        self._require('seller')
        resource_id = validate_draft_reference(self.principal, slug, version)
        validate_listing(metadata, content)
        if 'origin' in metadata:
            raise MarketError('host_transfer_required', '原站商品通过声明接口提交，不复制到素材账本。')
        if metadata['kind'] not in ('image', 'audio') or self.store.version != '2':
            raise MarketError('asset_upgrade_required', '素材取入需要显式创建或复制升级的v2开放市场。')
        from .assets import verify_bytes
        verify_bytes(metadata, raw)

        def change(db):
            if db.execute('SELECT 1 FROM submissions WHERE resource_id=? AND version=?', (resource_id, version)).fetchone():
                raise MarketError('version_exists', '素材版本已存在；修订内容需导入新版本。')
            db.execute('INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (resource_id, version, self.principal, compact(metadata), content,
                        version_hash(metadata, content), 1, 'draft', None, self.clock(), self.clock()))
            put_asset(db, resource_id, version, metadata, raw)
            return ({'status': 'draft', 'id': resource_id, 'version': version, 'revision': 1,
                     'asset_sha256': metadata['asset']['sha256']}, {'revision': 1, 'binary_bound': True})

        return self._mutation('draft_asset', request_id, [slug, version, metadata, content], change)

    def inspect_asset(self, id, version):
        """Trusted local seller/reviewer inspection; public inspection stays metadata-only."""
        if self.role not in ('seller', 'reviewer'):
            raise MarketError('forbidden', '需要卖方或审核身份。')
        with self.store.connect() as db:
            row = self._submission(db, id, version, own=self.role != 'reviewer')
            metadata = parse_json(row['metadata'])
            if 'asset' not in metadata:
                raise MarketError('not_asset', '该版本不是素材。')
            return get_asset(db, id, version, metadata)

    def read(self, acquisition_id, task_id, request_id):
        identifier(task_id, 'task_id')
        identifier(request_id, 'request_id')
        with self.store.connect() as db:
            acquired = self._owned(db, acquisition_id)
            item = PublishedCatalog(db, allow_existing=True).get(
                acquired['resource_id'], acquired['version'], acquired['purpose'])
            if 'api' in item:
                PublishedCatalog(db, allow_existing=True).read(item)
                release = self._check_release_binding(db, acquired)
                if release is None or item['sha256'] != acquired['content_hash']:
                    raise MarketError('release_binding_required', 'API权益必须绑定原发行。')
                return {'status': 'host_transfer_required', 'acquisition_id': acquisition_id,
                        'api': item['api'], 'release_digest': release, 'resource_protocol_version': 4,
                        'next_action': 'trusted_host_invoke_api', 'content_loaded': False,
                        'task_completed': False}
            if 'origin' in item:
                PublishedCatalog(db, allow_existing=True).read(item)
                self._release_delivery(db, acquired)
                if item['sha256'] != acquired['content_hash']:
                    raise MarketError('version_changed', '原站资源版本已变化。')
                return {'status': 'host_transfer_required', 'acquisition_id': acquisition_id,
                        'origin': item['origin'], 'resource_protocol_version': 3,
                        'next_action': 'trusted_host_deliver_origin', 'content_loaded': False}
            if 'asset' in item:
                if item['sha256'] != acquired['content_hash']:
                    raise MarketError('version_changed', '素材版本已变化。')
                PublishedCatalog(db, allow_existing=True).read(item)
                get_asset(db, item['id'], item['version'], item)
                return {'status': 'host_transfer_required', 'acquisition_id': acquisition_id,
                        'asset': item['asset'], 'resource_protocol_version': 2,
                        'next_action': 'trusted_host_read_asset', 'content_loaded': False}
        return super().read(acquisition_id, task_id, request_id)

    def read_asset(self, acquisition_id, task_id, request_id):
        """Trusted host only. Caller must remove asset_bytes before any JSON/model output."""
        self._require('buyer')
        identifier(task_id, 'task_id')
        identifier(request_id, 'request_id')
        with self.store.connect(write=True) as db:
            acquired = self._owned(db, acquisition_id)
            catalog = PublishedCatalog(db, allow_existing=True)
            item = catalog.get(acquired['resource_id'], acquired['version'], acquired['purpose'])
            catalog.read(item)
            if 'asset' not in item or item['sha256'] != acquired['content_hash']:
                raise MarketError('version_changed', '素材类型或版本不一致。')
            compatibility = preflight(item['usage_contract'], self.host_policy)
            if compatibility['status'] != 'ready':
                raise MarketError(compatibility['status'], '宿主不能接收此素材。')
            raw = get_asset(db, item['id'], item['version'], item)
            old = db.execute('SELECT * FROM usage_events WHERE principal=? AND task_id=? AND request_id=?',
                             (self.principal, task_id, request_id)).fetchone()
            if old and old['acquisition_id'] != acquisition_id:
                raise MarketError('idempotency_conflict', '读取编号已用于其他资源。')
            usage_id = old['id'] if old else 'u_' + uuid.uuid4().hex
            if not old:
                db.execute('INSERT INTO usage_events VALUES (?,?,?,?,?,?)',
                           (usage_id, acquisition_id, self.principal, task_id, request_id, self.clock()))
        return {'status': 'read', 'format': 'asset', 'usage_id': usage_id, 'asset': item['asset'],
                'asset_bytes': raw, 'license': item['license'], 'evidence': 'delivery_only'}

    def status(self, acquisition_id):
        try:
            result = super().status(acquisition_id)
        except MarketError as error:
            if error.code not in ('release_changed', 'integrity_failed'):
                raise
            return {'status': 'unavailable', 'error_code': error.code,
                    'acquisition_id': acquisition_id, 'next_action': 'stop'}
        if result['status'] == 'acquired':
            try:
                with self.store.connect() as db:
                    acquired = self._owned(db, acquisition_id)
                    result = self._release_delivery(db, acquired)
                    item = PublishedCatalog(db, allow_existing=True).get(
                        acquired['resource_id'], acquired['version'], acquired['purpose'])
                    if 'asset' in item:
                        get_asset(db, item['id'], item['version'], item)
            except MarketError as error:
                return {'status': 'unavailable', 'error_code': error.code, 'acquisition_id': acquisition_id}
        return result

    def revise(self, id, version, revision, metadata, content, request_id):
        self._require('seller')
        integer(revision, 1, 1_000_000, 'revision')
        validate_listing(metadata, content)
        if metadata['kind'] in ('image', 'audio') and 'origin' not in metadata:
            raise MarketError('host_transfer_required', '素材修订使用新的版本导入，不经文本接口覆盖。')

        def change(db):
            row = self._submission(db, id, version)
            if 'asset' in parse_json(row['metadata']):
                raise MarketError('host_transfer_required', '素材版本不能转换为文本商品。')
            if row['revision'] != revision:
                raise MarketError('revision_conflict', '草稿已变化，请读取最新版本。')
            if row['state'] not in ('draft', 'rejected'):
                raise MarketError('invalid_state', '待审和已批准版本不可修改正文。')
            db.execute('''UPDATE submissions SET metadata=?,content=?,content_hash=?,revision=revision+1,
                state='draft',review_note=NULL,updated_at=? WHERE resource_id=? AND version=?''',
                       (compact(metadata), content, version_hash(metadata, content), self.clock(), id, version))
            return ({'status': 'draft', 'id': id, 'version': version, 'revision': revision + 1}, {'revision': revision + 1})

        return self._mutation('revise', request_id, [id, version, revision, metadata, content], change)

    def submit(self, id, version, revision, request_id):
        self._require('seller')
        integer(revision, 1, 1_000_000, 'revision')

        def change(db):
            row = self._submission(db, id, version)
            if row['revision'] != revision:
                raise MarketError('revision_conflict', '请提交当前草稿版本。')
            if row['state'] != 'draft':
                raise MarketError('invalid_state', '只有草稿可提交审核。')
            meta = parse_json(row['metadata'])
            validate_listing(meta, row['content'])
            if 'asset' in meta:
                get_asset(db, id, version, meta)
            if 'usage_contract' not in meta:
                raise MarketError('contract_required', '草稿需要使用契约后才能提交审核。')
            db.execute("UPDATE submissions SET state='pending_review',updated_at=? WHERE resource_id=? AND version=?",
                       (self.clock(), id, version))
            return ({'status': 'pending_review', 'id': id, 'version': version, 'revision': revision}, {'revision': revision})

        return self._mutation('submit', request_id, [id, version, revision], change)

    def inspect_submission(self, id, version):
        if self.role not in ('seller', 'reviewer'):
            raise MarketError('forbidden', '需要卖方或审核入口。')
        with self.store.connect() as db:
            row = self._submission(db, id, version, own=self.role != 'reviewer')
            result = self._submission_reply(row, include_content=True)
            offer = db.execute('SELECT price_cents,revision,availability FROM offers WHERE resource_id=? AND version=?',
                               (id, version)).fetchone()
        result['offer'] = dict(offer) if offer else None
        return result

    def list_submissions(self, state=None, offset=0, limit=20):
        if self.role not in ('seller', 'reviewer'):
            raise MarketError('forbidden', '需要卖方或审核入口。')
        integer(offset, 0, 1_000_000, 'offset')
        integer(limit, 1, 50, 'limit')
        terms, params = [], []
        if self.role == 'seller':
            terms.append('seller=?')
            params.append(self.principal)
        if state is not None:
            choice(state, ('draft', 'pending_review', 'approved', 'rejected'), 'state')
            terms.append('state=?')
            params.append(state)
        where = ' WHERE ' + ' AND '.join(terms) if terms else ''
        with self.store.connect() as db:
            rows = db.execute('SELECT resource_id,version,seller,state,revision,updated_at FROM submissions' + where
                              + ' ORDER BY created_at,resource_id,version LIMIT ? OFFSET ?', [*params, limit + 1, offset]).fetchall()
        return {'status': 'ok', 'items': [dict(row) for row in rows[:limit]],
                'next_offset': offset + limit if len(rows) > limit else None}

    def review(self, id, version, revision, decision, checks, note, request_id):
        self._require('reviewer')
        integer(revision, 1, 1_000_000, 'revision')
        choice(decision, ('approve', 'reject'), 'decision')
        fields(checks, CHECKS)
        if any(type(checks[key]) is not bool for key in CHECKS):
            raise MarketError('invalid_params', '审核证据必须是布尔值。')
        text(note, 'note', 1200)
        if decision == 'approve' and not all(checks.values()):
            raise MarketError('review_incomplete', '批准前需确认来源许可、样例和内容检查。')

        def change(db):
            row = self._submission(db, id, version, own=False)
            if row['seller'] == self.principal:
                raise MarketError('self_review', '不能批准或审核自己提交的商品。')
            if row['state'] != 'pending_review' or row['revision'] != revision:
                raise MarketError('revision_conflict', '待审状态或草稿版本已变化。')
            meta = parse_json(row['metadata'])
            validate_listing(meta, row['content'])
            if 'asset' in meta:
                get_asset(db, id, version, meta)
            if decision == 'approve' and 'usage_contract' not in meta:
                raise MarketError('contract_required', '不能批准缺少使用契约的商品。')
            state = 'approved' if decision == 'approve' else 'rejected'
            db.execute('UPDATE submissions SET state=?,review_note=?,updated_at=? WHERE resource_id=? AND version=?',
                       (state, note, self.clock(), id, version))
            if decision == 'approve':
                db.execute('INSERT INTO offers VALUES (?,?,?,?,?,?)',
                           (id, version, meta['price_cents'], 1, 'active', self.clock()))
            return ({'status': state, 'id': id, 'version': version, 'revision': revision},
                    {'decision': decision, 'checks': checks, 'note': note, 'revision': revision})

        return self._mutation('review', request_id, [id, version, revision, decision, checks, note], change)

    def reprice(self, id, version, offer_revision, price_cents, request_id):
        self._require('seller')
        integer(offer_revision, 1, 1_000_000, 'offer_revision')
        integer(price_cents, 0, MAX_PRICE_CENTS, 'price_cents')

        def change(db):
            self._submission(db, id, version)
            row = db.execute('SELECT * FROM offers WHERE resource_id=? AND version=?', (id, version)).fetchone()
            if not row or row['revision'] != offer_revision:
                raise MarketError('revision_conflict', '报价版本不匹配。')
            if row['availability'] == 'suspended':
                raise MarketError('invalid_state', '隔离商品需先由运营处理。')
            db.execute('UPDATE offers SET price_cents=?,revision=revision+1,updated_at=? WHERE resource_id=? AND version=?',
                       (price_cents, self.clock(), id, version))
            return ({'status': 'repriced', 'id': id, 'version': version, 'offer_revision': offer_revision + 1,
                     'price_cents': price_cents}, {'old_cents': row['price_cents'], 'new_cents': price_cents})

        return self._mutation('reprice', request_id, [id, version, offer_revision, price_cents], change)

    def availability(self, id, version, offer_revision, state, reason, request_id):
        choice(state, ('active', 'withdrawn', 'suspended'), 'state')
        integer(offer_revision, 1, 1_000_000, 'offer_revision')
        text(reason, 'reason', 1200)
        if self.role not in ('seller', 'reviewer') or (self.role == 'seller' and state != 'withdrawn'):
            raise MarketError('forbidden', '卖方仅可主动停售；恢复或隔离由审核角色处理。')

        def change(db):
            submission = self._submission(db, id, version, own=self.role == 'seller')
            if self.role == 'reviewer' and state == 'active' and submission['seller'] == self.principal:
                raise MarketError('self_review', '不能自行恢复自己商品的公开状态。')
            row = db.execute('SELECT * FROM offers WHERE resource_id=? AND version=?', (id, version)).fetchone()
            if not row or row['revision'] != offer_revision:
                raise MarketError('revision_conflict', '商品状态版本已变化。')
            if self.role == 'seller' and row['availability'] == 'suspended':
                raise MarketError('forbidden', '卖方不能覆盖平台隔离状态。')
            db.execute('UPDATE offers SET availability=?,revision=revision+1,updated_at=? WHERE resource_id=? AND version=?',
                       (state, self.clock(), id, version))
            return ({'status': state, 'id': id, 'version': version, 'offer_revision': offer_revision + 1},
                    {'from': row['availability'], 'to': state, 'reason': reason})

        return self._mutation('availability', request_id, [id, version, offer_revision, state, reason], change)

    def search(self, q, kind=None, limit=3, purpose='commercial_use'):
        return super().search(q, kind, limit, purpose)

    def check_compatibility(self, id, version, purpose='commercial_use'):
        """Trusted host preflight also supports legitimately owned withdrawn versions."""
        return self._host_preflight(id, version, purpose)[0]

    def _host_preflight(self, id, version, purpose):
        """Keep conditions, ownership and their digest in one read snapshot."""
        with self.store.connect() as db:
            db.execute('BEGIN')
            item = PublishedCatalog(db, allow_existing=True, include_content=False).get(id, version, purpose)
            if item['availability'] != 'active' and not db.execute('''SELECT 1 FROM acquisitions
                    WHERE principal=? AND resource_id=? AND version=? AND purpose=? AND content_hash=?''',
                    (self.principal, id, version, purpose, item['sha256'])).fetchone():
                raise MarketError('not_found', '已停售商品不可新获取。')
            return (preflight(item.get('usage_contract'), self.host_policy),
                    self._release_digest(db, id, version))

    def resolve(self, id, version, purpose='commercial_use'):
        # Public details only include approved active offers, never the draft or content body.
        with self.store.connect() as db:
            db.execute('BEGIN')
            item = PublishedCatalog(db).get(id, version, purpose)
            release_digest = self._release_digest(db, id, version)
            fees = fee_snapshot(db, item['seller'], item['price_cents'], self.clock())
            owned = db.execute('''SELECT id,created_at FROM acquisitions WHERE principal=? AND resource_id=?
                AND version=? AND purpose=? AND content_hash=? ORDER BY created_at,id LIMIT 1''',
                               (self.principal, id, version, purpose, item['sha256'])).fetchone()
        result = self._resolved(item)
        result.update(content_hash=item['sha256'], release_digest=release_digest,
                      release_digest_schema='market-release-v1')
        result.update({'seller': item['seller'], 'license_text': item['license_text'],
                       'source_statement': item['source_statement'], 'offer_revision': item['offer_revision'],
                       'training_allowed': False, 'billing': 'one_time_version_use',
                       'fee_policy': fees})
        result['usage_contract'] = item.get('usage_contract')
        if 'api' in item:
            result.update(api=item['api'], host_transfer_required=True, resource_protocol_version=4,
                          billing='prepaid_call_bundle', renewal_supported=False)
        if 'origin' in item:
            result.update(origin=item['origin'], host_transfer_required=True, resource_protocol_version=3)
        if 'asset' in item:
            result.update(asset=item['asset'], rights=item['rights'], host_transfer_required=True,
                          resource_protocol_version=2)
        result['compatibility'] = preflight(item.get('usage_contract'), self.host_policy)
        result['permission_semantics'] = 'requested_not_granted'
        result['owned'] = bool(owned)
        if owned:
            result.update({'acquisition_id': owned['id'], 'next_action': 'read'})
            if 'api' in item:
                expires = int(owned['created_at']) + item['api']['validity_seconds']
                result.update(entitlement_expires_at=expires,
                              entitlement_state='expired' if self.clock() >= expires else 'within_validity',
                              next_action='stop_no_automatic_renewal' if self.clock() >= expires else 'trusted_host_invoke_api')
        if result['compatibility']['status'] != 'ready':
            result['next_action'] = 'stop_or_choose_other_resource'
        return result

    def quote(self, id, version, purpose, request_id):
        choice(purpose, USES, 'purpose')

        def change(db):
            item = PublishedCatalog(db).get(id, version, purpose)
            quote_id = 'q_' + uuid.uuid4().hex
            now = self.clock()
            fees = fee_snapshot(db, item['seller'], item['price_cents'], now)
            expires = min(now + 300, fees.get('valid_until') or now + 300)
            db.execute('INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (quote_id, self.principal, id, version, purpose, item['sha256'], item['offer_revision'],
                        item['price_cents'], fees['policy_version'], now, expires))
            freeze_quote_fee(db, quote_id, fees)
            reply = {'status': 'quoted', 'quote_id': quote_id, 'id': id, 'version': version,
                     'purpose': purpose, 'price_cents': item['price_cents'], 'currency': 'CNY',
                     'expires_at': expires, 'offer_revision': item['offer_revision'],
                     'fee_policy': fees, 'real_payments': False}
            return reply, {'quote_id': quote_id, 'price_cents': item['price_cents']}

        return self._mutation('quote', request_id, [id, version, purpose], change)

    def acquire(self, id, version, request_id, purpose='commercial_use', quote_id=None, expected_release_digest=None):
        identifier(request_id, 'request_id')
        if expected_release_digest is not None and (not isinstance(expected_release_digest, str)
                or not re.fullmatch('[0-9a-f]{64}', expected_release_digest)):
            raise MarketError('invalid_params', '预期发行摘要需要64位小写十六进制值。')
        if quote_id is not None:
            identifier(quote_id, 'quote_id')
        with self.store.connect(write=True) as db:
            catalog = PublishedCatalog(db, allow_existing=True)
            item = catalog.get(id, version, purpose)
            catalog.read(item)
            if 'api' in item and expected_release_digest is None:
                raise MarketError('release_binding_required', '获取API套餐需明确原发行摘要。')
            if 'asset' in item:
                get_asset(db, id, version, item)
            if (expected_release_digest is not None
                    and expected_release_digest != self._release_digest(db, id, version)):
                raise MarketError('release_changed', '取用前版本正文或使用条件已变化，请重新核对。')
            # Repeated paid acquisition points to the original entitlement. Keep
            # request idempotency without creating an alias that loses paid lineage.
            paid_request_key = 'paid_acquire_request:' + hashlib.sha256(compact(
                [self.principal, request_id]).encode('utf-8')).hexdigest()
            paid_fingerprint = hashlib.sha256(compact(
                [id, version, purpose, expected_release_digest]).encode('utf-8')).hexdigest()
            paid_reuse = db.execute('SELECT value FROM meta WHERE key=?', (paid_request_key,)).fetchone()
            if paid_reuse:
                pointer = parse_json(paid_reuse[0])
                fields(pointer, ('fingerprint', 'acquisition_id'))
                if pointer['fingerprint'] != paid_fingerprint:
                    raise MarketError('idempotency_conflict', '该请求已用于不同的已付资源或发行绑定。')
                return self._release_delivery(db, self._owned(db, pointer['acquisition_id']))
            old = db.execute('SELECT * FROM acquisitions WHERE principal=? AND request_id=?',
                             (self.principal, request_id)).fetchone()
            if old:
                if (old['resource_id'], old['version'], old['purpose'], old['content_hash']) != (id, version, purpose, item['sha256']):
                    raise MarketError('idempotency_conflict', '同一请求不能更换资源或用途。')
                if self._check_release_binding(db, old) != expected_release_digest:
                    raise MarketError('idempotency_conflict', '重放请求不能添加、更改或移除发行绑定。')
                return self._release_delivery(db, old)
            owned = db.execute('''SELECT * FROM acquisitions WHERE principal=? AND resource_id=?
                AND version=? AND purpose=? AND content_hash=? LIMIT 1''',
                               (self.principal, id, version, purpose, item['sha256'])).fetchone()
            if owned:
                self._check_release_binding(db, owned)
                # API quota belongs to one original entitlement, including free
                # bundles. A new acquire request cannot mint a new quota alias.
                if owned['id'].startswith('ap_pay_') or 'api' in item:
                    db.execute('INSERT INTO meta VALUES (?,?)', (paid_request_key, compact(
                        {'fingerprint':paid_fingerprint, 'acquisition_id':owned['id']})))
                    return self._release_delivery(db, owned)
            if not owned:
                compatibility = preflight(item.get('usage_contract'), self.host_policy)
                if compatibility['status'] != 'ready':
                    raise MarketError(compatibility['status'], '商品使用条件未满足；查看resolve兼容说明，不能由商品扩大授权。')
                if item['availability'] != 'active':
                    raise MarketError('not_found', '已停售商品不可新获取。')
                if quote_id is not None:
                    quote = db.execute('SELECT * FROM quotes WHERE id=? AND buyer=?', (quote_id, self.principal)).fetchone()
                    if not quote or (quote['resource_id'], quote['version'], quote['purpose'], quote['content_hash']) != (id, version, purpose, item['sha256']):
                        raise MarketError('invalid_quote', '报价不属于本主体或目标资源。')
                    if quote['expires_at'] <= self.clock():
                        raise MarketError('quote_expired', '报价已过期。')
                    if quote['offer_revision'] != item['offer_revision']:
                        raise MarketError('quote_changed', '报价已变化，请重新查看。')
                    quote_fee(db, quote, item['seller'], timestamp=self.clock())
                if item['price_cents']:
                    return {'status': 'payment_unavailable', 'id': id, 'version': version,
                            'price_cents': item['price_cents'], 'real_payment': False,
                            'next_action': 'choose_free_resource'}
            acquisition_id = ('ap_' if expected_release_digest is not None else 'a_') + uuid.uuid4().hex
            db.execute('INSERT INTO acquisitions VALUES (?,?,?,?,?,?,?,?)',
                       (acquisition_id, self.principal, request_id, id, version, purpose, item['sha256'], self.clock()))
            detail = {'acquisition_id': acquisition_id, 'reused': bool(owned), 'purpose': purpose}
            if expected_release_digest is not None:
                detail['release_digest'] = expected_release_digest
            audit_id = 'e_' + acquisition_id if expected_release_digest is not None else 'e_' + uuid.uuid4().hex
            db.execute('INSERT INTO exchange_audit VALUES (?,?,?,?,?,?,?)',
                       (audit_id, self.principal, 'acquire', id, version, compact(detail), self.clock()))
            return self._release_delivery(db, db.execute('SELECT * FROM acquisitions WHERE id=?', (acquisition_id,)).fetchone())

    def pricing_reference(self, kind='template'):
        choice(kind, KINDS, 'kind')
        return {'status': 'insufficient_evidence', 'kind': kind, 'reference_cents': None,
                'mandatory': False, 'reason': '没有足够经核实的独立成交，不能用挂牌价冒充市场成交价。',
                'commission_bps': 800, 'stall_fee_cents': 0,
                'method': '比较相同计费单位、用途许可及维护承诺；成本和需求由卖家自行评估。'}
