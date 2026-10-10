const assert=require('node:assert/strict');
const {test,before,after}=require('node:test');
const {spawn}=require('node:child_process');
const {chromium}=require('playwright');
let server,browser,base;
const root='a'.repeat(32);
before(async()=>{
 server=spawn(process.execPath,['scripts/settings_ui_preview.cjs','--port','0'],{stdio:['ignore','pipe','inherit']});
 base=await new Promise((resolve,reject)=>{server.once('error',reject);server.stdout.on('data',data=>{const url=data.toString().match(/http:\/\/127\.0\.0\.1:\d+/);if(url)resolve(url[0]);});});
 browser=await chromium.launch({headless:true});
});
after(async()=>{await browser?.close();server?.kill();});
async function open(t,width=1440,fixture='swarm-room',setup=async()=>{}){
 const page=await browser.newPage({viewport:{width,height:1000},reducedMotion:'reduce'}),errors=[];
 page.setDefaultTimeout(8000);page.on('pageerror',error=>errors.push(error.message));
 t.after(async()=>{await page.close();assert.deepEqual(errors,[]);});await setup(page);
 await page.goto(`${base}/?fixture=${fixture}#run=${root}`);
 await page.getByRole('button',{name:'Chat',exact:true}).click();
 return page;
}
for(const width of [1440,768,320])test(`shared room shows real authors and harnesses without overflowing at ${width}px`,async t=>{
 const page=await open(t,width),room=page.getByRole('log',{name:'Swarm group chat'});
 await room.getByText('20 agents',{exact:true}).waitFor();await room.locator('[data-room-id="board:2"]').waitFor();
 assert.equal(await room.locator('article').count(),7);
 assert.equal(await room.locator('[data-room-id="board:2"] .swarm-chat-lead').innerText(),'Lead');
 assert.equal(await room.locator('[data-room-id="message:2"] .swarm-chat-lead').count(),0);
 assert.match(await room.locator('[data-room-id="board:3"] .swarm-chat-byline').innerText(),/Codex/);
 assert.match(await room.locator('[data-room-id="board:3"] img').getAttribute('src'),/codex/);
 assert.equal(await room.getByText('Swarm continuation',{exact:true}).count(),0);
 const dims=await page.evaluate(()=>({width:innerWidth,body:document.body.scrollWidth,room:document.querySelector('#conversation').scrollWidth,client:document.querySelector('#conversation').clientWidth}));
 assert.ok(dims.body<=dims.width+1,JSON.stringify(dims));assert.ok(dims.room<=dims.client+1,JSON.stringify(dims));
 const input=page.getByRole('textbox',{name:'Message Moyai',exact:true});await input.fill('Keep this draft for the team.');
 await page.getByRole('button',{name:'Space',exact:true}).click();assert.equal(await room.isVisible(),false);
 await page.getByRole('button',{name:'Chat',exact:true}).click();assert.equal(await input.evaluate(el=>el.value??el.innerText),'Keep this draft for the team.');
 await room.locator('[data-room-id="board:3"]').getByRole('button',{name:'Open Agent 2 conversation',exact:true}).click();
 await page.getByRole('tabpanel',{name:'Agent 2',exact:true}).waitFor();
 await page.getByRole('complementary',{name:'Session workspace',exact:true}).getByRole('button',{name:'Hide workspace panel',exact:true}).click();
 assert.equal(new URL(page.url()).hash,`#run=${root}`);assert.equal(await input.evaluate(el=>el.value??el.innerText),'Keep this draft for the team.');
});

test('owner sends appear immediately without waiting for the feed poll, and drafts keep focus',async t=>{
 const page=await open(t);await page.locator('[data-room-id="board:3"]').waitFor();
 await page.route('**/swarm/chat?**',async route=>{await route.fulfill({json:{items:[],live_items:[],next_cursor:'7',previous_cursor:null,has_more:false}});});
 const input=page.getByRole('textbox',{name:'Message Moyai',exact:true});await input.fill('Continue testing for two more hours.');
 await input.press('ControlOrMeta+Enter');
 await page.locator('[data-room-id="message:10"]').getByText('Continue testing for two more hours.',{exact:true}).waitFor({timeout:1500});
 assert.equal(await input.evaluate(el=>el===document.activeElement),true);
 await input.fill('A new unsent draft');await page.waitForTimeout(4200);assert.equal(await input.evaluate(el=>el.value??el.innerText),'A new unsent draft');
 assert.equal(await input.evaluate(el=>el===document.activeElement),true);
});

