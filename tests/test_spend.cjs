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
    state:{pageVersion:1, role:'admin',view:'spend'}, URLSearchParams, clearTimeout, setTimeout,
    $:key=>{if (!elements.has(key)) elements.set(key, {focus(){}}); return elements.get(key);},
    api:async url=>{
      calls.push(url);
      if (url.startsWith('/api/spend?')) return data;
      if (scope === 'organization' && url === '/api/admin/identities/status') return {enabled:false, ready:false, missing_scopes:[]};
      throw new Error('Unexpected request: ' + url);
    },
    esc:value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),
    modelName:value=>value, summaryDollars:value=>'$'+Number(value).toFixed(2),
    renderCostSummary:()=>{assert.equal(scope,'organization'); return '<div>Organization totals</div>';},
    renderInfrastructure:()=>{assert.equal(scope,'organization'); return '<div>Infrastructure costs</div>';},
    bindInfrastructure:()=>{assert.equal(scope,'organization');},
    document:{querySelectorAll:()=>[]},
    showError:error=>{throw error;}, toast:()=>{},
  };
  vm.createContext(context);
  for(const file of ['analytics','adoption','spend-analytics','spend'])vm.runInContext(readFileSync('app/static/'+file+'.js','utf8'), context);
  const tabs=['overall','users','history','activity','infrastructure'].map(key=>Object.assign(context.$('#spend-tab-'+key),{id:'spend-tab-'+key,dataset:{spendTab:key}}));
  context.document.querySelectorAll=selector=>selector==='[data-spend-tab]'?tabs:[];
  return {context, elements, calls, data};
}

