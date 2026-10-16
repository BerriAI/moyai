// Supplemental real React components + real EventSource transport with synthetic API data.
// Actual restored-product/model proof is performed by the PR integration owner.
const assert = require('node:assert/strict');
const {test,before,after} = require('node:test');
const path = require('node:path');
const {chromium} = require('playwright');
let server,browser,base,state;
const sessionId='a'.repeat(32), legacyId='b'.repeat(32), turnId='turn-root';
const clients=new Set();
function reset(overrides={}) {
  for(const client of clients) client.end(); clients.clear();
  state={signedIn:true, individual:true, mode:'existing', google:true, password:true, access:false, denied:false, status:'running',
    messages:[{id:1,turn_id:turnId,role:'user',author_id:'viewer',content:'Inspect this repository.'}], requests:[], streams:[], ...overrides};
}
function session(id=sessionId) {return {id,title:'Current native work',owner_id:'viewer',visibility:'private',surface:'web',model:'test-model',status:state.status,active_turn:['running','saving'].includes(state.status)?turnId:null,archived:false,pinned:false};}
async function handle(req,res,next) {
  const url=new URL(req.url,'http://localhost');
  if(!url.pathname.startsWith('/api/'))return next();
  let body=''; for await(const chunk of req)body+=chunk;
  const data=body?JSON.parse(body):null;
  state.requests.push({path:url.pathname,query:url.search,method:req.method,body:data,headers:req.headers});
  const json=(value,status=200)=>{res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(value));};
  if(url.pathname==='/api/auth/config')return json({mode:state.mode,password_enabled:state.password,google_enabled:state.google,access_enabled:state.access});
  if(url.pathname==='/api/login'){state.signedIn=true;return json({csrf:'fixture-csrf'});}
  if(url.pathname==='/api/auth/google/start')return json({url:'https://accounts.google.com/fixture'});
  if(url.pathname==='/api/logout'){state.signedIn=false;return json({signed_out:true});}
  if(!state.signedIn)return json({detail:'Sign in.'},401);
  if(url.pathname==='/api/me')return json({user:{id:'viewer',name:'Test viewer',role:'admin',individual:state.individual},csrf:'fixture-csrf',models:['test-model'],default_model:'test-model',product_mode:state.mode});
  if(url.pathname==='/api/sessions'&&req.method==='POST')return json(session('c'.repeat(32)));
  if(url.pathname==='/api/sessions')return json(state.denied?[]:[session()]);
  if(url.pathname==='/api/connections'||url.pathname==='/api/session-folders')return json([]);
  if(url.pathname==='/api/pull-requests/statuses')return json({});
  if(url.pathname==='/api/history/sessions'||url.pathname==='/api/history/search') {
    const rows=[{...session(legacyId),key:'legacy:'+legacyId,source:'legacy',url:'/#run='+legacyId,title:'Saved earlier investigation',read_only:true,match:{snippet:'Body-only needle <img src=x onerror=alert(1)>'}},
      {...session(),key:'native:'+sessionId,source:'native',url:'/#'+sessionId,title:'Current native work'}];
    if(url.pathname==='/api/history/sessions'&&url.searchParams.get('limit')==='100'&&state.inventoryPages) {
      const page=url.searchParams.get('cursor')==='page-2'?1:0;
      return json({sessions:state.inventoryPages[page],has_more:page===0,next_cursor:page===0?'page-2':null});
    }
    if(url.searchParams.get('q')==='slow') {setTimeout(()=>json({sessions:[{id:'late',title:'Stale search result'}]}),700);return;}
    return json({sessions:rows,has_more:false});
  }
  if(url.pathname===`/api/history/sessions/${legacyId}`)return json({session:{...session(legacyId),source:'legacy',url:'/#run='+legacyId,title:'Saved earlier investigation',status:'completed',execution_engine:'legacy',read_only:true}});
  if(url.pathname===`/api/history/sessions/${sessionId}`)return json({session:{...session(),source:'native',url:'/#'+sessionId,read_only:false}});
  if(url.pathname.startsWith(`/api/product/sessions/${legacyId}/`)) {
    if(url.pathname.endsWith('/messages'))return json({messages:[{id:8,role:'assistant',content:'Historical saved answer'}],has_more:false,next_offset:null});
    if(url.pathname.endsWith('/children'))return json({sessions:[],has_more:false,next_offset:null});
    if(url.pathname.endsWith('/files'))return json({files:[],sources:[],has_more:{},next_offset:null});
  }
  if(url.pathname==='/api/settings/users/role')return state.roleConflict ? json({detail:'This role changed since you opened it. Refresh and try again.'},409) : json(data);
  if(url.pathname==='/api/settings/users')return json({source:'product',note:'Organization sign-in manages accounts.',capabilities:{roles:true,create:false,update:false,delete:false},users:[{id:'teammate@example.test',email:'teammate@example.test',username:'teammate@example.test',display_name:'Teammate',role:'member',revision:7,enabled:true}]});
  if(url.pathname==='/api/settings/overview')return json({counts:{},capabilities:{}});
  if(url.pathname==='/api/settings/skills')return json({skills:[],capabilities:{}});
  if(url.pathname==='/api/settings/automations')return json({automations:[],capabilities:{}});
  if(url.pathname.startsWith('/api/sessions/')) {
    if(state.denied)return json({detail:'Session access revoked.'},403);
    if(url.pathname.endsWith('/events')) {
      res.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-cache','Connection':'keep-alive'});
      res.write('event: connected\ndata: {}\n\n'); clients.add(res);
      state.streams.push({offset:url.searchParams.get('offset'),last:req.headers['last-event-id']});
      res.on('close',()=>clients.delete(res));return;
    }
    if(url.pathname.endsWith('/messages'))return json(req.method==='POST'?{}:state.messages);
    if(url.pathname.endsWith('/plan'))return json([]);
    if(url.pathname.endsWith('/read'))return json({});
    if(req.method==='PATCH')return json({...session(),...data});
    return json({session:session(url.pathname.split('/')[3]),turns:[{id:turnId,actor_id:'viewer',status:state.status,connections:[],usage:{}}],activity:[]});
  }
  return json({detail:'Fixture endpoint not implemented: '+url.pathname},404);
}
async function waitFor(predicate,label='condition') {const end=Date.now()+10000;while(!predicate()){if(Date.now()>end)throw new Error('Timed out: '+label);await new Promise(resolve=>setTimeout(resolve,25));}}
function emit(value,id,event) {for(const client of clients)client.write(`${id===undefined?'':`id: ${id}\n`}${event?`event: ${event}\n`:''}data: ${JSON.stringify(value)}\n\n`);}
async function pageFor(t,route='#'+sessionId,width=1440) {
  const page=await browser.newPage({viewport:{width,height:1000},reducedMotion:'reduce'});t.after(()=>page.close());
  const errors=[];page.on('pageerror',error=>errors.push(error.message));t.after(()=>assert.deepEqual(errors,[]));
  await page.goto(base+'/'+route);return page;
}
async function palette(page) {await page.keyboard.press('Control+k');const dialog=page.getByRole('dialog',{name:'Commands and session search'});await dialog.waitFor();return dialog;}
before(async()=>{
  reset();const {createServer}=await import('vite');
  server=await createServer({configFile:path.resolve('native/web/vite.config.ts'),server:{host:'127.0.0.1',port:0,strictPort:false},plugins:[{name:'synthetic-native-api',configureServer(vite){vite.middlewares.use((req,res,next)=>{void handle(req,res,next).catch(error=>{res.writeHead(500);res.end(JSON.stringify({detail:error.message}));});});}}]});
  await server.listen();base=`http://127.0.0.1:${server.httpServer.address().port}`;browser=await chromium.launch({headless:true});
});
after(async()=>{for(const client of clients)client.end();await browser?.close();await server?.close();});

