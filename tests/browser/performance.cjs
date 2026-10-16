const assert = require('node:assert/strict');
const {test, before, after} = require('node:test');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');
let server, browser, base;
before(async()=>{
  server=spawn(process.execPath,['scripts/settings_ui_preview.cjs','--port','0'],{stdio:['ignore','pipe','inherit']});
  base=await new Promise((resolve,reject)=>{server.once('error',reject);server.stdout.on('data',chunk=>{const match=chunk.toString().match(/http:\/\/127\.0\.0\.1:\d+/);if(match)resolve(match[0]);});});
  browser=await chromium.launch({headless:true});
});
after(async()=>{await browser?.close();server?.kill();});
async function open(t, fixture='populated', width=1440){
  const page=await browser.newPage({viewport:{width,height:1000},reducedMotion:'reduce'});
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  page.setDefaultTimeout(8000);
  t.after(async()=>{await page.close();assert.deepEqual(errors,[]);});
  await page.goto(`${base}/?fixture=${fixture}#performance`);
  return page;
}
for(const width of [1440,768,320])test(`Performance is usable at ${width}px`,async t=>{
  const page=await open(t,'populated',width);
  await page.locator('[data-performance-metric="model_dispatch"]').waitFor();
  assert.match(await page.locator('[data-performance-metric="model_dispatch"]').innerText(),/3.40 s/);
  assert.equal(await page.locator('#content h1').innerText(),'Performance');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  for(const selector of ['.performance-card','.performance-filters']){
    assert.equal(await page.locator(selector).evaluateAll(nodes=>nodes.every(el=>el.scrollWidth<=el.clientWidth+1)),true,selector+' has no clipped content');
  }
  const menu=page.getByRole('combobox',{name:'Conversation',exact:true});
  await menu.focus();await menu.press('Enter');
  await page.getByRole('option',{name:'Follow-ups',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('[data-performance-metric="model_dispatch"]')?.textContent.includes('1.90 s'));
  await page.getByRole('button',{name:'Refresh',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#performance-status')?.textContent.startsWith('Updated'));
  assert.equal(await page.locator('#performance-kind').inputValue(),'follow_up');
  await page.getByText('What these numbers measure',{exact:true}).click();
  await page.getByText('It is not time to first token or first visible answer.',{exact:false}).waitFor();
  await page.getByRole('button',{name:'Refresh',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.performance-method')?.open===true);
});
test('Empty data is not zero and filters reset through the shared select',async t=>{
  const page=await open(t,'empty');
  await page.getByRole('heading',{name:'No timing samples in this view'}).waitFor();
  assert.match(await page.locator('[data-performance-metric="model_dispatch"]').innerText(),/—/);
  await page.getByRole('combobox',{name:'Period',exact:true}).click();
  await page.getByRole('option',{name:'Last hour',exact:true}).click();
  await page.getByRole('button',{name:'Reset filters'}).click();
  await page.waitForFunction(()=>document.querySelector('#performance-hours')?.value==='24');
  assert.match(await page.getByRole('combobox',{name:'Period',exact:true}).innerText(),/Last 24 hours/);
});
test('Load error retries without presenting invented numbers',async t=>{
  const page=await open(t,'error');
  await page.getByRole('heading',{name:'Unable to load performance'}).waitFor();
  assert.equal(await page.locator('[data-performance-metric]').count(),0);
  await page.route('**/api/admin/performance?*',route=>route.fulfill({json:require('../../scripts/performance_ui_fixture.cjs')(new URL(route.request().url()).searchParams,false)}));
  await page.getByRole('button',{name:'Try again'}).click();
  await page.locator('[data-performance-metric="model_dispatch"]').waitFor();
});
test('Members cannot fetch or navigate to admin metrics',async t=>{
  const page=await open(t,'member');
  let reads=0;page.on('request',request=>{if(request.url().includes('/api/admin/performance'))reads++;});
  await page.reload();
  await page.getByText('Only administrators can view workspace performance.').waitFor();
  assert.equal(reads,0);
  assert.equal(await page.getByRole('link',{name:'Performance',exact:true}).count(),0);
});
test('A late response cannot replace the next route',async t=>{
  const page=await open(t);
  await page.locator('[data-performance-metric="model_dispatch"]').waitFor();
  let release, entered;
  const pending=new Promise(resolve=>entered=resolve), gate=new Promise(resolve=>release=resolve);
  await page.route('**/api/admin/performance?*',async route=>{entered();await gate;await route.continue();});
  await page.getByRole('button',{name:'Refresh',exact:true}).click();await pending;
  await page.evaluate(()=>location.hash='#settings');
  await page.getByRole('heading',{name:'Settings',exact:true}).waitFor();
  release();await page.unrouteAll({behavior:'wait'});
  assert.equal(await page.locator('#content h1').innerText(),'Settings');
  assert.equal(await page.locator('[data-performance-metric]').count(),0);
});
