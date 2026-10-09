const assert = require('node:assert/strict');
const {test} = require('node:test');
const {readFileSync} = require('node:fs');
const vm = require('./helpers/ui-vm.cjs');

function setup(scope = 'personal') {
  const elements = new Map(), calls = [];
  const element=key=>{if(!elements.has(key))elements.set(key,{focus(){},setSelectionRange(){},replaceWith(){}});return elements.get(key);};
  const total = {spend:'1.25', requests:2, sessions:1, pending_costs:0, missing_costs:0, total_tokens:120, prompt_tokens:100, completion_tokens:20};
  const user = {id:'google:maya', kind:'google', email:'maya@example.com', name:'Maya', ...total};
  const data = {scope, start:'2026-10-01', end:'2026-10-07', total, priced_requests:2,
    identities:[user], users:[user], sessions:[{run_id:'session-1', user_id:user.id, user_name:user.name, title:'Fix dashboard', ...total}],
    models:[{model:'test-model', ...total}], request_details:[], tracked_since:null};
  if (scope === 'organization') data.infrastructure = {pending:false};
  const prData=prFixture();
  const context = {
    state:{pageVersion:1, role:'admin',view:'spend'}, URL, URLSearchParams, clearTimeout, setTimeout, CSS:{escape:value=>value},
    $:element,
    api:async url=>{
      calls.push(url);
      if (url.startsWith('/api/spend?')) {const query=new URL(url,'http://localhost').searchParams;return {...data,start:query.get('start')||data.start,end:query.get('end')||data.end};}
      if (url.startsWith('/api/admin/pull-requests?')) return prData;
      if (scope === 'organization' && url.startsWith('/api/admin/adoption?')) return activity;
      if (scope === 'organization' && url === '/api/admin/identities/status') return {enabled:false, ready:false, missing_scopes:[]};
      throw new Error('Unexpected request: ' + url);
    },
    esc:value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),
    modelName:value=>value, summaryDollars:value=>'$'+Number(value).toFixed(2),
    renderCostSummary:()=>{assert.equal(scope,'organization'); return '<div>Organization totals</div>';},
    renderInfrastructure:()=>{assert.equal(scope,'organization'); return '<div>Infrastructure costs</div>';},
    bindInfrastructure:()=>{assert.equal(scope,'organization');},
    document:{querySelectorAll:selector=>selector==='[data-spend-tab]'?['overall','users','history','prs','leaderboard','infrastructure'].map(tab=>Object.assign(element('#spend-tab-'+tab),{id:'spend-tab-'+tab,dataset:{spendTab:tab}})):selector==='[data-pr-contributor]'?prData.leaderboard.map(row=>Object.assign(element('#person-'+row.user_id),{dataset:{prContributor:row.user_id}})):[]},
    showError:error=>{throw error;}, toast:()=>{},
  };
  vm.createContext(context);
  for(const file of ['analytics','adoption','spend-prs','spend-analytics','spend'])vm.runInContext(readFileSync('app/static/'+file+'.js','utf8'), context);
  return {context, elements, calls, data,prData};
}

function fakeTimers(c){
  const timers=new Map();let id=0;
  c.setTimeout=(fn,delay)=>{timers.set(++id,{fn,delay});return id;};
  c.clearTimeout=id=>timers.delete(id);
  return {timers,async tick(delay){
    const entry=[...timers].find(([,timer])=>timer.delay===delay);
    assert.ok(entry,'expected a scheduled '+delay+'ms poll');
    timers.delete(entry[0]);entry[1].fn();await settle();
  }};
}