for(const width of [1440,768,320])test(`palette keyboard, escaped body search, focus handoff and historical isolation at ${width}px`,async t=>{
  reset();const page=await pageFor(t,'#'+sessionId,width);await page.getByRole('heading',{name:'Current native work',exact:true}).waitFor();
  const input=page.getByRole('textbox',{name:'Message Moyai'});await input.focus();
  let dialog=await palette(page),search=dialog.getByRole('combobox');
  await dialog.getByRole('option',{name:'Saved earlier investigation',exact:false}).waitFor();
  await search.press('ArrowUp');assert.match(await dialog.locator('[role=option][aria-selected=true]').innerText(),/Current native work/);
  await search.press('ArrowDown');assert.match(await dialog.locator('[role=option][aria-selected=true]').innerText(),/Start session/);
  await search.press('Escape');await dialog.waitFor({state:'hidden'});assert.equal(await input.evaluate(el=>el===document.activeElement),true);
  dialog=await palette(page);search=dialog.getByRole('combobox');await search.fill('Rename');await search.press('Enter');
  const rename=page.getByRole('dialog',{name:'Rename session',exact:true});await rename.waitFor();assert.equal(await rename.getByRole('textbox').evaluate(el=>el===document.activeElement),true);
  await page.keyboard.press('Control+k');assert.equal(await page.getByRole('dialog',{name:'Commands and session search'}).count(),0);
  await page.keyboard.press('Escape');await rename.waitFor({state:'hidden'});
  dialog=await palette(page);search=dialog.getByRole('combobox');await search.fill('needle');
  const match=dialog.getByRole('option',{name:/Saved earlier investigation/});await match.getByText('Body-only needle <img src=x onerror=alert(1)>').waitFor();assert.equal(await match.locator('img').count(),0);
  const box=await dialog.boundingBox();assert.ok(box.x>=0&&box.x+box.width<=width+1);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await match.click();await page.getByRole('heading',{name:'Saved earlier investigation',exact:true}).waitFor();await page.getByText('Historical saved answer',{exact:true}).waitFor();
  assert.ok(!state.requests.some(req=>req.path.startsWith('/api/sessions/'+legacyId)));
  assert.ok(!state.requests.some(req=>req.path==='/api/pull-requests/statuses'&&(req.body.session_ids.includes('run='+legacyId)||req.body.session_ids.includes(legacyId))));
  dialog=await palette(page);assert.equal(await dialog.getByRole('option',{name:/Rename session|Move to folder|Pin session/}).count(),0);
  await page.keyboard.press('Escape');
});

