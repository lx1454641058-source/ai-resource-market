"""本项目原创。卖方宿主二进制提交协议；不向模型传输文件字节，不自动审核。"""
from .common import MarketError, compact, fields, parse_json
from .exchange import validate_draft_reference, validate_listing
from .assets import verify_bytes

ROUTE = '/v1/seller/assets'
MEDIA_TYPE = 'application/vnd.ai-supermarket.asset-v1'
MAGIC = b'AISASSET1\n'
MAX_METADATA = 131072
MAX_ASSET = 2097152
MAX_BODY = len(MAGIC) + 4 + MAX_METADATA + MAX_ASSET


def validate(request, raw):
    fields(request, ('seller', 'slug', 'version', 'metadata', 'content', 'request_id'))
    validate_draft_reference(request['seller'], request['slug'], request['version'])
    from .common import identifier
    identifier(request['request_id'], 'request_id')
    validate_listing(request['metadata'], request['content'])
    if request['metadata']['kind'] not in ('image', 'audio') or not isinstance(raw, bytes):
        raise MarketError('invalid_asset', '仅接受受限PNG/WAV原始字节。')
    verify_bytes(request['metadata'], raw)


def encode(request, raw):
    validate(request, raw)
    metadata = compact(request).encode('utf-8')
    if len(metadata) > MAX_METADATA:
        raise MarketError('invalid_size', '素材声明超过128KiB。')
    return MAGIC + len(metadata).to_bytes(4, 'big') + metadata + raw


def decode(body):
    header = len(MAGIC) + 4
    if not isinstance(body, bytes) or not header < len(body) <= MAX_BODY or not body.startswith(MAGIC):
        raise MarketError('invalid_asset_upload', '素材提交封装无效。')
    size = int.from_bytes(body[len(MAGIC):header], 'big')
    if not 1 <= size <= MAX_METADATA or not 1 <= len(body) - header - size <= MAX_ASSET:
        raise MarketError('invalid_size', '素材声明或文件长度无效。')
    request = parse_json(body[header:header + size])
    raw = body[header + size:]
    validate(request, raw)
    return request, raw


def describe():
    return {'version': 1, 'route': ROUTE, 'content_type': MEDIA_TYPE,
            'max_asset_bytes': MAX_ASSET, 'max_metadata_bytes': MAX_METADATA,
            'result': 'draft_only', 'automatic_review': False}


def receive(gateway, token, body):
    # Observe revocation again after receiving the body and before any mutation.
    market = gateway._interface(token).market
    market._require('seller')
    request, raw = decode(body)
    if request['seller'] != market.principal:
        raise MarketError('forbidden', '素材声明的卖方与凭据身份不符。')
    return market.draft_asset(request['slug'], request['version'], request['metadata'],
                              request['content'], raw, request['request_id'])
