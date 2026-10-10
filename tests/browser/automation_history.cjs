const assert=require('node:assert/strict');
const {test,before,after}=require('node:test');
const {spawn}=require('node:child_process');
const {chromium}=require('playwright');
let server,browser,base;
before(async()=>{
  server=spawn(process.execPath,['scripts/settings_ui_preview.cjs','--port','0'],{stdio:['ignore','pipe','inherit']});
  base=await new Promise((resolve,reject)=>{server.once('error',reject);server.stdout.on('data',data=>{const url=data.toString().match(/http:\/\/127\.0\.0\.1:\d+/);if(url)resolve(url[0]);});});
  browser=await chromium.launch({headless:true});
});
after(async()=>{await browser?.close();server?.kill();});
for(const width of [1440,768,320]) test(`complete automation editor and history at ${width}px`,async t=>{
  const page=await browser.newPage({viewport:{width,height:950}});t.after(()=>page.close());
  const history=Array.from({length:26},(_,i)=>({occurrence:`fixture-${String(26-i).padStart(2,'0')}`,created_at:'2026-10-10T12:00:00Z',status:'idle',outcome:'started',run_id:(i+1).toString(16).padStart(32,'0')}));
  let saved;
  await page.route(/\/api\/automations(?:[/?]|$)/,async route=>{
    const request=route.request(),url=new URL(request.url());
    if(request.method()==='PUT'){saved=request.postDataJSON();return route.fulfill({json:{}});}
    if(url.pathname.endsWith('/history'))return route.fulfill({json:{history:history.slice(20),next_cursor:null}});
    const response=await route.fetch(),data=await response.json();
    data.automations[0].history=history.slice(0,20);data.automations[0].history_cursor='fixture-cursor';
    await route.fulfill({json:data});
  });
  await page.goto(base+'/#automations');
  const card=page.locator('.automation-card').first();
  await card.locator('.automation-history > summary').click();
  await card.getByRole('button',{name:'Load earlier runs',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.automation-history [data-history-count]')?.textContent==='26 shown');
  assert.equal(await card.locator('.automation-history-row').count(),26);
  assert.equal(await card.getByRole('button',{name:'Load earlier runs',exact:true}).isVisible(),false);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);
  if(process.env.AUTOMATION_SCREENSHOT_DIR)await page.screenshot({path:`${process.env.AUTOMATION_SCREENSHOT_DIR}/history-${width}.png`});
  await card.getByRole('button',{name:'Edit',exact:true}).click();
  const dialog=page.locator('[data-slot="dialog-content"]'),prompt=dialog.locator('[name=prompt]');
  const text='Complete instructions 漢🙂 '.repeat(1300)+'END';
  assert.equal(await prompt.getAttribute('maxlength'),null);
  await prompt.fill(text);
  for(let i=0;i<21;i++){
    await dialog.locator('[data-add-metadata]').click();
    await dialog.locator('[name=metadata_key]').nth(i).fill('key-'+i);
    await dialog.locator('[name=metadata_value]').nth(i).fill(i===0?'Complete metadata '.repeat(1200)+'END':'Value '+i);
  }
  assert.equal(await dialog.locator('[data-add-metadata]').isEnabled(),true);
  for(let i=0;i<20;i++)await dialog.locator('[data-add-trigger]').click();
  assert.equal(await dialog.locator('[data-triggers] > *').count(),21);
  assert.equal(await dialog.locator('[data-add-trigger]').isEnabled(),true);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);
  await dialog.getByRole('button',{name:'Save paused',exact:true}).click();
  await page.waitForFunction(()=>!document.querySelector('[data-slot="dialog-content"]'));
  assert.equal(saved.definition.prompt,text);
  assert.equal(saved.definition.metadata['key-0'],'Complete metadata '.repeat(1200)+'END');
  assert.equal(Object.keys(saved.definition.metadata).length,21);
  assert.equal(saved.definition.triggers.length,21);
});
