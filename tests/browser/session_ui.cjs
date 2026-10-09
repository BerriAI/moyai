// Real Chromium regression coverage. Start scripts/session_ui_demo.py first.
// With Playwright installed: node --test tests/browser/session_ui.cjs
// Uses real session APIs + demo execution; only failure/delay cases intercept requests.
const assert=require('node:assert/strict');
const {test}=require('node:test');
const {chromium}=require('playwright');
const base=process.env.SESSION_UI_URL||'http://127.0.0.1:8830';
async function setup(t){
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  t.after(()=>browser.close());
  const page=await browser.newPage();
  await page.goto(base+'/demo/login');
  await page.locator('#prompt').waitFor();
  return page;
}
async function home(page){
  if(await page.locator('.settings-back').isVisible())await page.locator('.settings-back').click();
  else await page.locator('#new-task').click();
  await page.locator('#prompt').waitFor();
}
async function send(page,text,method='Enter'){
  await page.locator('#prompt').fill(text);
  if(method==='Enter')await page.locator('#prompt').press('Enter');
  else await page.getByRole('button',{name:'Start session',exact:true}).click();
  await page.locator('#followup').waitFor();
}

test('sender emails are visible for self and teammates, on desktop and mobile',async t=>{
  const page=await setup(t);
  await send(page,'Verify my sender email');
  const label=page.locator('.chat-message.user .message-label').first();
  assert.equal(await label.isVisible(),true);
  assert.match(await label.textContent(),/alex@example.com/);
  await page.reload();await page.locator('#followup').waitFor();
  assert.match(await label.textContent(),/alex@example.com/);
  const runs=await page.evaluate(()=>api('/api/runs?scope=all'));
  const teammate=runs.find(r=>r.prompt==='Local verification: sender labels');
  assert.ok(teammate);
  await page.evaluate(id=>openRun(id),teammate.id);
  assert.equal(await label.isVisible(),true);
  assert.match(await label.textContent(),/sam@example.com/);
  await page.setViewportSize({width:390,height:844});
  assert.equal(await label.isVisible(),true);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  // Exercise the real renderer's missing-identity fallback and escaping.
  await page.evaluate(()=>{const r=structuredClone(state.chatRun);r.messages[0].user_name='<img src=x onerror=alert(1)>@example.com';updateChat(r);});
  assert.equal(await label.locator('img').count(),0);
  assert.match(await label.textContent(),/<img src=x/);
  await page.evaluate(()=>{const r=structuredClone(state.chatRun);delete r.messages[0].user_name;updateChat(r);});
  assert.match(await label.textContent(),/Earlier message/);
});

for(const method of ['Enter','button'])test(`successful ${method} submission clears the input when returning home`,async t=>{
  const page=await setup(t),text=`Clear submitted text via ${method}`;
  await send(page,text,method);
  assert.equal(await page.locator('.chat-message.user .message-content').first().textContent(),text);
  await home(page);
  assert.equal(await page.locator('#prompt').evaluate(el=>el.value),'');
});

test('unsent drafts and rejected submissions retain text; retry clears only on success',async t=>{
  const page=await setup(t);
  await page.locator('#prompt').fill('Keep this draft until accepted');
  await page.locator('.nav-button[data-view="settings"]').click();
  await page.locator('.settings-back').waitFor();
  await home(page);
  assert.equal(await page.locator('#prompt').evaluate(el=>el.value),'Keep this draft until accepted');
  const reject=route=>route.request().method()==='POST'?route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'Test rejection: retry safely'})}):route.continue();
  await page.route('**/api/runs',reject);
  await page.locator('#prompt').press('Enter');
  await page.locator('#toast').filter({hasText:'Test rejection'}).waitFor();
  assert.equal(await page.locator('#prompt').evaluate(el=>el.value),'Keep this draft until accepted');
  await page.unroute('**/api/runs',reject);
  await page.locator('#prompt').press('Enter');await page.locator('#followup').waitFor();
  await home(page);
  assert.equal(await page.locator('#prompt').evaluate(el=>el.value),'');
});

test('text typed during a pending create is not discarded',async t=>{
  const page=await setup(t);
  let release,arrived;
  const waiting=new Promise(resolve=>arrived=resolve);
  const gate=new Promise(resolve=>release=resolve);
  await page.route('**/api/runs',async route=>{
    if(route.request().method()==='POST'){arrived();await gate;}
    await route.continue();
  });
  await page.locator('#prompt').fill('First request');await page.locator('#prompt').press('Enter');
  await waiting;
  await page.locator('#prompt').fill('Different next request');release();
  await page.locator('#followup').waitFor();await home(page);
  assert.equal(await page.locator('#prompt').evaluate(el=>el.value),'Different next request');
});

