const assert = require('node:assert/strict');
const {test} = require('node:test');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

function setup({automations = false, role = 'admin'} = {}) {
  const elements = new Map();
  const context = {
    state: {pageVersion: 1, role: 'admin', runsRefresh: 0},
    restoreSessionScope:()=>{},renderSidebar:()=>{},
    $: key => {if (!elements.has(key)) elements.set(key, {}); return elements.get(key);},
    api: async () => ({authenticated: true, role, csrf: 'fresh', user_id: 'test-user'}),
  };
  if (automations) context.renderAutomations = async () => {};
  vm.createContext(context);
  vm.runInContext(readFileSync('app/static/users.js', 'utf8'), context);
  vm.runInContext(readFileSync('app/static/settings.js', 'utf8'), context);
  return {context, elements};
}

test('standalone Settings has working sections without depending on automations', async () => {
  const {context, elements} = setup();
  await context.renderSettings();
  const html = elements.get('#content').innerHTML;
  for (const view of ['skills', 'connections', 'secrets', 'runtime', 'environments', 'users', 'spend', 'adoption']) {
    assert.match(html, new RegExp(`href="#${view}"`));
  }
  assert.doesNotMatch(html, /href="#automations"/);
  assert.equal(vm.runInContext("settingsViews.has('automations')", context), false);
});

test('every Settings card renders a non-empty decorative icon, including Adoption', async () => {
  const {context, elements} = setup({automations: true});
  await context.renderSettings();
  const html = elements.get('#content').innerHTML;
  const views = vm.runInContext('settingsGroups.flatMap(group => group.items.map(item => item.view))', context);
  assert.ok(views.includes('adoption'));
  for (const view of views) {
    const card = html.match(new RegExp(`<a class="settings-link" href="#${view}">([\\s\\S]*?)</a>`));
    assert.ok(card, `${view} card should be rendered`);
    assert.match(card[1], /<svg[^>]*aria-hidden="true"[^>]*>\s*<(?:path|circle|rect)\b/, `${view} icon should contain geometry`);
    assert.match(card[1], /viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"/);
  }
});

test('an installed automation page is included in Settings and direct-link routing', async () => {
  const {context, elements} = setup({automations: true});
  await context.renderSettings();
  assert.match(elements.get('#content').innerHTML, /href="#automations"/);
  assert.equal(vm.runInContext("settingsViews.has('automations')", context), true);
});

test('Settings refreshes access and hides administrative sections for members', async () => {
  const {context, elements} = setup({role: 'member'});
  await context.renderSettings();
  const html = elements.get('#content').innerHTML;
  assert.equal(context.state.role, 'member');
  assert.match(html, /href="#connections"/);
  assert.doesNotMatch(html, /href="#(?:users|spend|adoption|environments)"|settings-administration/);
});

test('a delayed Settings access check cannot overwrite a different page', async () => {
  const {context, elements} = setup();
  context.api = async () => {
    context.state.pageVersion++;
    elements.get('#content').innerHTML = 'The next page';
    return {authenticated: true, role: 'admin'};
  };
  await context.renderSettings();
  assert.equal(elements.get('#content').innerHTML, 'The next page');
});
