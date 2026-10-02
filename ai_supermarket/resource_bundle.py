"""来源：本项目原创。向已认证任务交付有界文件包；不执行资源或确认远端收货。"""
import hashlib
import io
import zipfile

from .common import MarketError, compact
from .exchange import PublishedCatalog
from .origin import _safe_path, deliver_origin
from .payment_orders import _hash


MAX_RESOURCE = 2 * 1024 * 1024
SUFFIXES = {'text/markdown': '.md', 'text/csv': '.csv', 'application/pdf': '.pdf',
            'image/png': '.png', 'audio/wav': '.wav'}


def _inspect(host, acquisition_id, value):
    with host.market.store.connect() as db:
        acquired = host.market._owned(db, acquisition_id)
        if [acquired['resource_id'], acquired['version'], acquired['purpose']] != [
                value['id'], value['version'], host.purpose]:
            raise MarketError('purchase_binding_mismatch', '文件必须属于原资源、版本及用途。')
        catalog = PublishedCatalog(db, allow_existing=True)
        item = catalog.get(acquired['resource_id'], acquired['version'], acquired['purpose'])
        catalog.read(item)
        if item['sha256'] != acquired['content_hash']:
            raise MarketError('version_changed', '文件版本与原权益不一致。')
        release = host.market._release_delivery(db, acquired)
    return item, release['release_digest']


def resource_bundle(host, result, value):
    """Return ZIP bytes only for a file already acquired by this task, otherwise None.

    Usage/paid-delivery records describe host receipt. HTTP completion is not a
    client acknowledgement, so this function never queues/sends channel receipts.
    """
    if result.get('status') not in ('host_transfer_required', 'delivered'):
        return None
    acquisition_id = result.get('acquisition_id')
    if acquisition_id is None:
        return None
    item, release_digest = _inspect(host, acquisition_id, value)
    if item['format'] not in ('asset', 'origin'):
        return None
    with host.market.store.connect() as db:
        flow = host._load(db, value['request_id'])
    paid_flow = flow is not None and flow['order_id'] is not None
    if paid_flow and result.get('order_id') != flow['order_id']:
        raise MarketError('purchase_binding_mismatch', '文件交付没有绑定原采购订单。')
    delivery_request = (_hash([host._key(value['request_id']), 'delivery']) if paid_flow
                        else _hash([host.task_id, value['request_id']]))
    if item['format'] == 'asset':
        delivery = host.market.read_asset(acquisition_id, host.task_id, delivery_request)
        raw = delivery['asset_bytes']
        descriptor = item['asset']
    else:
        descriptor = item['origin']
        client = host.clients.get(descriptor['provider'])
        if client is None or host.output_root is None:
            return None
        folder_key = (_hash([host._key(value['request_id']), acquisition_id]) if paid_flow else
                      _hash(['http-file', host.billing._scope, value['request_id'], acquisition_id]))
        delivery = deliver_origin(host.market, acquisition_id, host.task_id, delivery_request,
                                  client, host.output_root / folder_key)
        # Use only the trusted transfer's fixed output, bounded even if a local file changes.
        target = _safe_path(delivery['path'])
        if target.parent != _safe_path(host.output_root / folder_key):
            raise MarketError('invalid_path', '文件不在原请求交付目录。')
        with target.open('rb') as handle:
            raw = handle.read(MAX_RESOURCE + 1)
    digest = hashlib.sha256(raw).hexdigest()
    expected_size = descriptor['bytes'] if item['format'] == 'asset' else descriptor['size']
    if not 0 < len(raw) <= MAX_RESOURCE or len(raw) != expected_size or digest != descriptor['sha256']:
        raise MarketError('integrity_failed', '文件长度或摘要与原发行记录不符。')
    license_bytes = item['license_text'].encode('utf-8')
    if len(license_bytes) > 131072:
        raise MarketError('invalid_size', '许可证超过文件包大小限制。')
    current, current_digest = _inspect(host, acquisition_id, value)
    if current_digest != release_digest or current['license_text'] != item['license_text']:
        raise MarketError('version_changed', '打包期间资源或许可证发生变化。')
    if paid_flow:
        host.acknowledge_delivery(value['request_id'])
    filename = 'resource' + SUFFIXES[descriptor['media_type']]
    manifest = {'format': 'market-resource-zip-v1', 'id': item['id'], 'version': item['version'],
        'acquisition_id': acquisition_id, 'task_id': host.task_id, 'request_id': value['request_id'],
        'usage_id': delivery['usage_id'], 'purpose': host.purpose, 'release_digest': release_digest,
        'resource': {'path': filename, 'media_type': descriptor['media_type'], 'sha256': digest, 'size': len(raw)},
        'license': {'path': 'LICENSE.txt', 'name': item['license_name'],
                    'sha256': hashlib.sha256(license_bytes).hexdigest(), 'size': len(license_bytes)},
        'source_statement': item['source_statement'], 'task_completed': False,
        'execution_authorized': False, 'client_receipt_confirmed': False}
    if 'rights' in item:
        manifest['rights'] = item['rights']
    encoded = compact(manifest).encode('utf-8')
    if len(encoded) > 131072:
        raise MarketError('invalid_size', '文件清单超过大小限制。')
    output = io.BytesIO()
    # Fixed names and ZIP_STORED avoid traversal, executable flags and decompression bombs.
    # Fixed timestamps ensure retries return the same bytes and digest.
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, content in ((filename, raw), ('LICENSE.txt', license_bytes), ('manifest.json', encoded)):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return output.getvalue()
