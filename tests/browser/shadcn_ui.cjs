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

async function choose(page, source, value) {
  const label = await source.evaluate((select, value) => [...select.options].find(option => option.value === value).label, value);
  await source.locator('..').getByRole('combobox').click();
  await page.getByRole('option', { name: label, exact: true }).click();
}

async function inspectMenus(page, width) {
  const triggers = page.locator('[data-slot="select-trigger"]');
  for (const trigger of await triggers.all()) {
    if (!await trigger.isVisible() || await trigger.evaluate(el => !!el.closest('[aria-hidden=true],[inert]') || el.getBoundingClientRect().right <= 0)) continue;
    if (await trigger.isDisabled()) continue;
    assert.ok(await trigger.getAttribute('aria-label'), 'Dropdowns have a name, including dynamically inserted controls');
    await page.keyboard.press('Tab');
    await trigger.focus();
    assert.equal(await trigger.evaluate(el => getComputedStyle(el).outlineStyle), 'solid', `Keyboard focus is visible: ${await trigger.getAttribute('aria-label')}`);
    await page.keyboard.press('ArrowDown');
    const menu = page.getByRole('listbox');
    await menu.waitFor();
    const field = await trigger.boundingBox();
    const bounds = await menu.boundingBox();
    assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width + 1 && bounds.y >= 0 && bounds.y + bounds.height <= 1001, JSON.stringify(bounds));
    assert.ok(bounds.y >= field.y + field.height || bounds.y + bounds.height <= field.y, 'Menus never cover their field');
    const overflow = await menu.getByRole('option').evaluateAll(items => items.some(item => {
      const range = document.createRange();
      range.selectNodeContents(item);
      const bounds = item.getBoundingClientRect();
      return [...range.getClientRects()].some(rect => rect.width && (rect.left < bounds.left - 1 || rect.right > bounds.right + 1));
    }));
    assert.equal(overflow, false, 'Long option text wraps within the menu');
    await page.keyboard.press('End');
    const last = menu.locator('[role=option]:not([aria-disabled=true])').last();
    await page.waitForFunction(el => el === document.activeElement, await last.elementHandle());
    const lastBounds = await last.boundingBox();
    assert.ok(lastBounds.y >= bounds.y && lastBounds.y + lastBounds.height <= bounds.y + bounds.height + 1, 'Keyboard navigation scrolls the last option into view');
    await page.keyboard.press('Escape');
    await menu.waitFor({ state: 'detached' });
    await page.waitForFunction(el => el === document.activeElement, await trigger.elementHandle());
    assert.equal(await trigger.evaluate(el => el === document.activeElement), true, 'Escape restores the trigger');
  }
}

for (const width of [1440, 768, 320]) {
  test(`styled dropdowns fit every page and editor at ${width}px`, async t => {
    const page = await pageFor(t, 'tasks', 'populated', width);
    await page.getByLabel('Session options', { exact: true }).click();
    await inspectMenus(page, width);
    const editors = { skills: 'Add skill', memory: 'Add memory', automations: 'Create', secrets: 'Add credential', users: 'Add user', environments: 'New environment' };
    for (const route of ['memory', 'secrets', 'runtime', 'environments', 'users', 'automations', 'skills', 'connections', 'spend']) {
      await page.evaluate(view => navigate(view), route);
      await inspectMenus(page, width);
      if (route === 'spend') {
        await page.locator('.analytics-range > summary').click();
        await inspectMenus(page, width);
        await page.locator('.analytics-range > summary').click();
        for (const tab of ['prs', 'users', 'history', 'infrastructure']) {
          await page.locator(`#spend-tab-${tab}`).click();
          if (tab === 'infrastructure') await page.getByRole('button', { name: 'Add monthly bill', exact: true }).click();
          await inspectMenus(page, width);
        }
      }
      if (route === 'connections') {
        await page.locator('[data-manage="github"]').click();
        await inspectMenus(page, width);
        await page.keyboard.press('Escape');
        await page.getByRole('dialog').waitFor({ state: 'detached' });
      }
      if (editors[route]) {
        if (route === 'automations') await page.locator('.automation-create summary').click();
        await page.getByRole('button', { name: editors[route], exact: true }).click();
        await page.getByRole('dialog').waitFor();
        await inspectMenus(page, width);
        if (route === 'automations') {
          const dialog = page.getByRole('dialog');
          await choose(page, dialog.locator('select[name=frequency]'), 'weekly');
          await inspectMenus(page, width);
          await choose(page, dialog.locator('select[name=weekday]'), '2');
          assert.equal(await dialog.locator('select[name=weekday]').inputValue(), '2');
          await choose(page, dialog.locator('select[name=source]'), 'github');
          await inspectMenus(page, width);
          await choose(page, dialog.locator('select[name=event]'), 'check_run.completed');
          assert.equal(await dialog.locator('input[name=conclusion]').isVisible(), true);
          assert.equal(await dialog.locator('input[name=label]').isVisible(), false);
          await page.getByRole('listbox').waitFor({ state: 'detached' });
          await page.waitForFunction(el => el === document.activeElement, await dialog.getByRole('combobox', { name: 'Event', exact: true }).elementHandle());
        }
        await page.keyboard.press('Escape');
        await page.getByRole('dialog').waitFor({ state: 'detached' });
      }
    }
  });
}

