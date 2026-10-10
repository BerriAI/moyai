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
for (const width of [1440,768,320]) test(`composer message history at ${width}px`, async t => {
  const page = await browser.newPage({viewport:{width,height:1000}});
  t.after(()=>page.close());
  const errors=[];
  page.on('pageerror', error=>errors.push(error.message));
  t.after(()=>assert.deepEqual(errors,[]));
  const id='a'.repeat(32);
  await page.route(url=>url.pathname===`/api/runs/${id}`, async route => {
    const response=await route.fetch(), run=await response.json();
    run.messages=[
      {id:1,role:'user',user_id:'user-0',status:'completed',content:'First request'},
      {id:2,role:'assistant',status:'completed',content:'Assistant reply'},
      {id:3,role:'user',user_id:'another-user',status:'completed',content:'Teammate request'},
      {id:4,role:'user',user_id:'user-0',status:'completed',content:'Latest request'},
    ];
    await route.fulfill({response,json:run});
  });
  await page.goto(`${base}/?fixture=skill-picker#run=${id}`);
  const input=page.locator('#followup');
  await input.waitFor();
  const value=()=>input.evaluate(el=>el.value);
  const caret=pos=>input.evaluate((el,pos)=>el.setSelectionRange(pos,pos),pos);
  await input.fill('Unsent draft'); await caret(0);
  await input.press('ArrowUp'); assert.equal(await value(),'Latest request');
  await input.press('ArrowUp'); assert.equal(await value(),'First request');
  await input.press('ArrowUp'); assert.equal(await value(),'First request');
  await input.press('ArrowDown'); assert.equal(await value(),'Latest request');
  await input.press('ArrowDown'); assert.equal(await value(),'Unsent draft');
  // Edits to a recalled entry survive browsing without replacing the draft.
  await caret(0); await input.press('ArrowUp');
  await input.fill('Edited recall'); await caret(0);
  await input.press('ArrowUp'); assert.equal(await value(),'First request');
  await input.press('ArrowDown'); assert.equal(await value(),'Edited recall');
  await input.press('ArrowDown'); assert.equal(await value(),'Unsent draft');
  // Interior caret positions, selections and modifiers keep native editing.
  await input.fill('First line\nSecond line'); await caret(15);
  await input.press('ArrowUp'); assert.equal(await value(),'First line\nSecond line');
  await caret(0); await input.press('Shift+ArrowUp'); assert.equal(await value(),'First line\nSecond line');
  await input.evaluate(el=>el.setSelectionRange(0,5));
  await input.press('ArrowUp'); assert.equal(await value(),'First line\nSecond line');
  // Clearing after a send resets the snapshot; recall then returns to empty.
  await input.fill(''); await input.press('ArrowUp'); assert.equal(await value(),'Latest request');
  await input.press('ArrowDown'); assert.equal(await value(),'');
  // The slash picker owns arrow navigation while open.
  await input.fill('/team');
  await page.waitForFunction(()=>document.querySelectorAll('.skill-inline-option').length>0);
  await input.press('ArrowUp'); assert.equal(await value(),'/team');
  // A different conversation never inherits this composer's browsing state.
  await page.goto(`${base}/?fixture=skill-picker#run=${'b'.repeat(32)}`);
  await page.locator('#followup').waitFor();
  await page.locator('#followup').press('ArrowUp');
  assert.equal(await value(),'');
});
