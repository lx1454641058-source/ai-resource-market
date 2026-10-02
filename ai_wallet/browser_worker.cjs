/* 来源：本项目原创。受信宿主内部的浏览器执行组件；当前只允许本机结账实验。
 * Playwright 由宿主提供；不导入用户浏览器资料，不提供通用点击或脚本执行工具。
 */
'use strict';

function checkoutTarget(raw, amountCents) {
  const url = new URL(raw);
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port ||
      url.username || url.password || url.search || url.hash ||
      !/^\/checkout\/[a-f0-9]{32}$/.test(url.pathname) ||
      !Number.isSafeInteger(amountCents) || amountCents < 1 || amountCents > 100000000) {
    throw new Error('Only a fixed localhost checkout and integer demo amount are supported');
  }
  const siteOrderId = url.pathname.split('/').pop();
  return Object.freeze({url: url.href, payUrl: `${url.origin}/pay/${siteOrderId}`,
    origin: url.origin, siteOrderId, amountCents});
}

class RequestGate {
  constructor(target, authorize) {
    if (typeof authorize !== 'function') throw new Error('Host authorization callback required');
    this.target = target; this.authorize = authorize; this.state = 'reading';
    this.form = null;
  }
  arm(csrf, paymentRef) {
    if (this.state !== 'reading' || typeof csrf !== 'string' || !/^[A-Za-z0-9_-]{16,128}$/.test(csrf) ||
        typeof paymentRef !== 'string' || !/^DEMO-[A-Za-z0-9_-]{16,128}$/.test(paymentRef)) {
      throw new Error('Invalid demo form or checkout already attempted');
    }
    this.form = {csrf, paymentRef}; this.state = 'armed';
  }
  async allow({url, method, resourceType, mainFrame, postData}) {
    if (!mainFrame || resourceType !== 'document') return false;
    if (method === 'GET' && url === this.target.url) return true;
    if (method !== 'POST' || url !== this.target.payUrl || this.state !== 'armed') return false;
    // Consume locally before waiting on the host so concurrent requests cannot both pass.
    this.state = 'checking';
    const form = new URLSearchParams(postData || '');
    const entries = [...form.entries()];
    if (entries.length !== 2 || form.getAll('csrf').length !== 1 || form.getAll('payment_ref').length !== 1 ||
        form.get('csrf') !== this.form.csrf || form.get('payment_ref') !== this.form.paymentRef) {
      this.state = 'blocked'; this.form = null; return false;
    }
    try {
      const allowed = await this.authorize(Object.freeze({...this.target}));
      this.state = allowed === true ? 'submitted' : 'blocked';
      return allowed === true;
    } catch {
      this.state = 'unknown'; return false;
    } finally { this.form = null; }
  }
}

async function openCheckoutWorker({chromium, checkoutUrl, amountCents, authorize}) {
  const target = checkoutTarget(checkoutUrl, amountCents);
  const gate = new RequestGate(target, authorize);
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  let context, page, attempting = false;
  try {
    context = await browser.newContext({javaScriptEnabled: false, serviceWorkers: 'block', acceptDownloads: false});
    await context.routeWebSocket(/.*/, ws => ws.close());
    await context.route('**/*', async route => {
      const request = route.request();
      let allowed = false;
      try {
        allowed = await gate.allow({url: request.url(), method: request.method(),
          resourceType: request.resourceType(), mainFrame: Boolean(page) && request.frame() === page.mainFrame(),
          postData: request.postData()});
      } catch { /* Unknown frame/worker requests fail closed. */ }
      if (!allowed) { await route.abort(); return; }
      try {
        // Never let the fetch follow an uninspected redirect to a different destination.
        const response = await route.fetch({maxRedirects: 0, maxRetries: 0, timeout: 10000});
        if (response.status() >= 300 && response.status() < 400) {
          const location = response.headers().location;
          if (!location || new URL(location, request.url()).href !== target.url) {
            await route.fulfill({status: 502, contentType: 'text/plain', body: 'Unrecognized checkout redirect'});
            return;
          }
        }
        await route.fulfill({response});
      } catch {
        // A fetch timeout after authorization can mean the payment was sent. Never rearm.
        if (request.method() === 'POST') gate.state = 'unknown';
        await route.abort();
      }
    });
    page = await context.newPage();
    context.on('page', extra => { if (extra !== page) void extra.close().catch(() => {}); });
    await page.goto(target.url, {waitUntil: 'domcontentloaded', timeout: 15000});
    async function inspect() {
      if (page.url() !== target.url) throw new Error('Checkout page changed');
      const heading = await page.locator('main h2').innerText({timeout: 5000});
      const body = await page.locator('main').innerText({timeout: 5000});
      const status = /^结账状态：(awaiting_payment|approved|paid|failed|unknown|rejected|delivery_failed)$/.exec(heading)?.[1];
      const money = /(?:^|\n)应付 ([0-9]+)\.([0-9]{2}) 元(?:\n|$)/.exec(body);
      const amount = money ? Number(money[1]) * 100 + Number(money[2]) : NaN;
      if (!status || amount !== target.amountCents) throw new Error('Unrecognized or changed checkout');
      return {mode: 'demo', status, amount_cents: amount, currency: 'CNY', site_order_id: target.siteOrderId};
    }
    return Object.freeze({
      inspect,
      async submit(paymentRef) {
        if (attempting) throw new Error('Checkout already attempted; inspect original result');
        attempting = true;
        const current = await inspect();
        if (!['awaiting_payment', 'approved'].includes(current.status)) return current;
        const csrf = await page.locator('input[name="csrf"]').getAttribute('value');
        gate.arm(csrf, paymentRef);
        await page.getByLabel('测试支付凭据', {exact: true}).fill(paymentRef, {timeout: 5000});
        try {
          await page.getByRole('button', {name: '确认模拟订单', exact: true}).click({timeout: 15000});
          const observed = await inspect();
          return {...observed, submission: gate.state};
        } catch {
          return {mode: 'demo', status: 'unknown', submission: gate.state,
            next_action: 'verify_original', message: '读取失败；先核对原订单，不重新提交。'};
        }
      },
      async close() { await browser.close(); },
    });
  } catch (error) { await browser.close(); throw error; }
}

module.exports = {checkoutTarget, RequestGate, openCheckoutWorker};
