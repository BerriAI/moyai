// Actual React app with synthetic API responses; live product proof is separate.
const assert = require('node:assert/strict');
const {before,after,test}=require('node:test');
const {chromium}=require('playwright');
const {settingsFixture}=require('../fixtures/settings.cjs');
let fixture,browser;
before(async()=>{fixture=await settingsFixture();browser=await chromium.launch();});
after(async()=>{await browser?.close();await fixture?.close();});
async function scenario(t,width=1440){
 fixture.reset();fixture.state.productMode='standalone';
 const page=await browser.newPage({viewport:{width,height:900},reducedMotion:'reduce'});
 t.after(()=>page.close());const errors=[];page.on('pageerror',e=>errors.push(e.message));t.after(()=>assert.deepEqual(errors,[]));
 const state={opened:0,messages:[],available:true};
 await page.route('**/api/dot',route=>{if(route.request().method()==='POST')state.opened++;return route.fulfill({json:{session_id:'dot-chat',attention_available:state.available}});});
 await page.route('**/api/sessions/dot-chat**',route=>{
  const url=new URL(route.request().url());
  if(url.pathname.endsWith('/messages'))return route.fulfill({json:state.messages});
  if(url.pathname.endsWith('/read'))return route.fulfill({json:{ok:true}});
  if(url.pathname.endsWith('/work'))return route.fulfill({json:{messages:[],tools:[],subagents:[]}});
  if(url.pathname.endsWith('/workspace'))return route.fulfill({json:{links:[],files:[],sources:[],subagents:[]}});
  if(url.pathname.endsWith('/progress'))return route.fulfill({json:{messages:[],tools:[],subagents:[]}});
  return route.fulfill({json:{session:{id:'dot-chat',title:'Your Dot',status:'idle',active_turn:null,model:'test-model',visibility:'private',surface:'web',owner_id:'viewer'},turns:[],messages:state.messages,transcript_version:String(state.messages.length),pending:null}});
 });
 await page.goto(fixture.base);
 if(width<600)await page.getByRole('button',{name:'Toggle conversations'}).click();
 await page.getByRole('button',{name:'Your Dot',exact:true}).click();
 await page.getByRole('heading',{name:'What should I know about your day?'}).waitFor();
 return {page,state};
}
for(const width of [1440,768,320])test(`Persistent Dot opens and keeps drafts at ${width}px`,async t=>{
 const {page,state}=await scenario(t,width);
 assert.match(page.url(),/#dot-chat$/);assert.equal(state.opened,1);
 await page.getByRole('button',{name:'Help me prepare for my upcoming meetings.',exact:true}).click();
 const input=page.getByRole('textbox',{name:'Message Moyai',exact:true});
 assert.equal(await input.innerText(),'Help me prepare for my upcoming meetings.');
 assert.equal(await page.getByRole('button',{name:'Help me decide what to focus on today.',exact:true}).isEnabled(),false);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 await page.reload();await page.getByRole('heading',{name:'What should I know about your day?'}).waitFor();
 assert.equal(state.opened,1,'Reload reads the saved Dot without creating another');
});
for(const width of [1440,768,320])test(`Dot follow-up controls preserve the chat draft at ${width}px`,async t=>{
 const {page}=await scenario(t,width);
 let item={id:'item-one',revision:1,connection_id:'account-one',title:'Launch review awaits a reply',summary:'The customer asked for a launch review.',source:'https://example.test/review',state:'open',snoozed_until:null,last_seen_at:'2026-11-24T10:00:00Z'};
 await page.route('**/api/dot/items',route=>route.fulfill({json:{items:[item],limited:false}}));
 await page.route('**/api/dot/items/item-one',route=>{
  const edit=route.request().postDataJSON();assert.equal(edit.revision,item.revision);
  item={...item,...edit,revision:item.revision+1};return route.fulfill({json:item});
 });
 await page.getByRole('button',{name:'Help me prepare for my upcoming meetings.',exact:true}).click();
 await page.getByRole('button',{name:'Following',exact:true}).click();
 const dialog=page.getByRole('dialog');
 await dialog.getByRole('heading',{name:item.title}).waitFor();
 await dialog.getByRole('textbox',{name:'Filter follow-ups'}).fill('missing');
 await dialog.getByText('No matching follow-ups.').waitFor();
 await dialog.getByRole('textbox',{name:'Filter follow-ups'}).fill('');
 await dialog.getByRole('button',{name:'Dismiss',exact:true}).click();
 await dialog.getByText('dismissed',{exact:true}).waitFor();
 await dialog.getByRole('button',{name:'Reopen',exact:true}).click();
 await dialog.getByRole('button',{name:'Snooze 1 day',exact:true}).click();
 await dialog.getByText(/Snoozed until/).waitFor();
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 await page.keyboard.press('Escape');
 assert.equal(await page.getByRole('textbox',{name:'Message Moyai',exact:true}).innerText(),'Help me prepare for my upcoming meetings.');
});
test('A delivered message renders without an execution turn in the destination',async t=>{
 const {page,state}=await scenario(t);
 state.messages=[{id:1,turn_id:'isolated-worker',role:'assistant',content:'The release review is ready. Shall I prepare the next steps?',author_id:null,attachments:[]}];
 await page.reload();await page.getByText(state.messages[0].content,{exact:true}).waitFor();
 assert.equal(await page.getByRole('textbox',{name:'Message Moyai',exact:true}).isEnabled(),true);
});
for(const width of [1440,768,320])test(`Dot Slack opt-in and pause preserve the chat at ${width}px`,async t=>{
 const {page}=await scenario(t,width);
 let settings={revision:0,enabled:false,status:'off',description:'Receive useful updates in Slack.',last_delivery:null};
 const writes=[];
 await page.route('**/api/dot/notifications',route=>{
  if(route.request().method()==='PATCH'){
   const edit=route.request().postDataJSON();writes.push(edit);
   assert.equal(edit.revision,settings.revision);
   settings={...settings,revision:settings.revision+1,enabled:edit.enabled,status:edit.enabled?'on':'off',
    description:edit.enabled?'Your Dot sends updates to your Slack DM.':'Proactive messages are paused.'};
  }
  return route.fulfill({json:settings});
 });
 await page.getByRole('button',{name:'Help me prepare for my upcoming meetings.',exact:true}).click();
 await page.getByRole('button',{name:'Slack notifications',exact:true}).click();
 const dialog=page.getByRole('dialog');
 await dialog.getByRole('button',{name:'Enable Slack notifications',exact:true}).click();
 await dialog.getByText('Your Dot sends updates to your Slack DM.',{exact:true}).waitFor();
 await dialog.getByRole('button',{name:'Pause notifications',exact:true}).click();
 await dialog.getByText('Proactive messages are paused.',{exact:true}).waitFor();
 assert.deepEqual(writes,[{enabled:true,revision:0},{enabled:false,revision:1}]);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 await page.keyboard.press('Escape');
 assert.equal(await page.getByRole('textbox',{name:'Message Moyai',exact:true}).innerText(),'Help me prepare for my upcoming meetings.');
});
test('Slack setup failure refreshes state before another write',async t=>{
 const {page}=await scenario(t);let writes=0;
 await page.route('**/api/dot/notifications',route=>{
  if(route.request().method()==='PATCH'){writes++;return route.fulfill({status:409,json:{detail:'Your Slack account could not be verified.'}});}
  return route.fulfill({json:{revision:0,enabled:false,status:'off',description:'Connect your Slack DM.',last_delivery:null}});
 });
 await page.getByRole('button',{name:'Slack notifications',exact:true}).click();
 await page.getByRole('button',{name:'Enable Slack notifications',exact:true}).click();
 await page.getByRole('alert').filter({hasText:'Your Slack account could not be verified.'}).waitFor();
 assert.equal(writes,1);
 assert.equal(await page.getByRole('button',{name:'Enable Slack notifications',exact:true}).count(),0);
 await page.getByRole('button',{name:'Retry',exact:true}).click();
 await page.getByRole('button',{name:'Enable Slack notifications',exact:true}).waitFor();
 assert.equal(writes,1);
});
