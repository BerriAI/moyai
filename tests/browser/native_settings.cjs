const assert=require('node:assert/strict');
const {before,after,test}=require('node:test');
const {chromium}=require('playwright');
const path=require('node:path');
const fs=require('node:fs/promises');
const {settingsFixture,personalPath,mcpConnection}=require('../fixtures/settings.cjs');
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
    await page.locator('.ns-toolbar').first().waitFor();
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
    await page.locator(route==='overview'?'.ns-overview-grid':route==='general'?'.ns-records':['performance','usage'].includes(route)?'#analytics-report-panel[aria-busy="false"]':'.ns-toolbar').first().waitFor();
    assert.equal(await page.locator('.ns-content').evaluate(el=>el.scrollWidth<=el.clientWidth),true,route);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,route);
    if(process.env.MOYAI_UI_CAPTURE_DIR){await fs.mkdir(process.env.MOYAI_UI_CAPTURE_DIR,{recursive:true});await page.screenshot({path:path.join(process.env.MOYAI_UI_CAPTURE_DIR,`${route}-${width}-${theme}.png`)});}
  }
});

const mcpSection=page=>page.getByRole('region',{name:'MCP connections',exact:true});
const connectionWrites=()=>fixture.state.requests.filter(r=>r.path.startsWith('/api/connections')&&['POST','PUT'].includes(r.method)&&!r.path.endsWith('/check'));
test('Hosted connections retain organization integrations and expose only custom MCP rows and service choices',async t=>{
  fixture.reset();
  fixture.state.connections=[mcpConnection(),mcpConnection({id:'shared',name:'Workspace gateway',shared:true,owner_id:'another-admin'}),mcpConnection({id:'legacy-slack',kind:'slack',name:'Canonical Slack'})];
  const page=await open(t,'connections');
  await page.getByRole('region',{name:'Organization connections',exact:true}).getByRole('heading',{name:'Slack',exact:true}).waitFor();
  const section=mcpSection(page);
  await section.getByRole('heading',{name:'Research gateway'}).waitFor();
  assert.equal(await section.getByRole('heading',{name:'Canonical Slack'}).count(),0);
  const shared=section.locator('article').filter({hasText:'Workspace gateway'});
  assert.equal(await shared.getByRole('button',{name:/Edit|Disable/}).count(),0);
  await shared.getByRole('button',{name:/Check connection/}).waitFor();
  await section.getByRole('searchbox').fill('no match');
  await section.getByRole('heading',{name:'No matching MCP connections'}).waitFor();
  await section.getByRole('button',{name:'Clear filters',exact:true}).click();
  await section.getByRole('heading',{name:'Workspace gateway'}).waitFor();
  await section.getByRole('button',{name:'Add MCP connection',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'Add MCP connection',exact:true});
  await dialog.getByRole('combobox',{name:'Service',exact:true}).click();
  assert.equal(await page.getByRole('option').count(),1);
  await page.getByRole('option',{name:'MCP server / gateway',exact:true}).click();
  assert.equal(await dialog.getByLabel('Bearer token (optional)',{exact:true}).getAttribute('required'),null);
  assert.equal(await dialog.getByLabel('Server URL',{exact:true}).getAttribute('type'),'url');
  assert.match(await dialog.innerText(),/https:\/\/gateway\.example\.com\/mcp/);
  await page.keyboard.press('Escape');await dialog.waitFor({state:'hidden'});
  await page.waitForFunction(()=>document.activeElement?.textContent==='Add MCP connection');
});

for(const bearer of ['', 'synthetic-bearer'])test(`Hosted MCP creation supports ${bearer?'bearer':'no'} authentication and preserves default policy`,async t=>{
  fixture.reset();const page=await open(t,'connections');
  await mcpSection(page).getByRole('button',{name:'Add MCP connection',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'Add MCP connection',exact:true});
  await dialog.getByLabel('Account name',{exact:true}).fill('My MCP gateway');
  await dialog.getByLabel('Server URL',{exact:true}).fill('https://gateway.example.com/mcp');
  if(bearer)await dialog.getByLabel('Bearer token (optional)',{exact:true}).fill(bearer);
  await dialog.getByRole('button',{name:'Save',exact:true}).click();
  await dialog.waitFor({state:'hidden'});
  await mcpSection(page).getByRole('heading',{name:'My MCP gateway',exact:true}).waitFor();
  const [write]=connectionWrites();
  assert.equal(connectionWrites().length,1);assert.equal(write.method,'POST');
  assert.deepEqual(write.body,{name:'My MCP gateway',kind:'mcp',credential:bearer,clear_credential:false,shared:false,read_only:true,enabled:true,policy:{require_approval:true,mcp:{url:'https://gateway.example.com/mcp',transport:'streamable_http'}}});
  if(bearer)assert.doesNotMatch(await page.locator('.ns-content').innerText(),/synthetic-bearer/);
});

