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
  await page.goto(`${base}/?fixture=${fixture}#${route}`, { waitUntil: 'domcontentloaded' });
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
  const trigger = await source.locator('..').getByRole('combobox').elementHandle();
  await trigger.click();
  await page.getByRole('option', { name: label, exact: true }).click();
  await page.getByRole('listbox').waitFor({ state: 'detached' });
  // Radix restores focus after unmount. Wait before typing into the next field.
  await page.waitForFunction(el => !el.isConnected || el === document.activeElement, trigger);
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
    // Radix collision placement follows the portal's first visible frame.
    await page.waitForFunction(el => {
      const bounds = el.getBoundingClientRect();
      return bounds.left >= 0 && bounds.right <= innerWidth + 1 && bounds.top >= 0 && bounds.bottom <= innerHeight + 1;
    }, await menu.elementHandle()).catch(async error => {
      throw new Error(`Dropdown outside viewport: ${JSON.stringify({
        route: page.url(), field: await trigger.getAttribute('aria-label'), trigger: await trigger.boundingBox(), menu: await menu.boundingBox(),
      })}`, { cause: error });
    });
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
    await page.waitForFunction(el => {
      const item = el.getBoundingClientRect();
      const menu = el.closest('[role=listbox]').getBoundingClientRect();
      return item.top >= menu.top && item.bottom <= menu.bottom + 1;
    }, await last.elementHandle());
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
  await page.getByRole('listbox').waitFor();
  await page.keyboard.press('Escape');
  await page.getByRole('listbox').waitFor({ state: 'detached' });
  await page.waitForFunction(el => el === document.activeElement, await trigger.elementHandle());
});

test('replacement select options remain authoritative across snapshots, reset and teardown', async t => {
  const page = await pageFor(t);
  await page.evaluate(() => {
    MoyaiUI.render(document.querySelector('#content'), '<form id="option-owner"><label for="owner-select">Choice</label><select id="owner-select" name="choice"><option value="original" selected>Original</option></select><select id="owner-multiple" name="many" multiple><option value="original" selected>Original</option></select></form>');
    window.optionEvents = [];
    for (const name of ['input', 'change']) document.querySelector('#option-owner').addEventListener(name, event => window.optionEvents.push([name, event.target.id]));
  });
  const source = page.locator('#owner-select');
  const trigger = page.getByRole('combobox', { name: 'Choice', exact: true });
  for (let cycle = 0; cycle < 3; cycle++) {
    await source.evaluate((select, cycle) => {
      MoyaiUI.render(select, `<optgroup label="Updated"><option value="new-${cycle}" selected>New ${cycle}</option><option value="next-${cycle}">Next ${cycle}</option></optgroup>`);
      select.value = `next-${cycle}`;
      select.disabled = true;
    }, cycle);
    await page.waitForFunction(() => document.querySelector('#owner-select').parentElement.querySelector('[role=combobox]').disabled);
    await source.evaluate(select => { select.disabled = false; select.form.reset(); });
    await page.waitForFunction(cycle => document.querySelector('#owner-select').parentElement.querySelector('[role=combobox]').textContent.includes(`New ${cycle}`), cycle);
    await trigger.click();
    assert.deepEqual(await page.getByRole('option').allTextContents(), [`New ${cycle}`, `Next ${cycle}`]);
    await page.getByRole('option', { name: `Next ${cycle}`, exact: true }).click();
    assert.equal(await source.inputValue(), `next-${cycle}`);
    assert.equal(await source.evaluate(select => new FormData(select.form).get('choice')), `next-${cycle}`);
  }
  assert.deepEqual(await page.evaluate(() => window.optionEvents), Array.from({ length: 3 }, () => [['input', 'owner-select'], ['change', 'owner-select']]).flat());
  await page.locator('#owner-multiple').evaluate(select => {
    MoyaiUI.render(select, '<option value="a" selected>A &amp; B</option><option value="b" selected>B</option><option value="c">C</option>');
    select.options[0].selected = false;
    select.form.reset();
  });
  assert.deepEqual(await page.locator('#owner-multiple').evaluate(select => new FormData(select.form).getAll('many')), ['a', 'b']);
  await page.evaluate(() => MoyaiUI.render(document.querySelector('#content'), '<h1>Replaced</h1>'));
  assert.equal(await page.getByRole('listbox').count(), 0);
});

test('native option parsing preserves labels, empty values, disabled groups and reset defaults', async t => {
  const page = await pageFor(t);
  await page.evaluate(() => {
    MoyaiUI.render(document.querySelector('#content'), '<form><select id="parsed-options" name="choice" aria-label="Parsed choice" required></select><select id="native-listbox" size="3" aria-label="Native choices"><option selected>First</option><option>Second</option></select></form>');
    MoyaiUI.render(document.querySelector('#parsed-options'), '<option value="" selected>Choose</option><optgroup label="Unavailable" disabled><option value="blocked">Blocked</option></optgroup><option value="a&amp;b" label="A &amp; B" onclick="window.optionCodeExecuted=true">Different text</option><script>window.optionCodeExecuted=true</script>');
  });
  const source = page.locator('#parsed-options');
  await page.getByRole('combobox', { name: 'Parsed choice', exact: true }).click();
  assert.equal(await page.getByRole('option', { name: 'Blocked', exact: true }).getAttribute('aria-disabled'), 'true');
  await page.getByRole('option', { name: 'A & B', exact: true }).click();
  assert.equal(await source.inputValue(), 'a&b');
  assert.equal(await source.locator('[onclick],script').count(), 0);
  assert.equal(await page.evaluate(() => !!window.optionCodeExecuted), false);
  await source.evaluate(select => select.form.reset());
  assert.equal(await source.inputValue(), '');
  assert.equal(await source.evaluate(select => select.validity.valueMissing), true);
  const native = page.getByRole('listbox', { name: 'Native choices', exact: true });
  assert.equal(await native.evaluate(select => select.size), 3);
  await native.selectOption({ label: 'Second' });
  assert.equal(await native.inputValue(), 'Second');
  await native.evaluate(select => select.form.reset());
  assert.equal(await native.inputValue(), 'First');
});

