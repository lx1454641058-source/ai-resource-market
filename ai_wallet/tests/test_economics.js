/* 来源：本项目原创。验证经营测算的亏损、未知、精度和边界；无第三方依赖。 */
'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {calculate, money} = require('../planning/economics.js');
const sample = {customers:'5', price:'39', fixed:'200', variable:'5', minutes:'15', hourly:'60', target:'5000'};
test('五人试点计入支持时间后亏损；目标人数向上取整', () => {
  const r = calculate(sample);
  assert.equal(money(r.revenue), '¥195.00');
  assert.equal(money(r.cashSurplus), '-¥30.00');
  assert.equal(money(r.surplus), '-¥105.00');
  assert.equal(money(r.contribution), '¥19.00');
  assert.equal(r.cashBreakEven, 6);
  assert.equal(r.breakEven, 11);
  assert.equal(r.targetCustomers, 274);
  assert.equal(r.supportMinutes, 75);
});
test('未知成本不会当零，已知收入仍可展示', () => {
  const r = calculate({customers:'5',price:'39'});
  assert.equal(money(r.revenue), '¥195.00');
  assert.equal(r.surplus, undefined);
  assert.equal(r.cashBreakEven, undefined);
  assert.ok(r.missing.includes('每月固定现金成本'));
});
test('每人贡献为负或零时，扩大规模不能回收固定成本', () => {
  for (const price of ['0','20']) {
    const r = calculate({...sample,price});
    assert.equal(r.breakEven, null);
    assert.equal(r.targetCustomers, null);
  }
});
test('零客户仍发生固定成本，支持时间为零', () => {
  const r = calculate({...sample,customers:'0'});
  assert.equal(money(r.surplus), '-¥200.00');
  assert.equal(r.supportMinutes, 0);
});
test('零成本可保本但零贡献不能实现正结余目标', () => {
  const r = calculate({customers:'0',price:'0',fixed:'0',variable:'0',minutes:'0',hourly:'0',target:'1'});
  assert.equal(r.breakEven, 0);
  assert.equal(r.targetCustomers, null);
});
test('按分钟保留精度，避免整数边界多算一个客户', () => {
  const r = calculate({customers:'60',price:'1.00',fixed:'59',variable:'0',minutes:'1',hourly:'1',target:'0'});
  assert.equal(r.breakEven, 60);
  assert.equal(money(r.surplus), '¥0.00');
  assert.equal(money(-30), '-¥0.01');
  assert.equal(money(30), '¥0.01');
});
test('非法金额、负数、过大值与非整数客户必须报错', () => {
  for (const value of ['-1','Infinity','1e3','abc','0.001','100000.01']) assert.ok(calculate({...sample,price:value}).errors.length);
  for (const value of ['1.5','100001']) assert.ok(calculate({...sample,customers:value}).errors.length);
});
test('目标缺失不阻止已知成本的结余计算', () => {
  const r = calculate({...sample,target:''});
  assert.equal(money(r.surplus), '-¥105.00');
  assert.equal(r.targetCustomers, undefined);
});
test('最大输入组合仍在安全整数范围内', () => {
  const r = calculate({customers:'100000',price:'100000',fixed:'1000000',variable:'100000',minutes:'1440',hourly:'10000',target:'1000000'});
  for (const key of ['revenue','cashSurplus','contribution','surplus','supportMinutes']) assert.ok(Number.isSafeInteger(r[key]), key);
});