// Replacing innerHTML destroys browser handlers. The small DOM fake normally
// reuses its element objects, so explicitly model destruction in these tests.
function destroyHandlersOnPageReplacement(e){
  const root=e.get('#content');let html=root.innerHTML;
  Object.defineProperty(root,'innerHTML',{get:()=>html,set:value=>{
    html=value;
    for(const el of e.values())for(const key of ['onclick','oninput','onchange','onsubmit'])delete el[key];
  }});
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
  assert.deepEqual(calls, ['/api/spend?'],'unrelated account settings do not block the overview');
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
  assert.match(elements.get('#spend-identities').innerHTML, /Slack identities/);
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
  assert.match(elements.get('#content').innerHTML, /1 costs await confirmation; 1 costs are unavailable/);
  assert.match(elements.get('#content').innerHTML, /Missing costs are not treated as free/);
  vm.runInContext('clearTimeout(spendState.timer)',context);
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

function prFixture(){
  const pr={repository_id:42,number:123,url:'https://github.com/example/moyai/pull/123',title:'Keep saved filters',state:'merged',draft:false,created_at:'2026-10-01T09:00:00Z',tracked_at:'2026-10-02T10:00:00Z',merged_at:'2026-10-05T10:00:00Z',user_id:'google:maya',user_name:'Maya',user_email:'maya@example.com',sessions:[{id:'session-1',title:'Fix dashboard',deleted:false},{id:'deleted-1',title:'Earlier attempt',deleted:true}],spend:'1.250001',requests:4,pending_costs:1,missing_costs:1,stale:false};
  return {start:'2026-10-01',end:'2026-10-07',timezone:'UTC',currency:'USD',pull_requests:[pr],created_pull_requests:[pr],total_created:1,unknown_created_at:0,merged_pull_requests:[pr],leaderboard:[{user_id:'google:maya',name:'Maya',email:'maya@example.com',created_prs:1,status_counts:{merged:1,open:0,draft:0,closed:0,unknown:0},merged_prs:1,cost_per_merged_pr:pr.spend,sessions:2,spend:pr.spend,requests:4,pending_costs:1,missing_costs:1}],total_merged:1,contributors:1,unknown_status:1,stale_status:1,pending_refresh:false};
}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const selectTab=(elements,tab)=>elements.get('#spend-tab-'+tab).onclick();

test('PR reports load lazily and share one scoped response between tabs',async()=>{
  const {context,elements,calls}=setup('organization');
  await context.renderSpend();
  assert.ok(!calls.some(url=>url.includes('pull-requests')));
  selectTab(elements,'prs');
  assert.match(elements.get('#content').innerHTML,/Loading pull request analytics/);
  assert.equal(elements.get('#spend-export').disabled,true);
  await settle();
  const html=elements.get('#spend-panel').innerHTML;
  for(const text of ['Keep saved filters','First tracked in the selected dates','All time · USD','1 pending · 1 missing','Across all tracked PRs: 1 with unknown status','PR costs overlap'])assert.ok(html.includes(text),text);
  assert.match(html,/href="#run=session-1"/);
  assert.match(html,/Earlier attempt · Deleted/);
  assert.doesNotMatch(html,/href="#run=deleted-1"/);
  assert.equal(elements.get('#spend-export').disabled,false);
  selectTab(elements,'leaderboard');
  await settle();
  assert.match(elements.get('#content').innerHTML,/PR leaderboard/);
  assert.match(elements.get('#content').innerHTML,/Merged 1/);
  assert.equal(calls.filter(url=>url.includes('pull-requests')).length,1);
});

test('PR filters clear an empty result and exports keep exact full report values',async()=>{
  const {context,elements,prData}=setup('organization');
  prData.pull_requests=Array.from({length:520},(_,i)=>({...prData.pull_requests[0],number:i+1,url:'https://github.com/example/moyai/pull/'+(i+1),title:i===0?'=Unsafe CSV label':'Change '+i}));
  await context.renderSpend();selectTab(elements,'prs');await settle();
  let exported;
  context.downloadAnalyticsCSV=(filename,rows)=>{exported={filename,rows};};
  elements.get('#spend-export').onclick();
  assert.equal(exported.rows.length,521);
  assert.equal(exported.rows[1][11],'1.250001');
  assert.match(context.analyticsCSV(exported.rows),/"'=Unsafe CSV label"/);
  assert.match(exported.filename,/moyai-prs-2026-10-01-2026-10-07.csv/);
  elements.get('#spend-pr-search').value='missing phrase';
  elements.get('#spend-pr-search').oninput();
  assert.match(elements.get('#spend-panel').innerHTML,/No pull requests match these filters/);
  elements.get('#spend-pr-clear').onclick();
  assert.match(elements.get('#spend-panel').innerHTML,/Change 519/);
  elements.get('#spend-pr-status').value='open';
  elements.get('#spend-pr-status').onchange();
  assert.match(elements.get('#spend-panel').innerHTML,/No pull requests match these filters/);
  selectTab(elements,'leaderboard');await settle();
  elements.get('#spend-export').onclick();
  assert.equal(exported.rows.length,2);
  assert.equal(exported.rows[1][exported.rows[0].indexOf('Linked session LLM spend (all time, merged PRs) USD')],'1.250001');
  assert.equal(exported.rows[1][exported.rows[0].indexOf('Cost per merged PR USD (linked spend / merged PRs)')],'1.250001');
});

test('PR links allow only canonical GitHub PR destinations and escape text',async()=>{
  const {context,elements,prData}=setup('organization');
  prData.pull_requests=[{...prData.pull_requests[0],title:'<img src=x>',url:'javascript:alert(1)',spend:null},{...prData.pull_requests[0],url:'https://github.com@example.net/a/b/pull/123',title:'Wrong host'}];
  await context.renderSpend();selectTab(elements,'prs');await settle();
  const html=elements.get('#spend-panel').innerHTML;
  assert.match(html,/&lt;img src=x>/);
  assert.doesNotMatch(html,/javascript:|example.net|<img/);
  assert.match(html,/Unavailable/);
  for(const url of ['https://github.com/a/b/pull/123?next=evil','https://github.com/a/b/pull/124','http://github.com/a/b/pull/123','https://github.com/a/b/pull/123#x'])assert.equal(context.spendPRURL({url,number:123}),'');
  assert.equal(context.spendPRURL({url:'https://github.com/a/b/pull/123',number:123}),'https://github.com/a/b/pull/123');
});

test('shared dates and refresh reload the selected PR report',async()=>{
  const {context,elements,calls}=setup('organization');
  await context.renderSpend();selectTab(elements,'leaderboard');await settle();
  context.$('#spend-start').value='2026-10-03';context.$('#spend-end').value='2026-10-06';
  elements.get('#spend-filter-form').onsubmit({preventDefault(){}});await settle();
  assert.equal(calls.filter(url=>url.includes('pull-requests')).at(-1),'/api/admin/pull-requests?start=2026-10-03&end=2026-10-06');
  await elements.get('#sync-spend').onclick();await settle();
  assert.equal(calls.filter(url=>url.includes('pull-requests')).length,3);
  assert.match(elements.get('#spend-panel').innerHTML,/PR leaderboard/);
});

test('late PR responses cannot repaint another tab or a newer date range',async()=>{
  const {context,elements,prData}=setup('organization'),original=context.api;
  const pending=[];
  context.api=url=>url.includes('pull-requests')?new Promise(resolve=>pending.push(resolve)):original(url);
  await context.renderSpend();selectTab(elements,'prs');await settle();
  selectTab(elements,'users');
  pending[0](prData);await settle();
  assert.match(elements.get('#content').innerHTML,/LLM spend by user/);
  assert.equal(context.$('#spend-panel').innerHTML,undefined);
  selectTab(elements,'prs');await settle();
  await context.renderSpend();await settle();
  vm.runInContext("spendState.start='2026-10-03'",context);
  await context.renderSpend();await settle();
  pending[2]({...prData,pull_requests:[{...prData.pull_requests[0],title:'New range'}]});await settle();
  pending[1](prData);await settle();
  assert.match(elements.get('#spend-panel').innerHTML,/New range/);
  assert.doesNotMatch(elements.get('#spend-panel').innerHTML,/Keep saved filters/);
});

test('pending PR requests are shared after switching report tabs and blocked after navigation',async()=>{
  const {context,elements,prData}=setup('organization'),original=context.api;
  let finish;
  context.api=url=>url.includes('pull-requests')?new Promise(resolve=>{finish=resolve;}):original(url);
  await context.renderSpend();selectTab(elements,'prs');selectTab(elements,'leaderboard');
  finish(prData);await settle();
  assert.match(elements.get('#spend-panel').innerHTML,/PR leaderboard/);
  await context.renderSpend();
  context.state.pageVersion++;
  elements.get('#spend-panel').innerHTML='Another page';
  finish(prData);await settle();
  assert.equal(elements.get('#spend-panel').innerHTML,'Another page');
});

test('PR outage is isolated, retry succeeds, and personal scope never requests PR analytics',async()=>{
  const {context,elements,prData,data,calls}=setup('organization'),original=context.api;
  context.api=url=>url.includes('pull-requests')?Promise.reject(new Error('<unavailable>')):original(url);
  await context.renderSpend();selectTab(elements,'prs');await settle();
  assert.match(elements.get('#spend-panel').innerHTML,/Could not load pull request analytics.*&lt;unavailable>/);
  assert.equal(elements.get('#spend-export').disabled,true);
  context.api=original;
  await elements.get('#spend-pr-retry').onclick();
  assert.match(elements.get('#spend-panel').innerHTML,/Keep saved filters/);
  prData.pull_requests=[];prData.leaderboard=[];prData.total_merged=0;prData.contributors=0;
  await context.renderSpend();await settle();
  assert.match(elements.get('#spend-panel').innerHTML,/No pull requests were first tracked/);
  selectTab(elements,'leaderboard');await settle();
  assert.match(elements.get('#content').innerHTML,/No verified PRs created or merged/);
  const before=calls.filter(url=>url.includes('pull-requests')).length;
  data.scope='personal';await context.renderSpend();
  assert.equal(calls.filter(url=>url.includes('pull-requests')).length,before);
  assert.doesNotMatch(elements.get('#content').innerHTML,/Leaderboard|Pull requests/);
});

test('unattributed merged PRs retain their outcome count without a contributor rank',async()=>{
  const {context,elements,prData}=setup('organization');
  prData.leaderboard.unshift({...prData.leaderboard[0],user_id:'unattributed',name:'Unattributed',email:'',merged_prs:2});
  prData.total_merged=3;
  await context.renderSpend();selectTab(elements,'leaderboard');await settle();
  const html=elements.get('#spend-panel').innerHTML;
  assert.match(html,/analytics-pr-rank">—<.*?Unattributed/);
  assert.match(html,/analytics-pr-rank">1<.*?Maya/);
  assert.match(html,/Unattributed.*?<td><strong>2<\/strong><\/td>/);
});

test('leaderboard contributor controls expose only that contributor’s merged PR links',async()=>{
  const {context,elements,prData}=setup('organization');
  prData.merged_pull_requests.push({...prData.merged_pull_requests[0],title:'Another contributor PR',user_id:'someone-else'});
  await context.renderSpend();selectTab(elements,'leaderboard');await settle();
  elements.get('#person-google:maya').onclick();
  assert.match(elements.get('#spend-panel').innerHTML,/Pull requests · Maya/);
  assert.match(elements.get('#spend-panel').innerHTML,/Keep saved filters/);
  assert.doesNotMatch(elements.get('#spend-panel').innerHTML,/Another contributor PR/);
  elements.get('#spend-pr-close-contributor').onclick();
  assert.doesNotMatch(elements.get('#spend-panel').innerHTML,/Pull requests · Maya/);
});

test('status polling shares an in-flight refresh across tabs and releases export after completion',async()=>{
  const {context,elements,prData}=setup('organization'),original=context.api,timers=new Map();
  let timerId=0,finish,requests=0;
  context.setTimeout=fn=>{timers.set(++timerId,fn);return timerId;};context.clearTimeout=id=>timers.delete(id);
  prData.pending_refresh=true;
  context.api=url=>{if(!url.includes('pull-requests'))return original(url);requests++;return requests===1?Promise.resolve(prData):new Promise(resolve=>{finish=resolve;});};
  await context.renderSpend();selectTab(elements,'prs');await settle();
  assert.equal(timers.size,1);
  context.settingsInteractionActive=()=>true;
  context.document.querySelector=selector=>selector==='#spend-panel'?{contains:active=>active?.id==='spend-pr-search'}:null;
  context.document.activeElement={id:'spend-pr-search'};
  const pausedPoll=[...timers.values()][0];timers.clear();pausedPoll();
  assert.equal(requests,1);
  context.document.activeElement={id:'spend-tab-prs'};
  const poll=[...timers.values()][0];timers.clear();poll();
  assert.equal(elements.get('#spend-export').disabled,true);
  selectTab(elements,'leaderboard');
  finish({...prData,pending_refresh:false,total_merged:2});await settle();
  assert.equal(requests,2);
  assert.equal(elements.get('#spend-export').disabled,false);
  assert.match(elements.get('#spend-panel').innerHTML,/PR leaderboard/);
  assert.equal(timers.size,0);
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
test('Users combines spend and human activity with shared dates, refresh and a combined export',async()=>{
  const {context:c,elements,calls}=withActivity();
  await c.renderSpend();
  await elements.get('#spend-tab-users').onclick();
  assert.equal(c.state.view,'spend');
  assert.match(elements.get('#content').innerHTML,/id="spend-tab-users"[^>]*aria-pressed="true"/);
  assert.match(elements.get('#spend-activity').innerHTML,/Team activity/);
  assert.equal(calls.at(-1),'/api/admin/adoption?start=2026-10-01&end=2026-10-07');
  c.$('#spend-start').value='2026-10-02';c.$('#spend-end').value='2026-10-04';
  elements.get('#spend-filter-form').onsubmit({preventDefault(){}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls.at(-1),'/api/admin/adoption?start=2026-10-02&end=2026-10-04');
  await elements.get('#sync-spend').onclick();
  assert.equal(calls.at(-1),'/api/admin/adoption?start=2026-10-02&end=2026-10-04');
  let exported;c.downloadAnalyticsCSV=(name,rows)=>{exported={name,rows};};
  elements.get('#spend-export').onclick();
  assert.equal(exported.name,'moyai-users-2026-10-02-2026-10-04.csv');
  assert.equal(exported.rows[0][0],'User');
  assert.ok(exported.rows.some(row=>row[0]==='Team activity (all users)'));
  assert.ok(exported.rows.some(row=>row[1]==='Human requests'));
  assert.doesNotMatch(elements.get('#content').innerHTML,/data-spend-tab="activity"/);
  await elements.get('#spend-tab-users').onclick();
  assert.match(elements.get('#content').innerHTML,/LLM spend by user/);
  elements.get('#spend-export').onclick();
  assert.equal(exported.rows[0][0],'User');
});
test('human activity errors retain tabs and dates and retry without leaving spend',async()=>{
  const {context:c,elements}=withActivity();await c.renderSpend();
  const api=c.api;c.api=async url=>{if(url.startsWith('/api/admin/adoption?'))throw Error('Activity unavailable <retry>');return api(url);};
  await elements.get('#spend-tab-users').onclick();
  assert.match(elements.get('#spend-activity').innerHTML,/Activity unavailable &lt;retry>/);
  assert.match(elements.get('#content').innerHTML,/LLM spend by user/);
  assert.equal(elements.get('#spend-export').textContent,'Export spend CSV');
  assert.equal(elements.get('#spend-export').disabled,false);
  let exported;c.downloadAnalyticsCSV=(_name,rows)=>{exported=rows;};elements.get('#spend-export').onclick();
  assert.equal(exported[0][0],'User');
  assert.ok(!exported.some(row=>row[0]==='Team activity (all users)'));
  assert.equal(typeof elements.get('#spend-filter-form').onsubmit,'function');
  c.api=api;await elements.get('#activity-retry').onclick();
  assert.match(elements.get('#spend-activity').innerHTML,/Team activity/);
  assert.equal(elements.get('#spend-export').disabled,false);
});
test('delayed human activity cannot overwrite another tab, refreshed date range, or page',async()=>{
  for(const destination of ['tab','prs','refresh','page']){
    const {context:c,elements}=withActivity();await c.renderSpend();
    let finish;const api=c.api;c.api=url=>url.startsWith('/api/admin/adoption?')?new Promise(resolve=>{finish=resolve;}):api(url);
    const old=elements.get('#spend-tab-users').onclick();
    if(destination==='tab')await elements.get('#spend-tab-history').onclick();
    if(destination==='prs'){await elements.get('#spend-tab-prs').onclick();await settle();}
    if(destination==='refresh'){c.api=api;await c.renderSpend();}
    if(destination==='page')c.state.pageVersion++;
    c.$('#spend-activity').innerHTML='Current content';
    const exportHandler=elements.get('#spend-export').onclick;
    finish(activity);await old;
    assert.equal(elements.get('#spend-activity').innerHTML,'Current content',destination);
    assert.equal(elements.get('#spend-export').onclick,exportHandler,destination);
  }
});
test('legacy adoption links open the combined Users tab with one canonical spend destination',async()=>{
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
    assert.match(scope==='organization'?elements.get('#spend-activity').innerHTML:elements.get('#content').innerHTML,scope==='organization'?/Team activity/:/Your LLM spend/);
  }
});

test('filtering spend keeps team activity and exports both scopes without refetching',async()=>{
  const {context:c,elements,calls,data}=withActivity();
  data.users.push({...data.users[0],id:'google:sam',email:'sam@example.com',spend:'2.00'});
  await c.renderSpend();await elements.get('#spend-tab-users').onclick();
  const previous=calls.length,activityHTML=elements.get('#spend-activity').innerHTML;
  c.$('#spend-user').value='google:sam';elements.get('#spend-user').onchange();
  assert.equal(calls.length,previous);
  assert.equal(elements.get('#spend-activity').innerHTML,activityHTML);
  assert.match(elements.get('#content').innerHTML,/activity above stays team-wide/);
  let exported;c.downloadAnalyticsCSV=(_name,rows)=>{exported=rows;};elements.get('#spend-export').onclick();
  assert.equal(exported[1][0],'sam@example.com');
  assert.ok(!exported.some(row=>row[0]==='maya@example.com'));
  assert.ok(exported.some(row=>row[0]==='Team activity (all users)'));
  assert.ok(exported.some(row=>row[0]==='2026-10-07'&&row[1]===7));
  elements.get('#spend-clear-user').onclick();
  assert.equal(calls.length,previous);
  elements.get('#spend-export').onclick();
  assert.ok(exported.some(row=>row[0]==='maya@example.com'));
});


test('leaderboard shows created-cohort status counts and the exact supplied cost per merged PR',async()=>{
  const {context,elements,prData}=setup('organization');
  Object.assign(prData.leaderboard[0],{created_prs:5,status_counts:{merged:1,open:2,draft:1,closed:1,unknown:0},merged_prs:2,cost_per_merged_pr:'3.1415926535897932384626433832'});
  prData.total_created=5;prData.total_merged=2;prData.unknown_created_at=1;
  await context.renderSpend();selectTab(elements,'leaderboard');await settle();
  const html=elements.get('#spend-panel').innerHTML;
  for(const text of ['PRs created','PRs by status','PRs merged','$ per PR merged','Merged 1','Open 2','Draft 1','Closed 1','$3.14','Partial · 1 pending · 1 missing','1 with unknown creation date'])assert.ok(html.includes(text),text);
  assert.match(html,/aria-label="Statuses of PRs created in the selected period"/);
  assert.match(html,/pr-distribution-open" style="width:40%"/);
  assert.match(html,/Status bars show the current statuses of PRs created in this period/);
  assert.doesNotMatch(html,/NaN|Infinity|Share of merged PRs/);
  let exported;context.downloadAnalyticsCSV=(_name,rows)=>{exported=rows;};elements.get('#spend-export').onclick();
  const column=name=>exported[1][exported[0].indexOf(name)];
  assert.equal(column('PRs created in period'),5);
  assert.equal(column('Merged PRs (created in period)'),1);
  assert.equal(column('Open PRs (created in period)'),2);
  assert.equal(column('PRs merged in period'),2);
  assert.equal(column('Cost per merged PR USD (linked spend / merged PRs)'),'3.1415926535897932384626433832');
  assert.equal(column('Cost coverage'),'Partial');
});

test('contributors with creations and no merges remain useful and each drilldown labels its date cohort',async()=>{
  const {context,elements,prData}=setup('organization');
  const created={...prData.pull_requests[0],number:456,url:'https://github.com/example/moyai/pull/456',user_id:'creator-only',user_name:'Robin',user_email:'robin@example.com',title:'Prepare a new feature',state:'open',draft:true,merged_at:null};
  prData.created_pull_requests=[created];prData.total_created=1;
  Object.assign(prData.leaderboard[0],{created_prs:0,status_counts:{merged:0,open:0,draft:0,closed:0,unknown:0}});
  prData.leaderboard.push({user_id:'creator-only',name:'Robin',email:'robin@example.com',created_prs:1,status_counts:{merged:0,open:0,draft:1,closed:0,unknown:0},merged_prs:0,cost_per_merged_pr:null,spend:'0',sessions:0,requests:0,pending_costs:0,missing_costs:0});
  await context.renderSpend();selectTab(elements,'leaderboard');await settle();
  const html=elements.get('#spend-panel').innerHTML;
  assert.match(html,/No PRs created/);
  assert.match(html,/Robin/);
  assert.match(html,/Draft 1/);
  assert.match(html,/—<small>No merged PRs<\/small>/);
  elements.get('#person-creator-only').onclick();
  const detail=elements.get('#spend-panel').innerHTML;
  assert.match(detail,/Created in period \(1\)/);
  assert.match(detail,/Merged in period \(0\)/);
  assert.match(detail,/Prepare a new feature/);
  assert.doesNotMatch(detail,/Keep saved filters/);
  assert.match(detail,/No verified PRs merged in this period/);
  elements.get('#person-google:maya').onclick();
  assert.match(elements.get('#spend-panel').innerHTML,/Created in period \(0\)/);
  assert.match(elements.get('#spend-panel').innerHTML,/Merged in period \(1\)/);
  assert.match(elements.get('#spend-panel').innerHTML,/Keep saved filters/);
  let exported;context.downloadAnalyticsCSV=(_name,rows)=>{exported=rows;};elements.get('#spend-export').onclick();
  const robin=exported.find(row=>row[0]==='Robin');
  assert.equal(robin[exported[0].indexOf('Cost per merged PR USD (linked spend / merged PRs)')],null);
  assert.equal(robin[exported[0].indexOf('Cost coverage')],'No merged PRs');
});

for (const scope of ['personal','organization']) test(scope+' request costs distinguish pending billing from execution', async () => {
  const {context,elements,data}=setup(scope);
  data.request_details=[
    {id:'pending',run_id:'session-1',created_at:'2026-10-07',model:'test-model',status:'interrupted',cost:null,cost_status:'pending'},
    {id:'settled',run_id:'session-1',created_at:'2026-10-07',model:'test-model',status:'failed',cost:'0.0123456789',cost_status:'settled',cost_source:'gateway_recovery'},
    {id:'denied',run_id:'session-1',created_at:'2026-10-07',model:'test-model',status:'completed',cost:null,cost_status:'unresolved',cost_recovery_error:'receipt_access_denied'},
  ];
  await context.renderSpend();
  const html=scope==='organization'?context.spendRequests(data):elements.get('#content').innerHTML;
  assert.match(html,/Cost pending/);
  assert.match(html,/Recovered receipt/);
  assert.match(html,/\$0\.0123456789/);
  assert.match(html,/Receipt access required/);
  assert.doesNotMatch(html,/In progress/);
});

for(const background of [false,true])test(`same-range ${background?'automatic':'manual'} refresh retains the mounted leaderboard through both fetches`,async()=>{
  const {context:c,elements:e,data,prData}=setup('organization');
  c.settingsInteractionActive=()=>false;
  await c.renderSpend();selectTab(e,'leaderboard');await settle();
  e.get('#person-google:maya').onclick();
  const original=c.api,pending=[];
  c.api=url=>url.startsWith('/api/spend?')||url.includes('pull-requests')?new Promise(resolve=>pending.push(resolve)):original(url);
  const html=e.get('#content').innerHTML;
  const refresh=c.renderSpend(background);
  assert.equal(e.get('#content').innerHTML,html,'mounted content is never replaced by a loader');
  pending.shift()(data);await refresh;
  assert.match(e.get('#content').innerHTML,/PR leaderboard/);
  assert.match(e.get('#content').innerHTML,/Pull requests · Maya/);
  assert.doesNotMatch(e.get('#content').innerHTML,/Loading pull request analytics/);
  pending.shift()({...prData,total_merged:42});await settle();
  assert.match(e.get('#spend-panel').innerHTML,/42/);
  assert.match(e.get('#spend-panel').innerHTML,/Pull requests · Maya/);
});

for(const endpoint of ['spend','pull-requests'])test(`${endpoint} refresh failure retains data and retry recovers`,async()=>{
  const {context:c,elements:e}=setup('organization');
  await c.renderSpend();selectTab(e,'leaderboard');await settle();
  const mounted=e.get('#content').innerHTML;
  const original=c.api;c.api=url=>url.includes(endpoint)?Promise.reject(Error('<offline>')):original(url);
  await c.renderSpend();await settle();
  if(endpoint!=='pull-requests')assert.equal(e.get('#content').innerHTML,mounted);
  assert.match(e.get('#spend-panel').innerHTML,/PR leaderboard/);
  if(endpoint==='pull-requests'){
    assert.match(e.get('#spend-panel').innerHTML,/Maya/);
    assert.match(e.get('#spend-panel').innerHTML,/&lt;offline>/);
  }else assert.match(e.get('#spend-refresh-status').textContent,/<offline>/);
  c.api=original;await c.renderSpend();await settle();
  assert.match(e.get('#spend-panel').innerHTML,/Maya/);
  assert.equal(e.get('#spend-export').disabled,false);
});

test('slow or failed account settings never block spend charts and remain independently retryable',async()=>{
  const {context:c,elements:e}=setup('organization');const original=c.api;
  let reject;const waiting=new Promise((_,fail)=>{reject=fail;});
  c.api=url=>url==='/api/admin/identities/status'?waiting:original(url);
  await c.renderSpend();
  assert.match(e.get('#content').innerHTML,/Organization totals/);
  vm.runInContext("spendAnalyticsState.tab='infrastructure'",c);
  const rendering=c.renderSpend();await settle();
  assert.match(e.get('#content').innerHTML,/Infrastructure costs/);
  assert.match(e.get('#content').innerHTML,/Loading account links/);
  reject(Error('Temporarily unavailable'));await rendering;
  assert.match(e.get('#spend-identities-notice').innerHTML,/Could not load account links/);
  c.api=original;await e.get('#spend-identities-retry').onclick();
  assert.match(e.get('#spend-identities').innerHTML,/Slack identities/);
});

for(const status of [401,403])test(`authorization ${status} clears retained report`,async()=>{
  const {context:c,elements:e}=setup('organization');
  await c.renderSpend();selectTab(e,'leaderboard');await settle();
  c.api=()=>Promise.reject(Object.assign(Error('Access denied'),{status}));
  await c.renderSpend();
  assert.doesNotMatch(e.get('#content').innerHTML,/PR leaderboard|Maya/);
  assert.equal(vm.runInContext('spendPRState.data',c),null);
});

test('date changes and route changes do not reuse a displayed report',async()=>{
  const {context:c,elements:e}=setup('organization');
  await c.renderSpend();selectTab(e,'leaderboard');await settle();
  let finish;const original=c.api;c.api=url=>url.startsWith('/api/spend?')?new Promise(resolve=>finish=resolve):original(url);
  vm.runInContext("spendState.start='2026-10-03'",c);
  const pending=c.renderSpend();assert.match(e.get('#content').innerHTML,/Loading spend/);
  c.state.view='settings';c.state.pageVersion++;e.get('#content').innerHTML='Settings';
  await c.renderSpend();assert.equal(e.get('#content').innerHTML,'Settings');
  finish(await original('/api/spend?'));await pending;assert.equal(e.get('#content').innerHTML,'Settings');
});

test('hidden polling and interactions that start during transport leave the DOM untouched',async()=>{
  const {context:c,elements:e,calls,data}=setup('organization');const timers=new Map();let id=0,interacting=false;
  c.setTimeout=fn=>{timers.set(++id,fn);return id;};c.clearTimeout=id=>timers.delete(id);
  c.settingsInteractionActive=()=>interacting;
  await c.renderSpend();selectTab(e,'leaderboard');await settle();
  const html=e.get('#content').innerHTML,count=calls.length;
  c.document.hidden=true;await c.renderSpend(true);assert.equal(calls.length,count);assert.equal(timers.size,1);
  c.document.hidden=false;let finish;c.api=()=>new Promise(resolve=>finish=resolve);
  const pending=c.renderSpend(true);interacting=true;finish({...data,scope:'personal'});await pending;
  assert.doesNotMatch(e.get('#content').innerHTML,/PR leaderboard/,'scope downgrade still clears while interacting');
  assert.equal(vm.runInContext('spendPRState.data',c),null);
});

test('a background response defers commit when interaction begins during the request',async()=>{
  const {context:c,elements:e,data}=setup();const timers=new Map();let id=0,interacting=false;
  c.setTimeout=fn=>{timers.set(++id,fn);return id;};c.clearTimeout=id=>timers.delete(id);c.settingsInteractionActive=()=>interacting;
  await c.renderSpend();const html=e.get('#content').innerHTML;
  let finish;c.api=()=>new Promise(resolve=>finish=resolve);
  const pending=c.renderSpend(true);interacting=true;finish({...data,total:{...data.total,spend:'99'}});await pending;
  assert.equal(e.get('#content').innerHTML,html);assert.equal(timers.size,1);
});

test('old PR completion is rejected while a different date range is still loading spend',async()=>{
  const {context:c,elements:e,prData,data}=setup('organization'),original=c.api;
  let finishPR,finishSpend;
  c.api=url=>url.includes('pull-requests')?new Promise(resolve=>finishPR=resolve):original(url);
  await c.renderSpend();selectTab(e,'leaderboard');await settle();
  vm.runInContext("spendState.start='2026-10-03'",c);
  c.api=url=>url.startsWith('/api/spend?')?new Promise(resolve=>finishSpend=resolve):original(url);
  const pending=c.renderSpend();c.$('#spend-panel').innerHTML='unmounted';
  finishPR(prData);await settle();
  assert.match(e.get('#content').innerHTML,/Loading spend/);
  assert.equal(e.get('#spend-panel').innerHTML,'unmounted');
  finishSpend({...data,start:'2026-10-03'});await pending;await settle();
});

test('clearing PR filters does not restore the previous DOM values over the new state',async()=>{
  const {context:c,elements:e}=setup('organization');
  await c.renderSpend();selectTab(e,'prs');await settle();
  const search=c.$('#spend-pr-search'),status=c.$('#spend-pr-status'),panel=c.$('#spend-panel');
  Object.assign(search,{id:'spend-pr-search',matches:()=>true,value:'missing phrase'});
  Object.assign(status,{id:'spend-pr-status',matches:()=>true,value:'open'});
  const query=c.document.querySelectorAll;
  c.document.querySelectorAll=selector=>selector==='#content [id]'?[search,status]:query(selector);
  c.document.getElementById=id=>e.get('#'+id);
  let html=panel.innerHTML;
  Object.defineProperty(panel,'innerHTML',{get:()=>html,set:value=>{
    html=value;search.value=/id="spend-pr-search"[^>]*value="([^"]*)"/.exec(value)?.[1]||'';
    status.value=/<option value="([^"]*)" selected>/.exec(value)?.[1]||'';
  }});
  search.oninput();status.onchange();e.get('#spend-pr-clear').onclick();
  assert.equal(search.value,'');assert.equal(status.value,'');
  assert.match(panel.innerHTML,/Keep saved filters/);
});

for(const tab of ['prs','leaderboard'])test(tab+' controls work while parent-triggered PR refresh is in flight',async()=>{
  const {context:c,elements:e,prData}=setup('organization');
  await c.renderSpend();selectTab(e,tab);await settle();
  destroyHandlersOnPageReplacement(e);
  const original=c.api;let finish;
  c.api=url=>url.includes('pull-requests')?new Promise(resolve=>finish=resolve):original(url);
  await c.renderSpend();
  if(tab==='prs'){
    assert.equal(typeof e.get('#spend-pr-search').oninput,'function');
    e.get('#spend-pr-search').value='Keep saved';
    e.get('#spend-pr-search').oninput();
    e.get('#spend-pr-status').value='merged';e.get('#spend-pr-status').onchange();
  }else{
    assert.equal(typeof e.get('#person-google:maya').onclick,'function');
    e.get('#person-google:maya').onclick();
  }
  finish(prData);await settle();
  if(tab==='prs'){
    assert.match(e.get('#spend-panel').innerHTML,/value="Keep saved"/);
    assert.match(e.get('#spend-panel').innerHTML,/value="merged" selected/);
    e.get('#spend-pr-clear').onclick();
    assert.equal(vm.runInContext('spendPRState.search+spendPRState.status',c),'');
  }else assert.match(e.get('#spend-panel').innerHTML,/Pull requests · Maya/);
});

for(const endpoint of ['spend','identities'])test(endpoint+' failure does not stop independent PR status polling',async()=>{
  const {context:c,elements:e,prData}=setup('organization'),clock=fakeTimers(c);
  prData.pending_refresh=true;
  await c.renderSpend();selectTab(e,'leaderboard');await settle();
  const original=c.api;
  c.api=url=>url.includes(endpoint)?Promise.reject(Error('offline')):original(url);
  await c.renderSpend();
  assert.match(e.get('#spend-panel').innerHTML,/Checking GitHub status/);
  prData.pending_refresh=false;
  await clock.tick(4000);
  assert.doesNotMatch(e.get('#spend-panel').innerHTML,/Checking GitHub status/);
  assert.equal(vm.runInContext('spendPRState.data.pending_refresh',c),false);
  assert.equal(clock.timers.size,0);
});

for(const reason of ['hidden','interaction'])test(reason+' starting during spend transport preserves PR polling',async()=>{
  const {context:c,elements:e,data,prData}=setup('organization'),clock=fakeTimers(c);
  let interacting=false;c.settingsInteractionActive=()=>interacting;c.document.querySelector=()=>null;
  prData.pending_refresh=true;
  await c.renderSpend();selectTab(e,'leaderboard');await settle();
  const original=c.api;let finish;
  c.api=url=>url.startsWith('/api/spend?')?new Promise(resolve=>finish=resolve):original(url);
  const refresh=c.renderSpend(true);
  if(reason==='hidden')c.document.hidden=true;else interacting=true;
  finish(data);await refresh;
  c.document.hidden=false;interacting=false;prData.pending_refresh=false;
  await clock.tick(4000);
  assert.doesNotMatch(e.get('#spend-panel').innerHTML,/Checking GitHub status/);
  assert.equal(vm.runInContext('spendPRState.data.pending_refresh',c),false);
});

test('a transient PR polling failure retains data and automatically recovers',async()=>{
  const {context:c,elements:e,prData}=setup('organization'),clock=fakeTimers(c);
  prData.pending_refresh=true;
  await c.renderSpend();selectTab(e,'leaderboard');await settle();
  const original=c.api;
  c.api=url=>url.includes('pull-requests')?Promise.reject(Error('offline')):original(url);
  await clock.tick(4000);
  assert.match(e.get('#spend-panel').innerHTML,/offline/);
  assert.match(e.get('#spend-panel').innerHTML,/Maya/);
  c.api=original;prData.pending_refresh=false;
  await clock.tick(4000);
  assert.doesNotMatch(e.get('#spend-panel').innerHTML,/offline/);
  assert.doesNotMatch(e.get('#spend-panel').innerHTML,/Checking GitHub status/);
  assert.equal(vm.runInContext('spendPRState.data.pending_refresh',c),false);
  assert.equal(e.get('#spend-export').disabled,false);
  assert.equal(clock.timers.size,0);
});

for(const status of [401,403])test('PR authorization '+status+' clears cached data and stops polling',async()=>{
  const {context:c,elements:e,prData}=setup('organization'),clock=fakeTimers(c);
  prData.pending_refresh=true;
  await c.renderSpend();selectTab(e,'leaderboard');await settle();
  c.api=()=>Promise.reject(Object.assign(Error('Access denied'),{status}));
  await clock.tick(4000);
  assert.doesNotMatch(e.get('#spend-panel').innerHTML,/Maya|PR leaderboard/);
  assert.equal(vm.runInContext('spendPRState.data',c),null);
  assert.equal(clock.timers.size,0);
});

test('parent refreshes share a slow PR request without starving its completion',async()=>{
  const {context:c,elements:e,prData}=setup('organization');
  await c.renderSpend();selectTab(e,'leaderboard');await settle();
  const original=c.api;let finish,count=0;
  c.api=url=>url.includes('pull-requests')?(count++,new Promise(resolve=>finish=resolve)):original(url);
  await c.renderSpend();await c.renderSpend();await c.renderSpend();
  assert.equal(count,1);
  finish({...prData,total_merged:42});await settle();
  assert.match(e.get('#spend-panel').innerHTML,/42/);
  assert.equal(e.get('#spend-export').disabled,false);
});

test('pending spend costs recover automatically after a transient refresh failure',async()=>{
  const {context:c,elements:e,data}=setup(),clock=fakeTimers(c);
  c.settingsInteractionActive=()=>false;data.total.pending_costs=1;
  await c.renderSpend();const original=c.api;
  c.api=()=>Promise.reject(Error('offline'));
  await clock.tick(5000);
  assert.match(e.get('#spend-refresh-status').textContent,/offline/);
  c.api=original;data.total.pending_costs=0;
  await clock.tick(5000);
  assert.match(e.get('#content').innerHTML,/All tracked LLM requests priced/);
  assert.equal(clock.timers.size,0);
});

for(const type of ['date','number','checkbox','select-one'])test(type+' focus never reads unsupported selection during refresh',async()=>{
  const {context:c,elements:e}=setup();fakeTimers(c);
  await c.renderSpend();
  const input={id:'spend-start',tagName:type==='select-one'?'SELECT':'INPUT',type,focus(){}};
  for(const key of ['selectionStart','selectionEnd'])Object.defineProperty(input,key,{get(){throw Error('Unsupported selection');}});
  c.document.activeElement=input;c.document.getElementById=()=>input;
  await c.renderSpend();
  assert.match(e.get('#content').innerHTML,/Your LLM spend/);
});

test('text selection direction survives restoration and unsupported replacement is safe',()=>{
  const {context:c}=setup();
  let saved;
  const input={id:'search',tagName:'INPUT',type:'search',selectionStart:2,selectionEnd:5,selectionDirection:'backward',focus(){},setSelectionRange(...args){saved=args;}};
  c.document.activeElement=input;c.document.getElementById=()=>input;
  c.preserveSpendView()();assert.deepEqual(saved,[2,5,'backward']);
  const restore=c.preserveSpendView();input.type='date';input.setSelectionRange=()=>{throw Error('unsupported');};
  assert.doesNotThrow(restore);
});

for(const foreground of [false,true])test('deferred shared PR response survives '+(foreground?'overlapping Retry':'until interaction ends'),async()=>{
  const {context:c,elements:e,data,prData}=setup('organization'),clock=fakeTimers(c);
  let interacting=false;c.settingsInteractionActive=()=>interacting;c.document.querySelector=()=>null;
  await c.renderSpend();selectTab(e,'leaderboard');await settle();
  let finish,count=0;c.api=()=>{count++;return new Promise(resolve=>finish=resolve);};
  c.reportData=data;
  const background=vm.runInContext("loadSpendPRReport(reportData,'leaderboard',spendAnalyticsState.request,true,true)",c);
  interacting=true;
  const manual=foreground?vm.runInContext("loadSpendPRReport(reportData,'leaderboard',spendAnalyticsState.request,true)",c):null;
  finish({...prData,total_merged:42,pending_refresh:false});await background;await manual;
  if(!foreground){
    assert.doesNotMatch(e.get('#spend-panel').innerHTML,/42/);
    interacting=false;await clock.tick(4000);
  }
  assert.equal(count,1,'completed response is reused, not fetched again');
  assert.match(e.get('#spend-panel').innerHTML,/42/);
  assert.equal(e.get('#spend-export').disabled,false);
  assert.equal(clock.timers.size,0);
});