test('MCP edits preserve policies and blank tokens, and URL changes require rotation or explicit clear',async t=>{
  fixture.reset();const original=mcpConnection({shared:true,read_only:false});
  original.policy.require_approval=false;fixture.state.connections=[original];
  const page=await open(t,'connections'),section=mcpSection(page);
  await section.getByRole('button',{name:'Edit Research gateway',exact:true}).click();
  let dialog=page.getByRole('dialog',{name:'Edit connection',exact:true});
  assert.equal(await dialog.getByLabel('Replacement bearer token',{exact:true}).inputValue(),'');
  assert.equal(await dialog.getByLabel('Clear saved token',{exact:true}).isChecked(),false);
  await dialog.getByLabel('Account name',{exact:true}).fill('Renamed gateway');
  await dialog.getByRole('button',{name:'Save',exact:true}).click();await dialog.waitFor({state:'hidden'});
  let writes=connectionWrites();assert.equal(writes.length,1);assert.equal(writes[0].body.credential,'');
  assert.equal(writes[0].body.clear_credential,false);assert.deepEqual(writes[0].body.policy,original.policy);
  assert.equal(writes[0].body.shared,true);assert.equal(writes[0].body.read_only,false);

  await section.getByRole('button',{name:'Edit Renamed gateway',exact:true}).click();
  dialog=page.getByRole('dialog',{name:'Edit connection',exact:true});
  await dialog.getByLabel('Server URL',{exact:true}).fill('https://replacement.example.com/mcp');
  await dialog.getByRole('button',{name:'Save',exact:true}).click();
  await dialog.getByRole('alert').filter({hasText:'requires a replacement bearer token'}).waitFor();
  assert.equal(connectionWrites().length,1);
  await dialog.getByLabel('Replacement bearer token',{exact:true}).fill('replacement-synthetic-token');
  assert.equal(await dialog.getByLabel('Clear saved token',{exact:true}).isEnabled(),false);
  await dialog.getByRole('button',{name:'Save',exact:true}).click();await dialog.waitFor({state:'hidden'});
  writes=connectionWrites();assert.equal(writes.length,2);assert.equal(writes[1].body.credential,'replacement-synthetic-token');
  assert.equal(writes[1].body.policy.mcp.url,'https://replacement.example.com/mcp');
  assert.deepEqual(writes[1].body.policy.allowed_tools,['lookup']);assert.equal(writes[1].body.policy.require_approval,false);

  await section.getByRole('button',{name:'Edit Renamed gateway',exact:true}).click();
  dialog=page.getByRole('dialog',{name:'Edit connection',exact:true});
  await dialog.getByLabel('Server URL',{exact:true}).fill('https://public.example.com/mcp');
  await dialog.getByLabel('Clear saved token',{exact:true}).check();
  assert.equal(await dialog.getByLabel('Replacement bearer token',{exact:true}).isEnabled(),false);
  await dialog.getByRole('button',{name:'Save',exact:true}).click();await dialog.waitFor({state:'hidden'});
  writes=connectionWrites();assert.equal(writes.length,3);assert.equal(writes[2].body.credential,'');assert.equal(writes[2].body.clear_credential,true);
  assert.equal(writes[2].body.policy.mcp.url,'https://public.example.com/mcp');
});

