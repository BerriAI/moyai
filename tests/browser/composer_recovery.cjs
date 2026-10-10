// Real component interactions against isolated synthetic API responses.
const assert=require('node:assert/strict');
const {test,before,after}=require('node:test');
const {spawn}=require('node:child_process');
const {chromium}=require('playwright');
let server,browser,base;
before(async()=>{
 server=spawn(process.execPath,['scripts/settings_ui_preview.cjs','--port','0'],{stdio:['ignore','pipe','inherit']});
 base=await new Promise((resolve,reject)=>{server.once('error',reject);server.stdout.on('data',s=>{const m=s.toString().match(/http:\/\/127\.0\.0\.1:\d+/);if(m)resolve(m[0]);});});
 browser=await chromium.launch({headless:true});
});
after(async()=>{await browser?.close();server?.kill();});
async function pageFor(t,width=1440){
 const page=await browser.newPage({viewport:{width,height:900}});
 t.after(()=>page.close());await page.goto(base);await page.locator('#prompt').waitFor();return page;
}
for(const width of [1440,768,320]){
 test(`collapsed invalid repository opens for correction at ${width}`,async t=>{
  const p=await pageFor(t,width);let writes=0;p.on('request',r=>{if(r.method()==='POST'&&r.url().endsWith('/api/runs'))writes++;});
  await p.locator('#prompt').fill('Investigate the failed import');
  await p.getByLabel('Session options',{exact:true}).click();await p.locator('#repo').fill('example/project');
  await p.getByLabel('Session options',{exact:true}).click();await p.getByRole('button',{name:'Start session',exact:true}).click();
  await p.waitForTimeout(250);
  assert(await p.locator('#repo').isVisible());assert(await p.locator('#repo').evaluate(e=>document.activeElement===e));
  assert.equal(writes,0);assert.equal(await p.locator('#prompt').evaluate(e=>e.value),'Investigate the failed import');
 });
 test(`model draft survives navigation at ${width}`,async t=>{
  const p=await pageFor(t,width);
  await p.route('**/api/config',async r=>{const response=await r.fetch(),data=await response.json();data.models.push({id:'synthetic-model',name:'Synthetic model',default_harness:'codex'});await r.fulfill({response,json:data});});
  await p.reload();await p.locator('#prompt').fill('Retain my chosen model');
  await p.locator('#new-model').locator('..').getByRole('combobox').click();await p.getByRole('option',{name:'Synthetic model',exact:true}).click();
  await p.evaluate(()=>location.hash='connections');await p.locator('#prompt').waitFor({state:'detached'});
  await p.evaluate(()=>location.hash='tasks');await p.locator('#prompt').waitFor();
  assert.equal(await p.locator('#new-model').inputValue(),'synthetic-model');
  assert.equal(await p.locator('#prompt').evaluate(e=>e.value),'Retain my chosen model');
 });
}
