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

for (const width of [1440, 768, 320]) for (const fixture of ['populated', 'member']) test(`Settings gear survives tooltip updates for ${fixture} at ${width}px`, async t => {
  const page = await pageFor(t, 'tasks', fixture, width);
  const gear = page.getByRole('button', { name: 'Settings', exact: true });
  const tooltip = page.getByRole('tooltip', { name: 'Settings', exact: true });
  for (const activation of ['mouse', 'Enter', 'Space']) {
    if (width <= 850) await page.getByRole('button', { name: 'Open sidebar', exact: true }).click();
    if (activation === 'mouse') await gear.hover();
    else await gear.focus();
    await tooltip.waitFor();
    if (activation === 'mouse') await gear.click();
    else await gear.press(activation);
    await page.waitForURL(url => url.hash === '#settings', { timeout: 3000 });
    await page.getByRole('heading', { name: 'Settings', exact: true }).waitFor();
    assert.equal(await page.locator('#content h1').evaluate(el => el === document.activeElement), true);
    if (width <= 850) {
      assert.equal(await page.locator('body').evaluate(el => el.classList.contains('sidebar-open')), false);
      await page.getByRole('button', { name: 'Open sidebar', exact: true }).click();
    }
    await page.getByRole('link', { name: 'Back to workspace' }).click();
    await page.locator('#prompt').waitFor();
  }
});

for (const width of [1440, 768, 320]) test(`sidebar toggles survive tooltip updates at ${width}px`, async t => {
  const page = await pageFor(t, 'tasks', 'member', width);
  const firstOpen = width <= 850;
  for (const open of [firstOpen, !firstOpen, firstOpen, !firstOpen]) {
    // The mobile scrim also has the Close sidebar name; test the tooltip button.
    const button = page.locator(open ? '#open-sidebar' : '#close-sidebar');
    await button.hover();
    await page.getByRole('tooltip', { name: open ? 'Show sidebar' : 'Hide sidebar', exact: true }).waitFor();
    await button.click();
    await page.waitForFunction(({ width, open }) => {
      const classes = document.body.classList;
      return (width <= 850 ? classes.contains('sidebar-open') : !classes.contains('rail-collapsed')) === open;
    }, { width, open });
    if (open) {
      const box = await page.locator('#sidebar').boundingBox();
      assert.ok(box.x >= 0 && box.x + box.width <= width, 'The opened sidebar is inside the viewport');
    }
  }
});

for (const width of [1440, 768, 320]) for (const composer of ['new', 'reply']) test(`skill picker rows stay aligned in the ${composer} composer at ${width}px`, async t => {
  const page = await pageFor(t, 'tasks', 'skill-picker', width);
  if (composer === 'reply') await page.goto(`${base}/?fixture=skill-picker#run=${'a'.repeat(32)}`);
  const input = page.getByRole('textbox', { name: 'Message Moyai', exact: true });
  await input.fill('/team');
  await page.waitForFunction(() => document.querySelectorAll('.skill-inline-option').length === 5);
  await page.evaluate(() => document.fonts.ready);
  const popup = page.locator('.skill-inline');
  const geometry = await popup.evaluate(el => {
    const rect = node => node.getBoundingClientRect().toJSON();
    return {
      popup: rect(el), overflow: el.scrollWidth - el.clientWidth,
      rows: [...el.querySelectorAll('[role=option]')].map(row => ({
        box: rect(row), icon: rect(row.querySelector('.skill-inline-icon')),
        name: rect(row.querySelector('.skill-inline-label')),
        badge: rect(row.querySelector('[data-slot=badge]')),
        description: rect(row.querySelector('.skill-inline-description')),
        padding: parseFloat(getComputedStyle(row).paddingTop),
        shadow: getComputedStyle(row).boxShadow,
      })),
    };
  });
  assert.equal(geometry.overflow, 0, 'The picker does not scroll horizontally');
  assert.ok(geometry.popup.left >= 0 && geometry.popup.right <= width, 'The picker stays inside the viewport');
  assert.ok(geometry.popup.top >= 0 && geometry.popup.bottom <= 1000, 'The picker fits vertically');
  for (const [index, row] of geometry.rows.entries()) {
    assert.ok(row.box.height >= 64, 'Two-line choices have room for their content and padding');
    assert.ok(row.name.top >= row.box.top + row.padding - 1);
    assert.ok(row.description.bottom <= row.box.bottom - row.padding + 1, 'Descriptions remain inside their own row');
    assert.ok(row.name.bottom + 3 <= row.description.top, 'Wrapped names do not overlap descriptions');
    assert.ok(row.name.right + 7 <= row.badge.left, 'Names cannot push into scope badges');
    assert.ok(Math.abs(row.name.top - row.badge.top) <= 1, 'Scope badges align with the first title line');
    assert.ok(Math.abs(row.name.left - row.description.left) <= 1, 'Names and descriptions share a leading edge');
    assert.ok(Math.abs(row.badge.right - geometry.rows[0].badge.right) <= 1, 'Scope badges share a trailing edge');
    assert.ok(row.icon.right < row.name.left, 'Icons remain in their own column');
    assert.equal(row.shadow, 'none', 'Choices have a flat menu surface');
    if (index) assert.ok(geometry.rows[index - 1].box.bottom <= row.box.top, 'Adjacent rows cannot overlap');
  }
  await input.press('ArrowDown');
  assert.match(await popup.locator('[aria-selected=true]').innerText(), /Organization/);
  await input.press('Enter');
  assert.equal(await input.evaluate(el => el.value), '/org:team ');
  assert.equal(await popup.isVisible(), false);
  await input.fill('/team');
  await popup.locator('[role=option]').first().click();
  assert.equal(await input.evaluate(el => el.value), '/personal:team ');
  await input.fill('/no-matching-command');
  await popup.getByRole('status').waitFor();
  assert.match(await popup.getByRole('status').innerText(), /No matching skills/);
  await input.press('Escape');
  assert.equal(await popup.isVisible(), false);
});