test('older messages preserve the visible anchor and stay bounded; returning latest restarts polling',async t=>{
 const page=await open(t,1440,'swarm-room-history');const room=page.locator('#conversation');await room.locator('article').last().waitFor();
 assert.equal(await room.locator('article').count(),50);
 await room.evaluate(el=>{el.scrollTop=0;});const anchor=room.locator('article').first(),id=await anchor.getAttribute('data-room-id');
 const top=await anchor.evaluate(el=>el.getBoundingClientRect().top);
 await room.getByRole('button',{name:'Load earlier messages',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('[data-room-id]').length===100);
 const after=await room.locator(`[data-room-id="${id}"]`).evaluate(el=>el.getBoundingClientRect().top);
 assert.ok(Math.abs(top-after)<2,`${top} vs ${after}`);
 for(let i=0;i<4;i++){await room.getByRole('button',{name:'Load earlier messages',exact:true}).click();await page.waitForTimeout(80);}
 assert.ok(await room.locator('article').count()<=200);
 await room.getByRole('button',{name:'Back to latest',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('[data-room-id]').length===50);
 assert.equal(await room.locator('article').last().getAttribute('data-room-id'),'board:history-259');
});

test('transient failures offer retry, revoked access clears replies, and space stops polling',async t=>{
 let requests=0,mode='error';const page=await open(t,1440,'swarm-room',async page=>{
  await page.route('**/swarm/chat?**',async route=>{requests++;if(mode==='error')return route.fulfill({status:503,json:{detail:'Synthetic outage'}});if(mode==='denied')return route.fulfill({status:403,json:{detail:'Synthetic revoked access'}});return route.continue();});
 });
 await page.getByText('Couldn’t refresh team messages.',{exact:true}).waitFor();mode='good';await page.getByRole('button',{name:'Retry',exact:true}).click();await page.locator('[data-room-id="board:3"]').waitFor();
 await page.getByRole('button',{name:'Space',exact:true}).click();const count=requests;await page.waitForTimeout(4300);assert.equal(requests,count);
 mode='denied';await page.getByRole('button',{name:'Chat',exact:true}).click();await page.getByText('This team chat is no longer available.',{exact:true}).waitFor();assert.equal(await page.locator('[data-room-id]').count(),0);
});

test('checkpoint warnings remain visible with a route to activity',async t=>{
 const page=await open(t,1440,'swarm-room',async page=>{
  await page.route(url=>url.pathname===`/api/runs/${root}`,async route=>{const response=await route.fetch(),run=await response.json();run.checkpoint_error='The latest workspace files could not be saved.';await route.fulfill({response,json:run});});
 });
 await page.getByRole('alert').getByText('The latest workspace files could not be saved.',{exact:true}).waitFor();
 await page.getByRole('button',{name:'View activity',exact:true}).click();await page.getByRole('tabpanel',{name:'Activity',exact:true}).waitFor();
});

test('queued controls remain usable without duplicating waiting inputs in the room',async t=>{
 const page=await open(t,1440,'swarm-room',async page=>{
  await page.route(url=>url.pathname===`/api/runs/${root}`,async route=>{const response=await route.fetch(),run=await response.json();run.messages.push({id:80,role:'user',user_id:'user-0',status:'running',content:'Current turn'},{id:81,role:'user',user_id:'user-0',user_name:'Alex',status:'queued',content:'Review this after the current turn',revision:1});await route.fulfill({response,json:run});});
  await page.route('**/swarm/chat?**',async route=>{const response=await route.fetch(),data=await response.json();data.items.push({id:'message:81',run_id:root,kind:'human',content:'Review this after the current turn',status:'queued',created_at:'2026-10-07T16:00:00Z',author:{id:'user-0',role:'human',name:'Alex'}});await route.fulfill({response,json:data});});
 });
 await page.getByRole('button',{name:'Edit queued message',exact:true}).waitFor();
 await page.locator('[data-room-id="board:3"]').waitFor();assert.equal(await page.locator('[data-room-id="message:81"]').count(),0);
 await page.getByRole('button',{name:'Edit queued message',exact:true}).click();await page.getByRole('textbox',{name:'Edit queued message',exact:true}).fill('Keep this pending edit');
 await page.getByRole('button',{name:'Space',exact:true}).click();await page.getByRole('button',{name:'Chat',exact:true}).click();assert.equal(await page.getByRole('textbox',{name:'Edit queued message',exact:true}).inputValue(),'Keep this pending edit');
});

test('a shared file opens the author’s workspace and preserves the group draft',async t=>{
 const child=(21).toString(16).padStart(32,'0'),requests=[];
 const page=await open(t,1440,'swarm-room',async page=>{
  await page.route('**/swarm/chat?**',async route=>{const response=await route.fetch(),data=await response.json();data.items.find(item=>item.id==='board:3').content='I saved [the plan](sandbox:/workspace/plan.md).';await route.fulfill({response,json:data});});
  await page.route(url=>url.pathname.endsWith('/files'),async route=>{requests.push(new URL(route.request().url()).pathname);const childFile=new URL(route.request().url()).pathname===`/api/runs/${child}/files`;await route.fulfill({json:{files:childFile?[{path:'plan.md',name:'plan.md',workspace_path:'plan.md',archive_path:'new-files/plan.md',url:`/api/runs/${child}/download`,preview_url:`/api/runs/${child}/preview` }]:[]}});});
  await page.route(url=>url.pathname===`/api/runs/${child}/preview`,route=>route.fulfill({json:{format:'markdown',text:'This plan belongs to Agent 2.',truncated:false}}));
 });
 const input=page.getByRole('textbox',{name:'Message Moyai',exact:true});await input.fill('Preserve my group draft');
 await page.getByRole('link',{name:'the plan',exact:true}).click();
 await page.getByText('This plan belongs to Agent 2.',{exact:true}).waitFor();
 assert.equal(new URL(page.url()).hash,`#run=${child}`);assert.ok(requests.includes(`/api/runs/${child}/files`));
 await page.getByRole('button',{name:'← Parent session',exact:true}).click();
 assert.equal(await input.evaluate(el=>el.value??el.innerText),'Preserve my group draft');
});
