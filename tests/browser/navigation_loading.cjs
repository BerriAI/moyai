// Production frontend against deterministic API fixtures with 300 ms read latency.
const assert=require('node:assert/strict');
const {test,before,after}=require('node:test');
const {spawn}=require('node:child_process');
const {chromium}=require('playwright');
let server,browser,base;
before(async()=>{
  server=spawn(process.execPath,['scripts/settings_ui_preview.cjs','--port','0','--delay-ms','300','--root',process.env.NAVIGATION_STATIC_ROOT||'app/static'],{stdio:['ignore','pipe','inherit']});
  base=await new Promise((resolve,reject)=>{
    server.once('error',reject);server.once('exit',code=>reject(Error('Preview exited: '+code)));
    server.stdout.on('data',chunk=>{const match=chunk.toString().match(/http:\/\/127\.0\.0\.1:\d+/);if(match)resolve(match[0]);});
  });
  browser=await chromium.launch({headless:true});
});
after(async()=>{await browser?.close();server?.kill();});
async function setup(t){
  const page=await browser.newPage({viewport:{width:1440,height:1000},reducedMotion:'reduce'}),errors=[],reads=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('request',r=>{if(r.method()==='GET'&&r.url().includes('/api/'))reads.push(new URL(r.url()).pathname);});
  t.after(async()=>{await page.close();assert.deepEqual(errors,[]);});
  await page.goto(base+'/#settings');await page.locator('#title-model-status').filter({hasText:'Enter the exact'}).waitFor();
  return {page,reads};
}
async function click(page,selector,ready){
  return page.evaluate(({selector,ready})=>new Promise((resolve,reject)=>{
    const start=performance.now();
    const timer=setTimeout(()=>{observer.disconnect();reject(Error('View did not load: '+ready));},8000);
    const check=()=>{if(!document.querySelector(ready))return;observer.disconnect();clearTimeout(timer);requestAnimationFrame(()=>resolve(performance.now()-start));};
    const observer=new MutationObserver(check);observer.observe(document.querySelector('#content'),{childList:true,subtree:true});
    document.querySelector(selector).click();check();
  }),{selector,ready});
}
for(const width of [1440,768,320])test(`a two-minute-old conversation paints before revalidation at ${width}px`,async t=>{
  const page=await browser.newPage({viewport:{width,height:1000},reducedMotion:'reduce'}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  t.after(async()=>{await page.close();assert.deepEqual(errors,[]);});
  const id='a'.repeat(32),start=Date.parse('2026-01-01T00:00:00Z');
  await page.clock.setFixedTime(start);
  await page.goto(`${base}/?fixture=feedback#run=${id}`);
  await page.locator('#followup').waitFor();
  await page.evaluate(()=>navigate('tasks'));await page.locator('#prompt').waitFor();
  await page.clock.setFixedTime(start+120000);
  let release;const held=new Promise(resolve=>release=resolve);t.after(()=>release());
  let reads=0;
  await page.route(url=>url.pathname===`/api/runs/${id}`,async route=>{
    reads++;const response=await route.fetch(),run=await response.json();
    run.messages[1].content='Fresh response after revalidation';
    await held;await route.fulfill({response,json:run});
  });
  const ms=await page.evaluate(id=>new Promise(resolve=>{
    const start=performance.now();window.reopening=openRun(id);
    requestAnimationFrame(()=>resolve(performance.now()-start));
  }),id);
  assert.equal(await page.locator('#followup').count(),1,'cached transcript must mount before the held response');
  assert(ms<250,`cached conversation took ${ms}ms`);
  const composer=await page.locator('#followup').elementHandle();
  await page.locator('#followup').fill('Keep this unsent draft');
  assert.match(await page.locator('.chat-message.assistant').innerText(),/The change looks good/);
  release();await page.evaluate(()=>window.reopening);
  assert.equal(reads,1,'a cached preview must still revalidate');
  assert.equal(await composer.evaluate(el=>el.isConnected),true,'fresh data must retain the composer');
  assert.equal(await page.locator('#followup').evaluate(el=>el.value),'Keep this unsent draft');
  assert.match(await page.locator('.chat-message.assistant').innerText(),/Fresh response after revalidation/);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  t.diagnostic(`two-minute-old conversation painted in ${Math.round(ms)}ms at ${width}px with network held`);
});
test('returning to Settings and Users uses recent data; a settings write invalidates it',async t=>{
  const {page,reads}=await setup(t);
  await click(page,'a[href="#users"]','#user-search');
  await page.locator('#user-search').fill('Alex');
  await click(page,'#settings-navigation a[href="#settings"]','#title-model');
  const before=reads.filter(x=>x==='/api/admin/users').length;
  const ms=await click(page,'a[href="#users"]','#user-search');
  assert(ms<250,`cached Users took ${ms}ms with 300ms API latency`);
  assert.equal(reads.filter(x=>x==='/api/admin/users').length,before);
  assert.equal(await page.locator('#user-search').inputValue(),'Alex');
  await click(page,'#settings-navigation a[href="#settings"]','#title-model');
  await page.locator('#title-model-form button').click();
  await page.locator('#title-model-status').filter({hasText:'Saved.'}).waitFor();
  await click(page,'a[href="#users"]','#user-search');
  assert.equal(reads.filter(x=>x==='/api/admin/users').length,before+1);
});
test('PR and activity hover reads are reused; reports survive leaving and returning to Spend',async t=>{
  const {page,reads}=await setup(t);
  await click(page,'a[href="#spend"]','#spend-tab-prs');
  assert.equal(reads.includes('/api/admin/identities/status'),false,'account links do not block charts');
  const loaded=page.waitForResponse(r=>r.url().includes('/api/admin/pull-requests?'));
  await page.locator('#spend-tab-prs').hover();await loaded;
  await click(page,'#spend-tab-prs','.analytics-pr-table');
  await click(page,'#spend-tab-leaderboard','.analytics-pr-leaderboard');
  const before=reads.filter(x=>x==='/api/admin/pull-requests').length;
  await click(page,'#settings-navigation a[href="#settings"]','#title-model');
  const ms=await click(page,'a[href="#spend"]','.analytics-pr-leaderboard');
  assert(ms<250,`cached Leaderboard took ${ms}ms`);
  assert.equal(reads.filter(x=>x==='/api/admin/pull-requests').length,before);
  const activity=page.waitForResponse(r=>r.url().includes('/api/admin/adoption?'));
  await page.locator('#spend-tab-users').hover();await activity;
  await click(page,'#spend-tab-users','#spend-activity .analytics-section');
  assert.equal(reads.filter(x=>x==='/api/admin/adoption').length,1);
  const refreshed=page.waitForResponse(r=>r.url().includes('/api/admin/adoption?'));
  await page.locator('#sync-spend').click();await refreshed;
  assert.equal(reads.filter(x=>x==='/api/admin/adoption').length,2,'explicit Refresh bypasses the cache');
});

async function accountLinks(t,width=1440,costs={pending:0}){
  const {page,reads}=await setup(t);await page.setViewportSize({width,height:1000});
  await page.route('**/api/spend?*',async route=>{
    const response=await route.fetch(),data=await response.json();
    data.identities=[...data.users.slice(0,2).map(user=>({...user,kind:'google'})),
      {id:'slack:alex',kind:'slack',name:'Alex Morgan',email:'alex@example.com',link_status:'review'}];
    data.total.pending_costs=costs.pending;data.infrastructure.pending=false;
    await route.fulfill({response,json:data});
  });
  await click(page,'a[href="#spend"]','#spend-tab-infrastructure');
  await page.locator('#spend-tab-infrastructure').click();
  await page.locator('.identity-overrides > summary').click();
  const form=page.locator('.spend-link-form').first();
  await form.getByRole('combobox').click();
  await page.getByRole('option',{name:'sam@example.com',exact:true}).click();
  await page.getByRole('listbox').waitFor({state:'detached'});
  await page.waitForFunction(()=>document.activeElement.matches('.spend-link-form [role="combobox"]'));
  await page.evaluate(()=>document.activeElement.blur());
  assert.equal(await page.evaluate(()=>settingsInteractionActive()),false);
  return {page,form,reads};
}
test('the final cost poll applies deferred account updates after the menu closes without another fetch',async t=>{
  const costs={pending:1},{page,form,reads}=await accountLinks(t,1440,costs);
  costs.pending=0;
  let release;const held=new Promise(resolve=>{release=resolve;});t.after(()=>release());
  await page.route('**/api/admin/identities/status',async route=>{await held;await route.fulfill({json:{enabled:false,ready:false,missing_scopes:[]}});});
  const request=page.waitForRequest(r=>r.url().endsWith('/api/admin/identities/status'));
  await page.evaluate(()=>{window.spendPolling=renderSpend(true);});await request;
  await form.getByRole('combobox').click();release();await page.evaluate(()=>window.spendPolling);
  assert.equal(await page.evaluate(()=>spendState.pending),false,'cost polling is finished');
  assert.equal(await form.locator('[role="combobox"]').getAttribute('aria-expanded'),'true');
  assert.match(await page.locator('#slack-identities').innerText(),/Automatic matching is on/);
  const before=reads.length;
  await page.getByRole('option',{name:'sam@example.com',exact:true}).click();
  await page.getByRole('listbox').waitFor({state:'detached'});
  await page.waitForFunction(()=>document.activeElement.matches('.spend-link-form [role="combobox"]'));
  await page.locator('#page-title').click();
  await page.waitForFunction(()=>document.querySelector('#slack-identities')?.textContent.includes('Automatic matching is disabled'),{},{timeout:2500});
  assert.equal(await form.locator('select[name="google"]').inputValue(),'user-1');
  assert.equal(await page.locator('.identity-overrides').evaluate(el=>el.open),true);
  assert.equal(reads.length,before,'the completed update is applied without another API request');
});
for(const width of [1440,768,320])test(`background spend refresh retains account-link controls and drafts at ${width}px`,async t=>{
  const {page,form}=await accountLinks(t,width),original=await form.elementHandle();
  let release;const held=new Promise(resolve=>{release=resolve;});t.after(()=>release());
  await page.route('**/api/admin/identities/status',async route=>{await held;await route.continue();});
  const request=page.waitForRequest(r=>r.url().endsWith('/api/admin/identities/status'),{timeout:5000});
  await page.evaluate(()=>{document.activeElement.blur();window.spendPolling=renderSpend(true);});
  await request;
  assert.equal(await original.evaluate(el=>el.isConnected),true,'the form stays mounted while account links refresh');
  assert.equal(await page.getByText('Loading account links…',{exact:true}).count(),0);
  assert.equal(await form.locator('select[name="google"]').inputValue(),'user-1');
  release();await page.evaluate(()=>window.spendPolling);
  assert.equal(await original.evaluate(el=>el.isConnected),true,'unchanged account links retain their component state');
  assert.equal(await form.locator('select[name="google"]').inputValue(),'user-1');
  assert.equal(await page.locator('.identity-overrides').evaluate(el=>el.open),true);
});

test('account-link refresh preserves drafts on changed data and transient errors, but clears access failures',async t=>{
  const {page,form}=await accountLinks(t);let status=200,enabled=false;
  await page.route('**/api/admin/identities/status',route=>route.fulfill({status,json:status===200?{enabled,ready:enabled,missing_scopes:[]}:{detail:'Account lookup unavailable'}}));
  const poll=()=>page.evaluate(()=>{document.activeElement.blur();return renderSpend(true);});
  await poll();
  assert.match(await page.locator('#slack-identities').innerText(),/Automatic matching is disabled/);
  assert.equal(await form.locator('select[name="google"]').inputValue(),'user-1');
  assert.equal(await page.locator('.identity-overrides').evaluate(el=>el.open),true);
  status=503;await poll();
  assert.match(await page.locator('#spend-identities-notice').innerText(),/Could not load account links/);
  assert.equal(await form.locator('select[name="google"]').inputValue(),'user-1');
  status=200;enabled=true;await page.locator('#spend-identities-retry').click();
  await page.waitForFunction(()=>document.querySelector('#refresh-identities')?.disabled===false);
  assert.equal(await form.locator('select[name="google"]').inputValue(),'user-1');
  // Even an idempotent save must release its pending button state.
  await page.route('**/api/admin/spend/link-slack',route=>route.fulfill({json:{}}));
  await form.getByRole('button',{name:'Save manual link'}).click();
  await page.waitForFunction(()=>document.querySelector('.spend-link-form [type="submit"]')?.disabled===false);
  assert.equal(await form.locator('select[name="google"]').inputValue(),'user-1');
  status=403;await poll();
  assert.equal(await page.locator('.spend-link-form').count(),0);
  assert.match(await page.locator('#spend-identities-notice').innerText(),/Could not load account links/);
});

test('background account updates defer while a select is open and never repaint a new route',async t=>{
  const {page,form}=await accountLinks(t);
  let release;const held=new Promise(resolve=>{release=resolve;});t.after(()=>release());
  await page.route('**/api/admin/identities/status',async route=>{await held;await route.fulfill({json:{enabled:false,ready:false,missing_scopes:[]}});});
  const request=page.waitForRequest(r=>r.url().endsWith('/api/admin/identities/status'));
  await page.evaluate(()=>{document.activeElement.blur();window.spendPolling=renderSpend(true);});await request;
  await form.getByRole('combobox').click();release();await page.evaluate(()=>window.spendPolling);
  assert.equal(await form.locator('[role="combobox"]').getAttribute('aria-expanded'),'true');
  await page.getByRole('option',{name:'alex@example.com',exact:true}).click();
  await page.getByRole('listbox').waitFor({state:'detached'});
  await page.waitForFunction(()=>document.activeElement.matches('.spend-link-form [role="combobox"]'));
  assert.equal(await form.locator('select[name="google"]').inputValue(),'user-0');
  await click(page,'#settings-navigation a[href="#settings"]','#title-model');
  await page.waitForTimeout(600);
  assert.equal(await page.locator('#slack-identities').count(),0,'a queued update cannot repaint a new route');
  await click(page,'a[href="#spend"]','#slack-identities');
  let releaseNext;const heldNext=new Promise(resolve=>{releaseNext=resolve;});t.after(()=>releaseNext());
  await page.route('**/api/admin/identities/status',async route=>{await heldNext;await route.fulfill({json:{enabled:true,ready:true,missing_scopes:[]}});});
  const next=page.waitForRequest(r=>r.url().endsWith('/api/admin/identities/status'));
  await page.evaluate(()=>{document.activeElement.blur();window.spendPolling=renderSpend(true);});await next;
  await click(page,'#settings-navigation a[href="#settings"]','#title-model');
  releaseNext();await page.evaluate(()=>window.spendPolling);
  assert.equal(await page.locator('#slack-identities').count(),0);
  assert.equal(await page.locator('#title-model').count(),1);
});
