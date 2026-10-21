const assert=require('node:assert/strict');
const {before,after,test}=require('node:test');
const {chromium}=require('playwright');
const path=require('node:path');
const fs=require('node:fs/promises');
const {settingsFixture,personalPath,granolaPath}=require('../fixtures/settings.cjs');
let fixture,browser;
before(async()=>{fixture=await settingsFixture();browser=await chromium.launch({headless:true});});
after(async()=>{await browser?.close();await fixture?.close();});
async function open(t,route='personal-slack',width=1440,theme='dark'){
  const page=await browser.newPage({viewport:{width,height:1000},reducedMotion:'reduce'});t.after(()=>page.close());
  const errors=[];page.on('pageerror',e=>errors.push(e.message));t.after(()=>assert.deepEqual(errors,[]));
  await page.addInitScript(theme=>localStorage.setItem('moyai-appearance',theme),theme);
  await page.goto(fixture.base+'/#settings/'+route);
  await page.locator('.ns-content h1').waitFor();return page;
}
async function noOverflow(page){
  assert.equal(await page.locator('.ns-content').evaluate(el=>el.scrollWidth<=el.clientWidth),true);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  const bounds=await page.locator('.ns-connection-card').boundingBox();
  assert.ok(bounds.x>=0&&bounds.x+bounds.width<=page.viewportSize().width);
}
for(const theme of ['dark','light'])for(const width of [1440,768,320])test(`Personal Slack has a legible connected layout at ${width}px in ${theme}`,async t=>{
  fixture.reset();fixture.state.personal.label='Example workspace with a very long account name · alex@example.test';
  const page=await open(t,'personal-slack',width,theme);
  await page.getByText('Connected',{exact:true}).waitFor();
  assert.equal(await page.locator('.ns-notice').count(),0);
  assert.equal(await page.locator('.ns-content button[data-variant="default"]').innerText(),'Start private chat');
  await noOverflow(page);
  if(width<=600)for(const button of await page.locator(".ns-connection-card button").all())assert.ok((await button.boundingBox()).height>=44);
  if(process.env.MOYAI_UI_CAPTURE_DIR){await fs.mkdir(process.env.MOYAI_UI_CAPTURE_DIR,{recursive:true});await page.screenshot({path:path.join(process.env.MOYAI_UI_CAPTURE_DIR,`personal-slack-${width}-${theme}.png`)});}
  await page.getByRole('button',{name:'Disconnect…',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'Disconnect personal Slack?'});await dialog.waitFor();
  await page.keyboard.press('Escape');await dialog.waitFor({state:'hidden'});
  await page.waitForFunction(()=>document.activeElement?.textContent==='Disconnect…');
});
test('Personal Slack checks, cancels and confirms disconnect, then reconnects through OAuth',async t=>{
  fixture.reset();const page=await open(t);
  await page.getByRole('button',{name:'Check connection',exact:true}).click();await page.getByText('Checked',{exact:true}).waitFor();
  assert.ok(fixture.state.requests.some(r=>r.path===personalPath+'/check'&&r.method==='POST'));
  await page.getByRole('button',{name:'Disconnect…',exact:true}).click();
  await page.getByRole('button',{name:'Cancel',exact:true}).click();assert.ok(!fixture.state.requests.some(r=>r.method==='DELETE'));
  await page.getByRole('button',{name:'Disconnect…',exact:true}).click();
  await page.getByRole('button',{name:'Disconnect personal Slack',exact:true}).click();
  await page.getByText('Not connected',{exact:true}).waitFor();
  await page.getByRole('button',{name:'Connect Slack',exact:true}).click();
  await page.waitForURL('**/?fixture-oauth=1#settings/personal-slack');
  assert.ok(fixture.state.requests.some(r=>r.path===personalPath+'/oauth'&&r.method==='POST'));
});
test('Personal Slack shows actionable failures and retries a failed status load',async t=>{
  fixture.reset();fixture.state.personalError=true;const page=await open(t);
  await page.getByRole('heading',{name:'Unable to load this page'}).waitFor();
  fixture.state.personalError=false;await page.getByRole('button',{name:'Try again'}).click();
  await page.getByText('Connected',{exact:true}).waitFor();fixture.state.checkError=true;
  await page.getByRole('button',{name:'Check connection',exact:true}).click();
  await page.getByRole('alert').filter({hasText:'Reconnect your Slack account'}).waitFor();
  fixture.state.checkError=false;await page.getByRole('button',{name:'Check connection',exact:true}).click();
  await page.getByText('Checked',{exact:true}).waitFor();assert.equal(await page.getByRole('alert').count(),0);
});
for(const scenario of ['expired','shared','unconfigured'])test(`Personal Slack explains ${scenario} setup without implying it is ready`,async t=>{
  fixture.reset();
  if(scenario==='expired')fixture.state.personal.health='error';
  if(scenario==='shared'){fixture.state.individual=false;fixture.state.personal={...fixture.state.personal,available:false,connected:false,health:'absent'};}
  if(scenario==='unconfigured')fixture.state.personal={...fixture.state.personal,connected:false,health:'absent',oauth_configured:false};
  const page=await open(t);
  const text=scenario==='expired'?'Reconnect to restore access.':scenario==='shared'?'Sign in with your individual work account':'Ask a workspace administrator';
  await page.locator('.ns-notice').filter({hasText:text}).waitFor();
  const connect=page.getByRole('button',{name:scenario==='expired'?'Reconnect Slack':'Connect Slack',exact:true});
  assert.equal(await connect.isEnabled(),scenario==='expired');
  if(scenario==='shared')assert.equal(await page.getByRole('button',{name:'Start private chat'}).count(),0);
});
test('Memory opens without routine banners, preserves filtering and pauses without false warnings',async t=>{
  fixture.reset();const page=await open(t,'memory');
  await page.getByRole('heading',{name:'Keep project updates concise'}).waitFor();assert.equal(await page.locator('.ns-notice').count(),0);
  await page.getByRole('searchbox').fill('no match');await page.getByRole('heading',{name:'No matching memories'}).waitFor();
  await page.getByRole('button',{name:'Clear filters',exact:true}).click();
  fixture.state.memory.runtime.background_review=false;
  await page.getByRole('button',{name:'Pause memory',exact:true}).click();await page.getByRole('button',{name:'Resume memory',exact:true}).waitFor();
  assert.equal(await page.locator('.ns-notice').count(),0);
  assert.equal(await page.getByRole('combobox',{name:'How new memories are saved'}).isEnabled(),false);
});
test('Memory reports unavailable enabled features but manual saving is not an error',async t=>{
  fixture.reset();fixture.state.memory.runtime.auto_save=false;fixture.state.memory.preferences.auto_save=false;
  const page=await open(t,'memory');await page.getByRole('heading',{name:'Memory preferences'}).waitFor();assert.equal(await page.locator('.ns-notice').count(),0);
  fixture.state.memory.runtime.recall=false;await page.reload();
  await page.locator('.ns-notice').filter({hasText:'Currently unavailable: Using saved memories.'}).waitFor();
});
test('Other settings keep useful guidance without internal migration banners',async t=>{
  fixture.reset();const page=await open(t,'skills');
  for(const route of ['skills','connections','secrets','automations','users']){
    await page.goto(fixture.base+'/#settings/'+route);
    await page.locator('.ns-toolbar').waitFor();
    assert.equal(await page.locator('.ns-notice').count(),0);
    assert.doesNotMatch(await page.locator('.ns-content').innerText(),/Internal migration|canonical|credentials stay on the server/i);
  }
});

