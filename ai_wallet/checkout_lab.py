"""来源：本项目原创。隔离网页结账实验；不提供真实卡或第三方网站适配。"""

import argparse
from datetime import timedelta
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sys
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit
import uuid

from .agent_planner import TaskPlanner
from .allocation import AllocationWallet
from .dispatch import _OrderDispatch
from .engine import SPENT, WalletError


class _CheckoutFixtureWallet(AllocationWallet):
    def __init__(self, database, quotes):
        self.quotes = quotes
        super().__init__(database)

    def _resolve_product(self, product_id):
        quote = self.quotes.get(product_id)
        if not quote:
            raise WalletError('找不到本地结账会话', 'not_found', 404)
        return dict(quote['product']), quote['merchant_name']

    def _settle(self, order, delivery_failed=False):
        super()._settle(order, delivery_failed)
        # 银行卡付款和网站交付是不同事实；实验不把付款回执伪装为下载成果。
        order['delivery'] = None
        order['decision_reason'] = '本地模拟付款已确认；网站交付另行核对'


class CheckoutLab:
    """全新临时账本、两种本地商店；商户身份由实验宿主固定。"""

    def __init__(self, database, *, require_host_claim=False):
        if Path(database).exists():
            raise ValueError('结账实验必须使用不存在的新文件，不能载入用户账本')
        if type(require_host_claim) is not bool:
            raise ValueError('实验宿主模式必须是布尔值')
        self.require_host_claim = require_host_claim
        self.quotes = {}
        self.lock = threading.RLock()
        self.wallet = _CheckoutFixtureWallet(database, self.quotes)
        self.prices = {'gallery': 1200, 'market': 1900}
        self.shops = {'gallery': ('画廊测试店', 'studio'), 'market': ('集市测试店', 'insight')}
        self.charge_count = 0
        self.provider_results = {}
        self.deliveries = {}
        self.origins = {}
        # 只初始化隔离实验的模拟用户授权；不会读取或写入任一演示主账本。
        self.wallet.configure_pool({'total_cents': 10000, 'expected_revision': 1})
        self.planner = TaskPlanner(self.wallet, 'checkout-lab-agent')
        task = self.planner.propose('网页结账实验', 6000, '模拟用户确认的测试任务', 'lab-task')
        self.task_id = task['task_id']
        self.wallet.decide_budget(self.task_id, {'decision': 'approve', 'budget_cents': 6000,
                                                'expected_revision': task['revision']})

    def prepare(self, shop, quantity):
        if shop not in self.shops or type(quantity) is not int or not 1 <= quantity <= 10:
            raise WalletError('只接受本地店铺和1到10份商品')
        with self.lock:
            checkout_id = uuid.uuid4().hex
            name, merchant = self.shops[shop]
            self.quotes[checkout_id] = {'shop': shop, 'quantity': quantity, 'merchant_name': name,
                'expires_at': self.wallet._now() + timedelta(minutes=2),
                'product': {'id': checkout_id, 'merchant_id': merchant, 'title': f'原创演示配图凭据 × {quantity}',
                            'price_cents': self.prices[shop] * quantity, 'category': 'image', 'is_subscription': False}}
            self.quotes[checkout_id].update(order_id=None, payment_ref=None)
            return checkout_id

    def authorize_checkout(self, shop, checkout_id):
        """实验中的钱包宿主调用；商店页面不能创建支付权限或更改任务。"""
        with self.lock:
            quote = self._quote(shop, checkout_id)
            if quote['order_id'] is not None:
                return {'payment_ref': quote['payment_ref'], 'checkout': self.view(shop, checkout_id)}
            if quote['expires_at'] <= self.wallet._now():
                raise WalletError('结账报价已过期', 'quote_expired', 409)
            order = self.wallet.request({'task_id': self.task_id, 'product_id': checkout_id,
                'reason': '从本地网页进入结账，价格由测试商店生成', 'idempotency_key': 'checkout-' + checkout_id})
            quote['order_id'] = order['id']
            if order['status'] == 'approved':
                _OrderDispatch(self.wallet, self.task_id).bind(order['id'], self._binding(shop, checkout_id))
                quote['payment_ref'] = 'DEMO-' + secrets.token_urlsafe(24)
            return {'payment_ref': quote['payment_ref'], 'checkout': self.view(shop, checkout_id)}

    def _binding(self, shop, checkout_id):
        quote = self._quote(shop, checkout_id)
        return {'origin': self.origins.get(shop, f'https://{shop}.example'),
                'account_ref': 'demo-account', 'site_order_id': checkout_id,
                'session_ref': 'demo-checkout-' + checkout_id,
                'amount_cents': self.prices[shop] * quote['quantity'], 'currency': 'CNY'}

    def claim_for_browser(self, shop, checkout_id, *, origin, amount_cents):
        """实验宿主通道专用：浏览器请求转发之前持久申领，不向商店HTTP开放。"""
        if not self.require_host_claim:
            raise WalletError('此实验未启用独立浏览器宿主')
        with self.lock:
            quote = self._quote(shop, checkout_id)
            if not quote['order_id'] or quote['expires_at'] <= self.wallet._now():
                return False
            binding = self._binding(shop, checkout_id)
            if (origin != binding['origin'] or type(amount_cents) is not int
                    or amount_cents != binding['amount_cents']):
                return False
            dispatch_id = _OrderDispatch(self.wallet, self.task_id).claim(quote['order_id'], checkout=binding)
            if dispatch_id is None:
                return False
            quote['host_dispatch_id'] = dispatch_id
            return True

    def _quote(self, shop, checkout_id):
        quote = self.quotes.get(checkout_id)
        if not quote or quote['shop'] != shop:
            raise WalletError('找不到本店结账会话', 'not_found', 404)
        return quote

    def _order(self, quote):
        with self.wallet._read_connection() as db:
            return self.wallet._get(db, quote['order_id'])

    def view(self, shop, checkout_id):
        with self.lock:
            quote = self._quote(shop, checkout_id)
            if quote['order_id'] is None:
                return {'mode': 'demo', 'checkout_id': checkout_id, 'order_id': None,
                        'status': 'awaiting_payment', 'amount_cents': quote['product']['price_cents'],
                        'currency': 'CNY', 'message': '网站订单已准备；尚未取得任务支付权限',
                        'receipt': None, 'delivery': None}
            order = self._order(quote)
            return {'mode': 'demo', 'checkout_id': checkout_id, 'order_id': order['id'],
                    'status': order['status'], 'amount_cents': order['amount_cents'], 'currency': 'CNY',
                    'message': order['decision_reason'], 'receipt': order['receipt'],
                    'delivery': self.deliveries.get(checkout_id)}

    def pay(self, shop, checkout_id, payment_ref, *, scenario='success'):
        if scenario not in ('success', 'decline', 'timeout', 'delivery_failure'):
            raise WalletError('未知实验情景')
        with self.lock:
            quote = self._quote(shop, checkout_id)
            if (not quote['payment_ref'] or not isinstance(payment_ref, str) or not payment_ref.isascii()
                    or not secrets.compare_digest(quote['payment_ref'], payment_ref)):
                raise WalletError('没有匹配本结账会话的测试支付凭据', 'invalid_payment_ref', 403)
            order = self._order(quote)
            host_claimed = (self.require_host_claim and order['status'] == 'unknown'
                            and quote.get('host_dispatch_id') is not None
                            and quote['host_dispatch_id'] == order.get('dispatch', {}).get('id')
                            and not quote.get('host_consumed'))
            if self.require_host_claim and not host_claimed and order['status'] == 'approved':
                raise WalletError('浏览器宿主尚未申领原订单，拒绝付款', 'host_claim_required', 409)
            if not host_claimed and order['status'] != 'approved':
                return self.view(shop, checkout_id)
            expired = quote['expires_at'] <= self.wallet._now()
            if expired or self.prices[shop] * quote['quantity'] != order['amount_cents']:
                reason = '网站结账报价已过期，未付款；原付款占用已释放' if expired else '网站结账金额已变化，未付款；请重新核对报价'
                if host_claimed:
                    # 此受控测试商店确认尚未执行副作用；真实商店必须另查原记录。
                    quote['host_consumed'] = True
                    _OrderDispatch(self.wallet, self.task_id).record(order['id'], quote['host_dispatch_id'],
                        status='failed', amount_cents=order['amount_cents'], currency='CNY',
                        evidence_ref='DEMO-NOT-SENT-' + order['id'], simulated=True)
                    return self.view(shop, checkout_id)
                with self.wallet._connection() as db:
                    current = self.wallet._get(db, order['id'])
                    if current['status'] == 'approved':
                        current.update(status='rejected', decision_reason=reason)
                        self.wallet._save(db, current)
                        self.wallet._audit(db, 'checkout_invalidated', reason, current['id'], 'simulated_provider')
                return self.view(shop, checkout_id)
            # 在模拟店发生副作用前持久保存未决状态；不能先扣款再写账本。
            dispatch = _OrderDispatch(self.wallet, self.task_id)
            dispatch_id = quote['host_dispatch_id'] if host_claimed else dispatch.claim(order['id'], checkout=self._binding(shop, checkout_id))
            if dispatch_id is None:
                return self.view(shop, checkout_id)
            quote['host_consumed'] = True
            self.charge_count += 1
            self.provider_results[order['id']] = 'failed' if scenario == 'decline' else 'paid'
            if scenario != 'timeout':
                dispatch.record(order['id'], dispatch_id,
                    status='failed' if scenario == 'decline' else ('delivery_failed' if scenario == 'delivery_failure' else 'paid'),
                    amount_cents=order['amount_cents'], currency='CNY',
                    evidence_ref='DEMO-' + order['id'], simulated=True)
            if scenario == 'success':
                self.deliver(checkout_id)
            return self.view(shop, checkout_id)

    def reconcile(self, shop, checkout_id):
        """实验宿主查原模拟渠道结果；网页与AI没有此写入口。"""
        with self.lock:
            quote = self._quote(shop, checkout_id)
            order = self._order(quote)
            if order['status'] == 'unknown' and self.provider_results.get(order['id']) == 'paid':
                _OrderDispatch(self.wallet, self.task_id).record(order['id'], order['dispatch']['id'],
                    status='paid', amount_cents=order['amount_cents'], currency='CNY',
                    evidence_ref='DEMO-' + order['id'], simulated=True)
            return self.view(shop, checkout_id)

    def deliver(self, checkout_id):
        """独立网站交付；没有实际素材下载，不能把此凭据当成商业许可。"""
        with self.lock:
            quote = self.quotes[checkout_id]
            order = self._order(quote)
            if order['status'] not in SPENT or order['status'] == 'refunded':
                raise WalletError('尚未确认付款，不能交付')
            self.deliveries.setdefault(checkout_id, {'simulated': True, 'title': order['product_title'],
                'content': '本项目原创结账实验凭据；不是已购买的图片或商业许可。'})


class LabServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, lab, shop):
        self.lab, self.shop, self.csrf = lab, shop, secrets.token_urlsafe(32)
        super().__init__(('127.0.0.1', 0), LabHandler)
        self.origin = f'http://127.0.0.1:{self.server_port}'
        lab.origins[shop] = self.origin


class LabHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, status, body, kind='text/html; charset=utf-8'):
        raw = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def _check_host(self):
        if self.headers.get('Host') != urlsplit(self.server.origin).netloc:
            raise WalletError('仅限实验本机地址', 'invalid_host', 403)

    def _page(self, content):
        title = self.server.lab.shops[self.server.shop][0]
        return (f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                f'<title>{escape(title)} · 隔离结账实验</title><style>body{{font:17px/1.7 system-ui;max-width:760px;margin:40px auto;padding:0 20px;background:#f6f7f8;color:#18242b}}'
                'main{background:white;padding:24px;border-radius:16px}button,input{font:inherit;padding:10px;margin:8px 0}button{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#126c52}</style>'
                f'<h1>{escape(title)}</h1><p>仅隔离实验：预设模拟总额度100元、任务预算60元。无真实付款、银行卡或商家API。</p><main>{content}</main></html>')

    def do_GET(self):
        try:
            self._check_host()
            path = urlsplit(self.path).path
            lab, shop = self.server.lab, self.server.shop
            if path == '/':
                label = '选购配图凭据' if shop == 'gallery' else '购买原创测试资源'
                content = (f'<h2>{label}</h2><p>每份 {lab.prices[shop]/100:.2f} 元；只生成测试凭据。</p>'
                    f'<form method="post" action="/prepare"><input type="hidden" name="csrf" value="{self.server.csrf}">'
                    '<label>购买数量 <input name="quantity" type="number" min="1" max="10" value="1" required></label><br>'
                    '<button type="submit">进入结账</button></form>')
                self._reply(200, self._page(content))
            elif path.startswith('/checkout/'):
                checkout_id = path.removeprefix('/checkout/')
                value = lab.view(shop, checkout_id)
                content = f'<h2>结账状态：{escape(value["status"])}</h2><p>应付 {value["amount_cents"]/100:.2f} 元</p><p>{escape(value["message"])}</p>'
                if value['status'] in ('approved', 'awaiting_payment'):
                    content += (f'<form method="post" action="/pay/{checkout_id}"><input type="hidden" name="csrf" value="{self.server.csrf}">'
                                '<label>测试支付凭据 <input name="payment_ref" autocomplete="off" required></label><br>'
                                '<button type="submit">确认模拟订单</button></form><p>测试凭据由独立钱包宿主提供；不输入真实卡号。</p>')
                content += '<p>网站交付：' + ('已生成原创测试凭据' if value['delivery'] else '尚未交付') + '</p>'
                content += f'<details><summary>查看模拟记录</summary><pre>{escape(json.dumps(value, ensure_ascii=False, indent=2))}</pre></details><a href="/">返回商店</a>'
                self._reply(200, self._page(content))
            else:
                self._reply(404, self._page('<p>页面不存在</p>'))
        except WalletError as exc:
            self._reply(exc.status, self._page(f'<p>{escape(str(exc))}</p>'))

    def do_POST(self):
        try:
            self._check_host()
            if self.headers.get('Origin') != self.server.origin:
                raise WalletError('仅接受本店页面提交', 'invalid_origin', 403)
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 2048 or self.headers.get('Content-Type', '').split(';')[0] != 'application/x-www-form-urlencoded':
                raise WalletError('无效表单')
            data = parse_qs(self.rfile.read(length).decode('utf-8'), keep_blank_values=True)
            if data.get('csrf') != [self.server.csrf]:
                raise WalletError('无效实验会话', 'invalid_session', 403)
            path = urlsplit(self.path).path
            if path == '/prepare' and set(data) == {'csrf', 'quantity'} and len(data['quantity']) == 1:
                checkout_id = self.server.lab.prepare(self.server.shop, int(data['quantity'][0]))
            elif path.startswith('/pay/') and set(data) == {'csrf', 'payment_ref'} and len(data['payment_ref']) == 1:
                checkout_id = path.removeprefix('/pay/')
                self.server.lab.pay(self.server.shop, checkout_id, data['payment_ref'][0])
            else:
                raise WalletError('表单包含不支持的字段；不能指定金额、任务或付款结果')
            self.send_response(303)
            self.send_header('Location', '/checkout/' + checkout_id)
            self.send_header('Content-Length', '0')
            self.end_headers()
        except (WalletError, ValueError, UnicodeError) as exc:
            self._reply(exc.status if isinstance(exc, WalletError) else 400, self._page(f'<p>{escape(str(exc))}</p>'))


def main():
    parser = argparse.ArgumentParser(description='临时本机结账实验；退出后不保留测试账本，无真实付款')
    parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='ai-wallet-checkout-') as folder:
        lab = CheckoutLab(Path(folder) / 'isolated.sqlite')
        servers = [LabServer(lab, shop) for shop in lab.shops]
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        print(json.dumps({'mode': 'demo', 'stores': {s.shop: s.origin for s in servers}}, ensure_ascii=False), flush=True)
        try:
            # 这是实验控制通道，非商家API。只给固定模拟任务准备权限或读取状态。
            for line in sys.stdin:
                try:
                    request = json.loads(line)
                    if set(request) != {'command', 'shop', 'checkout_id'}:
                        raise ValueError('实验命令字段不匹配')
                    if request['command'] == 'authorize':
                        result = lab.authorize_checkout(request['shop'], request['checkout_id'])
                    elif request['command'] == 'status':
                        result = lab.view(request['shop'], request['checkout_id'])
                    else:
                        raise ValueError('只支持authorize或status实验命令')
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                except (ValueError, TypeError, KeyError) as exc:
                    print(json.dumps({'error': str(exc)}, ensure_ascii=False), flush=True)
        except KeyboardInterrupt:
            pass
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    main()