test('MCP save failures keep the draft, and disabling preserves policy and credential',async t=>{
  fixture.reset();fixture.state.connections=[mcpConnection()];const page=await open(t,'connections'),section=mcpSection(page);
  await section.getByRole('button',{name:'Edit Research gateway',exact:true}).click();
  const editor=page.getByRole('dialog',{name:'Edit connection',exact:true});
  await editor.getByLabel('Account name',{exact:true}).fill('Preserved draft');
  fixture.state.connectionSaveError='The endpoint is not permitted.';
  await editor.getByRole('button',{name:'Save',exact:true}).click();
  await editor.getByRole('alert').filter({hasText:'The endpoint is not permitted.'}).waitFor();
  assert.equal(await editor.getByLabel('Account name',{exact:true}).inputValue(),'Preserved draft');
  fixture.state.connectionSaveError=null;
  await editor.getByRole('button',{name:'Cancel',exact:true}).click();
  await section.getByRole('button',{name:'Disable Research gateway',exact:true}).click();
  const confirm=page.getByRole('dialog',{name:'Disable Research gateway?',exact:true});
  await confirm.getByRole('button',{name:'Disable connection',exact:true}).click();await confirm.waitFor({state:'hidden'});
  await section.getByText('MCP server / gateway · Disabled',{exact:true}).waitFor();
  const disabled=connectionWrites().at(-1).body;
  assert.equal(disabled.enabled,false);assert.equal(disabled.credential,'');assert.equal(disabled.clear_credential,false);
  assert.deepEqual(disabled.policy,fixture.state.connections[0].policy);
  assert.equal(await section.getByRole('button',{name:/Check connection/}).count(),0);
});

test('MCP tool preview sanitizes failures, retries, escapes descriptions and distinguishes an empty catalog',async t=>{
  fixture.reset();fixture.state.connections=[mcpConnection()];
  fixture.state.connectionCheckError='Authorization: Bearer NEVER_RENDER_THIS https://private.example.test/?token=secret';
  const page=await open(t,'connections');
  await mcpSection(page).getByRole('button',{name:'Check connection Research gateway',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'Check Research gateway',exact:true});
  await dialog.getByRole('alert').filter({hasText:'Verify the server URL and bearer token'}).waitFor();
  assert.doesNotMatch(await dialog.innerText(),/NEVER_RENDER_THIS|private\.example\.test|token=secret/);
  fixture.state.connectionCheckError=null;
  fixture.state.connectionCheck={tool_count:2,tools:[{name:'lookup',description:'<img src=x onerror=bad()>'},{name:'read_status',description:null}]};
  await dialog.getByRole('button',{name:'Try again',exact:true}).click();
  await dialog.getByRole('status').filter({hasText:'2 tools available'}).waitFor();
  await dialog.getByText('<img src=x onerror=bad()>',{exact:true}).waitFor();
  assert.equal(await dialog.locator('img').count(),0);assert.equal(await dialog.getByRole('listitem').count(),2);
  fixture.state.connectionCheck={tool_count:0,tools:[]};
  await dialog.getByRole('button',{name:'Check again',exact:true}).click();
  await dialog.getByText('0 tools available',{exact:true}).waitFor();
  await dialog.getByText('No tools are available under this connection’s policy.',{exact:false}).waitFor();
  assert.equal(connectionWrites().length,0);
  await page.keyboard.press('Escape');await dialog.waitFor({state:'hidden'});
  await page.waitForFunction(()=>document.activeElement?.textContent.includes('Check connection'));
});

test('A late MCP check cannot replace a new connection preview or a different settings page',async t=>{
  fixture.reset();fixture.state.connections=[mcpConnection(),mcpConnection({id:'22345678-1234-1234-1234-123456789abc',name:'Second gateway'})];
  let release;fixture.state.connectionCheckGate=new Promise(resolve=>{release=resolve;});t.after(()=>release());
  const page=await open(t,'connections'),section=mcpSection(page);
  await section.getByRole('button',{name:'Check connection Research gateway',exact:true}).click();
  await page.getByRole('dialog').getByText('Checking connection…',{exact:true}).waitFor();
  await page.getByRole('dialog').getByRole('button',{name:'Close',exact:true}).first().click();
  fixture.state.connectionCheckGate=null;
  await section.getByRole('button',{name:'Check connection Second gateway',exact:true}).click();
  await page.getByRole('dialog',{name:'Check Second gateway',exact:true}).getByText('1 tool available',{exact:true}).waitFor();
  await page.goto(fixture.base+'/#settings/general');
  await page.locator('.ns-content h1').filter({hasText:'General'}).waitFor();
  release();
  assert.equal(await page.getByRole('dialog').count(),0);
});

test('Revoked MCP access clears an open preview even when the actor and role stay the same',async t=>{
  fixture.reset();fixture.state.connections=[mcpConnection({shared:true,owner_id:'another-admin'})];
  const page=await open(t,'connections'),section=mcpSection(page);
  await section.getByRole('button',{name:'Check connection Research gateway',exact:true}).click();
  await page.getByRole('dialog').getByText('1 tool available',{exact:true}).waitFor();
  fixture.state.connections=[];
  await page.evaluate(()=>window.dispatchEvent(new Event('moyai:access-changed')));
  await page.getByRole('dialog').waitFor({state:'hidden'});
  await section.getByRole('heading',{name:'No MCP connections yet',exact:true}).waitFor();
  assert.equal(await section.getByRole('heading',{name:'Research gateway',exact:true}).count(),0);
});