test('personal Spend uses server scope even with a stale admin role and user filter', async () => {
  const {context, elements, calls} = setup();
  vm.runInContext("spendState.user='someone-else';spendAnalyticsState.tab='activity'", context);
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
  for (const text of ['Spend &amp;', 'Organization totals', 'Usage history']) {
    assert.ok(html.includes(text) || html.includes(text.replace('&amp;', '&')), text);
  }
  vm.runInContext("spendAnalyticsState.tab='users'", context);
  await context.renderSpend();
  assert.match(elements.get('#content').innerHTML, /LLM spend by user/);
  assert.equal(typeof elements.get('#spend-user').onchange, 'function');
  vm.runInContext("spendAnalyticsState.tab='infrastructure'", context);
  await context.renderSpend();
  assert.match(elements.get('#content').innerHTML, /Infrastructure costs/);
  assert.match(elements.get('#content').innerHTML, /Slack identities/);
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

const activity={start:'2026-10-01',end:'2026-10-07',total_requests:7,active_users:1,daily:[{date:'2026-10-07',requests:7,seven_day_average:1,active_users:1,partial:true}],weekly:{requests:0,previous_requests:0,percent_change:null,delta:0,start:'2026-09-30',end:'2026-10-06',previous_start:'2026-09-23',previous_end:'2026-09-29'}};
function withActivity(){
  const h=setup('organization'),spendAPI=h.context.api;
  h.context.api=async url=>{
    if(url.startsWith('/api/admin/adoption?')){h.calls.push(url);const query=new URLSearchParams(url.split('?')[1]);return {...activity,start:query.get('start'),end:query.get('end')};}
    const value=await spendAPI(url);
    if(url.startsWith('/api/spend?')){const query=new URLSearchParams(url.split('?')[1]);return {...value,start:query.get('start')||value.start,end:query.get('end')||value.end};}
    return value;
  };
  return h;
}
test('human activity shares navigation, date controls, refresh and current-tab export',async()=>{
  const {context:c,elements,calls}=withActivity();
  await c.renderSpend();
  await elements.get('#spend-tab-activity').onclick();
  assert.equal(c.state.view,'spend');
  assert.match(elements.get('#content').innerHTML,/id="spend-tab-activity"[^>]*aria-pressed="true"/);
  assert.match(elements.get('#spend-panel').innerHTML,/Human requests/);
  assert.equal(calls.at(-1),'/api/admin/adoption?start=2026-10-01&end=2026-10-07');
  c.$('#spend-start').value='2026-10-02';c.$('#spend-end').value='2026-10-04';
  elements.get('#spend-filter-form').onsubmit({preventDefault(){}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls.at(-1),'/api/admin/adoption?start=2026-10-02&end=2026-10-04');
  await elements.get('#sync-spend').onclick();
  assert.equal(calls.at(-1),'/api/admin/adoption?start=2026-10-02&end=2026-10-04');
  let exported;c.downloadAnalyticsCSV=(name,rows)=>{exported={name,rows};};
  elements.get('#spend-export').onclick();
  assert.equal(exported.name,'moyai-human-activity-2026-10-02-2026-10-04.csv');
  assert.equal(exported.rows[0][1],'Human requests');
  await elements.get('#spend-tab-users').onclick();
  assert.match(elements.get('#content').innerHTML,/LLM spend by user/);
  elements.get('#spend-export').onclick();
  assert.equal(exported.rows[0][0],'User');
});
test('human activity errors retain tabs and dates and retry without leaving spend',async()=>{
  const {context:c,elements}=withActivity();await c.renderSpend();
  const api=c.api;c.api=async url=>{if(url.startsWith('/api/admin/adoption?'))throw Error('Activity unavailable <retry>');return api(url);};
  await elements.get('#spend-tab-activity').onclick();
  assert.match(elements.get('#spend-panel').innerHTML,/Activity unavailable &lt;retry>/);
  assert.match(elements.get('#content').innerHTML,/id="spend-export" disabled/);
  assert.equal(typeof elements.get('#spend-filter-form').onsubmit,'function');
  c.api=api;await elements.get('#activity-retry').onclick();
  assert.match(elements.get('#spend-panel').innerHTML,/Human requests/);
  assert.equal(elements.get('#spend-export').disabled,false);
});
test('delayed human activity cannot overwrite another tab, refreshed date range, or page',async()=>{
  for(const destination of ['tab','refresh','page']){
    const {context:c,elements}=withActivity();await c.renderSpend();
    let finish;const api=c.api;c.api=url=>url.startsWith('/api/admin/adoption?')?new Promise(resolve=>{finish=resolve;}):api(url);
    const old=elements.get('#spend-tab-activity').onclick();
    if(destination==='tab')await elements.get('#spend-tab-users').onclick();
    if(destination==='refresh'){c.api=api;await c.renderSpend();}
    if(destination==='page')c.state.pageVersion++;
    c.$('#spend-panel').innerHTML='Current content';
    const exportHandler=elements.get('#spend-export').onclick;
    finish(activity);await old;
    assert.equal(elements.get('#spend-panel').innerHTML,'Current content',destination);
    assert.equal(elements.get('#spend-export').onclick,exportHandler,destination);
  }
});
test('legacy adoption links open the human activity tab with one canonical spend destination',async()=>{
  for(const scope of ['organization','personal']){
    const {context:c,elements}=scope==='organization'?withActivity():setup();
    c.state.role=scope==='organization'?'admin':'member';
    const routes=[];c.stopStream=()=>{};c.setView=(view,title)=>routes.push({view,title});c.updateSettingsNavigation=()=>{};
    c.history={replaceState:(_state,_title,url)=>routes.push({url})};
    vm.runInContext(readFileSync('app/static/settings.js','utf8'),c);
    const app=readFileSync('app/static/app.js','utf8');
    vm.runInContext(app.slice(app.indexOf('async function navigate('),app.indexOf('async function refreshRuns(')),c);
    await c.navigate('adoption');
    assert.equal(c.state.view,'spend');
    assert.equal(routes[0].title,scope==='organization'?'Spend & usage':'Spend');
    assert.equal(routes[1].url,'#spend');
    assert.match(scope==='organization'?elements.get('#spend-panel').innerHTML:elements.get('#content').innerHTML,scope==='organization'?/Human requests/:/Your LLM spend/);
  }
});