test('root streaming survives saved-state polling, reconnect replay, per-round retry and final settlement',async t=>{
  reset();const page=await pageFor(t);await page.getByRole('textbox',{name:'Message Moyai'}).waitFor();await waitFor(()=>clients.size>0,'SSE connected');
  emit({type:'text_start',index:0,text:'Unlabelled child text'},0);
  emit({type:'response_start',run_id:turnId,step:1},1);emit({type:'text_start',index:0,text:'I will inspect.'},2);
  await page.locator('.live-message').getByText('I will inspect.',{exact:true}).waitFor();
  const polls=state.requests.filter(req=>req.path===`/api/sessions/${sessionId}/messages`).length;
  await waitFor(()=>state.requests.filter(req=>req.path===`/api/sessions/${sessionId}/messages`).length>polls,'saved poll');
  assert.equal(await page.locator('.live-message .message-content').innerText(),'I will inspect.');
  emit({type:'tool_call',id:'call1',name:'read_file',arguments:{path:'private-tool-detail'}},3);
  await page.locator('.live-action').getByText('Using read file…',{exact:true}).waitFor();assert.equal(await page.getByText('private-tool-detail',{exact:true}).count(),0);
  for(const client of clients)client.end();await waitFor(()=>state.streams.length===2,'browser reconnect');assert.equal(state.streams[1].last,'3');
  emit({type:'text_delta',index:0,text:'duplicate'},2);emit({type:'response_start',run_id:turnId,step:2},4);emit({type:'text_start',index:0,text:'Wrong attempt'},5);
  emit({type:'response_start',run_id:turnId,step:2},6);emit({type:'text_start',index:0,text:'Final'},7);emit({type:'text_delta',index:0,text:' answer'},8);
  await page.locator('.live-message').getByText('Final answer',{exact:true}).waitFor();assert.equal(await page.getByText(/Unlabelled child text|Wrong attempt|duplicate/).count(),0);
  state.messages.push({id:2,turn_id:turnId,role:'assistant',author_id:null,content:'Final answer'});state.status='completed';emit({},undefined,'finished');
  await page.waitForFunction(()=>document.querySelectorAll('.message.assistant:not(.live-message)').length===1);
  assert.equal(await page.getByText('Final answer',{exact:true}).count(),1);assert.equal(await page.getByText('I will inspect.',{exact:true}).count(),1);
});

