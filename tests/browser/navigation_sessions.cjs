// Start chat_loading_demo.py and panel_loading_demo.py; these use real local APIs.
const assert=require('node:assert/strict');
const {test}=require('node:test');
const {chromium}=require('playwright');
const chatURL=process.env.CHAT_LOADING_URL||'http://127.0.0.1:8877';
const prURL=process.env.PR_LOADING_URL||'http://localhost:8880';
async function pageFor(t,url){
  const browser=await chromium.launch({headless:true}),page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  t.after(async()=>{await browser.close();assert.deepEqual(errors,[]);});
  await page.goto(url);await page.locator('.chat-message.assistant').first().waitFor();
  return page;
}
test('cached conversation is usable during revalidation and preserves the live composer',async t=>{
  const page=await pageFor(t,chatURL+'/demo/performance-login');
  await page.waitForFunction(()=>document.querySelectorAll('[data-run]').length>=90);
  const selected=await page.locator('.session-link.selected').getAttribute('data-run');
  await page.locator('.session-link:not(.selected)').first().click();await page.locator('#followup').waitFor();
  let release;const hold=new Promise(resolve=>{release=resolve;});t.after(()=>release());
  await page.route(`**/api/runs/${selected}?activity=summary`,async route=>{await hold;await route.continue();});
  // Exercise keyboard navigation without a neighboring session's tooltip.
  await page.keyboard.press('Escape');
  await page.locator(`[data-run="${selected}"]`).focus();await page.keyboard.press('Enter');
  await page.locator('#followup').waitFor({timeout:1000});
  const composer=await page.locator('#followup').elementHandle();
  await page.getByRole('textbox',{name:'Message Moyai',exact:true}).fill('Keep this draft through refresh');
  const updated=page.waitForResponse(r=>r.url().includes(`/api/runs/${selected}?activity=summary`));
  release();await updated;await page.waitForTimeout(150);
  assert(await composer.evaluate(el=>el.isConnected));
  assert.match(await page.locator('#followup').innerText(),/Keep this draft through refresh/);
  assert.equal(await page.locator('.chat-loading').count(),0);
});
test('prefetched PR details are reused after closing and reopening; Refresh bypasses them',async t=>{
  const page=await pageFor(t,prURL+'/demo/panel-performance-login');const reads=[];
  page.on('request',r=>{if(r.url().includes('/pull-request?'))reads.push(r.url());});
  const link=page.locator('.session-pull-requests [data-pr-url]').first();
  const preloaded=page.waitForResponse(r=>r.url().includes('/pull-request?'));
  await link.hover();await preloaded;
  await link.click();await page.locator('.pr-diff tr').first().waitFor();
  assert.equal(reads.length,1,'click reuses the hover response');
  await page.locator('.panel-tabs [data-close]').first().click();
  await link.click();await page.locator('.pr-diff tr').first().waitFor();
  assert.equal(reads.length,1,'reopening reuses the recent diff');
  const refreshed=page.waitForResponse(r=>r.url().includes('/pull-request?'));
  await page.locator('[data-refresh]').click();await refreshed;
  assert.equal(reads.length,2);
});