test('Connection role and individual prop changes clear drafts, previews and confirmations without an access event',async t=>{
  fixture.reset();fixture.state.connections=[mcpConnection()];
  const page=await browser.newPage({viewport:{width:1440,height:1000}});t.after(()=>page.close());
  const errors=[];page.on('pageerror',error=>errors.push(error.message));t.after(()=>assert.deepEqual(errors,[]));
  await page.goto(fixture.base+'/settings-props.html');
  const section=mcpSection(page);
  const updateUser=patch=>page.evaluate(patch=>window.renderSettingsUser({id:'viewer',name:'Alex Example',role:'admin',individual:true,...patch}),patch);
  await section.getByRole('button',{name:'Edit Research gateway',exact:true}).click();
  await page.getByRole('dialog').getByLabel('Replacement bearer token',{exact:true}).fill('transient-synthetic-token');
  let reads=fixture.state.requests.filter(r=>r.path==='/api/connections').length;
  await updateUser({role:'member'});
  await page.getByRole('dialog').waitFor({state:'hidden'});
  await section.getByRole('button',{name:'Edit Research gateway',exact:true}).waitFor();
  assert.ok(fixture.state.requests.filter(r=>r.path==='/api/connections').length>reads);
  await section.getByRole('button',{name:'Edit Research gateway',exact:true}).click();
  assert.equal(await page.getByRole('dialog').getByLabel('Replacement bearer token',{exact:true}).inputValue(),'');
  await page.getByRole('dialog').getByRole('button',{name:'Cancel',exact:true}).click();
  await section.getByRole('button',{name:'Check connection Research gateway',exact:true}).click();
  await page.getByRole('dialog').getByText('1 tool available',{exact:true}).waitFor();
  await updateUser({role:'member',individual:false});
  await page.getByRole('dialog').waitFor({state:'hidden'});
  await section.getByText('Sign in with your individual work account to add or edit connections.',{exact:true}).waitFor();
  await updateUser({});
  await section.getByRole('button',{name:'Disable Research gateway',exact:true}).click();
  await page.getByRole('dialog',{name:'Disable Research gateway?',exact:true}).waitFor();
  fixture.state.connections=[mcpConnection({owner_id:'next-viewer'})];
  reads=fixture.state.requests.filter(r=>r.path==='/api/connections').length;
  await updateUser({id:'next-viewer'});
  await page.getByRole('dialog').waitFor({state:'hidden'});
  await section.getByRole('button',{name:'Edit Research gateway',exact:true}).waitFor();
  assert.ok(fixture.state.requests.filter(r=>r.path==='/api/connections').length>reads);
  assert.equal(connectionWrites().length,0);
});

test('MCP ownership, admin publication and individual sign-in govern editing controls',async t=>{
  fixture.reset();fixture.state.role='member';fixture.state.connections=[mcpConnection(),mcpConnection({id:'shared',name:'Admin gateway',shared:true}),mcpConnection({id:'other',name:'Other owner gateway',owner_id:'another-admin',shared:true})];
  const page=await open(t,'connections'),section=mcpSection(page);
  await section.getByRole('button',{name:'Edit Research gateway',exact:true}).waitFor();
  assert.equal(await section.getByRole('button',{name:/Edit Admin gateway|Edit Other owner gateway/}).count(),0);
  await section.getByRole('button',{name:'Add MCP connection',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'Add MCP connection',exact:true});
  assert.equal(await dialog.getByLabel('Share with workspace members',{exact:true}).count(),0);
  await dialog.getByRole('button',{name:'Cancel',exact:true}).click();
  fixture.state.role='admin';await page.reload();
  await section.getByRole('button',{name:'Edit Admin gateway',exact:true}).click();
  await page.getByRole('dialog',{name:'Edit connection',exact:true}).waitFor();
  fixture.state.role='member';await page.evaluate(()=>window.dispatchEvent(new Event('moyai:access-changed')));
  await page.getByRole('dialog').waitFor({state:'hidden'});
  assert.equal(await section.getByRole('button',{name:'Edit Admin gateway',exact:true}).count(),0);
  fixture.state.individual=false;await page.reload();
  await section.getByText('Sign in with your individual work account to add or edit connections.',{exact:true}).waitFor();
  assert.equal(await section.getByRole('button',{name:'Add MCP connection',exact:true}).isEnabled(),false);
  assert.equal(await section.getByRole('button',{name:/Edit |Check connection|Disable /}).count(),0);
});

