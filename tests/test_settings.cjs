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
  for (const view of ['skills', 'connections', 'secrets', 'runtime', 'environments', 'users', 'spend']) {
    assert.match(html, new RegExp(`href="#${view}"`));
  }
  assert.doesNotMatch(html, /href="#(?:automations|adoption)"/);
  assert.equal((html.match(/href="#spend"/g)||[]).length,1);
  assert.match(html, /id="settings-administration"[\s\S]*href="#spend"/);
  assert.equal(vm.runInContext("settingsViews.has('adoption')", context), true);
  assert.equal(vm.runInContext("settingsViews.has('automations')", context), false);
});

test('every Settings card renders a non-empty decorative icon, including Spend & usage', async () => {
  const {context, elements} = setup({automations: true});
  await context.renderSettings();
  const html = elements.get('#content').innerHTML;
  const views = vm.runInContext('settingsGroups.flatMap(group => group.items.map(item => item.view))', context);
  assert.ok(views.includes('spend'));
  assert.ok(!views.includes('adoption'));
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
  assert.match(html, /href="#spend"/);
  assert.match(html, /See your own LLM usage and costs/);
  assert.doesNotMatch(html, /href="#(?:users|adoption|environments)"|settings-administration/);
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

test('members can save immediate sending, reload it, and recover from a failed change', async () => {
  const {context, elements} = setup({role:'member'});
  let saved=false,fail=false;
  context.api=async(url,options)=>{
    if(url==='/api/session')return {authenticated:true,role:'member',user_id:'test-user',preferences:{send_immediately:saved}};
    if(url==='/api/settings/preferences'){
      if(fail)throw Error('Connection lost.');
      saved=JSON.parse(options.body).send_immediately;
      return {send_immediately:saved};
    }
    return {};
  };
  await context.renderSettings();
  const input=elements.get('#send-immediately');
  assert.equal(context.state.preferences.send_immediately,false);
  input.checked=true;await input.onchange();
  assert.equal(saved,true);
  assert.equal(context.state.preferences.send_immediately,true);
  assert.match(elements.get('#send-immediately-status').textContent,/Saved/);
  await context.renderSettings();
  assert.match(elements.get('#content').innerHTML,/id="send-immediately" type="checkbox" checked/);
  fail=true;input.checked=false;await input.onchange();
  assert.equal(input.checked,true);
  assert.equal(input.disabled,false);
  assert.equal(context.state.preferences.send_immediately,true);
  assert.match(elements.get('#send-immediately-status').textContent,/Could not confirm.*Connection lost/);
  fail=false;input.checked=false;await input.onchange();
  assert.equal(context.state.preferences.send_immediately,false);
});

test('delayed preference save updates the account without overwriting a newly opened page', async () => {
  const {context,elements}=setup();
  await context.renderSettings();
  context.api=async()=>{
    context.state.pageVersion++;
    elements.get('#content').innerHTML='Another page';
    return {send_immediately:true};
  };
  elements.get('#send-immediately').checked=true;
  await elements.get('#send-immediately').onchange();
  assert.equal(elements.get('#content').innerHTML,'Another page');
  assert.equal(context.state.preferences.send_immediately,true);
});

test('settings navigation selects exactly one destination and respects member access', () => {
  const {context} = setup({automations: true});
  const member = context.settingsNavigation('skills', 'member');
  assert.equal((member.match(/aria-current="page"/g) || []).length, 1);
  assert.match(member, /href="#skills" aria-current="page"/);
  assert.doesNotMatch(member, /href="#(?:users|adoption|environments)"/);
  assert.match(member, /href="#spend"/);
  const admin = context.settingsNavigation('spend', 'admin');
  assert.equal((admin.match(/href="#spend"/g)||[]).length,1);
  assert.match(admin, /<h2>Administration<\/h2>[\s\S]*href="#spend" aria-current="page"/);
  assert.doesNotMatch(admin, /href="#adoption"/);
  assert.match(member, /<h2>Workspace<\/h2>[\s\S]*href="#spend"/);
  assert.doesNotMatch(member, /Administration/);
  for (const view of ['automations','skills','memory','connections','secrets','runtime','environments','users','spend']) {
    assert.match(admin, new RegExp(`href="#${view}"`));
  }
});

test('library filters combine query and scope, survive redraw, and recover from no matches', () => {
  const {context} = setup();
  let focused = false, clear;
  const search = {value:'', focus:()=>{focused=true;}};
  const scope = {value:''};
  const count = {};
  const empty = {querySelector:()=>({addEventListener:(_,fn)=>{clear=fn;}})};
  const rows = [
    {textContent:'API key', dataset:{filter:'personal'}, onclick:()=>{}},
    {textContent:'API staging', dataset:{filter:'organization'}, onclick:()=>{}},
    {textContent:'Browser access', dataset:{filter:'personal'}, onclick:()=>{}},
  ];
  const handler = rows[0].onclick;
  const nodes = {'#query':search,'#scope':scope,'#count':count,'#empty':empty};
  context.document = {querySelector:key=>nodes[key],querySelectorAll:()=>rows};
  const options = {input:'#query',select:'#scope',rows:'.row',count:'#count',empty:'#empty'};
  context.bindSettingsFilter(options);
  search.value = ' API '; search.oninput();
  scope.value = 'personal'; scope.onchange();
  assert.deepEqual(rows.map(row=>row.hidden),[false,true,true]);
  assert.equal(count.textContent,'1 of 3');
  search.value = ''; scope.value = '';
  context.bindSettingsFilter(options);
  assert.equal(search.value,' API ');
  assert.equal(scope.value,'personal');
  search.value = 'No such credential'; search.oninput();
  assert.equal(empty.hidden,false);
  clear();
  assert.equal(count.textContent,'3 of 3');
  assert.equal(empty.hidden,true);
  assert.equal(focused,true);
  assert.equal(rows[0].onclick,handler);
});

test('background refresh defers while a dialog or page control is active', () => {
  const {context} = setup();
  let modal = null, contains = true, interactive = true;
  context.document = {
    querySelector:key=>key==='dialog[open]'?modal:{contains:()=>contains},
    activeElement:{matches:()=>interactive},
  };
  assert.equal(context.settingsInteractionActive(),true);
  interactive = false;
  assert.equal(context.settingsInteractionActive(),false);
  contains = false; interactive = true;
  assert.equal(context.settingsInteractionActive(),false);
  modal = {};
  assert.equal(context.settingsInteractionActive(),true);
});
