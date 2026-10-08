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

async function assertTableTextContained(page) {
  const overlaps = await page.locator('#content th:visible, #content td:visible').evaluateAll(cells => cells.flatMap(cell => {
    const bounds = cell.getBoundingClientRect();
    const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
    const failures = [];
    while (walker.nextNode()) {
      if (!walker.currentNode.textContent.trim()) continue;
      const range = document.createRange();
      range.selectNodeContents(walker.currentNode);
      if ([...range.getClientRects()].some(rect => rect.width && (rect.left < bounds.left - 1 || rect.right > bounds.right + 1))) {
        failures.push(walker.currentNode.textContent);
      }
    }
    return failures;
  }));
  assert.deepEqual(overlaps, [], 'Table text stays inside its own column');
}

for (const width of [1440, 1024, 768, 320]) {
  test(`analytics headings and metric summaries have clear spacing at ${width}px`, async t => {
    const page = await pageFor(t, 'spend', 'populated', width);
    for (const tab of ['leaderboard', 'users', 'overall']) {
      await page.locator(`#spend-tab-${tab}`).click();
      await page.locator('.analytics-metrics').first().waitFor();
      if (tab === 'leaderboard') {
        assert.deepEqual(await page.locator('.analytics-metrics strong').allTextContents(), ['16', '18', '7']);
      }
      const summaries = await page.locator('.analytics-metrics').evaluateAll(summaries => summaries.map(summary => {
        const box = el => el.getBoundingClientRect().toJSON();
        const heading = summary.parentElement.querySelector(':scope > h2');
        const description = summary.previousElementSibling.tagName === 'P' ? summary.previousElementSibling : null;
        return {
          summary: box(summary), heading: box(heading), description: description && box(description),
          metrics: [...summary.children].map(metric => ({
            box: box(metric), label: box(metric.querySelector('span')), value: box(metric.querySelector('strong')),
            note: metric.querySelector('small') && box(metric.querySelector('small')),
          })),
        };
      }));
      for (const { summary, heading, description, metrics } of summaries) {
        if (description) {
          const headingGap = description.top - heading.bottom;
          const metricsGap = summary.top - description.bottom;
          assert.ok(headingGap >= 4 && headingGap <= 10, `${tab}: title and description stay together`);
          assert.ok(metricsGap >= 16 && metricsGap <= 24, `${tab}: metrics are separated from the introduction`);
        }
        assert.ok(Math.abs(summary.left - heading.left) <= 1, `${tab}: heading and metrics share a leading edge`);
        for (const metric of metrics) {
          assert.ok(metric.value.top >= metric.label.bottom, `${tab}: metric value follows its label`);
          if (metric.note) assert.ok(metric.note.top >= metric.value.bottom, `${tab}: supporting text follows its value`);
          assert.ok(metric.box.left >= summary.left && metric.box.right <= summary.right + 1, `${tab}: columns fit their summary`);
          for (const peer of metrics.filter(peer => Math.abs(peer.box.top - metric.box.top) <= 1)) {
            assert.ok(Math.abs(peer.value.top - metric.value.top) <= 1, `${tab}: values in the same row align`);
          }
        }
      }
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `${tab}: no page overflow`);
    }
  });

  test(`memory preference label, field and arrow stay aligned at ${width}px`, async t => {
    const page = await pageFor(t, 'memory', 'populated', width);
    const control = page.getByLabel('How new memories are saved', { exact: true });
    const aligned = async () => {
      const boxes = await page.locator('.memory-learning').evaluate(row => {
        const box = el => el.getBoundingClientRect().toJSON();
        return {
          row: box(row), label: box(row.querySelector('label')), select: box(row.querySelector('select')),
          wrapper: box(row.querySelector('[data-slot="native-select-wrapper"]')),
          arrow: box(row.querySelector('[data-slot="native-select-icon"]')),
          heading: box(document.querySelector('.memory-controls>div')),
          action: box(document.querySelector('#memory-toggle')),
        };
      });
      const { row, label, select, wrapper, arrow, heading, action } = boxes;
      assert.ok(Math.abs(wrapper.width - select.width) <= 1, 'The wrapper follows the field width');
      assert.ok(arrow.left >= select.left && arrow.right <= select.right, 'The arrow stays inside the field');
      assert.ok(Math.abs(arrow.y + arrow.height / 2 - select.y - select.height / 2) <= 1, 'The arrow is vertically centered');
      assert.ok(Math.abs(label.x - heading.x) <= 1, 'The label shares the heading leading edge');
      assert.ok(select.right <= row.right && select.left >= row.left, 'The field stays inside its row');
      if (width > 650) {
        assert.ok(Math.abs(select.right - action.right) <= 1, 'The field shares the action trailing edge');
        assert.ok(Math.abs(label.y + label.height / 2 - select.y - select.height / 2) <= 1, 'Desktop label and field share a center line');
        assert.ok(label.height < 24, 'The desktop label remains on one line');
      } else {
        assert.ok(label.bottom < select.top, 'The mobile field stacks below its label');
        assert.ok(Math.abs(label.left - select.left) <= 1, 'The mobile field and label share a leading edge');
        assert.ok(Math.abs(select.right - row.right + 21) <= 1, 'The mobile field fills the padded row');
      }
    };
    await aligned();
    await control.selectOption('manual');
    await page.waitForFunction(() => document.querySelector('#memory-learning')?.disabled === false && document.querySelector('#memory-learning').value === 'manual');
    await page.reload();
    await control.waitFor();
    assert.equal(await control.inputValue(), 'manual', 'The selected saving mode persists');
    await aligned();
    await page.getByRole('button', { name: 'Pause memory', exact: true }).click();
    await page.getByRole('button', { name: 'Resume memory', exact: true }).waitFor();
    assert.equal(await control.isDisabled(), true);
    await aligned();
    await page.getByRole('button', { name: 'Resume memory', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#memory-learning')?.disabled === false);
    await control.selectOption('auto');
    await page.waitForFunction(() => document.querySelector('#memory-learning')?.disabled === false && document.querySelector('#memory-learning').value === 'auto');
    await aligned();
  });

  test(`library rows keep their leading edge and compact actions at ${width}px`, async t => {
    const page = await pageFor(t, 'skills', 'populated', width);
    for (const route of ['skills', 'secrets']) {
      if (route !== 'skills') await page.evaluate(view => navigate(view), route);
      const rows = page.locator('.skill-card, .secret-card');
      await rows.first().waitFor();
      if (route === 'secrets') {
        const select = await page.locator('#secret-scope-filter').boundingBox();
        const icon = await page.locator('.settings-toolbar [data-slot="native-select-icon"]').boundingBox();
        assert.ok(icon.x >= select.x && icon.x + icon.width <= select.x + select.width, 'The scope filter arrow stays inside its control');
        if (width >= 768) assert.equal(select.y, (await page.locator('#secret-search').boundingBox()).y, 'Desktop filters share a row');
      }
      const geometry = await rows.evaluateAll(rows => rows.map(row => {
        const box = el => el.getBoundingClientRect().toJSON();
        return { row: box(row), info: box(row.firstElementChild), actions: box(row.lastElementChild) };
      }));
      for (const { row, info, actions } of geometry) {
        assert.ok(Math.abs(info.x - row.x - 4) <= 1, `${route}: details align with the row's leading edge`);
        assert.ok(actions.right <= row.right + 1, `${route}: actions stay inside the row`);
        if (width > 1150) {
          assert.ok(actions.x >= info.right, `${route}: desktop actions follow the details horizontally`);
          assert.ok(row.height <= Math.max(info.height, actions.height) + 46, `${route}: row remains compact`);
        } else if (route === 'skills' || width <= 650) {
          assert.ok(actions.y >= info.bottom, `${route}: narrow actions wrap below details`);
          assert.ok(Math.abs(actions.x - info.x) <= 1, `${route}: wrapped actions stay left-aligned`);
        }
      }
    }
  });

  test(`pull request columns wrap and remain reachable at ${width}px`, async t => {
    const page = await pageFor(t, 'spend', 'populated', width);
    await page.locator('#spend-tab-prs').click();
    const table = page.locator('.analytics-pr-table');
    await table.waitFor();
    const filter = page.locator('#spend-pr-status');
    const textRight = await filter.evaluate(select => {
      const style = getComputedStyle(select);
      const context = document.createElement('canvas').getContext('2d');
      context.font = style.font;
      return select.getBoundingClientRect().left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft) + context.measureText(select.selectedOptions[0].textContent).width;
    });
    const arrow = await page.locator('.analytics-pr-filters [data-slot="native-select-icon"]').boundingBox();
    assert.ok(textRight + 4 <= arrow.x, 'The selected status leaves space for its arrow');
    await assertTableTextContained(page);
    const region = page.getByRole('region', { name: 'Pull requests first tracked in selected dates', exact: true });
    const bounds = await table.boundingBox();
    const frame = await region.boundingBox();
    assert.ok(bounds.width >= 900, 'Narrow screens retain readable column widths');
    if (width === 1440) assert.ok(bounds.width <= frame.width, 'All six columns fit the desktop content area');
    if (bounds.width > frame.width) {
      await region.focus();
      await page.keyboard.press('ArrowRight');
      await page.waitForFunction(() => document.querySelector('.analytics-pr-table').closest('[role="region"]').scrollLeft > 0);
      await region.evaluate(el => { el.scrollLeft = el.scrollWidth; });
      const last = await table.locator('tbody tr').first().locator('td').last().boundingBox();
      assert.ok(last.x >= frame.x && last.x + last.width <= frame.x + frame.width + 1, 'The named scroll region reveals the spend column');
    }
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'The table never widens the page');
    await page.locator('#spend-pr-search').fill('no matching pull request');
    await page.getByRole('button', { name: 'Clear filters', exact: true }).click();
    await table.waitFor();
    await page.locator('#spend-pr-status').selectOption('merged');
    assert.ok(await table.locator('tbody tr').count());
    assert.equal(await table.locator('tbody .badge').evaluateAll(badges => badges.every(badge => badge.textContent === 'Merged')), true);
    await assertTableTextContained(page);
    await page.locator('#spend-tab-leaderboard').click();
    assert.ok((await page.locator('.analytics-pr-leaderboard').boundingBox()).width >= 980, 'Leaderboard columns do not collapse on mobile');
    await page.locator('[data-pr-contributor]').first().click();
    await page.locator('.analytics-pr-detail').waitFor();
    await assertTableTextContained(page);
    await page.getByRole('button', { name: 'Close details', exact: true }).click();
    for (const tab of ['overall', 'users', 'history', 'infrastructure']) {
      await page.locator(`#spend-tab-${tab}`).click();
      for (const summary of await page.locator('.analytics-breakdown>summary, .identity-overrides>summary').all()) {
        if (!await summary.evaluate(el => el.parentElement.open)) await summary.click();
      }
      await assertTableTextContained(page);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `${tab}: no page overflow`);
    }
  });
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
      await assertTableTextContained(page);
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
