// Production frontend against deterministic API fixtures with 300 ms read latency.
const assert=require('node:assert/strict');
const {test,before,after}=require('node:test');
const {spawn}=require('node:child_process');
const {chromium}=require('playwright');
let server,browser,base;
before(async()=>{
  server=spawn(process.execPath,['scripts/settings_ui_preview.cjs','--port','0','--delay-ms','300'],{stdio:['ignore','pipe','inherit']});
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
