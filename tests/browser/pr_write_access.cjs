// Run scripts/pr_write_access_demo.py first: real local app, controlled GitHub.
const assert=require('node:assert/strict');
const {test,before,after}=require('node:test');
const {chromium}=require('playwright');
let browser;
const base=process.env.PR_ACCESS_URL||'http://127.0.0.1:8794';
before(async()=>{browser=await chromium.launch({headless:true});});
after(async()=>{await browser?.close();});
for(const width of [1440,768,320]) for(const decision of ['Approve','Not Approve']) {
 test(`${decision} at ${width}px preserves draft and dismisses consent`,async t=>{
  const page=await browser.newPage({viewport:{width,height:1000}});
  t.after(()=>page.close());
  await page.goto(base+'/demo');
  await page.getByRole('button',{name:'Start demo chat',exact:true}).click();
  const button=page.getByRole('button',{name:decision,exact:true});
  await button.waitFor();
  assert.equal(await page.locator('#pr-write-access button').count(),2);
  const box=await button.boundingBox();
  assert.ok(box.x>=0&&box.x+box.width<=width);
  const input=page.getByRole('textbox',{name:'Message Moyai',exact:true});
  await input.fill('Keep my unsent draft');
  await button.focus();await page.keyboard.press('Enter');
  await page.waitForFunction(()=>document.querySelector('#pr-write-access').textContent==='');
  assert.equal(await input.innerText(),'Keep my unsent draft');
  const rid=new URL(page.url()).hash.slice('#run='.length);
  if(decision==='Approve') await page.locator('#conversation').getByText('Controlled GitHub results through actual broker routes:',{exact:false}).waitFor();
  const run=await (await page.request.get(`${base}/api/runs/${rid}`)).json();
  assert.equal(run.pr_write_access[0].status,decision==='Approve'?'approved':'denied');
  const continuations=run.messages.filter(m=>m.role==='user'&&m.content.includes('I approved access to PR'));
  assert.equal(continuations.length,decision==='Approve'?1:0);
  await page.reload();await input.waitFor();
  assert.equal(await page.locator('#pr-write-access').innerText(),'');
 });
}