for (const width of [1440, 768, 320]) test(`agent rows align titles, disclosures and status at every depth at ${width}px`, async t => {
  const page = await pageFor(t, 'tasks', 'agent-sidebar', width);
  if (width < 850) await page.locator('#open-sidebar').click();
  const parent = page.locator('[data-toggle-agents="00000000000000000000000000000001"]');
  const child = page.locator('[data-toggle-agents="00000000000000000000000000000002"]');
  await parent.press('Enter');
  await child.press('Enter');
  const checkGeometry = async () => {
    const rows = await page.locator('.parent-session').evaluateAll(elements => elements.map(el => {
      const rect = node => node?.getBoundingClientRect().toJSON();
      const title = el.querySelector('.session-link-title');
      return {
        box: rect(el), title: rect(title), font: getComputedStyle(title).fontSize,
        line: parseFloat(getComputedStyle(title).lineHeight), meta: rect(el.querySelector('.session-link-meta')),
        indicator: rect(el.querySelector('.session-indicator')),
        disclosure: rect(el.querySelector('.agent-disclosure,.agent-disclosure-space')),
        action: rect(el.querySelector('.session-move')), icon: rect(el.querySelector('.agent-disclosure svg')),
        toggle: !!el.querySelector('.agent-disclosure'),
      };
    }));
    assert.equal(rows.length, 7, 'Main agents, subagents and a grandchild are visible');
    const center = rect => rect.top + rect.height / 2;
    for (const row of rows) {
      assert.ok(Math.abs(row.indicator.left - rows[0].indicator.left) <= 1, 'All status indicators share a column, including rows without actions');
      assert.ok(Math.abs(center(row.indicator) - row.title.top - row.line / 2) <= 1, 'Status aligns with the first title line, including wrapped keyboard focus');
      assert.ok(Math.abs(center(row.disclosure) - center(row.indicator)) <= 1, 'Disclosures and leaf markers align with the title');
      assert.ok(row.disclosure.right <= row.title.left && row.title.right + 7 <= row.indicator.left, 'Leading controls, titles and status cannot overlap');
      assert.ok(Math.abs(row.meta.left - row.title.left) <= 1, 'Metadata uses the title leading edge');
      assert.equal(row.font, rows[0].font, 'Agent labels use the same readable size at every depth');
      assert.ok(row.box.left >= 0 && row.box.right <= width, 'Rows stay inside the viewport');
      if (row.toggle) {
        assert.ok(row.icon, 'Disclosures use the shared SVG icon');
        assert.ok(Math.abs(center(row.icon) - center(row.indicator)) <= 1, 'Chevron shares the title center');
      }
      if (row.action) {
        assert.ok(row.indicator.right <= row.action.left, 'Status stays separate from the action');
        assert.ok(Math.abs(center(row.action) - center(row.indicator)) <= 1, 'Action shares the title center');
      }
    }
    assert.ok(rows[0].title.left < rows[1].title.left && rows[1].title.left < rows[2].title.left, 'Indentation preserves the parent, child and grandchild hierarchy');
    assert.equal(rows[1].title.left, rows[3].title.left, 'Sibling titles share a leading edge');
  };
  await checkGeometry();
  const longTitle = page.locator('[data-run="00000000000000000000000000000006"]');
  assert.equal(await longTitle.locator('.session-link-title').evaluate(el => el.scrollWidth > el.clientWidth), true);
  await longTitle.focus();
  await checkGeometry();
  await parent.press('Enter');
  assert.equal(await child.count(), 0, 'Collapsing removes nested rows');
  assert.equal(await parent.evaluate(el => el === document.activeElement), true, 'Keyboard focus survives the toggle');
  await parent.press('Enter');
  await checkGeometry();
  await page.locator('[data-session-actions="00000000000000000000000000000005"]').click();
  await page.locator('#session-actions').getByRole('button', { name: 'Rename', exact: true }).waitFor();
  await page.keyboard.press('Escape');
  await checkGeometry();
  await page.locator('[data-run="00000000000000000000000000000004"]').click();
  if (width < 850) await page.locator('#open-sidebar').click();
  await page.locator('.child-session.selected').waitFor();
  await checkGeometry();
  const folderId = 'alignment-folder';
  await page.route('**/api/session-folders', route => route.fulfill({ json: { folders: [{ id: folderId, name: 'Agent alignment', revision: 1 }] } }));
  await page.route(/\/api\/runs(?:\?|$)/, async route => {
    const response = await route.fetch(), runs = await response.json();
    runs[0].folder_id = folderId;
    await route.fulfill({ json: runs });
  });
  await page.evaluate(() => refreshRuns());
  await page.locator('.session-folder .child-session').first().waitFor();
  await checkGeometry();
});

