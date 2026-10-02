/* 本项目原创。独立Node客户端通过标准输入输出实际调用Python MCP子进程；无第三方SDK。 */
'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn, spawnSync} = require('node:child_process');
const readline = require('node:readline');
const root = path.resolve(__dirname, '../..');
const python = process.env.AI_WALLET_PYTHON || 'python';

function connect(config, cwd) {
  const child = spawn(config.command, config.args, {cwd, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe']});
  const pending = new Map(); let sequence = 0;
  const lines = readline.createInterface({input: child.stdout});
  const completion = new Promise(resolve => child.once('close', code => {
    for (const item of pending.values()) { clearTimeout(item.timer); item.reject(new Error('MCP process closed')); }
    pending.clear(); resolve(code);
  }));
  child.stderr.resume();
  child.on('error', error => { for (const item of pending.values()) { clearTimeout(item.timer); item.reject(error); } pending.clear(); });
  lines.on('line', line => {
    let message;
    try { message = JSON.parse(line); }
    catch (error) {
      for (const item of pending.values()) { clearTimeout(item.timer); item.reject(error); }
      pending.clear(); child.kill(); return;
    }
    const item = pending.get(message.id);
    if (!item) return;
    pending.delete(message.id); clearTimeout(item.timer);
    try {
      assert.equal(message.jsonrpc, '2.0'); assert.equal('error' in message, false);
      item.resolve(message.result);
    } catch (error) { item.reject(error); }
  });
  return {
    request(method, params = {}) {
      const id = ++sequence;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => { pending.delete(id); reject(new Error('MCP response timeout')); }, 5000);
        pending.set(id, {resolve, reject, timer});
        child.stdin.write(JSON.stringify({jsonrpc: '2.0', id, method, params}) + '\n');
      });
    },
    notify(method) { child.stdin.write(JSON.stringify({jsonrpc: '2.0', method}) + '\n'); },
    async close() {
      child.stdin.end(); const timer = setTimeout(() => child.kill(), 5000);
      const code = await completion; clearTimeout(timer); lines.close(); assert.equal(code, 0);
    },
  };
}

test('独立Node客户端读取生成配置，握手、发现工具并跨进程重连原模拟采购', async () => {
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'wallet-mcp-client-'));
  const database = path.join(folder, 'isolated.sqlite');
  let client;
  try {
    const setup = spawnSync(python, ['-X', 'utf8', '-c', [
      'import sys,json', 'from ai_wallet.allocation import AllocationWallet',
      'from ai_wallet.agent_planner import TaskPlanner',
      'w=AllocationWallet(sys.argv[1])',
      'w.configure_pool({"total_cents":10000,"expected_revision":1})',
      't=TaskPlanner(w,"mcp-client-test").propose("MCP demo",3000,"isolated test","demo")',
      'w.decide_budget(t["task_id"],{"decision":"approve","budget_cents":3000,"expected_revision":t["revision"]})',
      'print(json.dumps(t))',
    ].join('\n'), database], {cwd: root, encoding: 'utf8', timeout: 10000, windowsHide: true});
    assert.equal(setup.status, 0); const task = JSON.parse(setup.stdout);
    const printed = spawnSync(python, [path.join(root, 'ai_wallet/mcp_entry.py'), '--database', database,
      '--agent-id', 'mcp-client-test', '--print-config', '--enable-demo-purchases'], {cwd: folder, encoding: 'utf8', timeout: 10000, windowsHide: true});
    assert.equal(printed.status, 0);
    const config = JSON.parse(printed.stdout).mcpServers['yuxing-wallet-demo'];
    async function initialize() {
      client = connect(config, folder);
      const response = await client.request('initialize', {protocolVersion: '2025-11-25', capabilities: {},
        clientInfo: {name: 'independent-node-fixture', version: '1'}});
      assert.equal(response.protocolVersion, '2025-11-25'); assert.ok(response.capabilities.tools);
      client.notify('notifications/initialized');
    }
    await initialize();
    const {tools} = await client.request('tools/list');
    assert.equal(tools.length, 6); assert.ok(tools.some(tool => tool.name === 'wallet_purchase_submit'));
    const args = {name: 'wallet_purchase_submit', arguments: {task_id: task.task_id,
      product_id: 'image-12', reason: 'MCP test', request_id: 'same-purchase'}};
    const first = await client.request('tools/call', args);
    assert.equal(first.isError, false); assert.equal(first.structuredContent.status, 'paid');
    assert.deepEqual(JSON.parse(first.content[0].text), first.structuredContent);
    await client.close(); client = null;
    await initialize();
    const again = await client.request('tools/call', args);
    assert.deepEqual(again, first);
    const context = await client.request('tools/call', {name: 'wallet_purchase_context', arguments: {task_id: task.task_id}});
    assert.equal(context.structuredContent.available_cents, 1800);
  } finally {
    try { if (client) await client.close(); }
    finally {
      const resolved = path.resolve(folder);
      assert.equal(path.dirname(resolved), path.resolve(os.tmpdir()));
      assert.ok(path.basename(resolved).startsWith('wallet-mcp-client-'));
      fs.rmSync(resolved, {recursive: true, force: true});
    }
  }
});