test('select adapter preserves values, validation, reset, disabled options and dynamic choices without unsolicited writes', async t => {
  const page = await pageFor(t);
  const writes = [];
  page.on('request', request => { if (!['GET', 'HEAD'].includes(request.method())) writes.push(request.url()); });
  await page.reload();
  await page.locator('#new-model').waitFor();
  await page.waitForTimeout(100);
  assert.deepEqual(writes, [], 'Mounting and synchronizing dropdowns never saves a preference');
  await page.evaluate(() => {
    MoyaiUI.render(document.querySelector('#content'), '<form id="select-test"><label for="choice">Choice</label><select id="choice" name="choice" required><option value="" selected disabled>Choose a value</option><optgroup label="Available"><option value="one">First option</option><option value="two">Second option</option><option value="blocked" disabled>Unavailable option</option></optgroup></select><button type="submit">Save</button></form>');
    window.selectEvents = [];
    for (const type of ['input', 'change']) document.querySelector('#select-test').addEventListener(type, event => window.selectEvents.push([type, event.target.id]));
    document.querySelector('#select-test').onsubmit = event => event.preventDefault();
  });
  const source = page.locator('#choice');
  const trigger = page.getByRole('combobox', { name: 'Choice', exact: true });
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  assert.equal(await trigger.getAttribute('aria-invalid'), 'true');
  assert.equal(await trigger.evaluate(el => el === document.activeElement), true);
  const invalidField = await trigger.boundingBox();
  const error = await page.locator('[data-slot="select-error"]').boundingBox();
  assert.ok(error.y >= invalidField.y + invalidField.height, 'Validation messages stay below the dropdown, outside its control');
  await trigger.click();
  assert.equal(await page.getByRole('option', { name: 'Unavailable option' }).getAttribute('aria-disabled'), 'true');
  await page.getByRole('option', { name: 'Second option', exact: true }).click();
  assert.equal(await source.inputValue(), 'two');
  assert.equal(await source.evaluate(el => new FormData(el.form).get('choice')), 'two');
  assert.deepEqual(await page.evaluate(() => window.selectEvents), [['input', 'choice'], ['change', 'choice']]);
  await source.evaluate(el => { el.value = 'one'; });
  await page.waitForFunction(() => document.querySelector('#choice').parentElement.querySelector('[data-slot="select-trigger"]').textContent.includes('First option'));
  await source.evaluate(el => { el.disabled = true; });
  await page.waitForFunction(() => document.querySelector('#choice').parentElement.querySelector('[data-slot="select-trigger"]').disabled);
  assert.equal(await trigger.isDisabled(), true);
  await source.evaluate(el => { el.disabled = false; el.form.reset(); });
  await page.waitForFunction(() => document.querySelector('#choice').parentElement.querySelector('[data-slot="select-trigger"]').textContent.includes('Choose a value'));
  await source.evaluate(el => MoyaiUI.render(el, '<option value="long">A long option label that remains readable in the styled dropdown</option><option value="new" selected>New choice</option>'));
  await page.waitForFunction(() => document.querySelector('#choice').parentElement.querySelector('[data-slot="select-trigger"]').textContent.includes('New choice'));
  await trigger.focus();
  await page.keyboard.press('ArrowDown');
  await page.getByRole('listbox').waitFor();
  await page.keyboard.press('Home');
  await page.waitForFunction(() => document.activeElement?.getAttribute('role') === 'option' && document.activeElement.textContent.includes('A long option'));
  await page.keyboard.press('Enter');
  assert.equal(await source.inputValue(), 'long');
  await page.locator('label[for=choice]').click();
  assert.equal(await trigger.evaluate(el => el === document.activeElement), true, 'Labels focus the styled control');
});