for (const width of [1440, 768, 320]) test(`folder headings keep their leading alignment at ${width}px`, async t => {
  const page = await pageFor(t, 'tasks', 'populated', width);
  const folders = ['hello', 'A very long folder name that must truncate within the sidebar'].map((name, i) => ({ id: String(i + 1).repeat(32), name, revision: 1 }));
  await page.route('**/api/session-folders', route => route.fulfill({ json: { folders } }));
  await page.evaluate(() => refreshRuns());
  if (width < 850) await page.locator('#open-sidebar').click();
  for (const folder of folders) {
    const toggle = page.locator(`[data-toggle-folder="${folder.id}"]`);
    for (const expanded of [true, false]) {
      assert.equal(await toggle.getAttribute('aria-expanded'), String(expanded));
      const bounds = await toggle.evaluate(el => {
        const rect = el.getBoundingClientRect(), chevron = el.querySelector('.folder-chevron').getBoundingClientRect();
        const menu = el.parentElement.querySelector('.folder-menu').getBoundingClientRect();
        const centers = [...el.querySelectorAll('svg,.folder-name,.folder-count')].map(node => {
          const box = node.getBoundingClientRect(); return box.top + box.height / 2;
        });
        return { leading: chevron.left - rect.left, padding: parseFloat(getComputedStyle(el).paddingLeft), right: rect.right, menuLeft: menu.left, centers, iconCount: el.querySelectorAll('svg').length };
      });
      assert.ok(Math.abs(bounds.leading - bounds.padding) <= 1, 'Folder contents start at the leading padding');
      assert.ok(bounds.right <= bounds.menuLeft, 'The folder menu remains outside the toggle');
      assert.equal(bounds.iconCount, 2, 'Folder and disclosure both use SVG icons, independent of font baselines');
      assert.ok(Math.max(...bounds.centers) - Math.min(...bounds.centers) <= 1, 'Icons, label and count share a vertical center');
      await toggle.focus();
      await page.keyboard.press('Enter');
    }
  }
  const longName = page.locator(`[data-toggle-folder="${folders[1].id}"] .folder-name`);
  assert.equal(await longName.evaluate(el => el.scrollWidth > el.clientWidth), true, 'Long names remain truncated');
  await page.getByRole('button', { name: 'Rename or remove hello', exact: true }).click();
  await page.getByRole('dialog', { name: 'Rename folder', exact: true }).waitFor();
  assert.equal(await page.getByLabel('Folder name', { exact: true }).inputValue(), 'hello');
  const dialog = page.getByRole('dialog', { name: 'Rename folder', exact: true });
  const geometry = await dialog.evaluate(el => {
    const rect = node => node.getBoundingClientRect().toJSON();
    return { title: rect(el.querySelector('h2')), close: rect(el.querySelector('.dialog-close')), input: rect(el.querySelector('input')), box: rect(el) };
  });
  assert.ok(Math.abs(geometry.title.top + geometry.title.height / 2 - geometry.close.top - geometry.close.height / 2) <= 1, 'Close control aligns with the dialog title');
  assert.ok(geometry.title.right <= geometry.close.left - 8, 'Title and close control do not overlap');
  assert.ok(Math.abs(geometry.close.right - geometry.input.right) <= 1, 'Close control and field share the trailing content edge');
  assert.ok(geometry.box.left >= 0 && geometry.box.right <= width, 'Dialog stays inside the viewport');
  await page.getByRole('button', { name: 'Close folder dialog', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  await page.waitForFunction(() => document.activeElement?.getAttribute('aria-label') === 'Rename or remove hello');
});

for (const width of [1440, 768, 320]) for (const fixture of ['populated', 'member']) test(`session scope uses a readable field in ${fixture} at ${width}px`, async t => {
  const page = await pageFor(t, 'tasks', fixture, width);
  await page.evaluate(fixture => { state.authenticated = true; state.role = fixture === 'member' ? 'member' : 'admin'; restoreSessionScope(); }, fixture);
  if (width < 850) await page.locator('#open-sidebar').click();
  const trigger = page.getByRole('combobox', { name: 'Filter sessions', exact: true });
  for (const count of ['4', '100', '100000']) {
    await page.locator('#task-count').evaluate((el, value) => { el.textContent = value; }, count);
    const geometry = await trigger.evaluate(el => {
      const label = el.querySelector('span'), row = el.closest('.sessions-label');
      const rect = node => node.getBoundingClientRect().toJSON();
      return { box: rect(el), label: rect(label), count: rect(row.querySelector('#task-count')), add: rect(row.querySelector('#new-folder')), textSize: parseFloat(getComputedStyle(label).fontSize), border: parseFloat(getComputedStyle(el).borderTopWidth), clipped: label.scrollWidth > label.clientWidth };
    });
    assert.ok(geometry.textSize >= 14, 'Heading text is at least normal control size');
    assert.ok(geometry.border >= 1 && geometry.box.height >= 36, 'Session scope is a visible field with a full control target');
    assert.equal(geometry.clipped, false, 'My sessions remains fully visible');
    assert.ok(geometry.box.right <= geometry.count.left && geometry.count.right <= geometry.add.left, 'Count and action do not overlap the field');
    assert.ok(Math.abs(geometry.box.top + geometry.box.height / 2 - geometry.add.top - geometry.add.height / 2) <= 1, 'The field and add-folder button share a center');
  }
  if (fixture === 'member') {
    assert.equal(await trigger.isEnabled(), false, 'Members cannot switch to all sessions');
    return;
  }
  const labelSize = await trigger.locator('span').first().evaluate(el => parseFloat(getComputedStyle(el).fontSize));
  await trigger.click();
  const option = page.getByRole('option', { name: 'All sessions', exact: true });
  const optionSize = await option.evaluate(el => parseFloat(getComputedStyle(el).fontSize));
  assert.ok(Math.abs(optionSize - labelSize) <= 2, 'The open menu and trigger have consistent typography');
  await option.click();
  assert.match(await trigger.innerText(), /All sessions/);
  await page.locator('#task-count').evaluate(el => { el.textContent = '100'; });
  const allSessions = await trigger.evaluate(el => {
    const rect = node => node.getBoundingClientRect().toJSON(), row = el.closest('.sessions-label');
    const label = el.querySelector('span');
    return { field: rect(el), label: rect(label), count: rect(row.querySelector('#task-count')), add: rect(row.querySelector('#new-folder')), clipped: label.scrollWidth > label.clientWidth };
  });
  assert.equal(allSessions.clipped, false, 'All sessions remains fully visible beside a three-digit count');
  assert.ok(allSessions.field.right + 4 <= allSessions.count.left && allSessions.count.right + 4 <= allSessions.add.left, 'All sessions, 100 and Add folder retain visible spacing');
  const centers = ['label', 'count', 'add'].map(key => allSessions[key].top + allSessions[key].height / 2);
  assert.ok(Math.max(...centers) - Math.min(...centers) <= 1, 'The scope label, count and add icon share a vertical center');
  await trigger.press('Enter');
  await page.keyboard.press('Escape');
  await page.getByRole('listbox').waitFor({ state: 'hidden' });
  assert.equal(await page.getByRole('listbox').count(), 0);
  await page.waitForFunction(() => document.activeElement === document.querySelector('.sessions-label [role=combobox]'));
});

for (const width of [1440, 768, 320]) test(`PR tab status refresh owns its tooltip and survives focused redraws and closure at ${width}px`, async t => {
  const page = await pageFor(t, 'tasks', 'populated', width);
  const title = 'Review <PR> "safely"', url = 'https://github.com/example/workspace/pull/101';
  await page.evaluate(({title,url}) => {
    MoyaiUI.render(document.querySelector('#content'), '<div class="chat-layout"></div>');
    const layout = document.querySelector('.chat-layout');
    const receipt = {title,url,state:'open',number:101,repository:'example/workspace'};
    window.prFixture = {...receipt,merged:false,draft:false,base:'main',files:[]};
    window.prHold = false;
    window.prPanel = MoyaiPanel.create({
      run:{id:'status-regression',pull_requests:[receipt]},layout,user:'test',models:[],
      escape:esc,markdown:renderMarkdown,size:String,computer:{},toast(){},
      api:async path => {
        if (!path.includes('/pull-request?')) return [];
        if (window.prHold) await new Promise(resolve => window.prRelease = resolve);
        return {...window.prFixture};
      }
    });
    window.prPanel.open('pulls');
    window.prPanel.openPullRequest(url);
  }, {title,url});
  const tabFor = state => page.getByRole('tab', {name:`${title} · ${state}`,exact:true});
  await tabFor('Open').hover();
  await page.getByRole('tooltip', {name:`${title} · Open`,exact:true}).waitFor();
  assert.equal(await tabFor('Open').getAttribute('title'), null, 'Only the shared tooltip owns the hover label');
  await page.getByRole('tab', {name:'Pull requests',exact:true}).click();
  assert.equal(await page.getByRole('tab', {name:'Pull requests',exact:true}).getAttribute('aria-selected'), 'true');
  await tabFor('Open').hover();
  await page.getByRole('tooltip', {name:`${title} · Open`,exact:true}).waitFor();
  await tabFor('Open').click();
  assert.equal(await tabFor('Open').getAttribute('aria-selected'), 'true', 'Opening the tooltip must not replace the tab click handler');
  for (const [state, merged, label] of [['closed',true,'Merged'],['closed',false,'Closed'],['open',false,'Open']]) {
    const previous = await page.locator('.panel-tabs [aria-selected=true]').getAttribute('aria-label');
    await page.evaluate(({state,merged}) => {
      Object.assign(window.prFixture,{state,merged});window.prHold=true;window.prRelease=null;
    }, {state,merged});
    await page.getByRole('button', {name:'Refresh pull request',exact:true}).click();
    await page.waitForFunction(() => typeof window.prRelease === 'function');
    await page.getByRole('tab', {name:previous,exact:true}).focus();
    const scrollLeft = await page.locator('.panel-tabs').evaluate(el => el.scrollLeft);
    await page.evaluate(() => {window.prHold=false;window.prRelease();});
    await tabFor(label).waitFor();
    assert.equal(await tabFor(label).evaluate(el => el === document.activeElement), true, 'Async status updates preserve tab focus');
    assert.equal(await page.locator('.panel-tabs').evaluate(el => el.scrollLeft), scrollLeft, 'Async status updates preserve tab scroll');
    assert.equal(await tabFor(label).getAttribute('title'), null);
    await page.mouse.move(0, 0);
    await page.getByRole('button', {name:'Refresh pull request',exact:true}).focus();
    await tabFor(label).hover();
    await page.getByRole('tooltip', {name:`${title} · ${label}`,exact:true}).waitFor();
    await page.getByRole('tab', {name:'Pull requests',exact:true}).click();
    assert.equal(await page.getByRole('tab', {name:'Pull requests',exact:true}).getAttribute('aria-selected'), 'true');
    await tabFor(label).hover();
    await page.getByRole('tooltip', {name:`${title} · ${label}`,exact:true}).waitFor();
    await tabFor(label).click();
    assert.equal(await tabFor(label).getAttribute('aria-selected'), 'true');
    await page.getByRole('heading', {name:title,exact:true}).waitFor();
  }
  await page.getByRole('button', {name:`Close ${title} tab`,exact:true}).click();
  assert.equal(await tabFor('Open').count(), 0);
  await page.getByRole('link', {name:`${title} example/workspace #101`,exact:true}).click();
  await tabFor('Open').waitFor();
  await page.evaluate(() => window.prPanel.dispose());
  assert.equal(await page.getByRole('tablist', {name:'Workspace tabs'}).count(), 0);
});

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
    // A visible portal can precede Radix's initial focus effect. Send End only
    // after an option owns focus, so keyboard navigation starts in the menu.
    await page.waitForFunction(el => el.contains(document.activeElement) &&
      document.activeElement?.getAttribute('role') === 'option', await menu.elementHandle());
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
          if (tab === 'users') await page.locator('#spend-activity').getByRole('heading', { name: 'Team activity', exact: true }).waitFor();
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

for (const width of [1440, 768, 320]) {
  for (const fixture of ['member', 'populated']) {
    test(`session filter paints only its styled label for ${fixture} at ${width}px`, async t => {
      const page = await pageFor(t, 'tasks', fixture, width);
      if (width < 850) await page.getByRole('button', { name: 'Open sidebar', exact: true }).click();
      const source = page.locator('#session-scope');
      const trigger = source.locator('..').getByRole('combobox');
      await trigger.waitFor({ state: 'visible' });
      assert.equal(await source.evaluate(node => {
        const style = getComputedStyle(node);
        return style.visibility === 'hidden' || Number(style.opacity) === 0;
      }), true, 'Native label must not paint beneath the styled label');
      assert.equal(await trigger.innerText(), 'My sessions');
      assert.equal(await trigger.locator('span').first().evaluate(node => node.scrollWidth <= node.clientWidth), true, 'Session label is not truncated');
      assert.ok((await source.boundingBox()).width > 0, 'Native select retains layout sizing');
      if (fixture === 'member') {
        assert.equal(await source.isDisabled(), true);
        assert.equal(await trigger.getAttribute('data-disabled'), '');
        await trigger.click({ force: true });
        assert.equal(await page.getByRole('listbox').count(), 0);
      } else {
        await choose(page, source, 'all');
        assert.equal(await source.inputValue(), 'all');
        assert.equal(await trigger.innerText(), 'All sessions');
        await choose(page, source, 'mine');
        assert.equal(await source.inputValue(), 'mine');
      }
    });
  }
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

test('retained workspace tabs and toolbar actions survive focus updates and repeated clicks', async t => {
  const page = await pageFor(t);
  await page.evaluate(() => {
    const run = {
      id: '77777777777777777777777777777777', mode: 'demo', status: 'completed',
      prompt: 'Verify workspace visibility', plugins: [], approvals: [], artifacts: [],
      credential_requests: [], messages: [], events: [],
    };
    state.selected = run.id;
    renderChat(run);
    state.source?.close(); state.source = null;
    workspacePanel.open('pulls');
    workspacePanel.open('agents');
    window.retainedPanelView = document.querySelector('.panel-agents');
    window.retainedPanelTabs = [...document.querySelectorAll('.panel-tabs [data-tab]')];
  });
  const panel = page.locator('#workspace-panel');
  const toggle = page.locator('#workspace-panel-toggle');
  const focusedClick = async control => {
    await control.focus();
    await control.press('Shift');
    // Let React process focus changes even when the tooltip remains closed.
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await control.click();
  };
  const selectedAgents = panel.getByRole('tab', { name: 'Subagents', exact: true });
  const assertSelected = async tab => {
    assert.equal(await tab.getAttribute('aria-selected'), 'true');
    assert.equal(await tab.getAttribute('tabindex'), '0');
    assert.equal(await panel.getByRole('tab').evaluateAll((tabs, selectedId) => tabs.every(item =>
      item.getAttribute('aria-selected') === String(item.id === selectedId) &&
      item.getAttribute('tabindex') === (item.id === selectedId ? '0' : '-1')),
    await tab.getAttribute('id')), true, 'Exactly the selected tab has aria-selected=true and tabindex=0');
  };
  // A selected tab must keep the controller's attributes through actual React
  // tooltip updates, including a click whose select() is intentionally a no-op.
  await page.locator('#followup').focus();
  await selectedAgents.focus();
  const selectedTooltip = page.getByRole('tooltip', { name: 'Subagents', exact: true });
  await selectedTooltip.waitFor({ state: 'visible' });
  assert.equal(await selectedAgents.getAttribute('data-state'), 'instant-open');
  await assertSelected(selectedAgents);
  await selectedAgents.click();
  await selectedTooltip.waitFor({ state: 'detached' });
  assert.equal(await selectedAgents.getAttribute('data-state'), 'closed');
  await assertSelected(selectedAgents);
  // First empty receipt sync reaches draw() with an unchanged tab template.
  await page.evaluate(() => workspacePanel.syncPullRequests({
    id: '77777777777777777777777777777777', pull_requests: [],
  }));
  await assertSelected(selectedAgents);
  assert.equal(await selectedAgents.evaluate(el => el === window.retainedPanelTabs[1]), true);
  for (const name of ['Pull requests', 'Subagents', 'Pull requests', 'Subagents']) {
    const tab = panel.getByRole('tab', { name, exact: true });
    await focusedClick(tab);
    await assertSelected(tab);
    assert.equal(await page.evaluate(() => [...document.querySelectorAll('.panel-tabs [data-tab]')].every((node, index) => node === window.retainedPanelTabs[index])), true);
  }
  const agents = panel.getByRole('tab', { name: 'Subagents', exact: true });
  await agents.press('Home');
  assert.equal(await panel.getByRole('tab', { name: 'Pull requests', exact: true }).getAttribute('aria-selected'), 'true');
  await panel.getByRole('tab', { name: 'Pull requests', exact: true }).press('End');
  assert.equal(await agents.getAttribute('aria-selected'), 'true');
  await focusedClick(panel.getByRole('button', { name: 'Close Pull requests tab', exact: true }));
  assert.equal(await panel.getByRole('tab', { name: 'Pull requests', exact: true }).count(), 0);
  assert.equal(await agents.getAttribute('aria-selected'), 'true');
  for (let attempt = 0; attempt < 2; attempt++) {
    await focusedClick(panel.locator('[data-add]'));
    assert.equal(await panel.locator('.panel-menu').isVisible(), true);
    await focusedClick(panel.locator('[data-add]'));
    assert.equal(await panel.locator('.panel-menu').isVisible(), false);
    assert.equal(await panel.isVisible(), true);
    await focusedClick(panel.locator('[data-expand]'));
    assert.equal(await page.locator('.chat-layout').evaluate(el => el.classList.contains('panel-expanded')), true);
    await focusedClick(panel.locator('[data-expand]'));
    assert.equal(await page.locator('.chat-layout').evaluate(el => el.classList.contains('panel-expanded')), false);
    await focusedClick(panel.locator('[data-hide]'));
    assert.equal(await panel.isVisible(), false);
    assert.equal(await toggle.evaluate(el => el === document.activeElement), true);
    await focusedClick(toggle);
    assert.equal(await panel.isVisible(), true);
    assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
    assert.equal(await panel.locator('.panel-agents').evaluate(el => el === window.retainedPanelView), true);
  }
  await toggle.press('Enter');
  assert.equal(await panel.isVisible(), false, 'One keyboard activation runs one toggle');
  assert.equal(await toggle.getAttribute('aria-expanded'), 'false');
  await page.evaluate(() => navigate('tasks'));
});

for (const width of [1440, 320]) test(`conversation space retains the composer through updates and releases on navigation at ${width}px`, async t => {
  const page = await pageFor(t, 'tasks', 'conversation-space', width);
  await page.route('**/api/runs/*/files', route => route.fulfill({ json: {
    files: ['release-checks.md', 'reconnect.test.js'].map(name => ({
      name, path: name, archive_path: `new-files/${name}`, kind: 'text', size: 128,
    })),
  } }));
  await page.evaluate(() => {
    const mount = MoyaiSpace.mount;
    window.conversationSpaceLifecycle = { mounts: 0, cleanups: 0 };
    MoyaiSpace.mount = (canvas, options) => {
      const cleanup = mount(canvas, options);
      if (canvas?.id !== 'conversation-space-field') return cleanup;
      window.conversationSpaceLifecycle.mounts++;
      return () => { window.conversationSpaceLifecycle.cleanups++; cleanup(); };
    };
    return openRun('1'.repeat(32));
  });
  const header = page.locator('#header-actions');
  const activity = header.locator('#toggle-details');
  const files = header.locator('#files-button');
  const panel = page.locator('#workspace-panel');
  assert.equal(await activity.getAttribute('aria-controls'), 'workspace-panel');
  assert.equal(await activity.getAttribute('aria-expanded'), 'false');
  assert.equal(await files.isVisible(), false, 'Files stays hidden until the session has saved results');
  await activity.focus();
  await activity.press('Enter');
  await panel.getByRole('heading', { name: 'Session activity', exact: true }).waitFor();
  assert.equal(await activity.getAttribute('aria-expanded'), 'true');
  assert.equal(await panel.getByRole('tab', { name: 'Activity', exact: true }).getAttribute('aria-selected'), 'true');
  await panel.getByRole('button', { name: 'Hide workspace panel', exact: true }).click();
  await panel.waitFor({ state: 'hidden' });
  assert.equal(await activity.getAttribute('aria-expanded'), 'false');
  await page.evaluate(() => updateChat({ ...state.chatRun, has_artifact: true }));
  await header.getByRole('button', { name: 'Files · 2', exact: true }).waitFor();
  await files.focus();
  await files.press('Enter');
  await panel.getByRole('searchbox', { name: 'Find a saved file', exact: true }).waitFor();
  assert.equal(await panel.locator('.panel-file-choice').count(), 2);
  assert.equal(await panel.getByRole('tab', { name: 'Files', exact: true }).getAttribute('aria-selected'), 'true');
  assert.equal(await activity.getAttribute('aria-expanded'), 'false');
  await panel.getByRole('button', { name: 'Hide workspace panel', exact: true }).click();
  await panel.waitFor({ state: 'hidden' });
  const input = page.locator('#followup');
  await input.fill('Keep my unfinished reply');
  await page.evaluate(() => {
    window.retainedConversationCanvas = document.querySelector('#conversation-space-field');
    window.retainedConversationComposer = document.querySelector('#followup');
    const next = structuredClone(state.chatRun);
    next.messages.at(-1).content += '\n\nThe transcript was refreshed.';
    updateChat(next);
  });
  await page.locator('#conversation').getByText('The transcript was refreshed.', { exact: true }).waitFor();
  assert.equal(await input.evaluate(el => el.value), 'Keep my unfinished reply');
  assert.deepEqual(await page.evaluate(() => ({
    sameCanvas: document.querySelector('#conversation-space-field') === window.retainedConversationCanvas,
    sameComposer: document.querySelector('#followup') === window.retainedConversationComposer,
    focusRetained: document.activeElement === window.retainedConversationComposer,
    rendered: window.retainedConversationCanvas.width > 0 && window.retainedConversationCanvas.height > 0,
    hiddenFromAccessibility: window.retainedConversationCanvas.getAttribute('aria-hidden') === 'true',
    ...window.conversationSpaceLifecycle,
  })), { sameCanvas: true, sameComposer: true, focusRetained: true, rendered: true, hiddenFromAccessibility: true, mounts: 1, cleanups: 0 });
  await page.evaluate(() => document.fonts.ready);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'The conversation stays within the viewport');
  const composer = await page.locator('#message-form').boundingBox();
  assert.ok(composer.width > 0 && composer.x >= 0 && composer.x + composer.width <= width + 1, 'The full reply composer stays reachable');
  await page.evaluate(() => navigate('tasks'));
  await page.locator('#prompt').waitFor();
  assert.deepEqual(await page.evaluate(() => ({
    oldCanvasConnected: window.retainedConversationCanvas.isConnected,
    oldComposerConnected: window.retainedConversationComposer.isConnected,
    ...window.conversationSpaceLifecycle,
  })), { oldCanvasConnected: false, oldComposerConnected: false, mounts: 1, cleanups: 1 });
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
    await page.getByRole('button', { name: 'Edit message', exact: true }).click();
    await page.locator('#prompt').waitFor();
    await page.getByLabel('Session options', { exact: true }).click();
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
  await page.getByRole('button', { name: 'Edit message', exact: true }).click();
  await page.locator('#prompt').waitFor();
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

for (const width of [1440, 768, 320]) test(`workspace dialogs keep their styles across settings routes at ${width}px`, async t => {
  const page = await pageFor(t, 'tasks', 'populated', width);
  const styles = locator => locator.evaluateAll(nodes => nodes.map(node => {
    const style = getComputedStyle(node);
    return Object.fromEntries(['fontSize', 'lineHeight', 'borderWidth', 'borderRadius', 'padding', 'minHeight', 'backgroundColor', 'color', 'outline', 'outlineOffset', 'width', 'maxHeight'].map(key => [key, style[key]]));
  }));
  await page.route('**/api/runs?*', route => new URL(route.request().url()).searchParams.has('search')
    ? route.fulfill({ status: 503, json: { detail: 'Search temporarily unavailable' } }) : route.continue());
  const appearances = [];
  for (const route of ['tasks', 'settings']) {
    await page.evaluate(view => navigate(view), route);
    await page.keyboard.press('Meta+k');
    await page.getByRole('option', { name: /^Search sessions/ }).click();
    await page.locator('#command-search').fill('retry needle');
    await page.getByRole('button', { name: 'Retry search', exact: true }).waitFor();
    const palette = await styles(page.locator('[data-dialog-id="command-palette"], #command-search, [data-command-back], [data-command-close], [data-command-retry]'));
    await page.keyboard.press('Escape');
    await page.getByRole('dialog').waitFor({ state: 'detached' });
    await page.keyboard.press('Meta+k');
    await page.getByRole('option', { name: /^New folder/ }).click();
    const folder = page.getByRole('dialog', { name: 'New folder', exact: true });
    await folder.waitFor();
    const folderStyles = await styles(folder.locator('input, button'));
    appearances.push({ palette, folder: folderStyles });
    await page.keyboard.press('Escape');
    await folder.waitFor({ state: 'detached' });
  }
  assert.deepEqual(appearances[1], appearances[0], 'Settings must not restyle global dialogs, including error and navigation controls');
  await page.evaluate(() => navigate('skills'));
  await page.getByRole('button', { name: 'Add skill', exact: true }).click();
  const input = page.locator('#skill-name');
  await input.waitFor();
  assert.equal(await input.evaluate(node => getComputedStyle(node).padding), '9px 12px', 'Settings editors retain their form styling');
  assert.equal(await input.evaluate(node => getComputedStyle(node).fontSize), width <= 768 ? '16px' : '14px');
  assert.equal(await page.getByRole('dialog').evaluate(node => getComputedStyle(node).borderRadius), '16px');
});

test('reused skill and credential dialogs restore the styling of each owning flow', async t => {
  const page = await pageFor(t);
  const cases = [
    { route: 'skills', open: 'Add skill', workspace: () => openSkillPicker('prompt') },
    { route: 'secrets', open: 'Add credential', workspace: () => openCredentialDialog({ id: 'input-test', provider: 'generic', name: 'synthetic-service', format: 'env', reason: 'Verify dialog ownership', generation: 1, can_personal: true, can_organization: true, preferred_scope: 'personal' }) },
  ];
  const appearance = async (scope = 'workspace') => {
    const dialog = page.getByRole('dialog');
    await dialog.waitFor();
    assert.equal(await dialog.evaluate(node => getComputedStyle(node).borderRadius), scope === 'settings' ? '16px' : '8px', `The ${scope} flow owns the dialog surface`);
    return dialog.locator('input, textarea, button').evaluateAll(nodes => nodes.map(node => {
      const s = getComputedStyle(node);
      return [s.fontSize, s.padding, s.borderWidth, s.minHeight, s.backgroundColor];
    }));
  };
  const close = async () => {
    await page.keyboard.press('Escape');
    await page.getByRole('dialog').waitFor({ state: 'detached' });
  };
  for (const flow of cases) {
    await page.evaluate(() => navigate('tasks'));
    await page.evaluate(flow.workspace);
    const workspace = await appearance();
    await close();
    let settings;
    for (let cycle = 0; cycle < 2; cycle++) {
      await page.evaluate(view => navigate(view), flow.route);
      await page.getByRole('button', { name: flow.open, exact: true }).click();
      const current = await appearance('settings');
      if (cycle) assert.deepEqual(current, settings, `${flow.route} restores settings styling after workspace use`);
      settings = current;
      await close();
      await page.evaluate(() => navigate('tasks'));
      await page.evaluate(flow.workspace);
      assert.deepEqual(await appearance(), workspace, `${flow.route} releases settings styling for the workspace flow`);
      await close();
    }
  }
});

for (const width of [1440, 768, 320]) test(`command palette preserves drafts, keyboard focus and viewport bounds at ${width}px`, async t => {
  const page = await pageFor(t, 'tasks', 'populated', width);
  const prompt = page.locator('#prompt');
  await prompt.fill('Keep my unsent session prompt');
  await page.locator('#session-list [data-run]').first().waitFor({ state: 'attached' });
  const sidebar = await page.locator('#session-list [data-run]').evaluateAll(rows => rows.map(row => row.dataset.run));
  const route = page.url();
  await prompt.press('Meta+k');
  const dialog = page.getByRole('dialog', { name: 'Commands and session search', exact: true });
  const input = page.locator('#command-search');
  await dialog.waitFor();
  assert.equal(page.url(), route, 'Opening commands does not navigate away from the draft');
  assert.equal(await input.getAttribute('role'), 'combobox');
  assert.equal(await input.getAttribute('aria-controls'), 'command-results');
  assert.equal(await input.evaluate(el => el === document.activeElement), true);
  const bounds = await dialog.boundingBox();
  assert.ok(bounds.x >= 0 && bounds.y >= 0 && bounds.x + bounds.width <= width + 1 && bounds.y + bounds.height <= 1001, JSON.stringify(bounds));
  assert.equal(await dialog.evaluate(el => el.scrollWidth <= el.clientWidth), true, 'Palette content fits its dialog');
  const selected = await input.getAttribute('aria-activedescendant');
  assert.ok(selected && await page.locator(`[id="${selected}"]`).getAttribute('role') === 'option');
  await input.press('ArrowDown');
  assert.notEqual(await input.getAttribute('aria-activedescendant'), selected);
  await input.press('ArrowUp');
  assert.equal(await input.getAttribute('aria-activedescendant'), selected);
  for (let step = 0; step < 5; step++) {
    await page.keyboard.press('Tab');
    assert.equal(await dialog.evaluate(el => el.contains(document.activeElement)), true, 'Tab remains inside the modal');
  }
  await page.keyboard.press('Escape');
  await dialog.waitFor({ state: 'detached' });
  await page.waitForFunction(() => document.activeElement?.id === 'prompt');
  assert.equal(await prompt.evaluate(el => el.value), 'Keep my unsent session prompt');
  assert.deepEqual(await page.locator('#session-list [data-run]').evaluateAll(rows => rows.map(row => row.dataset.run)), sidebar);
  await prompt.press('Control+k');
  await dialog.waitFor();
  await input.press('Enter');
  await dialog.waitFor({ state: 'detached' });
  await page.waitForFunction(() => document.activeElement?.id === 'prompt');
  assert.equal(await prompt.evaluate(el => el.value), 'Keep my unsent session prompt', 'Starting from the palette preserves the existing unsent draft');
});

test('palette session search isolates results, escapes message matches and discards delayed responses', async t => {
  const page = await pageFor(t);
  const input = page.locator('#command-search');
  const dialog = page.getByRole('dialog', { name: 'Commands and session search', exact: true });
  const releases = [];
  t.after(() => releases.forEach(release => release()));
  const gates = new Map(['old needle', 'closed needle'].map(query => [query, new Promise(resolve => releases.push(resolve))]));
  const worker = 'b'.repeat(32), parent = 'a'.repeat(32);
  let retries = 0;
  await page.route('**/api/runs?*', async route => {
    const query = new URL(route.request().url()).searchParams.get('search');
    if (!query) return route.continue();
    if (gates.has(query)) await gates.get(query);
    if (query === 'retry needle' && retries++ === 0) return route.fulfill({ status: 503, json: { detail: 'Search temporarily unavailable' } });
    const rows = query === 'absent needle' ? [] : [{ id: parent, display_title: query === 'current needle' ? 'Parent investigation' : query, status: 'idle', search_query: query, search_match: query !== 'current needle', children: query === 'current needle' ? [{ id: worker, parent_run_id: parent, agent_label: 'Matching worker', status: 'idle', search_query: query, search_match: true, search_snippet: 'Current needle <img src=x onerror=alert(1)> appears in a saved message' }] : [] }];
    return route.fulfill({ json: rows });
  });
  const sidebar = await page.locator('#session-list [data-run]').evaluateAll(rows => rows.map(row => row.dataset.run));
  await page.locator('#search-sessions').click();
  await dialog.waitFor();
  assert.equal(await input.getAttribute('placeholder'), 'Search titles and messages…');
  const waitForQuery = query => page.waitForRequest(request => new URL(request.url()).searchParams.get('search') === query);
  const oldRequest = waitForQuery('old needle');
  await input.fill('old needle');
  await oldRequest;
  await input.fill('current needle');
  const match = dialog.getByRole('option').filter({ hasText: 'Matching worker' });
  await match.waitFor();
  assert.match(await match.textContent(), /Current needle <img src=x onerror=alert\(1\)>/);
  assert.equal(await match.locator('img,script').count(), 0, 'Saved message content is text, never markup');
  const oldResponse = page.waitForResponse(response => new URL(response.url()).searchParams.get('search') === 'old needle');
  releases[0]();
  await (await oldResponse).finished();
  await page.waitForTimeout(50);
  assert.equal(await match.count(), 1, 'The older result cannot replace the current query');
  assert.deepEqual(await page.locator('#session-list [data-run]').evaluateAll(rows => rows.map(row => row.dataset.run)), sidebar, 'Palette searches do not filter the sidebar');
  await match.click();
  await dialog.waitFor({ state: 'detached' });
  await page.waitForFunction(id => location.hash === '#run=' + id, worker);
  await page.locator('#content h1').waitFor();
  await page.locator('#search-sessions').click();
  const closedRequest = waitForQuery('closed needle');
  await input.fill('closed needle');
  await closedRequest;
  await input.press('Escape');
  await dialog.waitFor({ state: 'detached' });
  const closedResponse = page.waitForResponse(response => new URL(response.url()).searchParams.get('search') === 'closed needle');
  releases[1]();
  await (await closedResponse).finished();
  await page.locator('#search-sessions').click();
  assert.equal(await input.inputValue(), '');
  assert.equal(await dialog.getByRole('option').filter({ hasText: 'closed needle' }).count(), 0);
  await input.fill('retry needle');
  await dialog.getByRole('button', { name: 'Retry search', exact: true }).click();
  await dialog.getByRole('option').filter({ hasText: 'retry needle' }).waitFor();
  assert.equal(retries, 2);
  await input.fill('absent needle');
  await dialog.getByRole('button', { name: 'Clear search', exact: true }).click();
  assert.equal(await input.inputValue(), '');
  assert.equal(await input.evaluate(el => el === document.activeElement), true);
});

test('palette invalidates admin search results on a real session demotion and uses the member scope', async t => {
  const page = await pageFor(t);
  await choose(page, page.locator('#session-scope'), 'all');
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  t.after(() => release());
  const scopes = [];
  await page.route('**/api/runs?*', async route => {
    const params = new URL(route.request().url()).searchParams, query = params.get('search');
    if (!query) return route.continue();
    scopes.push(params.get('scope'));
    if (query === 'admin needle') await gate;
    return route.fulfill({ json: [{ id: 'c'.repeat(32), display_title: query, search_query: query, search_match: true, status: 'idle', children: [] }] });
  });
  await page.locator('#search-sessions').click();
  const started = page.waitForRequest(request => new URL(request.url()).searchParams.get('search') === 'admin needle');
  await page.locator('#command-search').fill('admin needle');
  await started;
  await page.evaluate(() => applyUserSession({ authenticated: true, local: true, role: 'member', user_id: state.userId, csrf: state.csrf, identity: state.identity }));
  await page.getByRole('dialog').waitFor({ state: 'detached' });
  const response = page.waitForResponse(response => new URL(response.url()).searchParams.get('search') === 'admin needle');
  release();
  await (await response).finished();
  await page.locator('#search-sessions').click();
  await page.locator('#command-search').fill('member needle');
  await page.getByRole('option').filter({ hasText: 'member needle' }).waitFor();
  assert.equal(await page.getByRole('option').filter({ hasText: 'admin needle' }).count(), 0);
  assert.deepEqual(scopes, ['all', 'mine']);
  assert.equal(await page.locator('#session-scope').isDisabled(), true);
});

test('palette hands off to the existing folder dialog and new-session shortcuts respect modal ownership and authentication', async t => {
  const page = await pageFor(t);
  await page.locator('#command-menu').click();
  await page.getByRole('option', { name: /^New folder/ }).click();
  const folder = page.getByRole('dialog', { name: 'New folder', exact: true });
  await folder.waitFor();
  await page.waitForFunction(() => document.activeElement?.id === 'session-folder-name');
  await page.keyboard.press('Meta+k');
  await page.keyboard.press('Meta+Shift+o');
  assert.equal(await page.getByRole('dialog').count(), 1);
  assert.equal(await folder.isVisible(), true, 'Workspace shortcuts do not replace an active editor');
  await page.keyboard.press('Escape');
  await folder.waitFor({ state: 'detached' });
  for (const shortcut of ['Meta+Shift+o', 'Control+Shift+o']) {
    await page.evaluate(() => navigate('settings'));
    await page.keyboard.press('Meta+k');
    await page.locator('#command-search').waitFor();
    await page.keyboard.press(shortcut);
    await page.getByRole('dialog').waitFor({ state: 'detached' });
    await page.locator('#prompt').waitFor();
    await page.waitForFunction(() => document.activeElement?.id === 'prompt');
    assert.equal(new URL(page.url()).hash, '#tasks');
  }
  await page.evaluate(() => { applyUserSession({ authenticated: false, role: 'member', user_id: '', csrf: '' }); });
  const version = await page.evaluate(() => state.pageVersion);
  await page.keyboard.press('Meta+k');
  await page.keyboard.press('Control+Shift+o');
  assert.equal(await page.getByRole('dialog').count(), 0);
  assert.equal(await page.evaluate(() => state.pageVersion), version, 'Signed-out shortcuts do not navigate');
});