test('access revocation removes transcript, draft and panel state before reauthorization',async t=>{
  reset();const page=await pageFor(t);const input=page.getByRole('textbox',{name:'Message Moyai'});await input.waitFor();await input.fill('private draft');await waitFor(()=>clients.size>0);
  emit({type:'response_start',run_id:turnId,step:1},0);emit({type:'text_start',index:0,text:'Sensitive live text'},1);await page.getByText('Sensitive live text',{exact:true}).waitFor();
  state.denied=true;emit({},undefined,'access_revoked');await page.getByText('Error: Session access revoked.',{exact:true}).waitFor();
  assert.equal(await page.getByText('Sensitive live text',{exact:true}).count(),0);assert.equal(await input.count(),0);
  assert.equal(await page.locator('.tabbed-panel').count(),0);await waitFor(()=>clients.size===0);
});

test('capability-driven product/standalone login and shared session admission',async t=>{
  reset({signedIn:false,individual:false});const page=await pageFor(t,'');await page.getByRole('button',{name:'Continue with Google'}).waitFor();
  assert.equal(await page.getByLabel('Username',{exact:true}).count(),0);await page.getByLabel('Workspace password',{exact:true}).fill('synthetic-password');await page.getByRole('button',{name:'Sign in',exact:true}).click();
  const privateChoice=page.getByRole('checkbox',{name:'Private session · Only you'});await privateChoice.waitFor();assert.equal(await privateChoice.isEnabled(),false);assert.equal(await privateChoice.isChecked(),false);
  await page.getByRole('textbox',{name:'Message Moyai'}).fill('A shared task');await page.getByRole('button',{name:'Send message',exact:true}).click();
  await waitFor(()=>state.requests.some(req=>req.path==='/api/sessions'&&req.method==='POST'));assert.equal(state.requests.find(req=>req.path==='/api/sessions'&&req.method==='POST').body.visibility,'shared');
  const login=state.requests.find(req=>req.path==='/api/login');assert.equal(login.body.username,undefined);
  reset({signedIn:false,mode:'standalone',google:false});await page.goto(base+'/');await page.getByLabel('Username',{exact:true}).waitFor();assert.equal(await page.getByRole('button',{name:'Continue with Google'}).count(),0);
});

for(const privateSession of [false,true])test(`individual home defaults to shared with private opt-in: ${privateSession}`,async t=>{
  reset();const page=await pageFor(t,'');
  const privateChoice=page.getByRole('checkbox',{name:'Private session · Only you'});
  await privateChoice.waitFor();assert.equal(await privateChoice.isEnabled(),true);assert.equal(await privateChoice.isChecked(),false);
  await page.getByText(/Workspace members can see messages/).waitFor();
  if(privateSession)await privateChoice.check();
  await page.getByRole('textbox',{name:'Message Moyai'}).fill('A new task');await page.getByRole('button',{name:'Send message',exact:true}).click();
  await waitFor(()=>state.requests.some(req=>req.path==='/api/sessions'&&req.method==='POST'));
  assert.equal(state.requests.find(req=>req.path==='/api/sessions'&&req.method==='POST').body.visibility,privateSession?'private':'shared');
});

