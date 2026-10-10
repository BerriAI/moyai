const assert = require('node:assert/strict');
const { test, before, after } = require('node:test');
const { spawn } = require('node:child_process');
const { chromium } = require('playwright');

let server, browser, base;
before(async () => {
  server = spawn(process.execPath, ['scripts/settings_ui_preview.cjs', '--port', '0'], { stdio: ['ignore', 'pipe', 'inherit'] });
  base = await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.once('exit', code => reject(new Error(`Preview exited: ${code}`)));
    server.stdout.on('data', chunk => { const url = chunk.toString().match(/http:\/\/127\.0\.0\.1:\d+/); if (url) resolve(url[0]); });
  });
  browser = await chromium.launch({ headless: true });
});
after(async () => { await browser?.close(); server?.kill(); });

async function pageFor(t, width, fixture = 'swarm') {
  const page = await browser.newPage({ viewport: { width, height: 1000 }, reducedMotion: 'reduce' });
  page.setDefaultTimeout(8000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  t.after(async () => { await page.close(); assert.deepEqual(errors, []); });
  await page.goto(`${base}/?fixture=${fixture}#tasks`);
  await page.getByRole('button', { name: 'Swarm', exact: true }).click();
  return page;
}
async function choose(page, name, value) {
  await page.getByRole('combobox', { name, exact: true }).click();
  await page.getByRole('option', { name: value, exact: true }).click();
}

for (const width of [1440, 768, 320]) test(`continuous and multi-day swarm controls persist at ${width}px`, async t => {
  const page = await pageFor(t, width);
  assert.equal(await page.locator('#swarm-budget').inputValue(), '0');
  await choose(page, 'Swarm duration', 'Custom…');
  await page.getByRole('spinbutton', { name: 'Duration amount' }).fill('2');
  await choose(page, 'Duration unit', 'days');
  const overflow = await page.locator('.new-session-mode').evaluate(el => el.scrollWidth - el.clientWidth);
  assert.ok(overflow <= 1, 'Duration controls fit the viewport');
  await page.getByRole('textbox', { name: 'Message Moyai' }).fill('Research and improve the prototype.');
  const creation = page.waitForRequest(request => request.method() === 'POST' && new URL(request.url()).pathname === '/api/runs');
  await page.getByRole('button', { name: 'Start session', exact: true }).click();
  assert.equal((await creation).postDataJSON().swarm.budget_seconds, 172800);
  await page.getByRole('button', { name: 'Change swarm duration' }).click();
  const dialog = page.getByRole('dialog', { name: 'Swarm duration', exact: true });
  await dialog.waitFor();
  assert.equal(await dialog.getByRole('spinbutton', { name: 'Duration amount' }).inputValue(), '2');
  const box = await dialog.boundingBox();
  assert.ok(box.x >= 0 && box.x + box.width <= width + 1, 'Duration dialog fits the viewport');
  await choose(page, 'Duration unit', 'days');
  await dialog.getByRole('spinbutton', { name: 'Duration amount' }).fill('31');
  await dialog.getByRole('button', { name: 'Save duration' }).click();
  await dialog.getByRole('alert').getByText('Choose a duration from 1 minute to 30 days, or Until stopped.').waitFor();
  await choose(page, 'Swarm duration', 'Until stopped');
  const update = page.waitForRequest(request => request.method() === 'PATCH');
  await dialog.getByRole('button', { name: 'Save duration' }).click();
  assert.deepEqual((await update).postDataJSON(), { budget_seconds: 0 });
  await dialog.waitFor({ state: 'hidden' });
  await page.locator('.swarm-clock').getByText('Until stopped', { exact: true }).waitFor();
  await page.reload();
  await page.locator('.swarm-clock').getByText('Until stopped', { exact: true }).waitFor();
  await page.getByRole('button', { name: 'Change swarm duration' }).click();
  assert.equal(await page.locator('#swarm-time-budget').inputValue(), '0');
  await page.getByRole('combobox', { name: 'Swarm duration', exact: true }).click();
  await page.getByRole('option', { name: '1 day', exact: true }).press('Escape');
  assert.equal(await dialog.isVisible(), true, 'Escape closes the menu before its dialog');
  await dialog.getByRole('button', { name: 'Cancel' }).click();
});

test('expired swarms keep their conversation and accept a plain-English continuation', async t => {
  const page = await pageFor(t, 1440, 'swarm-expired');
  await page.goto(`${base}/?fixture=swarm-expired#run=${'a'.repeat(32)}`);
  await page.getByRole('button', { name: 'Resume', exact: true }).waitFor();
  const input = page.getByRole('textbox', { name: 'Message Moyai', exact: true });
  await input.fill('Keep going for 2 more days.');
  assert.equal(await page.getByRole('button', { name: 'Send message', exact: true }).isEnabled(), true);
  const request = page.waitForRequest(request => request.method() === 'POST' && request.url().endsWith('/messages'));
  await input.press('Enter');
  assert.equal((await request).postDataJSON().content, 'Keep going for 2 more days.');
  await page.getByRole('button', { name: 'Pause', exact: true }).waitFor();
  assert.equal(new URL(page.url()).hash, `#run=${'a'.repeat(32)}`);
});