test('read-only settings content remains keyboard scrollable', async t => {
  const page = await pageFor(t, 'runtime', 'member', 320);
  const content = page.locator('#content');
  assert.equal(await content.getAttribute('tabindex'), '0');
  await content.focus();
  await page.keyboard.press('PageDown');
  await page.waitForFunction(() => document.querySelector('#content').scrollTop > 0);
});

test('library cards do not stack shadcn spacing over existing page spacing', async t => {
  const page = await pageFor(t);
  for (const [route, card, first, next] of [
    ['memory', '.memory-card', '.memory-card-top', 'h2'],
    ['connections', '.connection-card', '.connection-top', ':scope > p'],
    ['environments', '.environment-card', '.environment-card-top', '.environment-card-state'],
  ]) {
    await page.evaluate(view => navigate(view), route);
    const gaps = await page.locator(card).evaluateAll((cards, { first, next }) => cards.map(card => card.querySelector(next).getBoundingClientRect().top - card.querySelector(first).getBoundingClientRect().bottom), { first, next });
    assert.ok(gaps.every(gap => gap >= 0 && gap <= 24), `${route}: related content retains one spacing step: ${gaps}`);
  }
});

test('identity linking disables its submit action, not the new dropdown trigger', async t => {
  const page = await pageFor(t, 'spend');
  await page.route('**/api/spend?*', async route => {
    const response = await route.fetch();
    const data = await response.json();
    data.identities = [
      { id: 'google-one', kind: 'google', name: 'Alex', email: 'alex@example.com' },
      { id: 'slack-one', kind: 'slack', name: 'Alex in Slack', email: 'alex@example.com' },
    ];
    await route.fulfill({ response, json: data });
  });
  await page.reload();
  await page.locator('#spend-tab-infrastructure').click();
  await page.locator('.identity-overrides > summary').click();
  const form = page.locator('.spend-link-form');
  await choose(page, form.locator('select[name=google]'), 'google-one');
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  t.after(() => release());
  const request = page.waitForRequest('**/api/admin/spend/link-slack');
  await page.route('**/api/admin/spend/link-slack', async route => {
    await gate;
    await route.fulfill({ status: 503, json: { detail: 'Link not saved. Try again.' } });
  });
  const save = form.getByRole('button', { name: 'Save manual link', exact: true });
  await save.click();
  assert.deepEqual((await request).postDataJSON(), { slack_user_id: 'slack-one', google_user_id: 'google-one' });
  assert.equal(await save.isDisabled(), true);
  assert.equal(await form.getByRole('combobox').isDisabled(), false);
  release();
  await page.locator('#toast').filter({ hasText: 'Link not saved. Try again.' }).waitFor();
  assert.equal(await save.isDisabled(), false);
});

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
    const control = page.locator('#memory-learning');
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
    await choose(page, control, 'manual');
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
    await choose(page, control, 'auto');
    await page.waitForFunction(() => document.querySelector('#memory-learning')?.disabled === false && document.querySelector('#memory-learning').value === 'auto');
    await aligned();
  });

  test(`library rows keep their leading edge and compact actions at ${width}px`, async t => {
    const page = await pageFor(t, 'skills', 'populated', width);
    for (const route of ['skills', 'secrets']) {
      if (route !== 'skills') await page.evaluate(view => navigate(view), route);
      const rows = page.locator('.skill-card, .secret-card');
      await rows.first().waitFor();
      if (route === 'skills') {
        const skills = await rows.evaluateAll(rows => rows.map(row => {
          const box = el => el.getBoundingClientRect().toJSON();
          return { row: box(row), info: box(row.firstElementChild), title: box(row.querySelector('.skill-title')),
            reference: box(row.querySelector('code')), description: box(row.querySelector('p')),
            actions: box(row.lastElementChild), buttons: [...row.querySelectorAll('button')].map(box) };
        }));
        for (const { row, info, title, reference, description, actions, buttons } of skills) {
          assert.ok(reference.top >= title.top && reference.bottom <= title.bottom + 1, 'Skill references belong to the title line');
          assert.ok(Math.abs(description.top - title.bottom - 4) <= 1, 'Skill descriptions stay close to their title');
          if (width > 650) {
            assert.ok(row.height <= Math.max(info.height, actions.height) + 18, 'Skill rows have compact vertical padding');
            assert.ok(Math.abs(actions.top + actions.height / 2 - info.top - info.height / 2) <= 1, 'Skill actions are centered with their details');
            if (width === 1440) assert.ok(row.height <= 72, 'Desktop skills fit in compact two-line rows');
          } else {
            assert.ok(Math.abs(actions.top - info.bottom - 8) <= 1, 'Mobile skill actions stay close to their details');
            assert.ok(buttons.every(button => button.height >= 44), 'Mobile skill actions retain touch-sized targets');
          }
        }
        if (width >= 768) {
          const search = await page.locator('#skill-search').boundingBox();
          const count = await page.locator('#skill-count').boundingBox();
          assert.ok(Math.abs(search.y + search.height / 2 - count.y - count.height / 2) <= 1, 'Skill counts share the search toolbar');
        }
      }
      if (route === 'secrets') {
        const select = await page.locator('#secret-scope-filter').boundingBox();
        const icon = await page.locator('.settings-toolbar [data-slot="native-select-icon"]').boundingBox();
        assert.ok(icon.x >= select.x && icon.x + icon.width <= select.x + select.width, 'The scope filter arrow stays inside its control');
        if (width >= 768) assert.equal(select.y, (await page.locator('#secret-search').boundingBox()).y, 'Desktop filters share a row');
        const secrets = await rows.evaluateAll(rows => rows.map(row => {
          const box = el => el.getBoundingClientRect().toJSON();
          return {
            row: box(row), info: box(row.firstElementChild), actions: box(row.lastElementChild),
            title: box(row.querySelector('strong')), reference: box(row.querySelector('.secret-reference')),
            badges: [...row.querySelectorAll('.secret-scope')].map(box),
            buttons: [...row.querySelectorAll('button')].map(button => {
              const text = document.createRange();
              text.selectNodeContents(button);
              return { box: box(button), textLines: [...text.getClientRects()].filter(rect => rect.width).length };
            }),
          };
        }));
        for (const [index, { row, info, actions, title, reference, badges, buttons }] of secrets.entries()) {
          if (index) assert.ok(Math.abs(row.top - secrets[index - 1].row.bottom) <= 1, 'Secret rows meet at their divider without extra gaps');
          assert.ok(Math.abs(badges[0].left - title.left) <= 1, 'Secret badges share the title leading edge');
          if (reference.top >= title.bottom) {
            assert.ok(Math.abs(reference.left - title.left) <= 1, 'Wrapped references share the title leading edge');
          } else {
            assert.ok(Math.abs(reference.left - title.right - 8) <= 1, 'Inline references stay close to their title');
          }
          for (let index = 1; index < badges.length; index++) {
            if (Math.abs(badges[index].top - badges[index - 1].top) <= 1) {
              assert.ok(Math.abs(badges[index].left - badges[index - 1].right - 8) <= 1, 'Adjacent badges use one consistent gap');
            } else {
              assert.ok(Math.abs(badges[index].left - title.left) <= 1, 'Wrapped badges keep the leading edge');
            }
          }
          for (const button of buttons) {
            assert.equal(button.textLines, 1, 'Credential action labels stay on one line');
            assert.ok(button.box.left >= row.left && button.box.right <= row.right + 1, 'Credential actions fit their row');
          }
          if (width > 650) {
            assert.ok(Math.abs(actions.top + actions.height / 2 - info.top - info.height / 2) <= 1, 'Desktop credential actions are centered with their details');
            assert.ok(row.height <= Math.max(info.height, actions.height) + 26, 'Secret rows have compact vertical padding');
            if (width === 1440 && !index) assert.ok(row.height <= 80, 'A standard desktop secret fits in a compact two-line row');
          } else {
            assert.ok(Math.abs(actions.top - info.bottom - 8) <= 1, 'Mobile actions stay close to their details');
            assert.ok(buttons.every(button => button.box.height >= 44), 'Mobile credential actions retain touch-sized targets');
          }
        }
      }
      const geometry = await rows.evaluateAll(rows => rows.map(row => {
        const box = el => el.getBoundingClientRect().toJSON();
        return { row: box(row), info: box(row.firstElementChild), actions: box(row.lastElementChild) };
      }));
      for (const { row, info, actions } of geometry) {
        assert.ok(Math.abs(info.x - row.x - 4) <= 1, `${route}: details align with the row's leading edge`);
        assert.ok(actions.right <= row.right + 1, `${route}: actions stay inside the row`);
        if (width > 650) {
          assert.ok(actions.x >= info.right, `${route}: desktop actions follow the details horizontally`);
          assert.ok(row.height <= Math.max(info.height, actions.height) + 46, `${route}: row remains compact`);
        } else {
          assert.ok(actions.y >= info.bottom, `${route}: narrow actions wrap below details`);
          assert.ok(Math.abs(actions.x - info.x) <= 1, `${route}: wrapped actions stay left-aligned`);
        }
      }
    }
  });

  test(`automation filters stay grouped at the right edge at ${width}px`, async t => {
    const page = await pageFor(t, 'automations', 'populated', width);
    await page.locator('[data-automation-scope][aria-pressed=true]').waitFor();
    for (const [value, expected] of [['all', 2], ['paused', 1], ['enabled', 1]]) {
      await page.locator('#automation-status').selectOption(value);
      assert.equal(await page.locator('.automation-card:visible').count(), expected);
      const { toolbar, scopes, search, status, count, arrow } = await page.locator('.automation-toolbar').evaluate(toolbar => {
        const box = el => el.getBoundingClientRect().toJSON();
        return { toolbar: box(toolbar), scopes: box(toolbar.querySelector('.automation-scopes')),
          search: box(toolbar.querySelector('input')), status: box(toolbar.querySelector('select')),
          count: box(toolbar.querySelector('[data-automation-count]')), arrow: box(toolbar.querySelector('[data-slot="native-select-icon"]')) };
      });
      assert.ok(Math.abs(scopes.left - toolbar.left) <= 1, 'Ownership controls remain left-aligned');
      assert.ok(Math.abs(count.right - toolbar.right) <= 1, 'The result count anchors the filter group to the right edge');
      assert.ok(arrow.left >= status.left && arrow.right <= status.right, 'The status arrow stays within its field');
      if (width > 768) {
        assert.ok(Math.abs(search.top - status.top) <= 1, 'Desktop search and status share a row');
        assert.ok(Math.abs(status.left - search.right - 12) <= 1, 'Search and status stay together');
        assert.ok(Math.abs(count.left - status.right - 12) <= 1, 'No spare space separates the status and count');
      } else {
        assert.ok(search.top >= scopes.bottom, 'Narrow search wraps below ownership');
        assert.ok(Math.abs(search.width - toolbar.width) <= 1, 'Narrow search fills its row');
        assert.ok(status.top >= search.bottom, 'Narrow status follows search');
      }
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    }
    await page.locator('#automation-search').fill('no matching automation');
    assert.equal(await page.locator('.automation-card:visible').count(), 0);
    await page.getByRole('button', { name: 'Clear filters', exact: true }).click();
    assert.equal(await page.locator('.automation-card:visible').count(), 2);
    await page.locator('[data-automation-scope="mine"]').click();
    assert.equal(await page.locator('[data-automation-scope="mine"]').getAttribute('aria-pressed'), 'true');
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
      const missing = await page.locator('#content button:not([data-slot]), #content select:not([data-slot]):not([aria-hidden=true]), #content textarea:not([data-slot]), #content table:not([data-slot])').count();
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

test('compact skills preserve long descriptions, references and archived actions', async t => {
  for (const width of [1440, 768, 320]) {
    const page = await pageFor(t, 'skills', 'populated', width);
    const name = 'review-cross-platform-production-integration-and-release-changes';
    const description = 'Review integration results, reproduction steps, and release readiness. '.repeat(4).trim();
    await page.route('**/api/skills?archived=true', async route => {
      const response = await route.fetch();
      const data = await response.json();
      data.skills[0] = { ...data.skills[0], name, description, reference: `org:${name}`, can_manage: false };
      data.skills[1].archived = true;
      await route.fulfill({ response, json: data });
    });
    await page.reload();
    const row = page.locator('.skill-card').first();
    await row.waitFor();
    assert.equal(await page.locator('.skill-card').count(), 4);
    assert.equal(await row.locator('strong').textContent(), name);
    assert.equal(await row.locator('code').textContent(), `/org:${name}`);
    assert.equal(await row.locator('p').textContent(), description);
    assert.equal(await row.getByRole('button', { name: 'View', exact: true }).count(), 1);
    assert.equal(await row.locator('[data-archive-skill]').count(), 0);
    const contained = await row.evaluate(row => {
      const bounds = row.getBoundingClientRect();
      return [...row.querySelectorAll('strong, code, p')].every(element => {
        const range = document.createRange();
        range.selectNodeContents(element);
        return [...range.getClientRects()].every(rect => rect.left >= bounds.left - 1 && rect.right <= bounds.right + 1 && rect.bottom <= bounds.bottom);
      });
    });
    assert.equal(contained, true, `Long skill content stays readable at ${width}px`);
    await page.getByRole('checkbox', { name: 'Show archived', exact: true }).click();
    assert.equal(await page.locator('.skill-card').count(), 5);
    const archived = page.locator('.skill-card').filter({ hasText: 'benchmark-report' });
    assert.equal(await archived.getByRole('button', { name: 'Restore', exact: true }).count(), 1);
    assert.equal(await archived.locator('[data-use-skill]').count(), 0);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  }
});

test('compact secrets preserve long identifiers, file metadata and managed access', async t => {
  for (const width of [1440, 768, 320]) {
    const page = await pageFor(t, 'secrets', 'populated', width);
    const label = 'PRODUCTION_SERVICE_ACCOUNT_'.repeat(4);
    const name = 'production-service-account-'.repeat(4);
    const envVar = 'PRODUCTION_CREDENTIAL_FILE_'.repeat(4);
    await page.route('**/api/credentials', async route => {
      const response = await route.fetch();
      const data = await response.json();
      data.secrets = [{ ...data.secrets[0], label, name, format: 'file', env_var: envVar, status: 'invalid', can_manage: false, expires_at: '2027-01-01T00:00:00Z' }];
      await route.fulfill({ response, json: data });
    });
    await page.reload();
    const row = page.locator('.secret-card');
    await row.waitFor();
    assert.equal(await row.locator('strong').textContent(), label);
    assert.equal(await row.locator('.secret-reference').textContent(), name);
    assert.ok((await row.locator('.secret-meta small').textContent()).includes(envVar));
    assert.ok((await row.textContent()).includes('Credential file'));
    assert.ok((await row.textContent()).includes('Expires'));
    assert.ok((await row.textContent()).includes('Needs updating'));
    assert.equal(await row.locator('button').count(), 0, 'Managed credentials have no write actions');
    await row.getByText('Managed by an admin', { exact: true }).waitFor();
    const overflow = await row.evaluate(row => {
      const bounds = row.getBoundingClientRect();
      const walker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT);
      const failures = [];
      while (walker.nextNode()) {
        if (!walker.currentNode.textContent.trim()) continue;
        const range = document.createRange();
        range.selectNodeContents(walker.currentNode);
        if ([...range.getClientRects()].some(rect => rect.width && (rect.left < bounds.left - 1 || rect.right > bounds.right + 1))) failures.push(walker.currentNode.textContent);
      }
      return failures;
    });
    assert.deepEqual(overflow, [], `Long secret metadata stays within the ${width}px layout`);
    await page.locator('#secret-search').fill('no matching credential');
    assert.equal(await row.isVisible(), false);
    await page.getByRole('button', { name: 'Clear filters', exact: true }).click();
    assert.equal(await row.isVisible(), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  }
});

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