test('product Users editor exposes roles only and sends the captured optimistic revision',async t=>{
  reset();const page=await pageFor(t,'#settings/users');await page.getByRole('button',{name:'Edit role for teammate@example.test'}).click();
  const dialog=page.getByRole('dialog',{name:'Edit workspace role'});await dialog.waitFor();assert.equal(await dialog.getByRole('textbox').count(),0);assert.equal(await page.getByRole('button',{name:'Add user',exact:true}).count(),0);
  await dialog.getByRole('combobox',{name:'Role'}).click();await page.getByRole('option',{name:'Administrator',exact:true}).click();await dialog.getByRole('button',{name:'Save',exact:true}).click();
  await waitFor(()=>state.requests.some(req=>req.path==='/api/settings/users/role'));assert.deepEqual(state.requests.find(req=>req.path==='/api/settings/users/role').body,{email:'teammate@example.test',role:'admin',revision:7});
});

test('composition and late search responses cannot trigger a command or cross a route change',async t=>{
  reset();const page=await pageFor(t);await page.getByRole('textbox',{name:'Message Moyai'}).waitFor();
  let dialog=await palette(page),search=dialog.getByRole('combobox');
  await search.dispatchEvent('compositionstart');await search.fill('Rename');await search.press('Enter');await search.press('Escape');
  assert.equal(await dialog.isVisible(),true);assert.equal(await page.getByRole('dialog',{name:'Rename session',exact:true}).count(),0);
  await search.dispatchEvent('compositionend',{data:'Rename'});await search.fill('slow');
  await waitFor(()=>state.requests.some(req=>req.path==='/api/history/search'&&req.query.includes('q=slow')));
  await search.fill('needle');await dialog.getByText('Body-only needle <img src=x onerror=alert(1)>').waitFor();
  await page.evaluate(()=>{location.hash='settings/overview';});await dialog.waitFor({state:'hidden'});
  await page.getByRole('heading',{name:'Settings',exact:true}).waitFor();
  dialog=await palette(page);await dialog.getByRole('option',{name:/Saved earlier investigation/}).waitFor();
  assert.equal(await dialog.getByRole('option',{name:'Stale search result'}).count(),0);
  assert.equal(await dialog.getByRole('option',{name:/Rename session|Pin session/}).count(),0);
});

for(const individual of [false,true])test(`side chat defaults to shared and settings aliases replace the chat rail: individual=${individual}`,async t=>{
  reset({individual});const page=await pageFor(t);await page.getByRole('heading',{name:'Current native work',exact:true}).waitFor();
  await page.getByRole('button',{name:'Open side panel',exact:true}).click();await page.getByRole('button',{name:'Add panel tab',exact:true}).click();
  await page.getByRole('menuitem',{name:/Side chat/}).click();await page.getByRole('button',{name:'New side chat',exact:true}).click();
  await waitFor(()=>state.requests.some(req=>req.path==='/api/sessions'&&req.method==='POST'));
  assert.equal(state.requests.find(req=>req.path==='/api/sessions'&&req.method==='POST').body.visibility,'shared');
  await page.evaluate(()=>{location.hash='skills';});await page.getByRole('heading',{name:'Skills',exact:true}).waitFor();
  assert.equal(await page.locator('.app > .sidebar').count(),0);assert.equal(await page.locator('.ns-rail').count(),1);
});

