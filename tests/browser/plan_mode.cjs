const assert = require('node:assert/strict');
const {test, before, after} = require('node:test');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');
let server, browser, base;
before(async () => {
  server = spawn(process.execPath, ['scripts/settings_ui_preview.cjs', '--port', '0'], {stdio:['ignore','pipe','inherit']});
  base = await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.stdout.on('data', data => {const url=data.toString().match(/http:\/\/127\.0\.0\.1:\d+/); if(url)resolve(url[0]);});
  });
  browser = await chromium.launch({headless:true});
});
after(async () => {await browser?.close(); server?.kill();});
for (const width of [1440,768,320]) test(`plan mode at ${width}px`, async t => {
  const page=await browser.newPage({viewport:{width,height:1000}});
  t.after(()=>page.close());
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  for(const route of ['#tasks','#run='+ 'a'.repeat(32)]) {
    await page.goto(`${base}/?fixture=skill-picker${route}`);
    const input=page.locator(route==='#tasks'?'#prompt':'#followup');
    await input.waitFor();await input.fill('Create a rollout plan');
    const button=page.locator('button[data-plan-mode]');
    await input.press('Shift+Tab');
    assert.equal(await button.getAttribute('aria-pressed'),'true');
    assert.equal(await input.evaluate(el=>el.value),'Create a rollout plan');
    assert.equal(await input.evaluate(el=>el===document.activeElement),true);
    await input.press('Shift+Tab');
    assert.equal(await button.getAttribute('aria-pressed'),'false');
    await button.click();
    assert.equal(await button.getAttribute('aria-pressed'),'true');
    assert(await page.locator('[data-plan-hint]').isVisible());
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    let payload;
    await page.route('**/api/runs'+(route==='#tasks'?'':'/'+ 'a'.repeat(32)+'/messages'),async request=>{
      if(request.request().method()!=='POST')return request.continue();
      payload=request.request().postDataJSON();
      await request.fulfill({status:409,json:{detail:'Synthetic submission captured'}});
    });
    await input.press('Enter');
    await page.waitForFunction(()=>document.body.innerText.includes('Synthetic submission captured'));
    assert.equal(payload.plan_mode,true);
  }
  assert.deepEqual(errors,[]);
});