test('model and session scope pickers retain choices through real controller updates', async t => {
  const page = await pageFor(t);
  await page.route('**/api/config', async route => {
    const response = await route.fetch();
    const data = await response.json();
    data.models.push({ id: 'synthetic-model', name: 'Synthetic model', default_harness: 'codex' });
    data.harnesses.push({ id: 'codex', name: 'Codex' });
    await route.fulfill({ response, json: data });
  });
  await page.reload();
  await page.locator('#new-model').waitFor();
  await choose(page, page.locator('#new-model'), 'synthetic-model');
  const harness = page.locator('#new-harness');
  await page.waitForFunction(() => document.querySelector('#new-harness').parentElement.querySelector('[role=combobox]').textContent.includes('Auto · Codex'));
  await choose(page, harness, 'codex');
  assert.equal(await harness.inputValue(), 'codex');
  await choose(page, harness, '');
  assert.equal(await harness.inputValue(), '');
  assert.equal(await harness.locator('option').count(), 3);
  await page.evaluate(() => { state.authenticated = true; state.role = 'admin'; restoreSessionScope(); });
  await choose(page, page.locator('#session-scope'), 'all');
  assert.equal(await page.evaluate(() => state.sessionScope), 'all');
  await page.evaluate(() => { state.role = 'member'; restoreSessionScope(); });
  assert.deepEqual(await page.locator('#session-scope').evaluate(select => [...select.options].map(option => option.value)), ['mine']);
  assert.equal(await page.locator('#session-scope').isDisabled(), true);
  await page.evaluate(() => { state.role = 'admin'; restoreSessionScope(); });
  await choose(page, page.locator('#session-scope'), 'all');
  assert.equal(await page.evaluate(() => state.sessionScope), 'all');
});

