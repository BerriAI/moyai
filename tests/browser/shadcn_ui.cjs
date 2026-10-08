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

async function pageFor(t, route = 'tasks', fixture = 'populated', width = 1440) {
  const page = await browser.newPage({ viewport: { width, height: 1000 }, reducedMotion: 'reduce' });
  page.setDefaultTimeout(8000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  t.after(async () => { await page.close(); assert.deepEqual(errors, [], 'No uncaught browser errors'); });
  await page.goto(`${base}/?fixture=${fixture}#${route}`);
  await page.locator('#content h1').waitFor();
  return page;
}

for (const width of [1440, 768, 320]) {
  test(`all workspace and settings routes use shadcn and fit at ${width}px`, async t => {
    const page = await pageFor(t, 'tasks', 'populated', width);
    for (const route of ['tasks', 'settings', 'skills', 'memory', 'automations', 'connections', 'secrets', 'runtime', 'environments', 'users', 'spend']) {
      await page.evaluate(view => navigate(view), route);
      assert.equal(await page.locator('#content h1').count(), 1, route);
      const missing = await page.locator('#content button:not([data-slot]), #content select:not([data-slot]), #content textarea:not([data-slot]), #content table:not([data-slot])').count();
      assert.equal(missing, 0, `${route}: every base control comes from shadcn`);
      assert.ok(await page.locator('#content [data-slot]').count(), `${route}: components rendered`);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `${route}: no page overflow`);
    }
  });
}

test('checkboxes, switches and select updates preserve native form values and server writes', async t => {
  const page = await pageFor(t);
  await page.getByLabel('Session options', { exact: true }).click();
  const github = page.getByRole('checkbox', { name: 'GitHub', exact: true });
  assert.equal(await github.isChecked(), true);
  await github.click();
  assert.equal(await github.isChecked(), false);
  assert.equal(await page.evaluate(() => state.newDraft.plugins.includes('github')), false);
  await page.locator('#new-harness').selectOption('claude-agent-sdk');
  assert.equal(await page.locator('#new-harness').inputValue(), 'claude-agent-sdk');
  await page.evaluate(() => navigate('settings'));
  const toggle = page.locator('#send-immediately');
  const old = await toggle.isChecked();
  await toggle.click();
  await page.waitForFunction(expected => document.querySelector('#send-immediately-status').textContent.includes('Saved') && document.querySelector('#send-immediately').checked === expected, !old);
  await page.reload();
  await toggle.waitFor();
  assert.equal(await toggle.isChecked(), !old);
});

for (const width of [1440, 768, 320]) {
  test(`dialog focus, cancellation, saving and menu placement at ${width}px`, async t => {
    const page = await pageFor(t, 'memory', 'populated', width);
    const add = page.getByRole('button', { name: 'Add memory', exact: true });
    await add.click();
    let dialog = page.getByRole('dialog');
    const box = await dialog.boundingBox();
    assert.ok(box.x >= 0 && box.x + box.width <= width && box.y >= 0, JSON.stringify(box));
    assert.equal(await page.locator('#memory-title').evaluate(el => el === document.activeElement), true);
    await page.keyboard.press('Shift+Tab');
    assert.equal(await dialog.evaluate(el => el.contains(document.activeElement)), true);
    await page.keyboard.press('Escape');
    assert.equal(await dialog.count(), 0);
    await page.waitForFunction(() => document.activeElement?.id === 'memory-add');
    await add.click();
    await page.locator('#memory-title').fill(`Shadcn browser verification ${width}`);
    await page.locator('#memory-content').fill('Keep the purple workspace palette.');
    await page.getByRole('button', { name: 'Save memory', exact: true }).click();
    await page.getByRole('heading', { name: `Shadcn browser verification ${width}`, exact: true }).waitFor();
    assert.equal(await dialog.count(), 0);
    await page.evaluate(() => navigate('tasks'));
    if (width < 850) await page.locator('#open-sidebar').click();
    const trigger = page.locator('[data-session-actions]').first();
    await trigger.click();
    const menu = page.locator('[data-slot="popover-content"]');
    const bounds = await menu.boundingBox();
    assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width && bounds.y >= 0 && bounds.y + bounds.height <= 1000, JSON.stringify(bounds));
    await page.keyboard.press('End');
    assert.equal(await page.evaluate(() => document.activeElement.textContent), await menu.getByRole('button').last().textContent());
    await page.keyboard.press('Escape');
    assert.equal(await menu.count(), 0);
    await page.waitForFunction(() => document.activeElement?.hasAttribute('data-session-actions'));
  });
}