test('Standalone connection kinds retain token requirements and can switch to remote MCP',async t=>{
  fixture.reset();fixture.state.productMode='standalone';const page=await open(t,'connections');
  await page.getByRole('button',{name:'Add connection',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'Add connection',exact:true});
  const service=dialog.getByRole('combobox',{name:'Service',exact:true});
  for(const name of ['GitHub','Slack','Linear','Notion']){
    await service.click();await page.getByRole('option',{name,exact:true}).click();
    assert.notEqual(await dialog.getByLabel('Access token',{exact:true}).getAttribute('required'),null);
    assert.equal(await dialog.getByLabel('Server URL',{exact:true}).count(),0);
  }
  await service.click();await page.getByRole('option',{name:'MCP server / gateway',exact:true}).click();
  assert.equal(await dialog.getByLabel('Bearer token (optional)',{exact:true}).getAttribute('required'),null);
  await dialog.getByLabel('Server URL',{exact:true}).fill('https://gateway.example.com/mcp');
  await service.click();await page.getByRole('option',{name:'Slack',exact:true}).click();
  await dialog.getByLabel('Account name',{exact:true}).fill('My Slack');
  await dialog.getByLabel('Access token',{exact:true}).fill('synthetic-slack-token');
  await dialog.getByRole('button',{name:'Save',exact:true}).click();await dialog.waitFor({state:'hidden'});
  const body=connectionWrites()[0].body;assert.equal(body.kind,'slack');assert.equal(body.policy.mcp,undefined);assert.equal(body.clear_credential,undefined);
});

for(const theme of ['dark','light'])for(const width of [1440,768,320])test(`MCP list, editor and tool preview fit ${width}px in ${theme}`,async t=>{
  fixture.reset();fixture.state.connections=[mcpConnection({name:'Research gateway with a long workspace identifier',policy:{...mcpConnection().policy,mcp:{url:'https://gateway.example.com/a-very-long-workspace-specific-endpoint-that-must-wrap-without-overflow/mcp',transport:'streamable_http'}}})];
  fixture.state.connectionCheck={tool_count:1,tools:[{name:'lookup_with_a_very_long_identifier_that_must_wrap_without_overflow',description:'Synthetic tool preview for responsive UI verification.'}]};
  const page=await open(t,'connections',width,theme),section=mcpSection(page);
  await section.getByRole('heading',{name:'Research gateway with a long workspace identifier',exact:true}).waitFor();
  const check=async(label,locator)=>{
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,label);
    assert.equal(await locator.evaluate(el=>el.scrollWidth<=el.clientWidth),true,label);
    if(process.env.MOYAI_UI_CAPTURE_DIR){await fs.mkdir(process.env.MOYAI_UI_CAPTURE_DIR,{recursive:true});await page.screenshot({path:path.join(process.env.MOYAI_UI_CAPTURE_DIR,`mcp-${label}-${width}-${theme}.png`)});}
  };
  await section.locator('article').scrollIntoViewIfNeeded();
  await check('list',page.locator('.ns-content'));
  await section.getByRole('button',{name:/^Edit /}).click();
  const editor=page.getByRole('dialog',{name:'Edit connection',exact:true});
  await editor.waitFor();await check('editor',editor);
  await editor.getByLabel('Server URL',{exact:true}).focus();await page.keyboard.press('Tab');
  assert.equal(await editor.getByLabel('Replacement bearer token',{exact:true}).evaluate(el=>el===document.activeElement),true);
  await editor.getByRole('button',{name:'Save',exact:true}).scrollIntoViewIfNeeded();
  const saveBounds=await editor.getByRole('button',{name:'Save',exact:true}).boundingBox();
  assert.ok(saveBounds.y>=0&&saveBounds.y+saveBounds.height<=page.viewportSize().height);
  if(width===320)await check('editor-actions',editor);
  await page.keyboard.press('Escape');await editor.waitFor({state:'hidden'});
  await section.getByRole('button',{name:/^Check connection /}).click();
  const preview=page.getByRole('dialog');await preview.getByText('1 tool available',{exact:true}).waitFor();
  await check('preview',preview);
});