test('accepted creation clears text even if subsequent sidebar refresh fails',async t=>{
  const page=await setup(t);
  await page.route('**/api/runs?*',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'Test sidebar unavailable'})}));
  await page.locator('#prompt').fill('Accepted despite sidebar outage');await page.locator('#prompt').press('Enter');
  await page.locator('#toast').filter({hasText:'Test sidebar unavailable'}).waitFor();
  assert.equal(await page.locator('#prompt').evaluate(el=>el.value),'');
  await page.unroute('**/api/runs?*');
  await home(page);assert.equal(await page.locator('#prompt').evaluate(el=>el.value),'');
});

async function renameDialog(page){
  const id=await page.evaluate(()=>state.selected);
  await page.locator(`[data-session-actions="${id}"]`).click();
  await page.locator('#session-actions').getByRole('button',{name:'Rename',exact:true}).click();
  await page.getByLabel('Session name',{exact:true}).waitFor();
  return page.locator('#session-folder-dialog');
}

test('session deletion dialog preserves cancellation, errors and locked shadcn controls during cleanup',async t=>{
  const page=await setup(t);
  await send(page,'Verify deletion through the migrated controls');
  const id=await page.evaluate(()=>state.selected);
  const dialog=page.getByRole('dialog',{name:'Delete session?'});
  const openDelete=async()=>{
    await page.getByRole('button',{name:'Session actions',exact:true}).click();
    await page.locator('[data-delete-session]').click();
    await dialog.waitFor();
  };
  const requests=[];
  let allow=false;
  await page.route(`**/api/runs/${id}`,route=>{
    if(route.request().method()!=='DELETE')return route.continue();
    requests.push(route.request());
    return allow?route.fulfill({status:202,json:{deleted:false}}):route.fulfill({status:503,json:{detail:'Synthetic cleanup rejection'}});
  });
  await openDelete();
  assert.equal(await dialog.getByRole('button',{name:'Cancel',exact:true}).evaluate(el=>el===document.activeElement),true);
  await page.keyboard.press('Escape');
  await dialog.waitFor({state:'detached'});
  assert.equal(requests.length,0);
  await openDelete();
  await dialog.getByRole('button',{name:'Delete session',exact:true}).click();
  await dialog.getByText('Synthetic cleanup rejection',{exact:true}).waitFor();
  assert.equal(await dialog.getByRole('button',{name:'Delete session',exact:true}).isEnabled(),true);
  allow=true;
  await dialog.getByRole('button',{name:'Delete session',exact:true}).click();
  await dialog.waitFor({state:'detached'});
  await page.waitForFunction(()=>document.querySelector('#message-form').inert);
  const picker=page.locator('#message-form [data-skill-picker]');
  await page.waitForFunction(el=>el.disabled,await picker.elementHandle());
  assert.equal(await page.locator('#followup').getAttribute('contenteditable'),'false');
  assert.equal(await page.locator('#message-form [type=submit]').first().isDisabled(),true);
  await page.evaluate(()=>updateChatStatus({...state.chatRun,status:'idle'}));
  assert.equal(await picker.isDisabled(),true,'An older response cannot unlock a deleting session');
  assert.equal(await page.locator('#message-form').evaluate(el=>el.inert),true);
  assert.equal(requests.length,2);
});

test('rename persists after reload, preserves messages and drafts, and rejects stale reads',async t=>{
  const page=await setup(t);
  await send(page,'Original rename request');
  await page.locator('#followup').fill('Unsent follow-up');
  let release,arrived;let count=0;
  const gate=new Promise(resolve=>release=resolve),ready=new Promise(resolve=>arrived=resolve);
  const stale=async route=>{
    const response=await route.fetch();
    if(++count===2)arrived();await gate;await route.fulfill({response});
  };
  const id=await page.evaluate(()=>state.selected);
  await page.route('**/api/runs?*',stale);
  await page.route(`**/api/runs/${id}`,stale);
  await page.evaluate(()=>{refreshRuns();refreshChat(state.selected);});await ready;
  const dialog=await renameDialog(page);
  await page.getByLabel('Session name',{exact:true}).fill('Navigation follow-up');
  await dialog.getByRole('button',{name:'Save',exact:true}).click();
  await page.locator('#toast').filter({hasText:'Session renamed.'}).waitFor();
  release();await page.unrouteAll({behavior:'wait'});
  assert.equal(await page.locator('#page-title').textContent(),'Navigation follow-up');
  assert.equal(await page.title(),'Navigation follow-up · Moyai');
  assert.equal(await page.locator(`[data-run="${id}"] .session-link-title`).textContent(),'Navigation follow-up');
  assert.equal(await page.locator('#followup').evaluate(el=>el.value),'Unsent follow-up');
  await page.reload();await page.locator('#followup').waitFor();
  assert.equal(await page.locator('#page-title').textContent(),'Navigation follow-up');
  assert.equal(await page.locator('.chat-message.user .message-content').first().textContent(),'Original rename request');
  let resume,loaded;
  const opening=new Promise(resolve=>loaded=resolve),hold=new Promise(resolve=>resume=resolve);
  await page.route(`**/api/runs/${id}`,async route=>{const response=await route.fetch();loaded();await hold;await route.fulfill({response});});
  await page.evaluate(id=>{openRun(id);},id);await opening;
  const duringOpen=await renameDialog(page);
  await page.getByLabel('Session name',{exact:true}).fill('Renamed while opening');
  await duringOpen.getByRole('button',{name:'Save',exact:true}).click();
  await duringOpen.waitFor({state:'hidden'});resume();await page.unrouteAll({behavior:'wait'});
  await page.waitForFunction(()=>state.chatRun?.display_title==='Renamed while opening');
  assert.equal(await page.locator('#page-title').textContent(),'Renamed while opening');
  await page.locator(`[data-session-actions="${id}"]`).click();
  await page.locator('#session-actions').getByRole('button',{name:'Move to folder'}).click();
  assert.equal(await page.getByRole('heading',{name:'Move session',exact:true}).isVisible(),true);
});