test('filters, empty results, failed loading and member permissions remain usable', async t => {
  const page = await pageFor(t, 'skills');
  const search = page.locator('#skill-search');
  await search.fill('no matching skill');
  await page.getByRole('button', { name: /Clear filters/i }).click();
  assert.equal(await search.inputValue(), '');
  await page.goto(`${base}/?fixture=error#skills`);
  await page.getByRole('button', { name: 'Try again', exact: true }).waitFor();
  await page.getByRole('button', { name: 'Try again', exact: true }).click();
  await page.getByRole('heading', { name: 'Unable to load this page' }).waitFor();
  await page.goto(`${base}/?fixture=member#settings`);
  await page.locator('.settings-page').waitFor();
  assert.equal(await page.locator('#settings-navigation a[href="#users"]').count(), 0);
  assert.equal(await page.locator('#settings-navigation a[href="#environments"]').count(), 0);
});

test('every settings editor opens with its existing fields and closes without leaking overlays', async t => {
  const page = await pageFor(t);
  const editors = [
    ['skills', 'Add skill'], ['automations', 'Create'],
    ['secrets', 'Add credential'], ['users', 'Add user'], ['environments', 'New environment'],
  ];
  for (const [view, label] of editors) {
    await page.evaluate(view => navigate(view), view);
    if (view === 'automations') await page.locator('.automation-create summary').click();
    await page.getByRole('button', { name: label, exact: true }).click();
    const dialog = page.getByRole('dialog');
    await dialog.waitFor();
    assert.ok(await dialog.locator('[data-slot="input"], [data-slot="textarea"]').count(), view);
    await page.keyboard.press('Escape');
    await dialog.waitFor({ state: 'detached' });
    assert.equal(await page.locator('[data-slot="dialog-overlay"]').count(), 0, view);
  }
});

test('reopening a dialog from a menu releases closed overlay layers before Escape', async t => {
  const page = await pageFor(t);
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  const open = async () => {
    await page.locator('[data-session-actions]').first().click();
    await page.locator('#session-actions').getByRole('button', { name: 'Rename', exact: true }).click();
    await page.getByLabel('Session name', { exact: true }).waitFor();
  };
  for (let attempt = 0; attempt < 5; attempt++) {
    await open();
    await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).click();
    await open();
    await page.getByLabel('Session name', { exact: true }).press('Escape');
    await page.getByRole('dialog').waitFor({ state: 'detached' });
    assert.equal(await page.locator('[data-slot="popover-content"]').count(), 0);
  }
});

for (const width of [1440, 768, 320]) {
  test(`automation drawer, metadata, checkboxes and template dialogs work at ${width}px`, async t => {
    const page = await pageFor(t, 'automations', 'populated', width);
    await page.locator('.automation-create summary').click();
    await page.getByRole('button', { name: 'Create', exact: true }).click();
    const dialog = page.getByRole('dialog');
    const bounds = await dialog.boundingBox();
    assert.ok(bounds.x >= 0 && Math.abs(bounds.x + bounds.width - width) <= 1, JSON.stringify(bounds));
    assert.equal(bounds.y, 0);
    assert.equal(bounds.height, 1000);
    await page.getByLabel('Automation name', { exact: true }).fill('Check the release');
    await page.getByLabel('Instructions', { exact: true }).fill('Review the release and summarize test results.');
    await page.getByRole('checkbox', { name: 'Queue overlapping event runs', exact: true }).click();
    assert.equal(await dialog.locator('form').evaluate(form => form.elements.queue_events.checked), true);
    await page.getByRole('button', { name: /Add metadata/ }).click();
    await page.getByLabel('Key', { exact: true }).fill('team');
    await page.getByLabel('Value', { exact: true }).fill('platform');
    const values = await dialog.locator('form').evaluate(form => Object.fromEntries(new FormData(form)));
    assert.equal(values.queue_events, 'on');
    assert.equal(values.metadata_key, 'team');
    assert.equal(values.metadata_value, 'platform');
    await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
    await dialog.waitFor({ state: 'detached' });
    await page.locator('.automation-create summary').click();
    await page.getByRole('button', { name: 'Template', exact: true }).click();
    await page.getByRole('dialog', { name: 'Automation templates' }).waitFor();
    await page.keyboard.press('Escape');
    assert.equal(await page.getByRole('dialog').count(), 0);
    assert.equal(await page.locator('[data-slot="dialog-overlay"]').count(), 0);
  });
}
