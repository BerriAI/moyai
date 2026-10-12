const assert = require('node:assert/strict');
const {test, before, after} = require('node:test');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');
let server, browser, base;
const id = 'a'.repeat(32);
before(async () => {
  server = spawn(process.execPath, ['scripts/settings_ui_preview.cjs', '--port', '0'], {stdio:['ignore','pipe','inherit']});
  base = await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.stdout.on('data', data => {const url=data.toString().match(/http:\/\/127\.0\.0\.1:\d+/); if(url)resolve(url[0]);});
  });
  browser = await chromium.launch({headless:true});
});
after(async () => {await browser?.close(); server?.kill();});
async function setup(t, width=1440, {failFirst=false}={}) {
  const page = await browser.newPage({viewport:{width,height:1000}});
  const errors=[], writes=[];
  page.on('pageerror', error=>errors.push(error.message));
  t.after(async()=>{await page.close(); assert.deepEqual(errors,[]);});
  const messages=[
    {id:1,role:'user',user_id:'user-0',status:'running',content:'Current request'},
    {id:2,role:'user',user_id:'user-0',status:'queued',content:'Queued correction',revision:0,queue_locked:0,attachments:[]},
  ];
  let steer=null;
  await page.route(url=>url.pathname===`/api/runs/${id}`, async route => {
    const response=await route.fetch(), run=await response.json();
    Object.assign(run,{status:'running',messages,steer_message_id:steer});
    await route.fulfill({response,json:run});
  });
  await page.route(url=>url.pathname.startsWith(`/api/runs/${id}/messages`), async route=>{
    const body=route.request().postDataJSON(); writes.push(body);
    if(failFirst&&writes.length===1){await route.fulfill({status:409,json:{detail:'Queue changed; try again.'}});return;}
    if(body.action==='steer')steer=2;
    if(body.action==='edit'){messages[1].content=body.content;messages[1].revision++;}
    await route.fulfill({json:{revision:messages[1].revision}});
  });
  await page.goto(`${base}/?fixture=skill-picker#run=${id}`);
  await page.locator('[data-queued-row="2"]').waitFor();
  return {page,writes,async refreshQueue(update=messages=>{messages[1].content='Updated queued correction';}){update(messages);await page.evaluate(id=>refreshChat(id),id);}};
}
for (const width of [1440,768,320]) {
  test(`Send now click survives tooltip hover at ${width}px`, async t=>{
    const {page,writes}=await setup(t,width);
    const button=page.locator('[data-queue-action="steer"]');
    await button.hover();
    await page.getByRole('tooltip').waitFor();
    await button.click();
    await page.waitForFunction(()=>document.querySelector('[data-queued-row]').textContent.includes('Send requested'),null,{timeout:3000});
    assert.deepEqual(writes,[{action:'steer',revision:0}]);
  });
}
for (const key of ['Control+Enter','Meta+Enter']) {
  for (const target of ['button','empty composer','draft composer']) {
    test(`${key} sends from ${target}`,async t=>{
      const {page,writes}=await setup(t);
      const input=page.locator('#followup');
      const element=target==='button'?page.locator('[data-queue-action="steer"]'):input;
      if(target==='draft composer')await input.fill('Immediate correction');
      await element.focus();
      await Promise.all([page.waitForResponse(response=>response.url().includes(`/api/runs/${id}/messages`)&&response.request().method()!=='GET',{timeout:3000}),element.press(key)]);
      assert.equal(writes.length,1);
      if(target==='draft composer'){assert.equal(writes[0].send_now,true);assert.equal(writes[0].content,'Immediate correction');}
      else assert.deepEqual(writes,[{action:'steer',revision:0}]);
    });
  }
}

for (const key of ['Control+Enter','Meta+Enter']) {
  test(`${key} still sends after a focused queue refresh`,async t=>{
    const {page,writes,refreshQueue}=await setup(t);
    const button=page.locator('[data-queue-action="steer"]');
    await button.focus();
    await refreshQueue();
    await Promise.all([page.waitForResponse(response=>response.url().includes(`/api/runs/${id}/messages`)&&response.request().method()==='PATCH',{timeout:3000}),page.keyboard.press(key)]);
    assert.deepEqual(writes,[{action:'steer',revision:0}]);
  });
}

for (const key of ['Control+Enter','Meta+Enter']) {
  test(`${key} saves an edited queued message before sending`,async t=>{
    const {page,writes}=await setup(t);
    await page.getByRole('button',{name:'Edit queued message',exact:true}).click();
    const editor=page.getByRole('textbox',{name:'Edit queued message',exact:true});
    await editor.fill('Edited correction');
    await editor.press(key);
    await page.waitForFunction(()=>document.querySelector('[data-queued-row]').textContent.includes('Send requested'));
    assert.deepEqual(writes,[{action:'edit',content:'Edited correction',revision:0},{action:'steer',revision:1}]);
  });
}
test('failed Send now re-enables the button for an explicit retry',async t=>{
  const {page,writes}=await setup(t,1440,{failFirst:true});
  const button=page.locator('[data-queue-action=steer]');
  await button.hover(); await page.getByRole('tooltip').waitFor(); await button.click();
  await page.waitForFunction(()=>document.querySelector('#toast').textContent.includes('Queue changed'));
  await page.waitForFunction(()=>!document.querySelector('[data-queue-action=steer]').disabled);
  await button.hover(); await page.getByRole('tooltip').waitFor(); await button.click();
  await page.waitForFunction(()=>document.querySelector('[data-queued-row]').textContent.includes('Send requested'));
  assert.deepEqual(writes,[{action:'steer',revision:0},{action:'steer',revision:0}]);
});
test('queue refresh does not steal composer focus or an unsent draft',async t=>{
  const {page,writes,refreshQueue}=await setup(t);
  const input=page.locator('#followup');
  await input.fill('Keep this draft');
  await refreshQueue();
  assert.equal(await input.evaluate(el=>el===document.activeElement),true);
  assert.equal(await input.evaluate(el=>el.value),'Keep this draft');
  assert.deepEqual(writes,[]);
});
test('pickup returns queue focus to the composer',async t=>{
  const {page,refreshQueue}=await setup(t);
  await page.locator('[data-queue-action=steer]').focus();
  await refreshQueue(messages=>{messages[1].status='running';});
  assert.equal(await page.locator('#followup').evaluate(el=>el===document.activeElement),true);
});
test('Delete queued message survives its tooltip',async t=>{
  const {page,writes}=await setup(t);
  const button=page.getByRole('button',{name:'Delete queued message',exact:true});
  await button.hover(); await page.getByRole('tooltip').waitFor();
  await Promise.all([page.waitForResponse(response=>response.request().method()==='PATCH'),button.click()]);
  assert.deepEqual(writes,[{action:'delete',revision:0}]);
});
test('ordinary Enter keeps the follow-up queued',async t=>{
  const {page,writes}=await setup(t);
  const input=page.locator('#followup');
  await input.fill('Next turn, not now');
  await Promise.all([page.waitForResponse(response=>response.request().method()==='POST'),input.press('Enter')]);
  assert.equal(writes.length,1);
  assert.equal(writes[0].send_now,false);
});
