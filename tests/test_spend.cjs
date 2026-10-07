const assert = require('node:assert/strict');
const {test} = require('node:test');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

function setup(scope = 'personal') {
  const elements = new Map(), calls = [];
  const total = {spend:'1.25', requests:2, sessions:1, pending_costs:0, missing_costs:0, total_tokens:120, prompt_tokens:100, completion_tokens:20};
  const user = {id:'google:maya', kind:'google', email:'maya@example.com', name:'Maya', ...total};
  const data = {scope, start:'2026-10-01', end:'2026-10-07', total, priced_requests:2,
    identities:[user], users:[user], sessions:[{run_id:'session-1', user_id:user.id, user_name:user.name, title:'Fix dashboard', ...total}],
    models:[{model:'test-model', ...total}], request_details:[], tracked_since:null};
  if (scope === 'organization') data.infrastructure = {pending:false};
  const context = {
    state:{pageVersion:1, role:'admin'}, URLSearchParams, clearTimeout, setTimeout,
    $:key=>{if (!elements.has(key)) elements.set(key, {}); return elements.get(key);},
    api:async url=>{
      calls.push(url);
      if (url.startsWith('/api/spend?')) return data;
      if (scope === 'organization' && url === '/api/admin/identities/status') return {enabled:false, ready:false, missing_scopes:[]};
      throw new Error('Unexpected request: ' + url);
    },
    esc:value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),
    modelName:value=>value,
    renderCostSummary:()=>{assert.equal(scope,'organization'); return '<div>Organization totals</div>';},
    renderInfrastructure:()=>{assert.equal(scope,'organization'); return '<div>Infrastructure costs</div>';},
    bindInfrastructure:()=>{assert.equal(scope,'organization');},
    document:{querySelectorAll:()=>[]},
    showError:error=>{throw error;}, toast:()=>{},
  };
  vm.createContext(context);
  vm.runInContext(readFileSync('app/static/spend.js','utf8'), context);
  return {context, elements, calls, data};
}

test('personal Spend uses server scope even with a stale admin role and user filter', async () => {
  const {context, elements, calls} = setup();
  vm.runInContext("spendState.user='someone-else'", context);
  await context.renderSpend();
  const html = elements.get('#content').innerHTML;
  assert.deepEqual(calls, ['/api/spend?']);
  for (const text of ['Your LLM spend', '$1.25', 'Your requests', 'Your tokens', 'Your sessions', 'Fix dashboard', 'Your models']) {
    assert.ok(html.includes(text), text);
  }
  assert.doesNotMatch(html, /LLM spend by user|spend-user|Slack identities|ORGANIZATION \/ ADMIN|Organization totals|Infrastructure costs|<th>User<\/th>/);
  assert.equal(vm.runInContext('spendState.user', context), '');
});

test('personal dates and refresh request only the authenticated report', async () => {
  const {context, elements, calls} = setup();
  await context.renderSpend();
  context.$('#spend-start').value = '2026-10-02';
  context.$('#spend-end').value = '2026-10-04';
  elements.get('#spend-filter-form').onsubmit({preventDefault(){}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls[1], '/api/spend?start=2026-10-02&end=2026-10-04');
  await elements.get('#sync-spend').onclick();
  assert.equal(calls.length, 3);
  assert.ok(calls.every(url=>url.startsWith('/api/spend?')));
});

test('admin Spend retains organization, infrastructure, user filters and identity controls', async () => {
  const {context, elements, calls} = setup('organization');
  context.state.role = 'member';
  await context.renderSpend();
  assert.deepEqual(calls, ['/api/spend?', '/api/admin/identities/status']);
  const html = elements.get('#content').innerHTML;
  for (const text of ['Usage & spend', 'Organization totals', 'Infrastructure costs', 'LLM spend by user', 'All users', 'Slack identities']) {
    assert.ok(html.includes(text), text);
  }
  assert.equal(typeof elements.get('#spend-user').onchange, 'function');
  assert.equal(typeof elements.get('#refresh-identities').onclick, 'function');
});

test('personal empty state and unpriced requests are clear', async () => {
  const {context, elements, data} = setup();
  data.total = {spend:'0', requests:0, sessions:0};
  data.priced_requests = 0;
  data.sessions = data.models = [];
  await context.renderSpend();
  assert.match(elements.get('#content').innerHTML, /No model requests attributed to you in this period/);
  assert.match(elements.get('#content').innerHTML, /Ready to track usage/);
  data.total = {spend:'0', requests:2, pending_costs:1, missing_costs:1};
  await context.renderSpend();
  assert.match(elements.get('#content').innerHTML, /1 requests are in progress; 1 returned no final cost/);
  assert.match(elements.get('#content').innerHTML, /Missing costs are not treated as free/);
});

test('spend loading clears old totals and delayed responses cannot replace a different page', async () => {
  const {context, elements, data} = setup();
  context.$('#content').innerHTML = 'Old organization totals';
  let finish;
  context.api = ()=>new Promise(resolve=>{finish=resolve;});
  const render = context.renderSpend();
  assert.equal(elements.get('#content').innerHTML, '<p class="subtext" role="status">Loading spend…</p>');
  context.state.pageVersion++;
  elements.get('#content').innerHTML = 'Another page';
  finish(data);
  await render;
  assert.equal(elements.get('#content').innerHTML, 'Another page');
});

test('failed date requests keep editable filters and allow retry', async () => {
  const {context, elements, data} = setup();
  context.api = async ()=>{throw new Error('Choose a date range of up to 93 days.');};
  await context.renderSpend();
  assert.match(elements.get('#content').innerHTML, /role="alert".*Choose a date range/);
  assert.doesNotMatch(elements.get('#content').innerHTML, /Loading spend/);
  assert.equal(typeof elements.get('#spend-filter-form').onsubmit, 'function');
  context.api = async ()=>data;
  await elements.get('#sync-spend').onclick();
  assert.match(elements.get('#content').innerHTML, /Your LLM spend/);
});

test('an older report cannot overwrite a newer refresh on the same page', async () => {
  const {context, elements, data} = setup();
  let finish;
  context.api = ()=>new Promise(resolve=>{finish=resolve;});
  const oldRender = context.renderSpend();
  context.api = async ()=>data;
  await context.renderSpend();
  const current = elements.get('#content').innerHTML;
  finish({...data, total:{...data.total, spend:'9999'}});
  await oldRender;
  assert.equal(elements.get('#content').innerHTML, current);
});