test('role conflict stays in the editor without silently overwriting the new revision',async t=>{
  reset({roleConflict:true});const page=await pageFor(t,'#settings/users');await page.getByRole('button',{name:'Edit role for teammate@example.test'}).click();
  const dialog=page.getByRole('dialog',{name:'Edit workspace role'});
  await dialog.getByRole('combobox',{name:'Role'}).click();await page.getByRole('option',{name:'Administrator',exact:true}).click();await dialog.getByRole('button',{name:'Save',exact:true}).click();
  await dialog.getByRole('alert').getByText('This role changed since you opened it. Refresh and try again.').waitFor();
  assert.equal(state.requests.filter(req=>req.path==='/api/settings/users/role').length,1);
  await dialog.getByRole('button',{name:'Cancel',exact:true}).click();await dialog.waitFor({state:'hidden'});
});

for(const width of [1440,768,320])test(`unified sidebar routes history and limits its menu to read-only actions at ${width}px`,async t=>{
  reset();const page=await pageFor(t,'#run='+legacyId,width);
  await page.getByRole('heading',{name:'Saved earlier investigation',exact:true}).waitFor();
  if(width===320)await page.getByRole('button',{name:'Toggle conversations',exact:true}).click();
  const row=page.locator(`[data-session-key="legacy:${legacyId}"]`);
  await row.waitFor();assert.equal(await row.locator('a').getAttribute('href'),'#run='+legacyId);
  assert.equal(await row.locator('a').getAttribute('aria-current'),'page');
  assert.equal(await row.getByRole('button').count(),1);
  await row.getByRole('button',{name:'Session actions for Saved earlier investigation'}).click();
  const menu=page.getByRole('menu',{name:'Session actions for Saved earlier investigation'});
  assert.deepEqual(await menu.getByRole('menuitem').allTextContents(),['Open history','Copy session link']);
  await menu.getByRole('menuitem',{name:'Open history',exact:true}).click();
  assert.ok(!state.requests.some(req=>req.path.startsWith('/api/sessions/'+legacyId)));
  assert.ok(!state.requests.some(req=>req.path==='/api/pull-requests/statuses'&&req.body.session_ids.includes(legacyId)));
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
});

test('source collisions and identically named legacy folders survive paged inventory refresh without exposing native edits',async t=>{
  reset();const collision=sessionId;
  state.inventoryPages=[
    [{...session(collision),source:'legacy',key:'legacy:'+collision,title:'Legacy collision',read_only:true,folder:'Original folder',folder_id:'old-folder',pinned:false}],
    [{...session(collision),source:'native',key:'native:'+collision,title:'Native collision',folder:'',pinned:false}],
  ];
  const page=await pageFor(t,'#run='+legacyId);const history=page.locator(`[data-session-key="legacy:${collision}"]`);
  await history.waitFor();assert.equal(await page.getByRole('button',{name:'Folder actions for Original folder'}).count(),0);
  assert.equal(await page.getByRole('button',{name:/Original folder/}).count(),1);
  assert.ok(!state.requests.some(req=>req.path==='/api/pull-requests/statuses'));
  await page.getByRole('button',{name:'Show more sessions',exact:true}).click();
  const native=page.locator(`[data-session-key="native:${collision}"]`);await native.waitFor();assert.equal(await history.count(),1);
  assert.equal(await history.locator('a').getAttribute('href'),'#run='+collision);assert.equal(await native.locator('a').getAttribute('href'),'#'+collision);
  const pageReads=state.requests.filter(req=>req.path==='/api/history/sessions'&&req.query.includes('limit=100')).length;
  await waitFor(()=>state.requests.filter(req=>req.path==='/api/history/sessions'&&req.query.includes('limit=100')).length>pageReads,'inventory refresh');
  assert.equal(await history.count(),1);assert.equal(await native.count(),1);
  await page.getByRole('button',{name:'Show archived conversations',exact:true}).click();
  await waitFor(()=>state.requests.some(req=>req.path==='/api/history/sessions'&&req.query.includes('archived=true')&&!req.query.includes('cursor=')));
  assert.equal(await native.count(),0,'changing list scope resets loaded pages');
});
