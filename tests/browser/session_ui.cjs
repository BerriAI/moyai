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

test('skill icons survive sending, queueing, side chats and history reloads',async t=>{
  const page=await setup(t),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  const skills=await page.evaluate(async()=>Promise.all(['personal','organization'].map(scope=>
    api('/api/skills',{method:'POST',body:JSON.stringify({name:'history-team',scope,
      icon:scope==='personal'?'team':'video',description:'Verify skill history',instructions:'Use the local demo.',
      client_id:crypto.randomUUID()})}))));
  await page.reload();await page.locator('#prompt').waitFor();
  await page.locator('#prompt').fill('Please use /personal:history-te');
  await page.locator('.skill-inline-option').waitFor();
  await page.locator('#prompt').press('Enter');
  await page.locator('#prompt .composer-skill').waitFor();
  const artwork=await page.locator('#prompt [data-skill-icon="team"]').innerHTML();
  await page.locator('#prompt').press('Enter');
  await page.locator('#followup').waitFor();
  const first=page.locator('.chat-message.user .message-content').first();
  await first.locator('[data-skill-icon="team"]').waitFor();
  assert.equal(await first.textContent(),'Please use history-team');
  assert.equal(await first.locator('[data-skill-icon="team"]').innerHTML(),artwork);
  const text='Then /org:history-team verify it.\n`/personal:history-team` <b>literal</b>';
  await page.locator('#followup').fill(text);await page.locator('#followup').press('Enter');
  await page.locator('.chat-message.user [data-skill-icon="video"]').waitFor();
  const id=await page.evaluate(()=>state.selected);
  const messages=await page.evaluate(id=>api('/api/runs/'+id),id);
  assert.equal(messages.messages.filter(m=>m.role==='user')[1].content,text);
  await page.reload();await first.locator('[data-skill-icon="team"]').waitFor();
  assert.equal(await page.locator('.chat-message.user .composer-skill').count(),2);
  assert.equal(await page.locator('.chat-message.user .message-content b').count(),0);
  assert.match(await page.locator('.chat-message.user .message-content').nth(1).textContent(),/`\/personal:history-team` <b>literal<\/b>/);
  for(const width of [1440,768,320]){
    await page.setViewportSize({width,height:900});
    assert.equal(await first.locator('[data-skill-icon="team"]').isVisible(),true);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  }
  await page.setViewportSize({width:1440,height:900});
  // Hold just execution at the API boundary to inspect the real queue renderer.
  const held=structuredClone(messages);held.status='running';
  held.messages=held.messages.filter(m=>m.role==='user');
  held.messages[0].status='running';held.messages[1].status='queued';
  await page.route(`**/api/runs/${id}*`,route=>route.fulfill({json:held}));
  await page.evaluate(()=>refreshChat(state.selected));
  await page.locator('.queued-content [data-skill-icon="video"]').waitFor();
  await page.getByRole('button',{name:'Edit queued message',exact:true}).click();
  assert.equal(await page.locator('[data-queue-edit]').inputValue(),text);
  await page.unroute(`**/api/runs/${id}*`);await page.reload();await page.locator('#followup').waitFor();
  // Side chat uses its real create/read APIs and separate transcript renderer.
  await page.evaluate(()=>workspacePanel.open('chat'));
  await page.getByRole('textbox',{name:'Message side chat',exact:true}).fill('/personal:history-team Check this separately');
  await page.getByRole('button',{name:'Send side chat message',exact:true}).click();
  await page.locator('.side-chat-messages [data-skill-icon="team"]').waitFor();
  // Archiving preserves historical recognition without adding it to the picker.
  await page.evaluate(skill=>api('/api/skills/'+skill.id+'/archive',{method:'POST',body:JSON.stringify({archived:true,revision:1})}),skills[0]);
  await page.reload();await first.locator('[data-skill-icon="team"]').waitFor();
  await page.locator('.side-chat-messages [data-skill-icon="team"]').waitFor();
  assert.deepEqual(errors,[]);
});

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
  await page.getByRole('alert').filter({hasText:'Test rejection'}).waitFor();
  assert.equal(await page.locator('.chat-message.user .message-content').textContent(),'Keep this draft until accepted');
  await page.unroute('**/api/runs',reject);
  await page.getByRole('button',{name:'Retry sending',exact:true}).click();await page.locator('#followup').waitFor();
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
  await home(page);
  await page.locator('#prompt').fill('Different next request');
  const accepted=page.waitForResponse(response=>response.url().endsWith('/api/runs')&&response.request().method()==='POST');
  release();await accepted;
  await page.waitForTimeout(100);
  assert.equal(await page.locator('#prompt').evaluate(el=>el.value),'Different next request');
});

test('accepted creation clears text even if subsequent sidebar refresh fails',async t=>{
  const page=await setup(t);
  await page.route('**/api/runs?*',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'Test sidebar unavailable'})}));
  await page.locator('#prompt').fill('Accepted despite sidebar outage');await page.locator('#prompt').press('Enter');
  await page.locator('#toast').filter({hasText:'Test sidebar unavailable'}).waitFor();
  await page.locator('#followup').waitFor();
  assert.equal(await page.locator('.chat-message.user .message-content').first().textContent(),'Accepted despite sidebar outage');
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

test('rename persists after reload, preserves messages and drafts, and rejects stale reads',{timeout:60000},async t=>{
  const page=await setup(t);
  await send(page,'Original rename request');
  await page.locator('#followup').fill('Unsent follow-up');
  let release,arrived;let count=0;
  const gate=new Promise(resolve=>release=resolve),ready=new Promise(resolve=>arrived=resolve);
  t.after(()=>release());
  const stale=async route=>{
    const response=await route.fetch();
    if(++count===2)arrived();await gate;await route.fulfill({response});
  };
  const id=await page.evaluate(()=>state.selected);
  const detail=new RegExp('/api/runs/'+id+'(?:\\?.*)?$');
  await page.route('**/api/runs?*',stale);
  await page.route(detail,stale);
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
  t.after(()=>resume());
  await page.route(detail,async route=>{const response=await route.fetch();loaded();await hold;await route.fulfill({response});});
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

test('submission is visible before save and opening never waits for a held sidebar',async t=>{
  const page=await setup(t);
  let save,sidebar;
  const saveGate=new Promise(resolve=>{save=resolve;});
  const sidebarGate=new Promise(resolve=>{sidebar=resolve;});
  t.after(()=>{save();sidebar();});
  let saved=false,sidebarHeld=false;
  await page.route('**/api/runs',async route=>{
    if(route.request().method()==='POST'){
      await saveGate;saved=true;
    }
    await route.continue();
  });
  await page.route('**/api/runs?*',async route=>{
    sidebarHeld=true;await sidebarGate;await route.continue();
  });
  await page.locator('#prompt').fill('Show this before the save finishes');
  await page.locator('#prompt').press('Enter');
  await page.getByRole('status').filter({hasText:'Creating session'}).waitFor();
  assert.equal(saved,false);
  assert.equal(await page.locator('.chat-message.user .message-content').textContent(),'Show this before the save finishes');
  save();
  await page.locator('#followup').waitFor();
  assert.equal(sidebarHeld,true);
  assert.equal(await page.locator('.chat-message.user').count(),1);
  sidebar();
});
