/* 本项目原创。浏览器请求决策测试；不启动或控制浏览器。 */
'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {checkoutTarget, RequestGate} = require('../browser_worker.cjs');
const url = 'http://127.0.0.1:8765/checkout/' + 'a'.repeat(32);
const csrf = 'c'.repeat(32), paymentRef = 'DEMO-' + 'p'.repeat(32);
const target = checkoutTarget(url, 1200);
const request = {url: target.payUrl, method: 'POST', mainFrame: true, resourceType: 'document',
  postData: new URLSearchParams({csrf, payment_ref: paymentRef}).toString()};

test('仅接受指定本机结账与整数金额，不能带账户或任意网址', () => {
  for (const bad of ['https://shop.example/checkout/'+'a'.repeat(32), url+'?token=abc',
    url+'#state', url.replace('127.0.0.1', 'user:pass@127.0.0.1'), url.replace('/checkout/', '/pay/')]) {
    assert.throws(() => checkoutTarget(bad, 1200));
  }
  for (const bad of [0, -1, true, 1.2, NaN]) assert.throws(() => checkoutTarget(url, bad));
});

test('未授权、异站、子框架及非文档请求均不放行', async () => {
  let calls = 0;
  const gate = new RequestGate(target, async () => { calls++; return true; });
  assert.equal(await gate.allow(request), false);
  gate.arm(csrf, paymentRef);
  for (const changes of [{url: 'https://other.example/pay'}, {mainFrame: false},
    {method: 'GET'}, {resourceType: 'fetch'}]) {
    assert.equal(await gate.allow({...request, ...changes}), false);
  }
  assert.equal(calls, 0);
  assert.equal(await gate.allow({...request, url, method: 'GET'}), true);
});

test('并发提交只调用一次宿主授权，之后不能重新授权', async () => {
  let calls = 0;
  const gate = new RequestGate(target, async () => { calls++; return true; });
  gate.arm(csrf, paymentRef);
  const results = await Promise.all(Array.from({length: 8}, () => gate.allow(request)));
  assert.equal(results.filter(Boolean).length, 1); assert.equal(calls, 1);
  assert.throws(() => gate.arm(csrf, paymentRef));
});

test('额外金额、重复字段或不同表单值不能到达授权入口', async () => {
  for (const postData of [request.postData+'&amount=1', request.postData+'&csrf='+csrf,
    request.postData.replace(paymentRef, 'DEMO-forged'), 'csrf='+csrf]) {
    let calls = 0;
    const gate = new RequestGate(target, async () => { calls++; return true; });
    gate.arm(csrf, paymentRef);
    assert.equal(await gate.allow({...request, postData}), false);
    assert.equal(calls, 0); assert.equal(gate.state, 'blocked');
  }
});

test('宿主拒绝、非布尔允许或异常不能放行，也不能再次提交', async () => {
  for (const authorize of [async () => false, async () => 'true', async () => { throw new Error('offline'); }]) {
    const gate = new RequestGate(target, authorize); gate.arm(csrf, paymentRef);
    assert.equal(await gate.allow(request), false);
    assert.equal(await gate.allow(request), false);
    assert.throws(() => gate.arm(csrf, paymentRef));
  }
});
