"""来源：本项目原创。目录准入、按需读取与规则检索；不会运行商品脚本。"""

import hashlib
from pathlib import Path
import re
import unicodedata

from .common import MarketError, choice, fields, identifier, integer, parse_json, text


DEFAULT_CATALOG = Path(__file__).with_name('catalog') / 'index.json'
KINDS = ('skill', 'template')
PURPOSES = ('project_local',)
RESOURCE_FIELDS = ('id', 'version', 'kind', 'title', 'summary', 'keywords', 'path', 'sha256',
                   'source', 'license', 'allowed_uses', 'permissions', 'format', 'state',
                   'price_cents', 'currency', 'input', 'output', 'check')


def _search_text(value):
    # Normalize lookup copies only: fullwidth input and nonbreaking spaces should
    # not hide an eligible item. Never rewrite source metadata or release hashes.
    return unicodedata.normalize('NFKC', value).casefold()


class Catalog:
    purposes = PURPOSES
    kinds = KINDS

    def __init__(self, path=DEFAULT_CATALOG):
        self.path = Path(path).resolve()
        self.root = self.path.parent
        raw = self.path.read_bytes()
        if len(raw) > 1_000_000:
            raise MarketError('invalid_catalog', '目录超过本地首版大小限制。')
        manifest = parse_json(raw)
        fields(manifest, ('schema_version', 'resources'))
        if manifest['schema_version'] != 1 or type(manifest['schema_version']) is not int:
            raise MarketError('invalid_catalog', '不支持的目录版本。')
        if not isinstance(manifest['resources'], list) or not 1 <= len(manifest['resources']) <= 1000:
            raise MarketError('invalid_catalog', '目录需要1～1000项资源。')
        self.revision = hashlib.sha256(raw).hexdigest()
        self.items = {}
        for item in manifest['resources']:
            self._validate(item)
            key = item['id'], item['version']
            if key in self.items:
                raise MarketError('invalid_catalog', '商品ID和版本重复。')
            self.items[key] = item

    def _validate(self, item):
        fields(item, RESOURCE_FIELDS)
        identifier(item['id'], 'id')
        identifier(item['version'], 'version')
        choice(item['kind'], KINDS, 'kind')
        choice(item['state'], ('active', 'suspended', 'retired'), 'state')
        choice(item['format'], ('markdown',), 'format')
        choice(item['currency'], ('CNY',), 'currency')
        integer(item['price_cents'], 0, 100_000_000, 'price_cents')
        for key, limit in [('title', 64), ('summary', 180), ('source', 200), ('license', 200),
                           ('input', 400), ('output', 400), ('check', 400)]:
            text(item[key], key, limit)
        if item['source'] != 'project-original' or item['license'] != 'Project-Local':
            raise MarketError('unapproved_source', '首版仅接收本项目原创、Project-Local资源。')
        if item['allowed_uses'] != ['project_local'] or item['permissions'] != []:
            raise MarketError('unapproved_permissions', '首版仅分发本项目内使用的资料，不运行脚本或授予权限。')
        keywords = item['keywords']
        if not isinstance(keywords, list) or not 1 <= len(keywords) <= 30:
            raise MarketError('invalid_catalog', '关键词数量无效。')
        for keyword in keywords:
            text(keyword, 'keyword', 64)
        digest = item['sha256']
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise MarketError('invalid_catalog', '资源校验值无效。')
        self.resource_path(item)

    def resource_path(self, item):
        relative = text(item['path'], 'path', 200)
        # The model only provides catalog IDs. Files are confined to this content root.
        candidate = (self.root / relative).resolve()
        if not candidate.is_relative_to(self.root) or candidate == self.root or candidate.suffix != '.md':
            raise MarketError('invalid_path', '资源路径必须是目录内的Markdown文件。')
        return candidate

    def get(self, resource_id, version, purpose='project_local'):
        identifier(resource_id, 'id')
        identifier(version, 'version')
        choice(purpose, self.purposes, 'purpose')
        item = self.items.get((resource_id, version))
        if not item or item['state'] != 'active':
            raise MarketError('not_found', '资源不可用或不存在。')
        if purpose not in item['allowed_uses']:
            raise MarketError('license_denied', '未获准此用途。')
        return item

    def read(self, item):
        try:
            path = self.resource_path(item)
            if path.stat().st_size > 24_000:
                raise MarketError('content_too_large', '说明超过首版读取上限，需要拆分资源。')
            raw = path.read_bytes()
            if len(raw) > 24_000 or hashlib.sha256(raw).hexdigest() != item['sha256']:
                raise MarketError('integrity_failed', '内容与已登记版本不一致，停止获取。')
            return raw.decode('utf-8')
        except (OSError, UnicodeError) as exc:
            raise MarketError('content_unavailable', '资源文件暂不可读取。') from exc

    def search(self, query, kind=None, limit=3, purpose='project_local'):
        query = _search_text(text(query, 'q', 512))
        integer(limit, 1, 5, 'limit')
        choice(purpose, self.purposes, 'purpose')
        if kind is not None:
            choice(kind, self.kinds, 'kind')
        words = set(re.findall(r'[a-z0-9_-]+', query))
        chinese_terms = set(re.findall(r'[\u3400-\u9fff]{2,}', query))
        ranked = []
        for item in self.items.values():
            if item['state'] != 'active' or purpose not in item['allowed_uses'] or (kind and kind != item['kind']):
                continue
            # Chinese key phrases match within the query; English matches complete words/phrases.
            matches = []
            for tag in item['keywords']:
                tag = _search_text(tag)
                chinese = re.search(r'[\u3400-\u9fff]', tag)
                # Use the same word boundary as the token extractor. Literal
                # substring matching makes "user research" hit "enduser
                # researcher" and outrank actual complete-word matches.
                phrase = (not chinese and ' ' in tag and re.search(
                    r'(?<![a-z0-9_-])' + r' +'.join(re.escape(part) for part in tag.split())
                    + r'(?![a-z0-9_-])', query))
                if (chinese and tag in query) or tag in words or phrase:
                    matches.append(tag)
            score = sum(2 + min(len(tag), 8) for tag in set(matches))
            if not score:
                # Short task terms must also find longer seller labels. Keep
                # this below explicit tag matches; never scan/load full bodies.
                labels = [_search_text(item['title']), *(_search_text(tag) for tag in item['keywords'])]
                label_words = set(re.findall(r'[a-z0-9_-]+', ' '.join(labels)))
                partial = {term for term in chinese_terms if any(term in label for label in labels)}
                partial.update(word for word in words if len(word) >= 2 and word in label_words)
                score = min(len(partial), 2)
            if item['id'] == query:
                score += 100
            if score:
                ranked.append((score, item))
        ranked.sort(key=lambda row: (-row[0], row[1]['id'], row[1]['version']))
        return [item for _, item in ranked[:limit]]
