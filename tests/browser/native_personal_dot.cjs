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
 await page.getByRole('heading',{name:'What should I help you keep track of?'}).waitFor();
 return {page,state};
}
for(const width of [1440,768,320])test(`Persistent Dot opens and keeps drafts at ${width}px`,async t=>{
 const {page,state}=await scenario(t,width);
 assert.match(page.url(),/#dot-chat$/);assert.equal(state.opened,1);
 await page.getByRole('button',{name:'Help me prepare for my upcoming meetings.',exact:true}).click();
 const input=page.getByRole('textbox',{name:'Message Moyai',exact:true});
 assert.equal(await input.innerText(),'Help me prepare for my upcoming meetings.');
 assert.equal(await page.getByRole('button',{name:'Help me follow up on important unanswered messages.',exact:true}).isEnabled(),false);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 await page.reload();await page.getByRole('heading',{name:'What should I help you keep track of?'}).waitFor();
 assert.equal(state.opened,1,'Reload reads the saved Dot without creating another');
});
test('A delivered message renders without an execution turn in the destination',async t=>{
 const {page,state}=await scenario(t);
 state.messages=[{id:1,turn_id:'isolated-worker',role:'assistant',content:'The release review is ready. Shall I prepare the next steps?',author_id:null,attachments:[]}];
 await page.reload();await page.getByText(state.messages[0].content,{exact:true}).waitFor();
 assert.equal(await page.getByRole('textbox',{name:'Message Moyai',exact:true}).isEnabled(),true);
});
