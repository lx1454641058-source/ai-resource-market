"""来源：本项目原创。供应商接入示例；仅组合既有卖方接口，不是新市场协议。

供应商明确授权导出的静态正文会复制到市场。此示例不支持原站保留正文的
远程履约、不执行支付、不自动审核、不根据目录缺项删除或停售商品。
"""

import hashlib
import ipaddress
import math
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from ai_supermarket.common import MarketError, compact, identifier, parse_json
from ai_supermarket.http_client import NoRedirect
from tools.market_seller import check_request


MAX_FEED_BYTES = 1_048_576
MAX_ITEMS = 16


def fetch_batch(url, timeout=10):
    """Read one exact host-configured URL; never follow redirects or item links.

    The host must choose and approve the supplier endpoint, not the model or feed.
    Feed URLs containing credentials are unsupported. No existing credentials,
    browser sessions, environment proxies or seller tokens are forwarded.
    """
    if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise MarketError('invalid_endpoint', '供应商地址无效。')
    try:
        parsed = urlsplit(url)
        port = parsed.port
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or port == 0):
            raise ValueError
        if parsed.scheme == 'http' and not ipaddress.ip_address(parsed.hostname).is_loopback:
            raise ValueError
    except ValueError:
        raise MarketError('invalid_endpoint', '需宿主批准的HTTPS地址；HTTP仅限本机IP，禁止凭据、查询和片段。') from None
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise MarketError('invalid_params', '等待时间须大于0且不超过30秒。')
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(Request(url, headers={'Accept': 'application/json'}), timeout=timeout) as response:
            if response.status != 200 or response.headers.get_content_type() != 'application/json':
                raise MarketError('invalid_feed', '供应商未返回200 JSON目录。')
            if response.headers.get('Content-Encoding', 'identity') != 'identity':
                raise MarketError('invalid_feed', '示例不接受压缩目录。')
            raw = response.read(MAX_FEED_BYTES + 1)
    except HTTPError as error:
        error.close()
        raise MarketError('feed_unavailable', '供应商请求失败或要求跳转；未发送商品到超市。') from None
    except (URLError, OSError):
        raise MarketError('feed_unavailable', '供应商未返回完整目录；未发送商品到超市。') from None
    if len(raw) > MAX_FEED_BYTES:
        raise MarketError('invalid_size', '目录超过1MiB。')
    return parse_json(raw)


def prepare_batch(requests, seller):
    """Validate the entire batch before any market write; freeze a JSON copy.

    Each element is an EXISTING seller_draft request including a stable request_id.
    Changing the content of a published version requires a new version; callers
    retain this exact batch for recovery, rather than generating new request IDs.
    """
    identifier(seller, 'seller')
    try:
        raw = compact(requests).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise MarketError('invalid_json', '目录需要有效JSON。') from None
    if len(raw) > MAX_FEED_BYTES:
        raise MarketError('invalid_size', '目录超过1MiB。')
    requests = parse_json(raw)
    if not isinstance(requests, list) or not 1 <= len(requests) <= MAX_ITEMS:
        raise MarketError('invalid_feed', '目录须为1～16条现有seller_draft请求的数组。')
    resources, keys = set(), set()
    for index, request in enumerate(requests):
        result = check_request(request, seller)
        if result['status'] != 'checked':
            raise MarketError('invalid_listing', f'第{index + 1}件商品未通过现有字段和契约检查。')
        resource = (request['slug'], request['version'])
        if resource in resources or request['request_id'] in keys:
            raise MarketError('duplicate_listing', '同批商品版本或请求编号重复。')
        resources.add(resource)
        keys.add(request['request_id'])
    return requests


def submit_batch(requests, seller, seller_client):
    """Compose seller_draft/submit/status through a host-bound local or remote client.

    This is not a distributed transaction: an interrupted batch can be partial.
    Never retry automatically. The caller keeps the original batch and receipts.
    Client exceptions and bodies are not echoed into the compact result.
    """
    requests = prepare_batch(requests, seller)
    completed = []
    result = {'status': 'submitted', 'items': completed, 'payment_available': False,
              'review_performed': False, 'atomic_batch': False}
    for index, request in enumerate(requests):
        resource = seller + ':' + request['slug']
        version = request['version']
        stage = 'seller_draft'
        try:
            reply = seller_client.call(stage, request)
            if reply.get('status') != 'draft':
                raise MarketError('operation_not_confirmed', '未确认草稿。')
            if reply.get('id') != resource or reply.get('version') != version or reply.get('revision') != 1:
                raise MarketError('invalid_response', '草稿回执不匹配。')
            stage = 'seller_submit'
            key = hashlib.sha256(compact([seller, request['request_id']]).encode()).hexdigest()
            reply = seller_client.call(stage, {'id': resource, 'version': version,
                                              'revision': 1, 'request_id': 'supplier-submit-' + key})
            if reply.get('status') != 'pending_review' or reply.get('id') != resource or reply.get('version') != version:
                raise MarketError('operation_not_confirmed', '未确认提交。')
            # Old mutation receipts can replay pending_review after approval. Read
            # the actual current state, never infer it from the old receipt.
            stage = 'seller_status'
            reply = seller_client.call(stage, {'id': resource, 'version': version})
            state = reply.get('status')
            if reply.get('id') != resource or reply.get('version') != version or state not in ('pending_review', 'approved', 'rejected'):
                raise MarketError('invalid_response', '无法确认当前审核状态。')
            completed.append({'id': resource, 'version': version, 'state': state})
        except (MarketError, OSError, ValueError, TypeError, AttributeError):
            result.update(status='stopped', stopped_index=index, stage=stage,
                          outcome='unconfirmed',
                          next_action='inspect_original_requests_and_server_state_before_replay')
            return result
    result['next_action'] = 'review_pending_items_or_inspect_current_state'
    return result
