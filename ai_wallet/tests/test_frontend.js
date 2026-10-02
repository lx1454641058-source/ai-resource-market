/* 本项目原创；仅使用 Node.js 标准库，模拟 DOM 与本机 API，不执行真实付款。 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

// 该轻量 DOM 只验证事件、表单草稿和请求行为；不模拟布局、CSS 或浏览器无障碍树。
class Element {
  constructor(tag, document) {
    this.tagName = tag.toUpperCase(); this.ownerDocument = document;
    this.children = []; this.parentNode = null; this.dataset = {}; this.attributes = {};
    this.listeners = new Map(); this.style = {}; this.className = ''; this._text = '';
    this._value = undefined; this.checked = false; this.disabled = false; this.hidden = false;
    this.classList = {
      add: (...tokens) => { this.className = [...new Set([...this.className.split(/\s+/), ...tokens])].filter(Boolean).join(' '); },
      remove: (...tokens) => { this.className = this.className.split(/\s+/).filter(x => !tokens.includes(x)).join(' '); },
      contains: token => this.className.split(/\s+/).includes(token),
      toggle: (token, force) => { const enabled = force === undefined ? !this.classList.contains(token) : force; enabled ? this.classList.add(token) : this.classList.remove(token); return enabled; },
    };
  }
  get textContent() { return this._text + this.children.map(x => x.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get childNodes() { return this.children; }
  get isConnected() { return this === this.ownerDocument.body || Boolean(this.parentNode?.isConnected); }
  get value() { return this._value === undefined ? (this.tagName === 'SELECT' ? this.children[0]?.value || '' : '') : this._value; }
  set value(value) { this._value = String(value); }
  append(...nodes) { nodes.forEach(node => { if (typeof node === 'string') node = this.ownerDocument.createTextNode(node); node.parentNode = this; this.children.push(node); }); }
  appendChild(node) { this.append(node); return node; }
  prepend(...nodes) { nodes.forEach(node => node.parentNode = this); this.children.unshift(...nodes); }
  contains(node) { return node === this || this.children.some(child => child.contains(node)); }
  replaceChildren(...nodes) { this.children.forEach(x => x.parentNode = null); this.children = []; this._text = ''; this.append(...nodes); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(x => x !== this); this.parentNode = null; }
  setAttribute(name, value) { this.attributes[name] = String(value); if (name === 'id') this.id = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  addEventListener(type, callback) { const list = this.listeners.get(type) || []; list.push(callback); this.listeners.set(type, list); }
  async dispatch(type) { const event = {target: this, currentTarget: this, preventDefault() {}, stopPropagation() {}}; await Promise.all((this.listeners.get(type) || []).map(callback => callback(event))); }
  focus() { this.ownerDocument.activeElement = this; }
  scrollIntoView() { this.scrolledIntoView = true; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  reportValidity() { return true; }
  matches(selector) {
    if (selector.includes(',')) return selector.split(',').some(part => this.matches(part.trim()));
    if (selector.startsWith('#')) return this.id === selector.slice(1);
    if (selector.startsWith('.')) return this.classList.contains(selector.slice(1));
    if (selector === 'dialog[open]') return this.tagName === 'DIALOG' && this.open;
    const attribute = selector.match(/^\[([^=\]]+)(?:=['"]?([^'"\]]+)['"]?)?\]$/);
    if (attribute) { const key = attribute[1]; const value = key.startsWith('data-') ? this.dataset[key.slice(5).replace(/-([a-z])/g, (_, x) => x.toUpperCase())] : this.attributes[key]; return value !== undefined && (attribute[2] === undefined || value === attribute[2]); }
    const typed = selector.match(/^(\w+)\[([^=]+)=['"]?([^'"\]]+)['"]?\]$/);
    if (typed) return this.tagName === typed[1].toUpperCase() && (this[typed[2]] ?? this.attributes[typed[2]]) === typed[3];
    return this.tagName === selector.toUpperCase();
  }
  querySelectorAll(selector) {
    const selectors = selector.split(',').map(x => x.trim()); const result = [];
    const visit = node => node.children.forEach(child => { if (selectors.some(x => child.matches(x))) result.push(child); visit(child); });
    visit(this); return result;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

function fixture() {
  return {
    mode: 'demo', csrf_token: 'test-token', owner_protected: false,
    policy: {enabled: true, monthly_limit_cents: 30000, daily_limit_cents: 10000,
      per_payment_limit_cents: 10000, approval_threshold_cents: 2000,
      allowed_categories: ['image', 'data', 'tool'], allowed_merchant_ids: ['studio', 'insight', 'compute'], revision: 1},
    summary: {month_available_cents: 25300, month_spent_cents: 0, month_reserved_cents: 4700,
      day_available_cents: 5300, day_spent_cents: 0, day_reserved_cents: 4700, pending_count: 1},
    tasks: [
      {id: 'campaign', title: '新品宣传方案', agent_id: 'creative-agent', status: 'active', revision: 1, budget_cents: 10000, available_cents: 8800, spent_cents: 0, reserved_cents: 1200},
      {id: 'research', title: '行业资料整理', agent_id: 'research-agent', status: 'active', revision: 1, budget_cents: 8000, available_cents: 4500, spent_cents: 0, reserved_cents: 3500},
    ],
    merchants: [{id: 'studio', name: '像素素材', category: 'image'}, {id: 'insight', name: '知数资料', category: 'data'}, {id: 'compute', name: '轻舟工具', category: 'tool'}],
    products: [{id: 'image-12', title: '宣传配图', merchant_id: 'studio', category: 'image', price_cents: 1200, license: '演示', is_subscription: false},
      {id: 'report-35', title: '行业概览', merchant_id: 'insight', category: 'data', price_cents: 3500, license: '演示', is_subscription: false}],
    orders: [makeOrder('1', 'pending_approval', 'research'), makeOrder('2', 'approved', 'campaign')], audit: [],
  };
}
function makeOrder(id, status, task) {
  return {id: id.padStart(20, '0'), task_id: task, agent_id: task === 'research' ? 'research-agent' : 'creative-agent',
    product_id: task === 'research' ? 'report-35' : 'image-12', product_title: task === 'research' ? '行业概览' : '宣传配图',
    merchant_name: task === 'research' ? '知数资料' : '像素素材', amount_cents: task === 'research' ? 3500 : 1200,
    status, reason: task === 'research' ? '市场调研' : '设计交付', created_at: '2026-09-10T10:00:00+08:00',
    expires_at: '2026-09-10T10:15:00+08:00', policy_revision: 1, decision_reason: '演示状态', receipt: null, delivery: null};
}
const clone = value => JSON.parse(JSON.stringify(value));
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); };

async function harness(initial = fixture()) {
  const document = {createElement(tag) { return new Element(tag, this); }, createTextNode(text) { const node = new Element('#text', this); node.textContent = text; return node; }};
  document.body = document.createElement('body');
  document.addEventListener = (type, callback) => document.body.addEventListener(type, callback);
  document.visibilityState = 'visible';
  document.querySelectorAll = selector => document.body.querySelectorAll(selector);
  document.querySelector = selector => document.body.querySelector(selector);
  for (const id of ['main', 'toast', 'breadcrumb-page', 'wallet-status', 'pause-button', 'refresh-button', 'logout-button', 'last-updated', 'sync-status', 'inbox-count']) {
    const node = document.createElement(id.includes('button') ? 'button' : 'div'); node.id = id; document.body.append(node);
  }
  for (const page of ['overview', 'inbox', 'policy', 'orders', 'tasks', 'operations']) {
    const node = document.createElement('button'); node.dataset.page = page; document.body.append(node);
  }
  let serverState = clone(initial); const tickets = []; const requests = []; const failures = new Map(); const storage = new Map();
  const windowListeners = new Map(); let uuid = 100;
  const sandbox = {
    document, console, Intl, Date, URLSearchParams, URL, AbortController, encodeURIComponent, decodeURIComponent,
    location: {hash: '', pathname: '/', search: ''},
    history: {replaceState(_state, _title, url) { sandbox.location.hash = String(url).split('#')[1] ? '#' + String(url).split('#')[1] : ''; }},
    crypto: {randomUUID: () => `generated-${++uuid}`},
    sessionStorage: {getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, String(value)), removeItem: key => storage.delete(key)},
    setTimeout: (callback, delay) => { if (!delay) setImmediate(callback); return 1; }, clearTimeout() {}, setInterval: () => 1, clearInterval() {},
    requestAnimationFrame: callback => callback(),
    window: {scrollTo() {}, addEventListener(type, callback) { windowListeners.set(type, callback); }},
  };
  let currentHash = '';
  Object.defineProperty(sandbox.location, 'hash', {
    get: () => currentHash,
    set: value => { currentHash = value ? '#' + String(value).replace(/^#/, '') : ''; },
  });
  const response = data => ({ok: true, status: 200, json: async () => clone(data)});
  sandbox.fetch = async (url, options = {}) => {
    const method = options.method || 'GET'; const body = options.body ? JSON.parse(options.body) : null;
    requests.push({url, method, body});
    if (failures.has(url)) throw new Error(failures.get(url));
    if (method === 'GET') return response(url === '/api/support/tickets' ? {tickets, external_delivery: false} : serverState);
    if (url === '/api/session/login' || url === '/api/session/logout') return response({ok: true});
    if (url === '/api/allocation/pool') {
      serverState.allocation.total_cents=body.total_cents;serverState.allocation.unallocated_cents=body.total_cents-serverState.allocation.allocated_cents-serverState.allocation.spent_cents;serverState.allocation.revision++;return response({allocation:serverState.allocation});
    }
    if (url === '/api/allocation/proposals') {
      const task={id:`proposal-${++uuid}`,title:body.title,agent_id:body.agent_id,budget_cents:0,proposed_budget_cents:body.budget_cents,budget_reason:body.reason,status:'pending_budget',revision:1,spent_cents:0,reserved_cents:0,available_cents:0};serverState.tasks.push(task);return response({task});
    }
    const allocationMatch=url.match(/^\/api\/allocation\/tasks\/([^/]+)\/(decide|propose|close)$/);
    if(allocationMatch){const task=serverState.tasks.find(t=>t.id===allocationMatch[1]);if(task.revision!==body.expected_revision)return {ok:false,status:409,json:async()=>({error:'提案已变化',code:'revision_conflict'})};const action=allocationMatch[2];if(action==='decide'){if(body.decision==='approve'){const delta=body.budget_cents-task.budget_cents;serverState.allocation.unallocated_cents-=delta;serverState.allocation.allocated_cents+=delta;task.budget_cents=body.budget_cents;task.available_cents=body.budget_cents-task.spent_cents-task.reserved_cents;if(task.status==='pending_budget')task.status='active';}else if(task.status==='pending_budget')task.status='rejected';task.proposed_budget_cents=null;}if(action==='propose'){task.proposed_budget_cents=body.budget_cents;task.budget_reason=body.reason;}if(action==='close'){task.status='closed';task.budget_cents=task.spent_cents+task.reserved_cents;task.available_cents=0;}task.revision++;serverState.allocation.revision++;return response({task});}
    if (url === '/api/tasks') {
      const task = {id: String(++uuid).padStart(20, '0'), title: body.title, agent_id: body.agent_id,
        budget_cents: body.budget_cents, status: 'active', revision: 1, spent_cents: 0, reserved_cents: 0, available_cents: body.budget_cents};
      serverState.tasks.push(task); return response({ok: true, task});
    }
    if (url.startsWith('/api/tasks/')) {
      const task = serverState.tasks.find(task => task.id === url.split('/').at(-1));
      if (body.expected_revision !== task.revision) return {ok: false, status: 409, json: async () => ({ok: false, code: 'revision_conflict', error: '任务已变化'})};
      const {expected_revision, ...changes} = body; Object.assign(task, changes, {revision: task.revision + 1});
      return response({ok: true, task});
    }
    if (url === '/api/support/tickets') {
      const order = serverState.orders.find(order => order.id === body.order_id);
      const ticket = {...body, id: String(++uuid).padStart(20, '0'), status: 'open', revision: 1,
        created_at: order.created_at, updated_at: order.created_at, resolution: '', order_snapshot: clone(order)};
      tickets.push(ticket); return response({ok: true, ticket});
    }
    if (url.startsWith('/api/support/tickets/')) {
      const ticket = tickets.find(ticket => ticket.id === url.split('/').at(-1));
      if (body.expected_revision !== ticket.revision) return {ok: false, status: 409, json: async () => ({ok: false, code: 'revision_conflict', error: '问题记录已变化'})};
      Object.assign(ticket, {status: body.status, resolution: body.resolution, revision: ticket.revision + 1});
      return response({ok: true, ticket});
    }
    if (url === '/api/orders') {
      const order = makeOrder(String(++uuid), body.product_id === 'report-35' ? 'pending_approval' : 'approved', body.task_id);
      order.reason = body.reason; serverState.orders.unshift(order); return response({ok: true, order});
    }
    if (url === '/api/policy') { Object.assign(serverState.policy, body); return response({ok: true, policy: serverState.policy}); }
    if (url === '/api/policy/commit') { Object.assign(serverState.policy, body.changes); return response({ok: true, policy: serverState.policy}); }
    const match = url.match(/\/api\/orders\/([^/]+)\/(approve|reject|execute|reconcile|refund)$/);
    if (match) {
      const order = serverState.orders.find(x => x.id === match[1]);
      assert.ok(order, `未知测试订单 ${match[1]}`);
      order.status = {approve: 'approved', reject: 'rejected', execute: 'paid', reconcile: 'paid', refund: 'refunded'}[match[2]];
      if(['execute','reconcile'].includes(match[2]))order.delivery={title:'模拟成果',content:'演示交付',simulated:true};
      return response({ok: true, order});
    }
    if (url.includes('/api/wallet/')) { serverState.policy.enabled = url.endsWith('resume'); return response({ok: true, policy: serverState.policy}); }
    throw new Error(`未模拟的 API ${url}`);
  };
  const context = vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8'), context, {filename: 'app.js'});
  await flush();
  const api = {
    context, sandbox, document, requests, failures, storage, windowListeners, tickets,
    get state() { return serverState; }, set state(value) { serverState = clone(value); },
    run(code) { return vm.runInContext(code, context); },
    async clickText(text, root = document.body) {
      const buttons = root.querySelectorAll('button').filter(x => x.textContent.includes(text));
      assert.equal(buttons.length, 1, `期望唯一按钮 ${text}，实际 ${buttons.length}`);
      await buttons[0].dispatch('click'); await flush();
    },
    posts() { return requests.filter(x => x.method === 'POST'); },
  };
  assert.equal(api.run('state !== null'), true, '页面必须成功初始化');
  return api;
}

test('页面使用本机状态初始化，未触发付款请求', async () => {
  const app = await harness();
  assert.equal(app.posts().length, 0);
  assert.ok(app.document.querySelector('#main').textContent.includes('采购'));
});

test('网站结账批准只更新原授权，不串联模拟付款', async () => {
  const initial=fixture(); initial.orders=[{...initial.orders[0],checkout:{site_order_id:'site-original'}}];
  const app=await harness(initial); app.run("navigate('inbox')");
  await app.clickText('批准此笔采购');
  assert.deepEqual(app.posts().map(call=>call.url.split('/').at(-1)),['approve']);
  assert.equal(app.state.orders[0].status,'approved');
  const card=cardFor(app,app.state.orders[0].id);
  assert.ok(card.textContent.includes('等待付款执行程序'));
  assert.equal(card.querySelector('select'),null);
  assert.ok(!card.querySelectorAll('button').some(b=>b.textContent.includes('模拟执行')));
});

test('未决网站付款只能刷新原单，刷新不发送核验或付款请求', async () => {
  const initial=fixture(); initial.orders=[{...initial.orders[1],status:'unknown',dispatch:{id:'original-dispatch'}}];
  const app=await harness(initial); app.run("navigate('inbox')");
  const card=cardFor(app,app.state.orders[0].id);
  assert.ok(card.textContent.includes('额度继续保留'));
  assert.ok(!card.querySelectorAll('button').some(b=>/模拟|取消采购/.test(b.textContent)));
  const before=app.requests.length; await app.clickText('刷新原单状态',card);
  assert.ok(app.requests.slice(before).some(r=>r.url==='/api/state'&&r.method==='GET'));
  assert.equal(app.posts().length,0); assert.equal(app.state.orders[0].status,'unknown');
});

test('网站订单仅支付时显示待交付，不显示模拟退款或已有成果', async () => {
  const initial=fixture(); initial.orders=[{...initial.orders[1],status:'paid',checkout:{site_order_id:'original'},dispatch:{id:'dispatch'}}];
  const app=await harness(initial); app.run("navigate('orders')");
  let card=cardFor(app,app.state.orders[0].id);
  assert.ok(card.textContent.includes('已支付 · 待确认交付'));
  assert.ok(!card.querySelectorAll('button').some(b=>/退款|模拟/.test(b.textContent)));
  await app.clickText('已付款',app.document.querySelector('.filter-row'));
  assert.equal(app.document.querySelectorAll('.order-card').length,1);
  app.state.orders[0].delivery={title:'实验成果',content:'已核对交付',simulated:true};
  await app.run('refreshState(true)');
  card=cardFor(app,app.state.orders[0].id);
  assert.ok(card.textContent.includes('已支付 · 已有交付记录'));
  assert.ok(!card.textContent.includes('尚未确认素材交付'));
  assert.equal(app.posts().length,0);
});

test('已付款未交付持续出现在待办与异常筛选，交付后移出待办', async () => {
  const initial=fixture(); initial.orders=[{...initial.orders[1],status:'paid',dispatch:{id:'original'}}];
  const app=await harness(initial);
  assert.equal(app.document.querySelector('#inbox-count').textContent,'1');
  app.run("navigate('inbox')"); assert.equal(app.document.querySelectorAll('.order-card').length,1);
  app.run("navigate('orders')"); await app.clickText('需要处理',app.document.querySelector('.filter-row'));
  assert.equal(app.document.querySelectorAll('.order-card').length,1);
  app.state.orders[0].delivery={title:'素材',content:'模拟交付',simulated:true};
  await app.run('refreshState(true)'); app.run("navigate('inbox')");
  assert.equal(app.document.querySelectorAll('.order-card').length,0);
  assert.equal(app.document.querySelector('#inbox-count').hidden,true);
  assert.equal(app.posts().length,0);
});

function cardFor(app, id) {
  const card = app.document.querySelectorAll('.order-card').find(node => node.textContent.includes(id));
  assert.ok(card, `订单 ${id} 应有可见卡片`); return card;
}

test('待办一次批准继续模拟执行，原单完成后移出待办', async () => {
  const app = await harness(); app.run("navigate('inbox')");
  const id = app.state.orders[0].id;
  await app.clickText('批准并模拟执行', cardFor(app, id));
  assert.equal(app.state.orders.find(order => order.id === id).status, 'paid');
  assert.equal(app.document.querySelectorAll('.order-card').some(node => node.textContent.includes(id)), false);
  assert.deepEqual(app.posts().map(call => call.url.split('/').at(-1)), ['approve', 'execute']);
});

test('取消未付款采购先确认，取消对话框不发送请求，确认只取消原订单', async () => {
  const app = await harness(); app.run("navigate('inbox')");
  const id = app.state.orders[1].id;
  const begin = () => cardFor(app, id).querySelectorAll('button').find(node => node.textContent === '取消采购').dispatch('click');
  let completion = begin(); await flush();
  assert.ok(app.document.querySelector('dialog').textContent.includes('12.00'));
  await app.clickText('取消', app.document.querySelector('dialog')); await completion;
  assert.equal(app.posts().length, 0);
  completion = begin(); await flush();
  await app.clickText('确认操作', app.document.querySelector('dialog')); await completion; await flush();
  assert.deepEqual(app.posts().map(call => call.url), [`/api/orders/${id}/reject`]);
});

test('订单直达只显示目标订单，返回列表恢复全部记录', async () => {
  const app = await harness(); const id = app.state.orders[0].id;
  app.run(`navigate('orders', '${id}')`);
  assert.equal(app.document.querySelectorAll('.order-card').length, 1);
  assert.ok(cardFor(app, id));
  await app.clickText('返回交易列表');
  assert.equal(app.document.querySelectorAll('.order-card').length, 2);
});

test('订单搜索、任务和状态筛选共同生效，清除后恢复列表', async () => {
  const initial = fixture(); initial.orders.push(makeOrder('3', 'approved', 'research'));
  const app = await harness(initial); app.run("navigate('orders')");
  const form = app.document.querySelector('.order-tools');
  const search = form.querySelector('input'); search.value = '知数'; await search.dispatch('input');
  const task = form.querySelector('select'); task.value = 'research'; await task.dispatch('change');
  await form.dispatch('submit'); await flush();
  await app.clickText('待执行', app.document.querySelector('.filter-row'));
  assert.equal(app.document.querySelectorAll('.order-card').length, 1);
  assert.ok(cardFor(app, initial.orders[2].id));
  await app.clickText('清除筛选');
  assert.equal(app.document.querySelectorAll('.order-card').length, 3);
});

test('采购草稿的任务商品理由情景在数据刷新和栏目切换后保留', async () => {
  const app = await harness(); app.run("navigate('integration')"); let form = app.document.querySelector('form');
  const selects = form.querySelectorAll('select');
  selects[0].value = 'research'; selects[1].value = 'report-35'; selects[2].value = 'timeout';
  form.querySelector('textarea').value = '尚未提交的详细采购理由'; await form.dispatch('input');
  app.document.querySelector('#pause-button').focus();
  await app.run('refreshState(true)'); app.run("navigate('orders'); navigate('integration')");
  form = app.document.querySelector('form');
  assert.deepEqual(form.querySelectorAll('select').map(node => node.value), ['research', 'report-35', 'timeout']);
  assert.equal(form.querySelector('textarea').value, '尚未提交的详细采购理由');
  assert.equal(app.posts().length, 0);
});

test('编辑中的控件刷新时保持原DOM与输入，不把服务器状态写入草稿', async () => {
  const app = await harness(); app.run("navigate('integration')"); const form = app.document.querySelector('form');
  const reason = form.querySelector('textarea'); reason.value = '正在输入'; await form.dispatch('input'); reason.focus();
  await app.run('refreshState(false)');
  assert.equal(app.document.querySelector('textarea'), reason);
  assert.equal(reason.value, '正在输入');
});

test('付款网络失败后仅查询状态，不自动重放execute或创建订单', async () => {
  const app = await harness(); app.run("navigate('inbox')");
  const id = app.state.orders[1].id; const endpoint = `/api/orders/${id}/execute`;
  app.failures.set(endpoint, '模拟连接中断');
  await app.clickText('模拟执行', cardFor(app, id));
  assert.deepEqual(app.posts().map(call => call.url), [endpoint]);
  assert.ok(app.requests.filter(call => call.method === 'GET').length >= 2);
  assert.ok(app.document.querySelector('#toast').textContent.includes('模拟连接中断'));
});

test('创建结果未知时保存请求编号，用户重试沿用原编号而非新建请求', async () => {
  const app = await harness(); app.run("navigate('integration')"); app.failures.set('/api/orders', '模拟创建响应丢失');
  await app.document.querySelector('form').dispatch('submit'); await flush();
  assert.equal(app.posts().length, 1);
  const key = app.posts()[0].body.idempotency_key;
  assert.ok(key);
  assert.ok([...app.storage.values()].some(value => value.includes(key)));
  app.failures.delete('/api/orders');
  await app.clickText('核验并重试原申请'); await flush();
  const creates = app.posts().filter(call => call.url === '/api/orders');
  assert.equal(creates.length, 2);
  assert.equal(creates[1].body.idempotency_key, key);
  assert.equal(app.storage.size, 0);
});

test('原采购结果未知且全部任务归档后仍能核验原编号，但不自动付款', async () => {
  const app = await harness(); app.run("navigate('integration')"); app.failures.set('/api/orders', '原创建响应未确认');
  await app.document.querySelector('form').dispatch('submit'); await flush();
  const key = app.posts()[0].body.idempotency_key;
  app.state.tasks.forEach(task => task.status = 'archived');
  app.failures.delete('/api/orders');
  await app.run('refreshState(true)');
  await app.clickText('核验并重试原申请');
  const calls = app.posts();
  assert.equal(calls.length, 2);
  assert.ok(calls.every(call => call.url === '/api/orders'));
  assert.equal(calls[1].body.idempotency_key, key);
  assert.equal(app.storage.size, 0);
});

test('两个状态请求逆序返回时旧快照不能覆盖较新结果', async () => {
  const app = await harness(); const resolvers = [];
  app.sandbox.fetch = () => new Promise(resolve => resolvers.push(resolve));
  const first = app.run('fetchState()'); const second = app.run('fetchState()');
  const newest = fixture(); newest.policy.revision = 8;
  const oldest = fixture(); oldest.policy.revision = 7;
  resolvers[1]({ok: true, json: async () => newest}); await second;
  resolvers[0]({ok: true, json: async () => oldest}); await first;
  assert.equal(app.run('state.policy.revision'), 8);
});

test('执行中的重复点击不能产生第二笔POST', async () => {
  const app = await harness(); app.run("navigate('inbox')");
  const id = app.state.orders[1].id;
  const button = cardFor(app, id).querySelectorAll('button').find(node => node.textContent === '模拟执行');
  const originalFetch = app.sandbox.fetch; let release;
  app.sandbox.fetch = async (url, options) => {
    if (url.endsWith('/execute')) await new Promise(resolve => release = resolve);
    return originalFetch(url, options);
  };
  const first = button.dispatch('click'); await flush();
  await button.dispatch('click'); release(); await first; await flush();
  assert.equal(app.posts().filter(call => call.url.endsWith('/execute')).length, 1);
});

test('采购只提供明确active任务，暂停归档未知状态均不可选', async () => {
  const initial = fixture();
  initial.tasks[1].status = 'paused';
  initial.tasks.push({...initial.tasks[0], id: 'archived-task', status: 'archived'});
  initial.tasks.push({...initial.tasks[0], id: 'missing-status', status: undefined});
  const app = await harness(initial);
  app.run("navigate('integration')");
  const options = app.document.querySelector('form').querySelector('select').children.map(option => option.value);
  assert.deepEqual(options, ['campaign']);
});

test('任务创建要求确认，提交整数分预算，失败重试复用编号', async () => {
  const app = await harness(); app.run("navigate('tasks')");
  let form = app.document.querySelector('form'); const inputs = form.querySelectorAll('input');
  inputs[0].value = '新的交付任务'; inputs[1].value = 'test-bot'; inputs[2].value = '80.12'; await form.dispatch('input');
  app.failures.set('/api/tasks', '模拟任务创建响应丢失');
  let completion = form.dispatch('submit'); await flush();
  assert.equal(app.posts().length, 0);
  await app.clickText('确认操作', app.document.querySelector('dialog')); await completion; await flush();
  const first = app.posts()[0];
  assert.equal(first.body.budget_cents, 8012);
  assert.equal(first.body.agent_id, 'test-bot');
  assert.ok([...app.storage.values()].some(value => value.includes(first.body.idempotency_key)));
  app.failures.delete('/api/tasks');
  form = app.document.querySelector('form'); completion = form.dispatch('submit'); await flush();
  await app.clickText('确认操作', app.document.querySelector('dialog')); await completion; await flush();
  const creates = app.posts().filter(request => request.url === '/api/tasks');
  assert.equal(creates[1].body.idempotency_key, first.body.idempotency_key);
  assert.ok(app.state.tasks.some(task => task.title === '新的交付任务'));
  assert.equal(app.posts().some(request => request.url.startsWith('/api/orders')), false);
});

test('任务编辑草稿跨刷新保留，版本变化后阻止覆盖保存', async () => {
  const app = await harness(); app.run("navigate('tasks', 'campaign')");
  let form = app.document.querySelector('form'); form.querySelector('input').value = '尚未保存的新名称'; await form.dispatch('input');
  app.state.tasks[0].revision = 2; app.state.tasks[0].title = '其他页面的新名称';
  app.document.querySelector('#refresh-button').focus(); await app.run('refreshState(true)');
  form = app.document.querySelector('form');
  assert.equal(form.querySelector('input').value, '尚未保存的新名称');
  await form.dispatch('submit'); await flush();
  assert.equal(app.posts().length, 0);
  assert.ok(app.document.querySelector('#toast').textContent.includes('版本已变化'));
});

test('归档任务详情只读，任务交易链接保留筛选', async () => {
  const initial = fixture(); initial.tasks[0].status = 'archived';
  const app = await harness(initial); app.run("navigate('tasks', 'campaign')");
  assert.equal(app.document.querySelectorAll('form').length, 0);
  assert.ok(app.document.querySelector('#main').textContent.includes('不能恢复'));
  await app.clickText('查看此任务的全部交易');
  assert.equal(app.document.querySelectorAll('.order-card').length, 1);
  assert.ok(cardFor(app, app.state.orders[1].id));
});

test('规则保存展示失效数量金额，并向原子提交接口传确认快照', async () => {
  const app = await harness(); app.run("navigate('policy')");
  const form = app.document.querySelector('form'); form.querySelector('input').value = '310.00'; await form.dispatch('input');
  const completion = form.dispatch('submit'); await flush();
  const dialog = app.document.querySelector('dialog');
  assert.ok(dialog.textContent.includes('2 笔'));
  assert.ok(dialog.textContent.includes('47.00'));
  assert.equal(app.posts().length, 0);
  await app.clickText('确认操作', dialog); await completion; await flush();
  assert.equal(app.posts().length, 1);
  const request = app.posts()[0]; assert.equal(request.url, '/api/policy/commit');
  assert.equal(request.body.expected_revision, 1);
  assert.deepEqual(request.body.expected_pending_ids.sort(), app.state.orders.map(order => order.id).sort());
  assert.equal(request.body.changes.monthly_limit_cents, 31000);
});

test('锁定清除账目和草稿但保留未确认编号，登录口令不写入存储', async () => {
  const app = await harness(); app.run("navigate('integration')"); const form = app.document.querySelector('form');
  form.querySelector('textarea').value = '不应留在锁定页面的草稿'; await form.dispatch('input');
  app.storage.set('yuxing.demo.pendingPurchase.v1', '{"key":"pending-fixture"}');
  const originalFetch = app.sandbox.fetch;
  app.sandbox.fetch = async (url, options) => url === '/api/state' ? {ok: false, status: 401, json: async () => ({code: 'login_required'})} : originalFetch(url, options);
  await app.run('refreshState(true)');
  assert.equal(app.run('state'), null);
  assert.equal(app.run('Object.keys(drafts).length'), 0);
  assert.equal(app.document.querySelectorAll('.order-card').length, 0);
  assert.equal(app.document.querySelector('#main').textContent.includes('不应留在锁定页面的草稿'), false);
  assert.equal(app.storage.size, 1);
  const password = app.document.querySelector('.login-form').querySelector('input'); password.value = 'fixture-password-only';
  app.sandbox.fetch = originalFetch;
  await app.document.querySelector('.login-form').dispatch('submit'); await flush();
  assert.equal(password.value, '');
  assert.equal(app.run('state !== null'), true);
  assert.equal([...app.storage.values()].some(value => value.includes('fixture-password-only')), false);
  assert.equal(app.run("JSON.stringify(drafts).includes('fixture-password-only')"), false);
});

test('已锁定后迟到的旧状态成功响应不能恢复账目', async () => {
  const app = await harness(); let release;
  app.sandbox.fetch = () => new Promise(resolve => release = resolve);
  const pending = app.run('fetchState()'); app.run('showLogin()');
  release({ok: true, status: 200, json: async () => fixture()}); await pending;
  assert.equal(app.run('state'), null);
  assert.ok(app.document.querySelector('.login-form'));
});

test('登录失效后的旧问题请求失败不能污染新会话状态', async () => {
  const app = await harness(); let rejectOld;
  const originalFetch = app.sandbox.fetch;
  app.sandbox.fetch = (url, options) => url === '/api/support/tickets' ? new Promise((_resolve, reject) => rejectOld = reject) : originalFetch(url, options);
  const pending = app.run('loadSupportTickets()'); app.run('showLogin()');
  app.sandbox.fetch = originalFetch; await app.run('init()');
  rejectOld(new Error('属于旧会话的错误')); await pending;
  assert.equal(app.run('supportError'), '');
});

test('订单详情发起问题自动关联原订单，问题进度不触发付款退款', async () => {
  const app = await harness(); const id = app.state.orders[1].id;
  app.run(`navigate('orders', '${id}')`); await app.clickText('记录问题');
  const form = app.document.querySelector('.support-create-form');
  assert.equal(form.querySelector('select').value, id);
  form.querySelector('textarea').value = '请核对交付内容'; await form.dispatch('input');
  await form.dispatch('submit'); await flush();
  assert.equal(app.tickets.length, 1);
  const record = app.document.querySelector('.support-ticket');
  record.querySelector('select').value = 'resolved'; record.querySelector('textarea').value = '已人工核对演示记录';
  await record.dispatch('input'); await record.dispatch('submit'); await flush();
  assert.equal(app.tickets[0].status, 'resolved');
  assert.equal(app.posts().length, 2);
  assert.ok(app.posts().every(request => request.url.startsWith('/api/support/tickets')));
  assert.equal(app.state.orders.find(order => order.id === id).status, 'approved');
});

test('问题保存失败保留草稿和编号，手动重试不更换请求编号', async () => {
  const app = await harness(); app.run("navigate('operations')"); await flush();
  let form = app.document.querySelector('.support-create-form'); form.querySelector('textarea').value = '待核对的付款'; await form.dispatch('input');
  app.failures.set('/api/support/tickets', '模拟响应丢失');
  await form.dispatch('submit'); await flush();
  const first = app.posts()[0]; app.failures.delete('/api/support/tickets');
  form = app.document.querySelector('.support-create-form');
  assert.equal(form.querySelector('textarea').value, '待核对的付款');
  await form.dispatch('submit'); await flush();
  assert.equal(app.posts()[1].body.idempotency_key, first.body.idempotency_key);
  assert.equal(app.tickets.length, 1);
});

test('结束编辑后应用被推迟的刷新并保留采购草稿', async () => {
  const app = await harness(); app.run("navigate('integration')"); const form = app.document.querySelector('form');
  const reason = form.querySelector('textarea'); reason.value = '编辑期间保留'; await form.dispatch('input'); reason.focus();
  app.state.summary.month_available_cents = 12345;
  await app.run('refreshState(false)'); assert.equal(app.document.querySelector('textarea'), reason);
  app.document.querySelector('#refresh-button').focus(); await app.document.querySelector('#main').dispatch('focusout'); await flush();
  assert.notEqual(app.document.querySelector('textarea'), reason);
  assert.equal(app.document.querySelector('textarea').value, '编辑期间保留');
  app.run("navigate('overview')");
  assert.ok(app.document.querySelector('#main').textContent.includes('123.45'));
});

test('三步引导只导航定位，不保存规则或自动采购，重复hash事件不清焦点', async () => {
  const initial=fixture();initial.orders=[];
  const app=await harness(initial);
  assert.equal(app.document.querySelector('.start-guide').open,false);
  await app.clickText('创建我的任务');
  const target=app.document.querySelector('#task-create-start');
  assert.equal(app.document.activeElement,target);
  app.windowListeners.get('hashchange')();
  assert.equal(app.document.querySelector('#task-create-start'),target);
  app.run("navigate('overview')");
  await app.clickText('查看授权规则');
  assert.equal(app.document.activeElement,app.document.querySelector('#policy-start'));
  app.run("navigate('overview')");
  await app.clickText('查看我的待办');
  assert.equal(app.run('activePage'),'inbox');
  assert.equal(app.posts().length,0);
  app.sandbox.location.hash='#orders';app.windowListeners.get('hashchange')();
  assert.equal(app.run('activePage'),'orders');
});

test('引导手动收起跨刷新保留，帮助可再次打开；全部任务暂停不被引导恢复', async () => {
  const initial=fixture();initial.orders=[];initial.tasks.forEach(task=>task.status='paused');
  const app=await harness(initial);
  const guide=app.document.querySelector('.start-guide');guide.open=false;await guide.dispatch('toggle');
  await app.run('refreshState(true)');
  assert.equal(app.document.querySelector('.start-guide').open,false);
  app.run("navigate('operations')");await flush();
  await app.clickText('从三步引导开始');
  assert.equal(app.document.querySelector('.start-guide').open,true);
  app.run("navigate('integration')");
  assert.ok(app.document.querySelector('#purchase-start').textContent.includes('没有可采购的任务'));
  assert.equal(app.posts().length,0);
});

test('帮助搜索、展开和无结果恢复跨页面保留，不发起支付或问题写入', async () => {
  const app=await harness();app.run("navigate('operations')");await flush();
  assert.ok(!app.document.querySelector('#main').textContent.includes('AI 总管：拆解与跟进'));
  let search=app.document.querySelector('input[type="search"]');search.value='退款';await search.dispatch('input');
  assert.ok(app.document.querySelectorAll('.help-topic').length>0);
  const topic=app.document.querySelector('.help-topic');topic.open=true;await topic.dispatch('toggle');
  const title=topic.querySelector('summary').textContent;
  app.run("navigate('overview');navigate('operations')");await flush();
  search=app.document.querySelector('input[type="search"]');
  assert.equal(search.value,'退款');
  assert.equal(app.document.querySelectorAll('.help-topic').find(x=>x.querySelector('summary').textContent===title).open,true);
  search.value='<不存在的帮助>';await search.dispatch('input');
  assert.equal(app.document.querySelectorAll('.help-topic').length,0);
  await app.clickText('清空帮助搜索');
  assert.equal(app.document.querySelectorAll('.help-topic').length,12);
  assert.equal(app.posts().length,0);
});

test('用户首页不再要求选商品或填写AI标识，演示仍可进入且未知申请有入口', async () => {
  const app=await harness();
  assert.equal(app.document.querySelector('form'),null);
  assert.equal(app.document.querySelector('#purchase-start'),null);
  assert.ok(app.document.querySelector('#main').textContent.includes('新增任务'));
  const entry=app.document.querySelector('#main').querySelectorAll('button').find(b=>b.textContent==='AI 接入');
  await entry.dispatch('click');
  assert.ok(app.document.querySelector('#purchase-start'));
  app.storage.set('yuxing.demo.pendingPurchase.v1',JSON.stringify({key:'keep',payload:{task_id:'campaign',product_id:'image-12',reason:'保留原单'},scenario:'success'}));
  app.run("navigate('overview')");await app.clickText('核对原申请');
  assert.ok(app.document.querySelector('#purchase-start').textContent.includes('核验并重试原申请'));
  assert.equal(app.posts().length,0);
});

test('用户只填任务名称和预算，演示标识自动分配并在失败重试中保持', async () => {
  const app=await harness();app.run("navigate('tasks')");
  let form=app.document.querySelector('form');const inputs=form.querySelectorAll('input');
  inputs[0].value='配图任务';inputs[2].value='40';
  app.failures.set('/api/tasks','暂未确认');
  let submitted=form.dispatch('submit');await flush();await app.clickText('确认操作',app.document.querySelector('dialog'));await submitted;await flush();
  const first=app.posts()[0].body;
  assert.ok(first.agent_id.startsWith('demo-'));
  app.failures.delete('/api/tasks');form=app.document.querySelector('form');
  submitted=form.dispatch('submit');await flush();await app.clickText('确认操作',app.document.querySelector('dialog'));await submitted;await flush();
  assert.equal(app.posts()[1].body.agent_id,first.agent_id);
  assert.equal(app.posts()[1].body.idempotency_key,first.idempotency_key);
});

test('一键批准中途失败不盲重试，批准未确认时不执行付款', async () => {
  for(const failingAction of ['approve','execute']){
    const app=await harness();app.run("navigate('inbox')");
    const id=app.state.orders[0].id;
    app.failures.set(`/api/orders/${id}/${failingAction}`,'连接中断');
    await app.clickText('批准并模拟执行',cardFor(app,id));
    assert.deepEqual(app.posts().map(call=>call.url.split('/').at(-1)),failingAction==='approve'?['approve']:['approve','execute']);
    assert.equal(app.state.orders.find(order=>order.id===id).status,failingAction==='approve'?'pending_approval':'approved');
  }
});

function allocationFixture(){const initial=fixture();initial.allocation={total_cents:100000,unallocated_cents:100000,allocated_cents:0,spent_cents:0,revision:1};initial.orders=[];initial.tasks=[{id:'proposal',title:'视频创作任务',agent_id:'demo-agent',budget_cents:0,proposed_budget_cents:30000,budget_reason:'购买配图和视频素材',status:'pending_budget',revision:1,spent_cents:0,reserved_cents:0,available_cents:0}];return initial;}

test('权限工作台不引导模拟购买，接入页默认折叠开发工具并显示未接通保护',async()=>{
  const app=await harness(allocationFixture());const main=app.document.querySelector('#main');
  assert.ok(main.textContent.includes('管理 AI 的预算与消费权限'));
  assert.ok(!main.querySelectorAll('button').some(b=>/体验|模拟/.test(b.textContent)));
  assert.ok(main.textContent.includes('尚未形成银行卡扣款保护'));
  app.run("navigate('integration')");
  assert.equal(app.document.querySelector('#developer-tools').open,false);
  assert.ok(main.textContent.includes('支付工具：未连接'));
  assert.ok(main.textContent.includes('没有批准预算、修改总额度或付款工具'));
  app.run("navigateToSection('integration','proposal-start')");
  assert.equal(app.document.querySelector('#developer-tools').open,true);
  assert.equal(app.posts().length,0);
});
function labeled(app,label,root=app.document){const node=root.querySelectorAll('input,textarea,select').find(x=>x.getAttribute('aria-label')===label);assert.ok(node,`找不到 ${label}`);return node;}

test('任务预算模式首页优先总额度与预算提案，任务列表不能直接新建已授权任务',async()=>{
  const app=await harness(allocationFixture());const main=app.document.querySelector('#main');assert.ok(main.textContent.includes('尚可分配给任务'));assert.ok(main.textContent.includes('视频创作任务'));assert.equal(app.document.querySelector('#inbox-count').textContent,'1');assert.equal(app.document.querySelector('#purchase-start'),null);
  app.run("navigate('tasks')");assert.equal(app.document.querySelector('form'),null);assert.ok(!main.textContent.includes('创建模拟任务'));assert.equal(app.posts().length,0);
  app.run("navigate('operations')");await flush();assert.ok(main.textContent.includes('AI 提出任务和建议预算'));assert.ok(!main.textContent.includes('点击首页“新增任务”'));
});

test('用户可修改任务预算后一次确认，申请与分配分开且不触发采购',async()=>{
  const app=await harness(allocationFixture());const card=app.document.querySelector('.budget-decision-card'),input=labeled(app,'确认任务总预算（元）',card);input.value='250';await input.dispatch('input');assert.ok(card.textContent.includes('750.00'));await card.querySelector('form').dispatch('submit');await flush();
  assert.deepEqual(app.posts().map(p=>p.url),['/api/allocation/tasks/proposal/decide']);assert.deepEqual(app.posts()[0].body,{decision:'approve',budget_cents:25000,expected_revision:1});assert.equal(app.state.tasks[0].status,'active');assert.equal(app.state.allocation.unallocated_cents,75000);
});

test('预算提案版本变化阻止旧草稿覆盖，刷新后仍要求读取最新提案',async()=>{
  const app=await harness(allocationFixture());const input=labeled(app,'确认任务总预算（元）');input.value='280';await input.dispatch('input');await app.document.querySelector('.budget-decision-card').querySelector('form').dispatch('input');app.state.tasks[0].revision=2;app.state.tasks[0].proposed_budget_cents=40000;
  await app.document.querySelector('.budget-decision-card').querySelector('form').dispatch('submit');await flush();assert.equal(app.posts().length,0);assert.ok(app.document.querySelector('#toast').textContent.includes('提案已变化'));assert.equal(labeled(app,'确认任务总预算（元）').value,'280');assert.ok(app.document.querySelector('.budget-decision-card').textContent.includes('当前草稿不能直接提交'));await app.clickText('读取最新提案');assert.equal(labeled(app,'确认任务总预算（元）').value,'400.00');
});

test('预算决定超额度或网络未知时不自动重试、不发起采购',async()=>{
  const app=await harness(allocationFixture());let input=labeled(app,'确认任务总预算（元）');input.value='1001';await app.document.querySelector('.budget-decision-card').querySelector('form').dispatch('submit');await flush();assert.equal(app.posts().length,0);
  input=labeled(app,'确认任务总预算（元）');input.value='300';app.failures.set('/api/allocation/tasks/proposal/decide','连接中断');await app.document.querySelector('.budget-decision-card').querySelector('form').dispatch('submit');await flush();assert.equal(app.posts().length,1);assert.equal(app.state.tasks[0].status,'pending_budget');assert.equal(app.state.allocation.unallocated_cents,100000);
});

test('模拟AI提案网络失败后保留原参数与编号，核验不会分配预算',async()=>{
  const app=await harness(allocationFixture());app.run("navigate('integration')");const form=app.document.querySelector('#proposal-start').querySelector('form');labeled(app,'任务名称',form).value='宣传短片';labeled(app,'建议任务预算（元）',form).value='300';labeled(app,'预算用途与理由',form).value='需要图片和视频素材';app.failures.set('/api/allocation/proposals','连接中断');await form.dispatch('submit');await flush();const first=app.posts()[0].body;assert.ok(app.storage.has('yuxing.demo.pendingBudgetProposal.v1'));assert.equal(app.document.querySelector('#proposal-start').querySelector('input'),null);
  app.failures.delete('/api/allocation/proposals');await app.clickText('核验并重试原预算提案');assert.deepEqual(app.posts()[1].body,first);assert.equal(app.storage.has('yuxing.demo.pendingBudgetProposal.v1'),false);assert.equal(app.state.tasks.at(-1).status,'pending_budget');assert.equal(app.state.tasks.at(-1).budget_cents,0);assert.equal(app.state.allocation.unallocated_cents,100000);assert.ok(app.posts().every(p=>p.url==='/api/allocation/proposals'));
});

test('新版任务详情不能直接改预算，追加提案与结束都有各自接口',async()=>{
  const initial=allocationFixture();Object.assign(initial.tasks[0],{status:'active',budget_cents:30000,available_cents:30000,proposed_budget_cents:null});initial.allocation.unallocated_cents=70000;initial.allocation.allocated_cents=30000;
  const app=await harness(initial);app.run("navigate('tasks','proposal')");assert.ok(!app.document.querySelector('#main').textContent.includes('保存任务修改'));let form=app.document.querySelector('form');labeled(app,'建议调整后的总预算（元）',form).value='250';labeled(app,'预算用途与理由',form).value='增加素材';await form.dispatch('submit');await flush();assert.equal(app.posts().length,0);labeled(app,'建议调整后的总预算（元）',form).value='400';await form.dispatch('submit');await flush();assert.equal(app.posts()[0].url,'/api/allocation/tasks/proposal/propose');assert.equal(app.state.tasks[0].budget_cents,30000);
  const closing=app.clickText('结束任务并释放未用额度');await flush();await app.clickText('取消',app.document.querySelector('dialog'));await closing;assert.equal(app.posts().length,1);const retry=app.clickText('结束任务并释放未用额度');await flush();await app.clickText('确认操作',app.document.querySelector('dialog'));await retry;assert.equal(app.posts().at(-1).url,'/api/allocation/tasks/proposal/close');assert.equal(app.state.tasks[0].status,'closed');
});

test('总额度设置显示资金分配影响并确认，过时版本不覆盖、调整不创建任务',async()=>{
  const initial=allocationFixture();initial.tasks=[];initial.allocation.total_cents=0;initial.allocation.unallocated_cents=0;const app=await harness(initial);app.run("navigate('policy')");let form=app.document.querySelector('#allocation-pool');labeled(app,'AI 可操控总额度（元）',form).value='1000';const saving=form.dispatch('submit');await flush();const dialog=app.document.querySelector('dialog');assert.ok(dialog.textContent.includes('1,000.00'));await app.clickText('确认操作',dialog);await saving;await flush();assert.deepEqual(app.posts()[0].body,{total_cents:100000,expected_revision:1});assert.equal(app.state.tasks.length,0);
  form=app.document.querySelector('#allocation-pool');labeled(app,'AI 可操控总额度（元）',form).value='1500';app.state.allocation.revision++;await form.dispatch('submit');await flush();assert.equal(app.posts().length,1);assert.ok(app.document.querySelector('#toast').textContent.includes('额度已变化'));assert.equal(app.document.querySelector('dialog'),null);
});

test('追加确认可以低于建议但不能低于旧预算，读取最新提案实际向服务查询',async()=>{
  const initial=allocationFixture();Object.assign(initial.tasks[0],{status:'active',budget_cents:30000,available_cents:30000,proposed_budget_cents:50000});initial.allocation.unallocated_cents=70000;initial.allocation.allocated_cents=30000;const app=await harness(initial);
  let card=app.document.querySelector('.budget-decision-card');let input=labeled(app,'确认任务总预算（元）',card);input.value='250';await input.dispatch('input');assert.ok(card.textContent.includes('不能低于已确认预算'));await card.querySelector('form').dispatch('submit');await flush();assert.equal(app.posts().length,0);
  const before=app.requests.length;await app.clickText('读取最新提案');assert.ok(app.requests.slice(before).some(r=>r.url==='/api/state'&&r.method==='GET'));
  card=app.document.querySelector('.budget-decision-card');input=labeled(app,'确认任务总预算（元）',card);input.value='400';await card.querySelector('form').dispatch('submit');await flush();assert.equal(app.posts().length,1);assert.equal(app.posts()[0].body.budget_cents,40000);assert.equal(app.state.tasks[0].budget_cents,40000);
});

test('首页待办摘要先于额度卡，仅导航不审批，无待办不占位',async()=>{
  const app=await harness(allocationFixture());const main=app.document.querySelector('#main');const summary=main.querySelector('.inbox-summary'),stats=main.querySelector('.stats-grid');assert.ok(summary.textContent.includes('1 项任务预算待确认'));assert.ok(main.children.indexOf(summary)<main.children.indexOf(stats));await app.clickText('处理待办',summary);assert.equal(app.sandbox.location.hash,'#inbox');assert.equal(app.posts().length,0);app.state.tasks=[];await app.run('fetchState()');app.run("navigate('overview')");assert.equal(main.querySelector('.inbox-summary'),null);
});
