/* 本项目原创。显式运行才启动独立浏览器；仅访问本程序新建的临时本机商店。 */
'use strict';
const path = require('node:path');
const readline = require('node:readline');
const {spawn} = require('node:child_process');
const assert = require('node:assert/strict');
const {openCheckoutWorker} = require('../browser_worker.cjs');

async function main() {
  if (process.argv.length !== 3 || process.argv[2] !== '--run-demo') {
    console.log('Prepared browser verification. Use --run-demo only when independent Playwright browser execution is authorized.');
    return;
  }
  const {chromium} = require(process.env.AI_WALLET_PLAYWRIGHT_PATH || 'playwright');
  const fixture = spawn(process.env.AI_WALLET_PYTHON || 'python', ['-X', 'utf8', '-m', 'ai_wallet.tests.browser_fixture'],
    {cwd: path.resolve(__dirname, '../..'), windowsHide: true, stdio: ['pipe', 'pipe', 'pipe']});
  let pending, exited = false, unavailable = false;
  const lines = readline.createInterface({input: fixture.stdout});
  lines.on('line', line => { if (pending) { const {resolve, reject, timer} = pending; pending = null;
    clearTimeout(timer); try { resolve(JSON.parse(line)); } catch (e) { reject(e); } } });
  fixture.stderr.resume();
  const completion = new Promise(resolve => fixture.once('close', code => {
    exited = true;
    if (pending) { clearTimeout(pending.timer); pending.reject(new Error('Fixture stopped')); pending = null; }
    resolve(code);
  }));
  fixture.on('error', error => { if (pending) { clearTimeout(pending.timer); pending.reject(error); pending = null; } });
  function ask(command) {
    if (pending || exited || unavailable) throw new Error('Fixture unavailable');
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { pending = null; unavailable = true; reject(new Error('Fixture timeout')); }, 10000);
      pending = {resolve, reject, timer}; fixture.stdin.write(JSON.stringify(command) + '\n');
    });
  }
  const workers = [];
  try {
    const excessive = await ask({action: 'prepare', shop: 'market', quantity: 4});
    let rejectedChecks = 0;
    const overBudget = await openCheckoutWorker({chromium, checkoutUrl: excessive.url,
      amountCents: excessive.amount_cents, authorize: async () => { rejectedChecks++; return true; }});
    workers.push(overBudget);
    assert.equal((await overBudget.submit(excessive.payment_ref)).status, 'rejected');
    assert.equal(rejectedChecks, 0);
    assert.equal((await ask({action: 'state'})).charges, 0);
    await overBudget.close(); workers.pop();

    const denied = await ask({action: 'prepare', shop: 'gallery', quantity: 1});
    const blocked = await openCheckoutWorker({chromium, checkoutUrl: denied.url,
      amountCents: denied.amount_cents, authorize: async () => false});
    workers.push(blocked);
    await blocked.submit(denied.payment_ref);
    assert.equal((await ask({action: 'state'})).charges, 0);
    await blocked.close(); workers.pop();

    const ready = await ask({action: 'prepare', shop: 'market', quantity: 1});
    let checks = 0;
    const worker = await openCheckoutWorker({chromium, checkoutUrl: ready.url, amountCents: ready.amount_cents,
      // No unconditional grant: persist the wallet claim before allowing network submission.
      authorize: async observed => {
        checks++;
        const result = await ask({action: 'claim', shop: 'market', checkout_id: observed.siteOrderId,
          origin: observed.origin, amount_cents: observed.amountCents});
        return result.allowed === true;
      }});
    workers.push(worker);
    assert.equal((await worker.inspect()).status, 'approved');
    assert.equal((await worker.submit(ready.payment_ref)).status, 'paid');
    await assert.rejects(worker.submit(ready.payment_ref));
    const state = await ask({action: 'state'});
    assert.equal(checks, 1); assert.equal(state.charges, 1); assert.equal(state.spent_cents, 1900);
    await worker.close(); workers.pop();

    // Commit the original wallet claim, then lose the host response before HTTP can be sent.
    const interrupted = await ask({action: 'prepare', shop: 'market', quantity: 1});
    const uncertain = await openCheckoutWorker({chromium, checkoutUrl: interrupted.url,
      amountCents: interrupted.amount_cents, authorize: async observed => {
        const result = await ask({action: 'claim', shop: 'market', checkout_id: observed.siteOrderId,
          origin: observed.origin, amount_cents: observed.amountCents});
        assert.equal(result.allowed, true);
        throw new Error('Simulated host response loss');
      }});
    workers.push(uncertain);
    await uncertain.submit(interrupted.payment_ref);
    await uncertain.close(); workers.pop();
    let recoveryChecks = 0;
    const resumed = await openCheckoutWorker({chromium, checkoutUrl: interrupted.url,
      amountCents: interrupted.amount_cents, authorize: async () => { recoveryChecks++; return true; }});
    workers.push(resumed);
    assert.equal((await resumed.inspect()).status, 'unknown');
    assert.equal((await resumed.submit(interrupted.payment_ref)).status, 'unknown');
    assert.equal(recoveryChecks, 0);
    const recovered = await ask({action: 'state'});
    assert.equal(recovered.charges, 1); assert.equal(recovered.spent_cents, 1900);
    assert.equal(recovered.statuses.filter(status => status === 'unknown').length, 1);
    console.log(JSON.stringify({mode: 'demo', browser_verified: true, denied_charges: 0,
      successful_charges: 1, spent_cents: 1900, duplicate_submit_rejected: true,
      budget_rejected_before_submit: true, reopened_unknown_not_resubmitted: true}));
  } finally {
    const closed = await Promise.allSettled(workers.map(worker => worker.close()));
    if (!exited) fixture.stdin.end(JSON.stringify({action: 'close'}) + '\n');
    const timer = setTimeout(() => fixture.kill(), 5000);
    const fixtureExit = await completion; clearTimeout(timer); lines.close();
    assert.equal(fixtureExit, 0, 'Temporary fixture did not close cleanly');
    assert.equal(closed.some(result => result.status === 'rejected'), false, 'A temporary browser did not close cleanly');
  }
}
main().catch(() => { console.error('Browser verification failed; no claim of browser acceptance.'); process.exitCode = 1; });