test('Start private chat selects private visibility and normal navigation keeps its public default',async t=>{
  fixture.reset();const page=await open(t);
  await page.getByRole('button',{name:'Start private chat',exact:true}).click();
  await page.getByRole('button',{name:'Private session',exact:true}).waitFor();
  assert.equal(await page.getByRole('button',{name:'Private session',exact:true}).getAttribute('aria-pressed'),'true');
  await page.goto(fixture.base+'/#settings/personal-slack');
  await page.getByRole('button',{name:'Back to workspace',exact:true}).click();
  await page.getByRole('button',{name:'Private session',exact:true}).waitFor();
  assert.equal(await page.getByRole('button',{name:'Private session',exact:true}).getAttribute('aria-pressed'),'false');
});
for(const theme of ['dark','light'])for(const width of [1440,768,320])test(`Settings pages wrap at ${width}px in ${theme}`,async t=>{
  fixture.reset();const page=await open(t,'memory',width,theme);
  for(const route of ['memory','skills','connections','secrets','automations','users','general','overview','performance','usage']){
    await page.goto(fixture.base+'/#settings/'+route);
    await page.waitForFunction(()=>!document.querySelector('.ns-content')?.textContent.includes('Loading settings…'));
    await page.locator(route==='overview'?'.ns-overview-grid':route==='general'?'.ns-records':['performance','usage'].includes(route)?'#analytics-report-panel[aria-busy="false"]':'.ns-toolbar').waitFor();
    assert.equal(await page.locator('.ns-content').evaluate(el=>el.scrollWidth<=el.clientWidth),true,route);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,route);
    if(process.env.MOYAI_UI_CAPTURE_DIR){await fs.mkdir(process.env.MOYAI_UI_CAPTURE_DIR,{recursive:true});await page.screenshot({path:path.join(process.env.MOYAI_UI_CAPTURE_DIR,`${route}-${width}-${theme}.png`)});}
  }
});