for (const width of [1440, 768, 320]) test(`picker label chrome opens only the styled menu at ${width}px`, async t => {
  const page = await pageFor(t, 'tasks', 'populated', width);
  await page.evaluate(() => {
    window.nativeSelectClicks = [];
    document.addEventListener('click', event => {
      if (event.target instanceof HTMLSelectElement) window.nativeSelectClicks.push(event.defaultPrevented);
    });
  });
  const picker = page.locator('#new-model').locator('..').locator('..');
  const trigger = picker.getByRole('combobox');
  const clickChrome = async selector => {
    const chrome = picker.locator(selector);
    await chrome.scrollIntoViewIfNeeded();
    const bounds = await chrome.boundingBox();
    // Decorative chrome delegates hit testing to the enclosing label.
    await page.mouse.click(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
  };
  for (const chrome of ['.provider-logo', '.picker-chevron']) {
    await clickChrome(chrome);
    await page.getByRole('listbox').waitFor();
    await page.keyboard.press('Escape');
    await page.getByRole('listbox').waitFor({ state: 'detached' });
    await page.waitForFunction(el => el === document.activeElement, await trigger.elementHandle());
    assert.equal(await trigger.evaluate(el => el === document.activeElement), true);
  }
  assert.equal(await page.evaluate(() => window.nativeSelectClicks.length > 0 && window.nativeSelectClicks.every(Boolean)), true, 'Native label activation is prevented');
  await page.locator('#new-model').evaluate(select => { select.disabled = true; });
  await page.waitForFunction(() => document.querySelector('#new-model').parentElement.querySelector('[role=combobox]').disabled);
  await clickChrome('.picker-chevron');
  assert.equal(await page.getByRole('listbox').count(), 0);
  await page.evaluate(() => MoyaiUI.render(document.querySelector('#content'), '<form><label for="explicit-choice">Explicit choice</label><select id="explicit-choice" name="choice"><option value="a">Alpha</option><option value="b">Beta</option></select></form>'));
  await page.locator('label[for=explicit-choice]').click();
  await page.getByRole('option', { name: 'Beta', exact: true }).click();
  assert.equal(await page.locator('#explicit-choice').inputValue(), 'b');
});

for (const route of ['automations', 'environments']) for (const inFlight of [false, true]) test(`${route} polling preserves an open menu ${inFlight ? 'during an in-flight request' : 'before a request'}`, async t => {
  const page = await pageFor(t, route);
  const endpoint = route === 'automations' ? '**/api/automations' : '**/api/admin/environments';
  const name = route === 'automations' ? 'Automation status' : 'Filter environments';
  let requests = 0, release;
  const gate = new Promise(resolve => { release = resolve; });
  t.after(() => release());
  await page.route(endpoint, async request => { requests++; if (inFlight) await gate; await request.continue(); });
  await page.evaluate(() => { clearTimeout(automationRefresh); clearTimeout(environmentRefresh); document.querySelector('#content h1').focus(); });
  if (inFlight) {
    const request = page.waitForRequest(endpoint);
    await page.evaluate(route => { window.pendingPoll = route === 'automations' ? renderAutomations(true) : renderEnvironments(true); }, route);
    await request;
  }
  const trigger = page.getByRole('combobox', { name, exact: true });
  await trigger.click();
  const menu = page.getByRole('listbox');
  await menu.waitFor();
  await page.evaluate(() => { window.openPollMenu = document.querySelector('[role=listbox]'); });
  if (inFlight) { release(); await page.evaluate(() => window.pendingPoll); }
  else await page.evaluate(route => route === 'automations' ? renderAutomations(true) : renderEnvironments(true), route);
  assert.equal(await menu.count(), 1, 'Background rendering never closes the menu');
  assert.equal(await menu.evaluate(el => el === window.openPollMenu), true);
  assert.equal(requests, inFlight ? 1 : 0);
  await page.keyboard.press('Escape');
  await menu.waitFor({ state: 'detached' });
  await page.locator('#content h1').click();
  await page.evaluate(route => route === 'automations' ? renderAutomations(true) : renderEnvironments(true), route);
  assert.equal(requests, inFlight ? 2 : 1, 'Polling resumes after the interaction ends');
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
  test(`list searches fill their toolbar with compact trailing controls at ${width}px`, async t => {
    const page = await pageFor(t, 'users', 'populated', width);
    for (const [route, selector, searchSelector] of [
      ['users', '.users-toolbar', '.users-search'],
      ['environments', '.settings-toolbar', '#environment-search'],
      ['secrets', '.settings-toolbar', '#secret-search'],
      ['skills', '.skills-filters', '#skill-search'],
      ['memory', '.memory-filter', '#memory-search'],
      ['automations', '.automation-toolbar', '#automation-search'],
      ['spend', '.analytics-pr-filters', 'label:first-child'],
    ]) {
      await page.evaluate(view => navigate(view), route);
      if (route === 'spend') await page.locator('#spend-tab-prs').click();
      const layout = await page.locator(selector).evaluate((toolbar, searchSelector) => {
        const css = getComputedStyle(toolbar);
        const box = el => el.getBoundingClientRect().toJSON();
        const slot = toolbar.querySelector(searchSelector);
        const search = slot.matches('input') ? slot : slot.querySelector('input');
        const siblings = [...toolbar.children].filter(el => el !== slot && el.checkVisibility() && getComputedStyle(el).position !== 'absolute');
        return { width: toolbar.clientWidth - parseFloat(css.paddingLeft) - parseFloat(css.paddingRight),
          gap: parseFloat(css.columnGap), slot: box(slot), search: box(search), siblings: siblings.map(box) };
      }, searchSelector);
      const stacked = width <= 650 || (route === 'automations' && width <= 768);
      if (stacked) {
        assert.ok(Math.abs(layout.search.width - layout.width) <= 1, `${route}: narrow search uses the full content width`);
      } else {
        const remaining = layout.width - layout.siblings.reduce((sum, rect) => sum + rect.width, 0) - layout.gap * layout.siblings.length;
        assert.ok(Math.abs(layout.search.width - remaining) <= 1, `${route}: search fills all space left by compact controls: ${JSON.stringify(layout)}`);
        for (const sibling of layout.siblings) {
          const center = sibling.y + sibling.height / 2;
          assert.ok(center >= layout.search.y && center <= layout.search.bottom + 1, `${route}: controls share the search row`);
        }
      }
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    }
  });

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

test('template form controls preserve editing, native values, validation and reset across input types', async t => {
  const page = await pageFor(t);
  const fields = [
    ['text', 'Original', 'Updated'], ['search', 'release', 'tests'],
    ['email', 'before@example.com', 'after@example.com'], ['url', 'https://example.com/before', 'https://example.com/after'],
    ['tel', '123456', '987654'], ['password', 'synthetic-before', 'synthetic-after'],
    ['number', '0', '42'], ['date', '2026-10-01', '2026-10-08'],
    ['time', '09:00', '10:30'], ['datetime-local', '2026-10-01T09:00', '2026-10-08T10:30'],
    ['month', '2026-10', '2026-11'], ['week', '2026-W40', '2026-W41'],
  ];
  await page.evaluate(fields => {
    MoyaiUI.render(document.querySelector('#content'), `<form id="input-contract">
      ${fields.map(([type, value]) => `<label>${type}<input type="${type}" name="${type}" value="${value}" required></label>`).join('')}
      <label>Initially empty<input name="empty" value=""></label>
      <label>Initially absent<input name="absent"></label>
      <label>Notes<textarea name="notes">Original notes</textarea></label>
      <label>First<input type="radio" name="scope" value="first" checked></label>
      <label>Second<input type="radio" name="scope" value="second"></label>
      <label>Enabled<input type="checkbox" name="enabled" value="yes" checked></label>
      <label>Notify<input type="checkbox" role="switch" name="notify" value="yes" checked></label>
      <label>Read only<input name="readonly" value="Keep this" readonly></label>
      <label>Disabled<input name="disabled" value="Skip this" disabled></label>
      <label>Upload<input name="upload" type="file"></label>
      <input name="hidden" type="hidden" value="metadata">
      <input type="submit" value="Submit changes"><input type="reset" value="Restore defaults"><input type="button" value="Plain action">
    </form>`);
    const form = document.querySelector('#input-contract');
    window.inputSubmissions = [];
    form.onsubmit = event => { event.preventDefault(); window.inputSubmissions.push(Object.fromEntries(new FormData(form))); };
  }, fields);
  const form = page.locator('#input-contract');
  assert.equal(await form.locator('[name=readonly]').isEditable(), false);
  assert.equal(await form.locator('[name=disabled]').isDisabled(), true);
  for (const [type, initial, edited] of fields) {
    const input = form.locator(`[name="${type}"]`);
    assert.equal(await input.inputValue(), initial, `${type}: initial value`);
    if (['text', 'search', 'email', 'url', 'tel', 'password', 'number'].includes(type)) {
      await input.fill('');
      await input.pressSequentially(edited);
    } else await input.fill(edited);
    await input.press('Tab');
    assert.equal(await input.inputValue(), edited, `${type}: typed value survives blur`);
  }
  await form.locator('[name=empty]').pressSequentially('Now filled');
  await form.locator('[name=absent]').pressSequentially('Also filled');
  await form.locator('[name=notes]').fill('Edited notes');
  await form.getByRole('radio', { name: 'Second', exact: true }).click();
  assert.equal(await form.getByRole('radio', { name: 'First', exact: true }).isChecked(), false);
  await form.getByRole('checkbox', { name: 'Enabled', exact: true }).click();
  await form.getByRole('switch', { name: 'Notify', exact: true }).click();
  await form.locator('[name=upload]').setInputFiles({ name: 'fixture.txt', mimeType: 'text/plain', buffer: Buffer.from('Synthetic upload') });
  await form.getByRole('button', { name: 'Submit changes', exact: true }).click();
  const submitted = await page.evaluate(() => window.inputSubmissions.at(-1));
  for (const [type, , edited] of fields) assert.equal(submitted[type], edited, `${type}: submitted value`);
  assert.equal(submitted.scope, 'second');
  assert.equal(submitted.notes, 'Edited notes');
  assert.equal(submitted.empty, 'Now filled');
  assert.equal(submitted.absent, 'Also filled');
  assert.equal(submitted.readonly, 'Keep this');
  assert.equal(submitted.hidden, 'metadata');
  for (const name of ['enabled', 'notify', 'disabled']) assert.equal(name in submitted, false);
  assert.equal(await form.locator('[name=upload]').evaluate(input => input.files[0].name), 'fixture.txt');
  await form.locator('[name=text]').evaluate(input => { input.value = 'Controller update'; input.focus(); input.setSelectionRange(input.value.length, input.value.length); });
  await form.locator('[name=text]').pressSequentially(' kept');
  assert.equal(await form.locator('[name=text]').inputValue(), 'Controller update kept');
  await form.locator('[name=text]').fill('');
  assert.equal(await form.evaluate(form => form.checkValidity()), false);
  await form.getByRole('button', { name: 'Restore defaults', exact: true }).click();
  for (const [type, initial] of fields) assert.equal(await form.locator(`[name="${type}"]`).inputValue(), initial, `${type}: reset`);
  assert.equal(await form.locator('[name=empty]').inputValue(), '');
  assert.equal(await form.locator('[name=absent]').inputValue(), '');
  assert.equal(await form.locator('[name=notes]').inputValue(), 'Original notes');
  assert.equal(await form.getByRole('radio', { name: 'First', exact: true }).isChecked(), true);
  assert.equal(await form.getByRole('checkbox', { name: 'Enabled', exact: true }).isChecked(), true);
  assert.equal(await form.getByRole('switch', { name: 'Notify', exact: true }).isChecked(), true);
  assert.equal(await form.locator('[name=upload]').evaluate(input => input.files.length), 0);
  assert.equal(await form.evaluate(form => form.checkValidity()), true);
});

test('prefilled session repository stays editable and survives draft restoration', async t => {
  const page = await pageFor(t);
  await page.getByLabel('Session options', { exact: true }).click();
  await page.locator('#repo').pressSequentially('https://github.com/example/first');
  await page.evaluate(() => navigate('connections'));
  await page.evaluate(() => navigate('tasks'));
  await page.getByLabel('Session options', { exact: true }).click();
  assert.equal(await page.locator('#repo').inputValue(), 'https://github.com/example/first');
  await page.locator('#repo').press('End');
  await page.locator('#repo').pressSequentially('-updated');
  await page.locator('#repo').press('Tab');
  assert.equal(await page.evaluate(() => state.newDraft.repo), 'https://github.com/example/first-updated');
});

for (const scenario of [
  { route: 'automations', open: '[data-edit-automation="auto-0"]', path: '/api/automations/auto-0', submit: 'Save paused', fields: [['[name=name]', 'Release review'], ['[name=repo_url]', 'https://github.com/example/release'], ['[name=max_runs_per_hour]', '60']], expected: { definition: { name: 'Release review', repo_url: 'https://github.com/example/release', max_runs_per_hour: 60 } } },
  { route: 'skills', open: '[data-edit-skill="skill-0"]', path: '/api/skills/skill-0', submit: 'Save skill', fields: [['#skill-name', 'release-review'], ['#skill-description', 'Review release notes and tests.']], expected: { name: 'release-review', description: 'Review release notes and tests.' } },
  { route: 'memory', open: '[data-memory-edit="memory-0"]', path: '/api/memory/memory-0', submit: 'Save memory', fields: [['#memory-title', 'Release review preferences']], expected: { title: 'Release review preferences' } },
  { route: 'secrets', open: '[data-edit-secret="secret-0"]', path: '/api/credentials/secrets/secret-0', submit: 'Save changes', fields: [['#secret-label', 'Release API access']], expected: { label: 'Release API access' } },
  { route: 'environments', open: '[data-edit-environment="env-0"]', path: '/api/admin/environments/env-0', submit: 'Save recipe', fields: [['#env-name', 'Release environment'], ['#env-repository', 'example/release'], ['#env-ref', 'release']], expected: { recipe: { name: 'Release environment', repository: 'example/release', ref: 'release' } } },
]) {
  for (const width of [1440, 768, 320]) test(`prefilled ${scenario.route} editor accepts typing and preserves save payloads and drafts at ${width}px`, async t => {
    const page = await pageFor(t, scenario.route, 'populated', width);
    await page.route(`**${scenario.path}`, route => ['PUT', 'PATCH'].includes(route.request().method())
      ? route.fulfill({ status: 503, json: { detail: 'Form regression: save rejected' } }) : route.continue());
    await page.locator(scenario.open).click();
    for (const [selector, edited] of scenario.fields) {
      const input = page.locator(selector);
      assert.notEqual(await input.inputValue(), '', `${selector}: exercising a prefilled field`);
      await input.fill('');
      await input.pressSequentially(edited);
      await input.press('Tab');
      assert.equal(await input.inputValue(), edited, `${selector}: keystrokes survive blur`);
    }
    const request = page.waitForRequest(request => new URL(request.url()).pathname === scenario.path && ['PUT', 'PATCH'].includes(request.method()));
    await page.getByRole('button', { name: scenario.submit, exact: true }).click();
    assert.partialDeepStrictEqual((await request).postDataJSON(), scenario.expected);
    await page.getByText('Form regression: save rejected', { exact: true }).waitFor();
    for (const [selector, edited] of scenario.fields) assert.equal(await page.locator(selector).inputValue(), edited, `${selector}: failed-save draft`);
    await page.keyboard.press('Escape');
    await page.getByRole('dialog').waitFor({ state: 'detached' });
  });
}

for (const width of [1440, 768, 320]) test(`credential availability radios submit the chosen access boundary at ${width}px`, async t => {
  const page = await pageFor(t, 'secrets', 'populated', width);
  await page.route('**/api/credentials/requests/input-test', route => route.fulfill({ status: 503, json: { detail: 'Synthetic request: save rejected' } }));
  await page.evaluate(() => openCredentialDialog({ id: 'input-test', provider: 'generic', name: 'synthetic-service', format: 'env', reason: 'Verify availability controls', generation: 1, can_personal: true, can_organization: true, preferred_scope: 'personal' }));
  assert.equal(await page.locator('#secret-use-personal').isChecked(), true);
  await page.getByRole('radio', { name: 'Organization', exact: true }).click();
  assert.equal(await page.locator('#secret-use-organization').isChecked(), true);
  assert.equal(await page.locator('#secret-use-personal').isChecked(), false);
  assert.equal(await page.locator('#secret-scope').inputValue(), 'organization');
  assert.equal(await page.locator('#secret-lifetime').inputValue(), 'persistent');
  await page.locator('#secret-use-organization').focus();
  await page.keyboard.press('ArrowLeft');
  assert.equal(await page.locator('#secret-use-personal').isChecked(), true);
  await page.keyboard.press('ArrowLeft');
  assert.equal(await page.locator('#secret-use-session').isChecked(), true);
  assert.equal(await page.locator('#secret-scope').inputValue(), 'personal');
  assert.equal(await page.locator('#secret-lifetime').inputValue(), 'session');
  await page.locator('#secret-value').fill('{"DEMO_TOKEN":"synthetic-only"}');
  const request = page.waitForRequest('**/api/credentials/requests/input-test');
  await page.getByRole('button', { name: 'Submit', exact: true }).click();
  assert.partialDeepStrictEqual((await request).postDataJSON(), { decision: 'provide', scope: 'personal', lifetime: 'session', value: '{"DEMO_TOKEN":"synthetic-only"}' });
  await page.getByText('Synthetic request: save rejected', { exact: true }).waitFor();
  assert.equal(await page.locator('#secret-use-session').isChecked(), true);
  assert.equal(await page.locator('#secret-value').inputValue(), '', 'Failed saves still clear sensitive replacement values');
});

test('incremental assistant updates render, preserve reading state and retain copy actions', async t => {
  const page = await pageFor(t);
  await page.evaluate(() => {
    MoyaiUI.render(document.querySelector('#content'), '<div id="activity-test"><div data-activity-slot="1"></div></div>');
    window.activityRun = {
      id: 'activity-test', status: 'running', active_message_id: 1,
      messages: [{ id: 1, role: 'user', status: 'running' }],
      events: [
        { id: 1, kind: 'chat', message: 'Response started', data: { message_id: 1 }, created_at: '2026-10-08T12:00:00Z' },
        { id: 2, kind: 'message', message: 'Checking the [release](https://example.com/release).\n\n```sh\nnpm test\n```', data: { turn_id: 1 }, created_at: '2026-10-08T12:00:01Z' },
      ],
    };
    window.copiedUpdates = [];
    window.syncActivityTest = () => MoyaiActivity.sync(document.querySelector('#activity-test'), window.activityRun, {
      markdown: renderMarkdown, copy: text => window.copiedUpdates.push(text),
    });
    window.syncActivityTest();
    window.firstUpdate = document.querySelector('.assistant-update');
  });
  const update = page.locator('.assistant-update').first();
  assert.equal(await update.locator('.copy-update').getAttribute('data-slot'), 'tooltip-trigger');
  await update.locator('.copy-update').click();
  await update.locator('.copy-code').click();
  assert.deepEqual(await page.evaluate(() => window.copiedUpdates), [
    'Checking the [release](https://example.com/release).\n\n```sh\nnpm test\n```', 'npm test\n',
  ]);
  await update.getByRole('link').focus();
  await page.evaluate(() => {
    const range = document.createRange();
    range.selectNodeContents(window.firstUpdate.querySelector('.message-content p'));
    getSelection().removeAllRanges();
    getSelection().addRange(range);
    window.activityRun.events.push(
      { id: 3, kind: 'tool', message: 'Run tests', data: { turn_id: 1, activity_version: 1, call_id: 'test', phase: 'started', category: 'command', command: 'npm test' }, created_at: '2026-10-08T12:00:02Z' },
      { id: 4, kind: 'message', message: 'Tests are passing.', data: { turn_id: 1 }, created_at: '2026-10-08T12:00:03Z' },
    );
    window.syncActivityTest();
  });
  assert.deepEqual(await page.locator('[data-activity-slot] > *').evaluateAll(nodes => nodes.map(node => node.dataset.timelineKey.split(':')[0])), ['update', 'work', 'update', 'work']);
  assert.equal(await page.evaluate(() => document.querySelector('.assistant-update') === window.firstUpdate), true);
  assert.equal(await update.getByRole('link').evaluate(el => el === document.activeElement), true);
  assert.equal(await page.evaluate(() => getSelection().toString()), 'Checking the release.');
  await page.evaluate(() => {
    window.activityRun.events[1].message = 'Release checks are complete.';
    window.syncActivityTest();
  });
  assert.equal(await page.evaluate(() => window.firstUpdate.isConnected), false);
  assert.equal(await update.locator('.message-content').textContent(), 'Release checks are complete.\n');
  await update.locator('.copy-update').click();
  assert.equal(await page.evaluate(() => window.copiedUpdates.at(-1)), 'Release checks are complete.');
  await page.evaluate(() => {
    window.activityRun.events.push({
      id: 5, kind: 'message', message: 'Compacting saved context before continuing. Completed tool receipts are preserved.',
      data: { turn_id: 1 }, created_at: '2026-10-08T12:00:04Z',
    });
    window.syncActivityTest();
  });
  const compaction = page.locator('.context-compaction');
  await compaction.locator('summary').click();
  assert.equal(await compaction.evaluate(el => el.open), true);
  assert.equal(await compaction.locator('.copy-update').count(), 0);
  await page.evaluate(() => window.syncActivityTest());
  assert.equal(await compaction.evaluate(el => el.open), true, 'Unchanged notices preserve their expanded state');
  assert.match(await compaction.locator('.context-compaction-detail').textContent(), /Saved progress and completed tool results stay available/);
  await page.mouse.move(0, 0);
  await update.locator('.copy-update').hover();
  await page.getByRole('tooltip').waitFor();
  await page.evaluate(() => { window.activityRun.events = []; window.activityRun.messages = []; window.syncActivityTest(); });
  assert.equal(await page.locator('[data-activity-slot] > *').count(), 0);
  await page.getByRole('tooltip').waitFor({ state: 'detached' });
  await page.evaluate(() => navigate('tasks'));
});

for (const width of [1440, 768, 320]) test(`runtime provider drafts remain editable across the merged Lambda picker at ${width}px`, async t => {
  const page = await pageFor(t, 'runtime', 'populated', width);
  await page.route('**/api/settings/sandboxes', route => route.request().method() === 'PUT'
    ? route.fulfill({ status: 503, json: { detail: 'Synthetic runtime: save rejected' } }) : route.continue());
  const provider = page.locator('#sandbox-provider');
  await provider.waitFor({ state: 'attached' });
  await page.locator('#modal_app_name').fill('moyai-preview');
  await choose(page, provider, 'lambda');
  assert.equal(await page.locator('#lambda_region').inputValue(), 'us-east-1');
  await page.locator('#lambda_region').fill('us-west-2');
  await page.locator('#lambda_image').fill('arn:aws:lambda:us-west-2:123456789012:synthetic-image');
  await choose(page, provider, 'modal');
  assert.equal(await page.locator('#modal_app_name').inputValue(), 'moyai-preview');
  await choose(page, provider, 'lambda');
  const region = page.locator('#lambda_region');
  assert.equal(await region.inputValue(), 'us-west-2');
  await region.press('ControlOrMeta+A');
  await region.pressSequentially('eu-west-1');
  await region.press('Tab');
  assert.equal(await region.inputValue(), 'eu-west-1');
  const request = page.waitForRequest(request => new URL(request.url()).pathname === '/api/settings/sandboxes' && request.method() === 'PUT');
  await page.getByRole('button', { name: 'Connect and use', exact: true }).click();
  assert.partialDeepStrictEqual((await request).postDataJSON(), {
    provider: 'lambda', values: { lambda_region: 'eu-west-1', lambda_image: 'arn:aws:lambda:us-west-2:123456789012:synthetic-image' },
  });
  await page.getByText('Synthetic runtime: save rejected', { exact: true }).waitFor();
  assert.equal(await region.inputValue(), 'eu-west-1');
  assert.equal(await provider.inputValue(), 'lambda');
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
});

test('preserved roots survive synchronous moves and release when not reattached', async t => {
  const page = await pageFor(t);
  assert.equal(await page.evaluate(() => {
    const host = document.createElement('div');
    document.querySelector('#content').appendChild(host);
    MoyaiUI.render(host, '<div data-retained></div>');
    const slot = host.querySelector('[data-retained]');
    // Match activity roots rendered detached and attached before the observer runs.
    const article = document.createElement('article');
    MoyaiUI.render(article, '<details open><summary>Retained tools</summary><button title="Copy result">Copy</button></details>');
    slot.appendChild(article);
    const disclosure = article.firstElementChild;
    MoyaiUI.render(host, '<div data-retained></div>', { preserve: [slot] });
    host.querySelector('[data-retained]').replaceWith(slot);
    const retained = article.firstElementChild === disclosure && disclosure.open;
    window.droppedActivity = article;
    MoyaiUI.render(host, '<p>New transcript</p>', { preserve: [slot] });
    return retained;
  }), true);
  await page.waitForFunction(() => !window.droppedActivity.childNodes.length);
});

for (const surface of ['main', 'side']) test(`${surface} transcript replacement retains rendered activity and releases removed controls`, async t => {
  const page = await pageFor(t);
  await page.evaluate(surface => {
    const event = (id, kind, message, data = {}) => ({ id, kind, message, data, created_at: `2026-10-08T12:00:0${id}Z` });
    window.lifecycleRun = {
      id: '99999999999999999999999999999999', mode: 'demo', status: 'running', active_message_id: 1,
      prompt: 'Verify retained activity', plugins: [], approvals: [], artifacts: [], credential_requests: [],
      messages: [{ id: 1, role: 'user', status: 'running', content: 'Verify retained activity' }],
      events: [event(1, 'chat', 'Response started', { message_id: 1 }),
        event(2, 'message', 'Checking the [release](https://example.com/release).', { turn_id: 1 }),
        event(3, 'tool', 'Run verification', { turn_id: 1, activity_version: 1, call_id: 'check', phase: 'completed', category: 'command', command: 'npm test', output: 'Checks passed', duration_ms: 1200 })],
    };
    window.lifecycleCopies = [];
    if (surface === 'main') {
      copyText = text => window.lifecycleCopies.push(text);
      state.selected = window.lifecycleRun.id;
      renderChat(structuredClone(window.lifecycleRun));
      state.source?.close(); state.source = null;
      window.refreshLifecycle = () => updateChat(structuredClone(window.lifecycleRun));
    } else {
      MoyaiUI.render(document.querySelector('#content'), '<div class="chat-layout"></div>');
      Object.defineProperty(navigator.clipboard, 'writeText', { configurable: true, value: async text => window.lifecycleCopies.push(text) });
      window.lifecyclePanel = MoyaiPanel.create({
        run: { id: '88888888888888888888888888888888', mode: 'demo' }, layout: document.querySelector('.chat-layout'),
        api: async path => path.endsWith('/side-chats') ? [] : structuredClone(window.lifecycleRun),
        markdown: renderMarkdown, escape: esc, size: fileSize, user: 'synthetic-lifecycle', models: [], toast,
      });
      window.lifecyclePanel.open('chat', { chatId: window.lifecycleRun.id, title: 'Verification side chat' });
      window.refreshLifecycle = () => { window.lifecyclePanel.hide(); window.lifecyclePanel.open('chat', { chatId: window.lifecycleRun.id }); };
    }
  }, surface);
  const container = page.locator(surface === 'main' ? '#conversation' : '.side-chat-messages');
  const update = container.locator('.assistant-update');
  const tool = container.locator('[data-work-key]').first();
  await update.waitFor();
  await tool.locator(':scope > summary').click();
  await page.evaluate(selector => {
    const container = document.querySelector(selector);
    window.retainedUpdate = container.querySelector('.assistant-update');
    window.retainedTool = container.querySelector('[data-work-key]');
    window.lifecycleRun.messages.push({ id: 2, role: 'assistant', status: 'completed', content: 'Verification finished.' });
    window.refreshLifecycle();
  }, surface === 'main' ? '#conversation' : '.side-chat-messages');
  await container.getByText('Verification finished.', { exact: true }).waitFor();
  assert.equal(await update.locator('.message-content').textContent(), 'Checking the release.\n');
  assert.match(await tool.textContent(), /npm test/);
  assert.equal(await tool.evaluate(el => el === window.retainedTool && el.open), true, 'Expanded tools remain the same live DOM');
  assert.equal(await update.evaluate(el => el === window.retainedUpdate), true);
  await update.locator('.copy-update').click();
  await page.waitForFunction(() => window.lifecycleCopies.length === 1);
  assert.deepEqual(await page.evaluate(() => window.lifecycleCopies), ['Checking the [release](https://example.com/release).']);
  await tool.locator(':scope > summary').click();
  assert.equal(await tool.evaluate(el => el.open), false, 'Retained shadcn disclosures still respond');
  await update.locator('.copy-update').hover();
  await page.getByRole('tooltip').waitFor();
  await page.evaluate(() => {
    window.lifecycleRun.messages = [];
    window.lifecycleRun.events = [];
    if (!window.lifecyclePanel) state.chatRun.events = [];
    window.refreshLifecycle();
  });
  await container.locator('[data-activity-slot]').waitFor({ state: 'detached' });
  await page.getByRole('tooltip').waitFor({ state: 'detached' });
  await page.evaluate(() => window.lifecyclePanel?.dispose());
});

test('failed sidebar refreshes retain component tooltips, actions and retry behavior', async t => {
  const page = await pageFor(t);
  let failing = true;
  await page.route('**/api/runs?*', route => failing
    ? route.fulfill({ status: 503, json: { detail: 'Synthetic sidebar outage' } }) : route.continue());
  for (let attempt = 0; attempt < 3; attempt++) {
    await page.locator('#content h1').click();
    await page.evaluate(() => refreshRuns().catch(() => {}));
    const list = page.locator('#session-list');
    assert.equal(await list.locator('[data-search-retry]').count(), 1);
    assert.equal(await list.locator('.session-link').count(), 4);
    await list.locator('.session-link').first().hover();
    await page.getByRole('tooltip', { name: 'Review release readiness', exact: true }).waitFor();
    await page.keyboard.press('Escape');
    await page.getByRole('tooltip').waitFor({ state: 'detached' });
    await list.locator('[data-session-actions]').first().click();
    await page.locator('#session-actions').getByRole('button', { name: 'Rename', exact: true }).waitFor();
    await page.keyboard.press('Escape');
    await page.locator('[data-slot="popover-content"]').waitFor({ state: 'detached' });
  }
  failing = false;
  await page.locator('[data-search-retry]').click();
  await page.locator('[data-search-retry]').waitFor({ state: 'detached' });
  assert.equal(await page.locator('#session-list .session-link').count(), 4);
});

test('audio control restores its icon through recording, cancellation and permission failures', async t => {
  const page = await pageFor(t);
  await page.evaluate(() => {
    window.audioRequests = []; window.audioFiles = []; window.audioTracks = [];
    Object.defineProperty(navigator.mediaDevices, 'getUserMedia', { configurable: true, value: () => new Promise((resolve, reject) => window.audioRequests.push({ resolve, reject })) });
    window.MediaRecorder = class {
      static isTypeSupported() { return true; }
      constructor(stream, options) { this.mimeType = options.mimeType; this.state = 'inactive'; }
      start() { this.state = 'recording'; }
      stop() { this.state = 'inactive'; queueMicrotask(() => { this.ondataavailable({ data: new Blob(['synthetic audio']) }); this.onstop(); }); }
    };
    MoyaiUI.insert(document.querySelector('#content'), 'beforeend', '<form id="audio-lifecycle"><div class="composer-toolbar"></div></form>');
    window.audioController = bindAudioRecorder(document.querySelector('#audio-lifecycle'), file => window.audioFiles.push(file.name), () => false);
    window.acquireAudio = () => {
      const track = { stopped: false, stop() { this.stopped = true; } };
      window.audioTracks.push(track);
      window.audioRequests.at(-1).resolve({ getTracks: () => [track] });
    };
  });
  const form = page.locator('#audio-lifecycle');
  const button = form.locator('.record-button');
  assert.equal(await button.locator('svg').count(), 1);
  for (const cancel of [false, true, false]) {
    await button.click();
    assert.equal(await button.getAttribute('aria-label'), 'Opening microphone…');
    await page.evaluate(() => window.acquireAudio());
    await form.getByRole('button', { name: 'Stop recording', exact: true }).waitFor();
    if (cancel) await form.getByRole('button', { name: 'Cancel recording', exact: true }).click();
    else await button.click();
    await form.getByRole('button', { name: 'Record audio', exact: true }).waitFor();
    assert.equal(await button.locator('svg').count(), 1);
  }
  await button.click();
  await page.evaluate(() => window.audioRequests.at(-1).reject(Object.assign(new Error('Denied'), { name: 'NotAllowedError' })));
  await form.getByRole('button', { name: 'Record audio', exact: true }).waitFor();
  assert.equal(await button.locator('svg').count(), 1);
  assert.deepEqual(await page.evaluate(() => window.audioFiles), ['Voice message.webm', 'Voice message.webm']);
  assert.equal(await page.evaluate(() => window.audioTracks.every(track => track.stopped)), true);
  await page.evaluate(() => { window.audioController.destroy(); MoyaiUI.render(document.querySelector('#audio-lifecycle'), ''); });
  assert.equal(await form.locator('button').count(), 0);
});

test('session connection selections survive navigation and reach the submit payload', async t => {
  const page = await pageFor(t);
  await page.route('**/api/runs', route => route.request().method() === 'POST'
    ? route.fulfill({ status: 503, json: { detail: 'Session test: keep the draft' } }) : route.continue());
  await page.getByLabel('Session options', { exact: true }).click();
  await page.getByRole('checkbox', { name: 'GitHub', exact: true }).click();
  const selected = ['linear', 'notion', 'slack'];
  assert.deepEqual(await page.evaluate(() => [...state.newDraft.plugins].sort()), selected);
  await page.evaluate(() => navigate('connections'));
  await page.evaluate(() => navigate('tasks'));
  await page.getByLabel('Session options', { exact: true }).click();
  assert.equal(await page.getByRole('checkbox', { name: 'GitHub', exact: true }).isChecked(), false);
  await page.locator('#prompt').fill('Check the selected connections.');
  for (const expected of [selected, []]) {
    if (!expected.length) {
      for (const checkbox of await page.locator('#plugin-options').getByRole('checkbox').all()) if (await checkbox.isChecked()) await checkbox.click();
    }
    assert.deepEqual(await page.evaluate(() => [...state.newDraft.plugins].sort()), expected);
    const request = page.waitForRequest(request => new URL(request.url()).pathname === '/api/runs' && request.method() === 'POST');
    await page.getByRole('button', { name: 'Start session', exact: true }).click();
    assert.deepEqual((await request).postDataJSON().plugins.sort(), expected);
    await page.waitForFunction(() => !state.sending.has('new'));
  }
});

test('repository checkboxes save selected IDs and reject an empty selection', async t => {
  const page = await pageFor(t, 'connections');
  const writes = [];
  await page.route('**/api/connections/github/repositories', route => {
    if (route.request().method() === 'POST') {
      writes.push(route.request().postDataJSON());
      return route.fulfill({ json: { ok: true } });
    }
    return route.fulfill({ json: { repositories: [{ id: 101, full_name: 'example/first' }, { id: 202, full_name: 'example/second' }], selected_ids: [101], installation_url: 'https://example.com/install' } });
  });
  await page.locator('[data-manage="github"]').click();
  await page.getByRole('button', { name: 'Choose repositories', exact: true }).click();
  await page.getByRole('checkbox', { name: 'example/first', exact: true }).click();
  await page.getByRole('button', { name: 'Save repositories', exact: true }).click();
  assert.match(await page.locator('#connection-error').textContent(), /Select at least one repository/);
  assert.deepEqual(writes, []);
  await page.getByRole('checkbox', { name: 'example/second', exact: true }).click();
  await page.getByRole('button', { name: 'Save repositories', exact: true }).click();
  await page.getByRole('dialog').waitFor({ state: 'detached' });
  assert.deepEqual(writes, [{ repository_ids: [202] }]);
});

test('automation connection count follows checkbox changes and saved values', async t => {
  const page = await pageFor(t, 'automations');
  await page.route('**/api/automations', route => route.request().method() === 'POST'
    ? route.fulfill({ status: 503, json: { detail: 'Automation test: keep the draft' } }) : route.continue());
  await page.locator('.automation-create summary').click();
  await page.getByRole('button', { name: 'Create', exact: true }).click();
  const connections = page.locator('.automation-connections');
  const count = connections.locator('[data-selected-connections]');
  let selected = await connections.getByRole('checkbox', { checked: true }).count();
  assert.equal(await count.textContent(), `${selected} selected`);
  for (const checkbox of await connections.getByRole('checkbox').all()) {
    if (!await checkbox.isChecked()) continue;
    await checkbox.click();
    assert.equal(await count.textContent(), `${--selected} selected`);
  }
  for (const id of ['github', 'slack']) {
    await connections.locator(`[data-connection="${id}"]`).getByRole('checkbox').click();
    assert.equal(await count.textContent(), `${++selected} selected`);
  }
  await page.getByLabel('Search connections and tools', { exact: true }).fill('GitHub');
  assert.equal(await connections.getByRole('checkbox').count(), 1);
  assert.equal(await count.textContent(), '2 selected', 'Filtering does not change the selection');
  await page.getByLabel('Automation name', { exact: true }).fill('Connection regression');
  await page.getByLabel('Instructions', { exact: true }).fill('Review the release and report results.');
  const request = page.waitForRequest(request => new URL(request.url()).pathname === '/api/automations' && request.method() === 'POST');
  await page.getByRole('button', { name: 'Save paused', exact: true }).click();
  assert.deepEqual((await request).postDataJSON().definition.plugins.sort(), ['github', 'slack']);
  await page.getByText('Automation test: keep the draft', { exact: true }).waitFor();
  assert.equal(await count.textContent(), '2 selected');
});

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
    const metadata = page.getByLabel('Value', { exact: true });
    const boundary = String.fromCodePoint(0x1f680).repeat(16384);
    await metadata.fill(boundary);
    assert.equal(await metadata.evaluate(el => el.checkValidity()), true);
    await metadata.fill(boundary + 'x');
    assert.equal(await metadata.evaluate(el => el.validity.customError), true);
    await metadata.fill('platform\nRelease checks & owners');
    assert.equal(await metadata.evaluate(el => el.checkValidity()), true);
    const values = await dialog.locator('form').evaluate(form => Object.fromEntries(new FormData(form)));
    assert.equal(values.queue_events, 'on');
    assert.equal(values.metadata_key, 'team');
    assert.equal(values.metadata_value, 'platform\nRelease checks & owners');
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

for (const width of [1440, 768, 320]) test(`Pi selection, keyboard, logo and submitted harness at ${width}px`, async t => {
  const page = await pageFor(t, 'tasks', 'populated', width);
  await page.route('**/api/config', async route => {
    const response = await route.fetch();
    const data = await response.json();
    data.harnesses.push({ id: 'pi', name: 'Pi' });
    await route.fulfill({ response, json: data });
  });
  await page.reload();
  const source = page.locator('#new-harness');
  const trigger = source.locator('..').getByRole('combobox');
  await trigger.focus();
  await page.keyboard.press('Space');
  await page.getByRole('option', { name: 'Pi', exact: true }).waitFor();
  await page.waitForFunction(() => document.activeElement?.getAttribute('role') === 'option');
  await page.keyboard.press('End');
  await page.waitForFunction(() => document.activeElement?.textContent === 'Pi');
  await page.keyboard.press('Enter');
  await page.getByRole('listbox').waitFor({ state: 'detached' });
  assert.equal(await source.inputValue(), 'pi');
  await page.waitForFunction(() => document.querySelector('.harness-picker img')?.src.endsWith('/harness-logos/pi.svg'));
  const logo = page.locator('.harness-picker img');
  await logo.evaluate(img => img.decode());
  assert.equal(await logo.evaluate(img => img.naturalWidth > 0), true);
  await trigger.click();
  const menu = await page.getByRole('listbox').boundingBox();
  assert.ok(menu.x >= 0 && menu.x + menu.width <= width + 1, 'Menu fits viewport');
  await page.screenshot({ path: `test-results/pi-picker-${width}.png`, fullPage: true });
  await page.keyboard.press('Escape');
  let submitted;
  await page.route('**/api/runs', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    submitted = route.request().postDataJSON();
    await route.fulfill({ status: 400, json: { detail: 'Synthetic submission captured.' } });
  });
  await page.locator('#prompt').fill('Check the Pi integration');
  await page.locator('#task-form button[type=submit]').click();
  await page.getByText('Synthetic submission captured.', { exact: true }).waitFor();
  assert.equal(submitted.harness, 'pi');
  assert.equal(submitted.prompt, 'Check the Pi integration');
  assert.equal(await source.inputValue(), 'pi', 'Selection survives a failed submission');
});

async function assertCardContentsFit(cards) {
  const failures = await cards.evaluateAll(nodes => nodes.flatMap(card => {
    const bounds = card.getBoundingClientRect();
    return [...card.querySelectorAll('img,strong,small,.file-type')].flatMap(child => {
      const rect = child.getBoundingClientRect();
      return rect.width && rect.height && (rect.top < bounds.top - 1 || rect.bottom > bounds.bottom + 1 || rect.left < bounds.left - 1 || rect.right > bounds.right + 1)
        ? [{ card: card.getAttribute('aria-label') || card.textContent.slice(0, 40), child: child.tagName, height: bounds.height, bottomOverflow: rect.bottom - bounds.bottom }] : [];
    });
  }));
  assert.deepEqual(failures, [], 'Content, filename and metadata stay inside the interactive card');
}

for (const width of [1440, 768, 320]) test(`attachment cards preserve content geometry across chat owners at ${width}px`, async t => {
  const page = await pageFor(t, 'tasks', 'populated', width);
  const files = ['png', 'txt', 'wav'].map((extension, index) => ({
    id: String(index + 1).padStart(32, '0'), name: `layout-${'long-filename-'.repeat(3)}.${extension}`, size: 123,
    media_type: ['image/png', 'text/plain', 'audio/wav'][index],
    ...(index === 0 ? { preview_url: '/static/favicon.svg' } : { preview_text: 'Synthetic attachment' }),
  }));
  await page.route('**/api/attachments/**', route => {
    const url = new URL(route.request().url()), file = files.find(file => file.name === url.searchParams.get('name'));
    return route.fulfill({ json: file ? { ...file, id: url.pathname.split('/').at(-1) } : {} });
  });
  for (const formSelector of ['#task-form', '#message-form']) {
    if (formSelector === '#message-form') {
      const run = { id: '9'.repeat(32), chat_enabled: true, mode: 'demo', status: 'running', prompt: 'Attachment layout', plugins: [], events: [], approvals: [],
        messages: [
          { id: 1, role: 'user', status: 'completed', content: 'Please inspect these files.', attachments: files },
          { id: 2, role: 'assistant', status: 'completed', content: 'These files remain inside their message.', attachments: files },
          { id: 3, role: 'user', status: 'running', content: 'Keep working.' },
          { id: 4, role: 'user', status: 'queued', content: 'Check these next.', attachments: files },
        ] };
      await page.route(`**/api/runs/${run.id}?*`, route => route.fulfill({ json: run }));
      await page.evaluate(async id => { await openRun(id); state.source?.close(); state.source = null; }, run.id);
      await assertCardContentsFit(page.locator('.sent-attachment'));
      assert.equal(await page.evaluate(() => {
        const messages = [...document.querySelectorAll('#conversation .chat-message')];
        return messages.every((message, index) => (!index || messages[index - 1].getBoundingClientRect().bottom <= message.getBoundingClientRect().top)
          && [...message.querySelectorAll('.sent-attachment')].every(card => card.getBoundingClientRect().bottom <= message.getBoundingClientRect().bottom));
      }), true, 'Messages contain their attachments and never overlap the following reply');
      for (const selector of ['.chat-message.user', '.chat-message.assistant', '.queued-message']) {
        const card = page.locator(`${selector} .sent-attachment.is-image`).first();
        await card.click();
        await page.getByRole('dialog').waitFor();
        await page.keyboard.press('Escape');
        await page.getByRole('dialog').waitFor({ state: 'detached' });
        await page.waitForFunction(el => el === document.activeElement, await card.elementHandle());
      }
    }
    const form = page.locator(formSelector);
    await form.locator('input[type=file]').setInputFiles(files.map(file => ({ name: file.name, mimeType: file.media_type, buffer: Buffer.from('synthetic file') })));
    await form.locator('.draft-attachment small').filter({ hasText: '123 B' }).nth(2).waitFor();
    await assertCardContentsFit(form.locator('.attachment-open'));
    await assertCardContentsFit(form.locator('.draft-attachment'));
    const preview = form.locator('.draft-attachment.is-image .attachment-open');
    await preview.click();
    await page.getByRole('dialog').waitFor();
    await page.keyboard.press('Escape');
    await page.getByRole('dialog').waitFor({ state: 'detached' });
    await page.waitForFunction(el => el === document.activeElement, await preview.elementHandle());
    assert.deepEqual(await form.locator('.send-button').evaluate(el => ({ width: el.offsetWidth, height: el.offsetHeight })), { width: 28, height: 28 }, 'Explicit icon geometry survives content sizing');
  }
});

test('template buttons retain multiline content and native flex shrink outside Settings', async t => {
  const page = await pageFor(t);
  await page.evaluate(() => {
    MoyaiUI.render(document.querySelector('#content'), `<div style="width:180px">
      <button class="skill-choice"><strong>Release review</strong><span>Review the release and summarize all validation results.</span></button>
      <button class="saved-file-choice"><span class="saved-file-icon">▤</span><span><strong>release-notes.md</strong><small>outputs/release-notes.md · 1 KB</small></span></button>
      <div class="approval-actions" style="width:150px"><button class="primary small">Approve once</button><button class="small">Deny</button></div>
    </div>`);
  });
  await assertCardContentsFit(page.locator('.skill-choice,.saved-file-choice'));
  assert.equal(await page.locator('.skill-choice').evaluate(el => el.scrollHeight <= el.clientHeight && el.scrollWidth <= el.clientWidth), true, 'Multiline descriptions fit their card');
  assert.equal(await page.locator('.approval-actions').evaluate(el => el.scrollWidth <= el.clientWidth), true, 'Action buttons shrink and wrap within the available width');
});
