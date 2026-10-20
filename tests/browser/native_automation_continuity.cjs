// Real React UI with synthetic API responses; no production/provider proof.
const assert=require('node:assert/strict');
const {before,after,test}=require('node:test');
const {chromium}=require('playwright');
const fs=require('node:fs/promises');
const path=require('node:path');
const {settingsFixture}=require('../fixtures/settings.cjs');
let fixture,browser;
const chatId='c41e343b-d4ec-4317-9b49-1f54127e453f';
const automationId='a'.repeat(32);
const definition=(extra={})=>({name:'Daily project review',prompt:'Review progress and continue the work.',session_id:chatId,model:'test-model',mode:'modal',harness:'pydantic-v1',repo_url:'',github_repository_id:null,environment_id:'none',plugins:[],metadata:{},queue_events:false,max_runs_per_hour:50,self_management:{instructions:false,schedule:false},triggers:[{id:'daily',schedule:{frequency:'daily',time:'09:00',weekday:0,timezone:'UTC',cron:'',run_at:null}}],...extra});
const chat=(extra={})=>({id:chatId,owner_id:'viewer',title:'Project progress',surface:'web',visibility:'shared',archived:false,deleted:false,can_manage:true,participated:true,pinned:false,folder:'',model:'test-model',status:'idle',active_turn:null,save_warning:null,unread:false,updated_at:'2026-10-19T10:00:00Z',workspace:null,...extra});
before(async()=>{fixture=await settingsFixture();browser=await chromium.launch({headless:true});});
after(async()=>{await browser?.close();await fixture?.close();});
async function open(t,{width=1440,theme='dark',saved=null,chats=[chat()],chatFailure=false}={}){
 const page=await browser.newPage({viewport:{width,height:1000},reducedMotion:'reduce'});t.after(()=>page.close());
 const errors=[];page.on('pageerror',e=>errors.push(e.message));t.after(()=>assert.deepEqual(errors,[]));
 const state={saved,writes:[],chatFailure};
 const row=()=>({id:automationId,owner_id:'viewer',owned:true,name:state.saved.name,revision:1,triggers:state.saved.triggers,legacy_paused:true,native:null});
 await page.addInitScript(theme=>localStorage.setItem('moyai-appearance',theme),theme);
 await page.route('**/api/sessions',route=>route.fulfill({status:state.chatFailure?503:200,json:state.chatFailure?{detail:'Chats are temporarily unavailable.'}:chats}));
 await page.route('**/api/product/environments',route=>route.fulfill({json:[]}));
 await page.route('**/api/product/automations**',async route=>{
  const request=route.request(),url=new URL(request.url());
  if(request.method()!=='GET'){
   const body=request.postDataJSON();state.writes.push(body);state.saved=body.definition;
   return route.fulfill({json:{id:automationId,revision:1,native_state:'paused'}});
  }
  if(url.pathname.endsWith('/history'))return route.fulfill({json:{history:[],next_cursor:null,untrusted_reference:true}});
  if(url.pathname.endsWith('/preview'))return route.fulfill({json:{...row(),definition:state.saved,blockers:[],supported:true,execution_visibility:'shared',activation:'explicit_owner_action_required',sharing_notice:'Shared workspace resources.',overlap:'ALLOW_ALL',catchup_seconds:900,capabilities:{update:true,migrate:true,enable:false,pause:false,run:false,retire:false,webhook:false,test_event:false}}});
  return route.fulfill({json:{source:'canonical_product',automations:state.saved?[row()]:[],next_offset:null,capabilities:{create:true},event_choices:{}}});
 });
 await page.goto(fixture.base+'/#settings/automations');
 await page.getByRole('button',{name:'Create automation',exact:true}).waitFor();
 return {page,state};
}
async function choose(page,value){await page.getByRole('combobox',{name:'Run in',exact:true}).click();await page.getByRole('option',{name:value,exact:true}).click();}
for(const width of [1440,768,320])for(const theme of ['light','dark'])test(`Automation destination at ${width}px in ${theme}`,async t=>{
 const {page,state}=await open(t,{width,theme});
 await page.getByRole('button',{name:'Create automation',exact:true}).click();
 const dialog=page.getByRole('dialog',{name:'Create automation'});await dialog.waitFor();
 await page.getByRole('combobox',{name:'Run in',exact:true}).waitFor();
 await page.getByLabel('Automation name',{exact:true}).fill('Daily project review');
 await page.getByLabel('Instructions',{exact:true}).fill('Review progress and continue the work.');
 assert.match(await page.getByRole('combobox',{name:'Run in',exact:true}).innerText(),/New chat each run/);
 await page.getByLabel('Repository',{exact:true}).fill('https://github.com/example/project');
 await choose(page,'Project progress');
 assert.equal(await page.getByLabel('Repository',{exact:true}).count(),0);
 await page.getByText(/Each run continues this chat/).waitFor();
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 assert.equal(await dialog.evaluate(el=>el.scrollWidth<=el.clientWidth),true);
 const bounds=await dialog.boundingBox();assert.ok(bounds.x>=0&&bounds.x+bounds.width<=width);
 await page.getByRole('combobox',{name:'Run in',exact:true}).focus();await page.keyboard.press('Tab');
 assert.equal(await page.getByRole('combobox',{name:'Automation model'}).evaluate(el=>el===document.activeElement),true);
 if(process.env.MOYAI_UI_CAPTURE_DIR){await fs.mkdir(process.env.MOYAI_UI_CAPTURE_DIR,{recursive:true});await page.screenshot({path:path.join(process.env.MOYAI_UI_CAPTURE_DIR,`automation-${width}-${theme}.png`)});}
 await choose(page,'New chat each run');
 assert.equal(await page.getByLabel('Repository',{exact:true}).inputValue(),'https://github.com/example/project');
 await choose(page,'Project progress');
 await page.getByRole('button',{name:'Save paused',exact:true}).click();await dialog.waitFor({state:'hidden'});
 assert.equal(state.writes[0].definition.session_id,chatId);
});
test('Saved unavailable destination survives edits and filtering recovers',async t=>{
 const {page,state}=await open(t,{saved:definition(),chats:[]});
 await page.getByRole('searchbox',{name:'Search automations'}).fill('missing');
 await page.getByRole('heading',{name:'No matching automations'}).waitFor();
 await page.getByRole('button',{name:'Clear filters',exact:true}).click();
 await page.getByRole('button',{name:'Review Daily project review'}).click();
 await page.getByRole('link',{name:'Continue existing chat'}).waitFor();
 await page.getByRole('button',{name:'Edit definition',exact:true}).click();
 await page.getByRole('combobox',{name:'Run in',exact:true}).waitFor();
 assert.match(await page.getByRole('combobox',{name:'Run in',exact:true}).innerText(),/Saved chat/);
 await page.getByLabel('Instructions',{exact:true}).fill('Updated recurring instructions.');
 await page.getByRole('button',{name:'Save paused',exact:true}).click();
 await page.getByRole('dialog').waitFor({state:'hidden'});
 assert.equal(state.writes[0].definition.session_id,chatId);
});
test('Chat load retry preserves draft and destination list excludes private and foreign chats',async t=>{
 const {page,state}=await open(t,{chatFailure:true,chats:[chat(),chat({id:'private',title:'Private chat',visibility:'private'}),chat({id:'foreign',title:'Other owner',owner_id:'someone-else'})]});
 await page.getByRole('button',{name:'Create automation',exact:true}).click();
 await page.getByLabel('Automation name',{exact:true}).fill('Keep my draft');
 await page.getByRole('button',{name:'Retry chats',exact:true}).waitFor();state.chatFailure=false;
 await page.getByRole('button',{name:'Retry chats',exact:true}).click();
 await page.getByRole('combobox',{name:'Run in',exact:true}).click();
 await page.getByRole('option',{name:'Project progress',exact:true}).waitFor();
 assert.equal(await page.getByRole('option',{name:'Private chat',exact:true}).count(),0);
 assert.equal(await page.getByRole('option',{name:'Other owner',exact:true}).count(),0);
 await page.keyboard.press('Escape');assert.equal(await page.getByLabel('Automation name',{exact:true}).inputValue(),'Keep my draft');
 await page.keyboard.press('Escape');await page.getByRole('dialog').waitFor({state:'hidden'});
});
