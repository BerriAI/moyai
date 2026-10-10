const assert = require('node:assert/strict');
const {test} = require('node:test');
const {readFileSync} = require('node:fs');
const vm = require('./helpers/ui-vm.cjs');
const registry = JSON.parse(readFileSync('frontend/config/settings-navigation.json', 'utf8'));

function setup({role = 'admin'} = {}) {
  const elements = new Map();
  const context = {
    MoyaiConfig: {settingsGroups: registry},
    state: {pageVersion: 1, role: 'admin', runsRefresh: 0},
    document: {activeElement:null,body:{}},
    restoreSessionScope:()=>{},renderSidebar:()=>{},
    $: key => {if (!elements.has(key)) elements.set(key, {}); return elements.get(key);},
    api: async () => ({authenticated: true, role, csrf: 'fresh', user_id: 'test-user'}),
  };
  vm.createContext(context);
  vm.runInContext(readFileSync('app/static/users.js', 'utf8'), context);
  vm.runInContext(readFileSync('app/static/settings.js', 'utf8'), context);
  return {context, elements};
}

test('Settings routes come from the shared registry, plus old Usage bookmarks', () => {
  const {context} = setup();
  const views = vm.runInContext('[...settingsViews]', context);
  for (const group of registry) for (const item of group.items) assert.ok(views.includes(item.view), item.view);
  assert.ok(views.includes('adoption') && views.includes('settings'));
  assert.ok(!registry.some(group => group.items.some(item => item.view === 'adoption')));
});

test('settings navigation selects exactly one destination and respects member access', () => {
  const {context} = setup();
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

test('background refresh defers while a dialog, portaled select or page control is active', () => {
  const {context} = setup();
  let modal = null, menu = null, contains = true, interactive = true;
  context.document = {
    querySelector:key=>key==='[data-slot="dialog-content"][data-state="open"]'?modal:key==='[data-slot="select-trigger"][data-state="open"]'?menu:{contains:()=>contains},
    activeElement:{matches:()=>interactive},
  };
  assert.equal(context.settingsInteractionActive(),true);
  interactive = false;
  assert.equal(context.settingsInteractionActive(),false);
  contains = false; interactive = true;
  assert.equal(context.settingsInteractionActive(),false);
  modal = {};
  assert.equal(context.settingsInteractionActive(),true);
  modal = null; menu = {};
  assert.equal(context.settingsInteractionActive(),true);
  menu = null;
  assert.equal(context.settingsInteractionActive(),false);
});
