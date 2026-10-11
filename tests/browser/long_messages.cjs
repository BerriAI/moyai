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
for (const width of [1440,768,320]) for (const reply of [false,true]) test(`complete long ${reply?'reply':'request'} at ${width}px`, async t => {
  const page = await browser.newPage({viewport:{width,height:900}});
  t.after(()=>page.close());
  const id='a'.repeat(32), text='Complete request 日志🔎 '.repeat(2000)+'final requirement';
  let sent;
  await page.route(reply ? `**/api/runs/${id}/messages` : '**/api/runs', async route => {
    if(route.request().method()!=='POST') return route.continue();
    sent=route.request().postDataJSON();
    // Test the submission boundary without starting work in the fixture.
    return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'Synthetic offline fixture'})});
  });
  await page.goto(`${base}/?fixture=skill-picker${reply?'#run='+id:''}`);
  const input=page.locator(reply?'#followup[contenteditable]':'#prompt[contenteditable]');
  await input.click(); await page.keyboard.insertText(text);
  assert.equal(await input.evaluate(e=>e.value),text);
  assert.equal(await input.evaluate(e=>e.maxLength),-1);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);
  await Promise.all([page.waitForResponse(response=>response.request().method()==='POST'&&response.status()===503),
    page.locator(reply?'#message-form [aria-label="Send message"]':'#task-form [aria-label="Start session"]').click()]);
  assert.ok((reply?sent.content:sent.prompt)===text, 'The submitted message retains every character');
});