test('rename supports cancel, Escape, blank validation, failure retry and literal text',async t=>{
  const page=await setup(t);await send(page,'Rename error recovery');
  let dialog=await renameDialog(page);
  await page.getByLabel('Session name',{exact:true}).fill('Cancelled name');
  await dialog.getByRole('button',{name:'Cancel',exact:true}).click();
  assert.equal(await page.locator('#page-title').textContent(),'Rename error recovery');
  dialog=await renameDialog(page);await page.getByLabel('Session name',{exact:true}).press('Escape');
  await dialog.waitFor({state:'hidden'});
  assert.equal(await dialog.isVisible(),false);
  dialog=await renameDialog(page);
  await page.getByLabel('Session name',{exact:true}).fill('   ');
  await dialog.getByRole('button',{name:'Save',exact:true}).click();
  assert.equal(await dialog.getByRole('alert').textContent(),'Enter a session name.');
  await page.route('**/api/runs/*/title',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'Temporary save failure'})}));
  await page.getByLabel('Session name',{exact:true}).fill('<b>Literal name</b>');
  await dialog.getByRole('button',{name:'Save',exact:true}).click();
  await dialog.getByRole('alert').filter({hasText:'Temporary save failure'}).waitFor();
  assert.equal(await page.getByLabel('Session name',{exact:true}).inputValue(),'<b>Literal name</b>');
  assert.equal(await page.locator('#page-title').textContent(),'Rename error recovery');
  await page.unroute('**/api/runs/*/title');
  await dialog.getByRole('button',{name:'Save',exact:true}).click();
  await page.locator('#toast').filter({hasText:'Session renamed.'}).waitFor();
  assert.equal(await page.locator('#page-title').textContent(),'<b>Literal name</b>');
  assert.equal(await page.locator('#page-title b').count(),0);
  dialog=await renameDialog(page);
  await page.evaluate(()=>api('/api/runs/'+state.selected+'/title',{method:'PUT',body:JSON.stringify({title:'Changed in another tab',expected_title:state.chatRun.display_title})}));
  await page.getByLabel('Session name',{exact:true}).fill('My stale draft');
  await dialog.getByRole('button',{name:'Save',exact:true}).click();
  await dialog.getByRole('alert').filter({hasText:'This session name changed'}).waitFor();
  await dialog.getByRole('button',{name:'Cancel',exact:true}).click();
  dialog=await renameDialog(page);
  assert.equal(await page.getByLabel('Session name',{exact:true}).inputValue(),'Changed in another tab');
});

test('session actions and rename remain usable at desktop, tablet and narrow mobile sizes',async t=>{
  const page=await setup(t);await send(page,'Responsive session rename');
  for(const width of [1440,768,320]){
    await page.setViewportSize({width,height:900});
    if(width<850)await page.locator('#open-sidebar').click();
    const id=await page.evaluate(()=>state.selected),trigger=page.locator(`[data-session-actions="${id}"]`);
    await trigger.focus();await trigger.press('Enter');
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('ArrowDown');
    assert.equal(await page.evaluate(()=>document.activeElement.textContent),'Move to folder');
    await page.keyboard.press('ArrowUp');await page.keyboard.press('Enter');
    const dialog=page.locator('[data-dialog-id="session-folder-dialog"]'),box=await dialog.boundingBox();
    assert.ok(box.x>=0&&box.x+box.width<=width);
    assert.equal(await page.getByLabel('Session name',{exact:true}).evaluate(el=>el===document.activeElement),true);
    await page.getByLabel('Session name',{exact:true}).fill('Renamed at '+width);
    await dialog.getByRole('button',{name:'Save',exact:true}).click();
    await dialog.waitFor({state:'hidden'});
    assert.equal(await page.locator('#page-title').textContent(),'Renamed at '+width);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    if(width<850)await page.locator('#close-sidebar').click();
  }
});