for(const theme of ['dark','light'])for(const width of [1440,768,320])test(`Granola read-only connection fits ${width}px in ${theme}`,async t=>{
  fixture.reset();const page=await open(t,'granola',width,theme);
  await page.getByRole('heading',{name:'Your Granola notes'}).waitFor();
  await page.getByText('Connected',{exact:true}).waitFor();
  assert.match(await page.locator('.ns-connection-card').innerText(),/read-only.*private web chats/s);
  assert.match(await page.locator('.ns-connection-card').innerText(),/cannot edit notes or switch/);
  await noOverflow(page);
  if(process.env.MOYAI_UI_CAPTURE_DIR){await fs.mkdir(process.env.MOYAI_UI_CAPTURE_DIR,{recursive:true});await page.screenshot({path:path.join(process.env.MOYAI_UI_CAPTURE_DIR,`granola-${width}-${theme}.png`)});}
  await page.getByRole('button',{name:'Disconnect…',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'Disconnect Granola?'});await dialog.waitFor();
  await page.keyboard.press('Escape');await dialog.waitFor({state:'hidden'});
  await page.waitForFunction(()=>document.activeElement?.textContent==='Disconnect…');
});
test('Granola disconnect requires confirmation and OAuth uses generated client',async t=>{
  fixture.reset();const page=await open(t,'granola');
  await page.getByRole('button',{name:'Disconnect…',exact:true}).click();
  await page.getByRole('button',{name:'Cancel',exact:true}).click();assert.ok(!fixture.state.requests.some(r=>r.method==='DELETE'));
  await page.getByRole('button',{name:'Disconnect…',exact:true}).click();
  await page.getByRole('button',{name:'Disconnect Granola',exact:true}).click();
  await page.getByText('Not connected',{exact:true}).waitFor();
  await page.getByRole('button',{name:'Connect Granola',exact:true}).click();
  await page.waitForURL('**/?fixture-oauth=1#settings/granola');
  assert.ok(fixture.state.requests.some(r=>r.path===granolaPath+'/oauth'&&r.method==='POST'));
});
test('Granola has retryable errors and blocks unavailable setup',async t=>{
  fixture.reset();fixture.state.granolaError=true;const page=await open(t,'granola');
  await page.getByRole('heading',{name:'Unable to load this page'}).waitFor();
  fixture.state.granolaError=false;await page.getByRole('button',{name:'Try again'}).click();
  await page.getByText('Connected',{exact:true}).waitFor();fixture.state.granolaOAuthError=true;
  await page.getByRole('button',{name:'Reconnect Granola',exact:true}).click();
  await page.getByRole('alert').filter({hasText:'Granola access failed'}).waitFor();
  fixture.state.granola={...fixture.state.granola,connected:false,available:false};await page.reload();
  await page.getByText('Sign in with your individual work account to connect Granola.').waitFor();
  assert.equal(await page.getByRole('button',{name:'Connect Granola',exact:true}).isEnabled(),false);
  assert.equal(await page.getByRole('button',{name:'Start private chat'}).count(),0);
  fixture.state.granola={...fixture.state.granola,available:true,oauth_configured:false};await page.reload();
  await page.getByText("Ask a workspace administrator to configure Moyai's public HTTPS URL.").waitFor();
  assert.equal(await page.getByRole('button',{name:'Connect Granola',exact:true}).isEnabled(),false);
});
