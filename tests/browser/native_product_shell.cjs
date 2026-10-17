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
function session(id=sessionId) {return {id,title:'Current native work',owner_id:state.ownerId??'viewer',visibility:'private',surface:'web',model:'test-model',status:state.status,active_turn:['running','saving','waiting'].includes(state.status)?turnId:null,archived:false,pinned:false,...(state.swarm?{swarm_id:sessionId}:{}),...(state.sessionOverrides?.[id]||{}),...(state.titles?.[id]===undefined?{}:{title:state.titles[id]})};}
async function handle(req,res,next) {
  const url=new URL(req.url,'http://localhost');
  if(!url.pathname.startsWith('/api/'))return next();
  let body=''; for await(const chunk of req)body+=chunk;
  const data=body?JSON.parse(body):null;
  state.requests.push({path:url.pathname,query:url.search,method:req.method,body:data,headers:req.headers});
  if(state.holds?.[url.pathname])await state.holds[url.pathname];
  const json=(value,status=200)=>{res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(value));};
  if(url.pathname==='/api/auth/config')return json({mode:state.mode,password_enabled:state.password,google_enabled:state.google,access_enabled:state.access});
  if(url.pathname==='/api/login'){state.signedIn=true;return json({csrf:'fixture-csrf'});}
  if(url.pathname==='/api/auth/google/start')return json({url:'https://accounts.google.com/fixture'});
  if(url.pathname==='/api/logout'){state.signedIn=false;return json({signed_out:true});}
  if(!state.signedIn)return json({detail:'Sign in.'},401);
  if(url.pathname==='/api/me')return json({user:{id:'viewer',name:'Test viewer',role:'admin',individual:state.individual},csrf:'fixture-csrf',models:['test-model'],default_model:'test-model',product_mode:state.mode});
  if(url.pathname==='/api/sessions'&&req.method==='POST'){state.createdSession=true;return json(session('c'.repeat(32)));}
  if(url.pathname==='/api/sessions')return json(state.denied?[]:[session(),...(state.createdSession?[session('c'.repeat(32))]:[])]);
  if(url.pathname==='/api/connections'||url.pathname==='/api/session-folders')return json([]);
  if(url.pathname==='/api/pull-requests/statuses')return json(state.prStatuses||{});
  if(url.pathname==='/api/history/sessions'||url.pathname==='/api/history/search') {
    if(url.pathname==='/api/history/sessions'&&state.inventoryRows)return json({sessions:state.inventoryRows,ancestors:state.inventoryAncestors||[],has_more:false});
    const rows=[{...session(legacyId),key:'legacy:'+legacyId,source:'legacy',url:'/#run='+legacyId,title:'Saved earlier investigation',read_only:true,match:{snippet:'Body-only needle <img src=x onerror=alert(1)>'}},
      {...session(),key:'native:'+sessionId,source:'native',url:'/#'+sessionId}];
    if(state.createdSession)rows.push({...session('c'.repeat(32)),key:'native:'+'c'.repeat(32),source:'native',url:'/#'+'c'.repeat(32)});
    if(url.pathname==='/api/history/sessions'&&url.searchParams.get('limit')==='100'&&state.inventoryPages) {
      const page=url.searchParams.get('cursor')==='page-2'?1:0;
      return json({sessions:state.inventoryPages[page],has_more:page===0,next_cursor:page===0?'page-2':null});
    }
    if(url.searchParams.get('q')==='slow') {setTimeout(()=>json({sessions:[{id:'late',title:'Stale search result'}]}),700);return;}
    return json({sessions:rows,has_more:false});
  }
  if(url.pathname===`/api/history/sessions/${legacyId}`)return json({session:{...session(legacyId),source:'legacy',url:'/#run='+legacyId,title:'Saved earlier investigation',status:'completed',execution_engine:'legacy',read_only:true}});
  if(url.pathname===`/api/history/sessions/${sessionId}`)return json({session:{...session(),source:'native',url:'/#'+sessionId,read_only:false}});
  if(url.pathname.startsWith('/api/history/sessions/')&&state.sessionOverrides?.[url.pathname.split('/').at(-1)]) {
    const id=url.pathname.split('/').at(-1);return json({session:{...session(id),source:'native',url:'/#'+id,read_only:false}});
  }
  if(state.swarm && url.pathname.startsWith('/api/history/sessions/')) {
    const id=url.pathname.split('/').at(-1);
    if(state.swarm.members.some(member=>member.session_id===id))return json({session:{...session(id),source:'native',url:'/#'+id,read_only:false}});
  }
  if(state.createdSession&&url.pathname==='/api/history/sessions/'+'c'.repeat(32))return json({session:{...session('c'.repeat(32)),source:'native',url:'/#'+'c'.repeat(32),read_only:false}});
  if(url.pathname.startsWith(`/api/product/sessions/${legacyId}/`)) {
    if(url.pathname.endsWith('/messages'))return json({messages:[{id:8,role:'assistant',content:'Historical saved answer'}],has_more:false,next_offset:null});
    if(url.pathname.endsWith('/children'))return json({sessions:[],has_more:false,next_offset:null});
    if(url.pathname.endsWith('/files'))return json({files:[],sources:[],has_more:{},next_offset:null});
  }
  if(url.pathname==='/api/settings/users/role')return state.roleConflict ? json({detail:'This role changed since you opened it. Refresh and try again.'},409) : json(data);
  if(url.pathname==='/api/settings/users')return json({source:'product',note:'Organization sign-in manages accounts.',capabilities:{roles:true,create:false,update:false,delete:false},users:[{id:'teammate@example.test',email:'teammate@example.test',username:'teammate@example.test',display_name:'Teammate',role:'member',revision:7,enabled:true}]});
  if(url.pathname==='/api/settings/overview')return json({counts:{},capabilities:{}});
  if(url.pathname==='/api/skills') {
    if(state.skillError)return json({detail:'Temporary skill outage.'},503);
    return json({skills:(state.skills||[]).filter(row=>url.searchParams.get('shared')!=='true'||row.scope!=='personal'),has_more:false,next_offset:null});
  }
  if(url.pathname==='/api/settings/skills')return json({skills:[],capabilities:{}});
  if(url.pathname==='/api/settings/automations')return json({automations:[],capabilities:{}});
  if(url.pathname.startsWith('/api/turns/')&&req.method==='PATCH') {
    const message=state.messages.find(row=>row.turn_id===url.pathname.split('/')[3]);
    if(message)message.content=data.prompt;
    return json({updated:true});
  }
  if(url.pathname.startsWith('/api/sessions/')) {
    if(state.denied)return json({detail:'Session access revoked.'},403);
    if(url.pathname.endsWith('/workspace'))return json(state.workspace||{links:[],files:[],sources:[],subagents:[],subagents_limited:false});
    if(url.pathname.endsWith('/swarm'))return json(state.swarm);
    if(url.pathname.endsWith('/progress/'+turnId))return json({messages:[{id:'saved-progress',phase:'commentary',text:'I will inspect.'}]});
    if(url.pathname.endsWith('/events')) {
      res.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-cache','Connection':'keep-alive'});
      res.write('event: connected\ndata: {}\n\n'); clients.add(res);
      state.streams.push({offset:url.searchParams.get('offset'),last:req.headers['last-event-id']});
      res.on('close',()=>clients.delete(res));return;
    }
    if(url.pathname.endsWith('/messages')) {
      const messages=state.messagesBySession?.[url.pathname.split('/')[3]]||state.messages;
      if(req.method==='POST') {
        messages.push({id:messages.length+1,turn_id:'turn-skill',role:'user',author_id:'viewer',content:data.prompt});
        return json({id:'turn-skill',actor_id:'viewer',status:'completed'});
      }
      return json(messages);
    }
    if(url.pathname.endsWith('/plan'))return json(state.plan||[]);
    if(url.pathname.endsWith('/activity'))return json([]);
    if(url.pathname.endsWith('/workspace'))return json(state.workspaceData||{links:[],files:[],sources:[],subagents:[],subagents_limited:false});
    if(url.pathname.endsWith('/read'))return json({});
    if(req.method==='PATCH') {
      if(state.holdRename)await state.holdRename;
      if(state.renameError)return json({detail:'Could not save the session name.'},503);
      const id=url.pathname.split('/')[3];
      if(data.title!==undefined)state.titles={...state.titles,[id]:data.title};
      return json({updated:true});
    }
    const row=session(url.pathname.split('/')[3]);
    const result={session:row,reference:{...row,source:'native',url:'/#'+row.id,...state.reference},turns:state.turns||[{id:turnId,actor_id:'viewer',status:state.status,connections:[],usage:{}}],activity:[],pending:state.pending,has_unsettled_effects:state.effects?.length>0,effects:state.effects};
    if(state.holdSessionRead)await state.holdSessionRead;
    return json(result);
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

const renameRequests=()=>state.requests.filter(row=>row.method==='PATCH'&&row.body?.title!==undefined);
async function editTitle(page) {
  await page.getByRole('button',{name:/^Rename session:/}).click();
  const input=page.getByRole('textbox',{name:'Session name',exact:true});
  await input.waitFor();
  return input;
}

test('header rename trims, updates sidebar and document title, survives reload, and saves once on blur',async t=>{
  reset();const page=await pageFor(t);const input=await editTitle(page);
  assert.deepEqual(await input.evaluate(el=>[el.selectionStart,el.selectionEnd]),[0,'Current native work'.length]);
  await input.fill('  Release checklist  ');await input.press('Enter');
  await page.getByRole('button',{name:'Rename session: Release checklist',exact:true}).waitFor();
  assert.equal(await page.title(),'Release checklist · Moyai');
  assert.equal(await page.locator('.session-row.selected .session-title').innerText(),'Release checklist');
  assert.equal(await page.getByRole('button',{name:'Rename session: Release checklist',exact:true}).evaluate(el=>el===document.activeElement),true);
  assert.deepEqual(renameRequests().map(row=>row.body.title),['Release checklist']);
  await page.reload();
  await page.getByRole('button',{name:'Rename session: Release checklist',exact:true}).waitFor();
  const again=await editTitle(page);await again.fill('Release notes');
  await page.getByRole('textbox',{name:'Message Moyai',exact:true}).click();
  await page.getByRole('button',{name:'Rename session: Release notes',exact:true}).waitFor();
  assert.equal(renameRequests().length,2);
  assert.equal(await page.getByRole('textbox',{name:'Message Moyai',exact:true}).evaluate(el=>el===document.activeElement),true);
});

test('header editing cancels Escape, ignores blank and unchanged names, and retains the 300-character limit',async t=>{
  reset();const page=await pageFor(t);
  const trigger=page.getByRole('button',{name:'Rename session: Current native work',exact:true});
  await trigger.waitFor();await trigger.focus();await page.keyboard.press('Enter');
  const input=page.getByRole('textbox',{name:'Session name',exact:true});
  assert.equal(await input.getAttribute('maxlength'),'300');
  await input.fill('Do not save');await input.press('Escape');
  await trigger.waitFor();assert.equal(await trigger.evaluate(el=>el===document.activeElement),true);
  await trigger.press('Space');await input.fill('   ');await input.press('Enter');await trigger.waitFor();
  await trigger.click();await input.press('Enter');await trigger.waitFor();
  assert.equal(renameRequests().length,0);
});

test('header draft survives polls and failed saves, and IME Enter does not commit',async t=>{
  reset({renameError:true});const page=await pageFor(t);const input=await editTitle(page);
  await input.fill('Keep this draft');
  const reads=()=>state.requests.filter(row=>row.path===`/api/sessions/${sessionId}`&&row.method==='GET').length;
  const count=reads();state.titles={[sessionId]:'Background title'};
  await waitFor(()=>reads()>count,'background session poll');
  assert.equal(await input.inputValue(),'Keep this draft');
  await input.dispatchEvent('keydown',{key:'Enter',code:'Enter',isComposing:true});
  assert.equal(renameRequests().length,0);
  await input.press('Enter');await page.getByRole('alert').filter({hasText:'Could not save the session name.'}).waitFor();
  assert.equal(await input.inputValue(),'Keep this draft');
  state.renameError=false;
  await input.press('Enter');await page.getByRole('button',{name:'Rename session: Keep this draft',exact:true}).waitFor();
  assert.equal(renameRequests().length,2);
});

test('pending header save cannot be submitted twice or rename a newly opened session',async t=>{
  let release;reset({createdSession:true,holdRename:new Promise(resolve=>{release=resolve;})});
  t.after(()=>release());const page=await pageFor(t);const input=await editTitle(page);
  await input.fill('Saved on the first session');await input.press('Enter');
  await waitFor(()=>renameRequests().length===1,'pending title write');
  await input.press('Enter');await input.press('Tab');
  assert.equal(renameRequests().length,1);
  await page.locator(`.session-row a[href="#${'c'.repeat(32)}"]`).click();
  await page.waitForURL('**/#'+'c'.repeat(32));
  await page.getByRole('button',{name:'Rename session: Current native work',exact:true}).waitFor();
  release();
  await waitFor(()=>state.titles?.[sessionId]==='Saved on the first session','original write committed');
  await waitFor(()=>state.requests.filter(row=>row.path==='/api/history/sessions').length>1,'sidebar refreshed');
  assert.equal(await page.locator('.topbar h1').innerText(),'Current native work');
  assert.equal(state.titles?.['c'.repeat(32)],undefined);
});

test('a session read started before rename cannot restore the old title after save',async t=>{
  reset();const page=await pageFor(t);const input=await editTitle(page);
  let release;state.holdSessionRead=new Promise(resolve=>{release=resolve;});t.after(()=>release());
  const reads=()=>state.requests.filter(row=>row.path===`/api/sessions/${sessionId}`&&row.method==='GET').length;
  const count=reads();await input.fill('Keep the saved title');
  await waitFor(()=>reads()>count,'old session read in flight');
  await input.press('Enter');await page.getByRole('button',{name:'Rename session: Keep the saved title',exact:true}).waitFor();
  const response=page.waitForResponse(res=>new URL(res.url()).pathname===`/api/sessions/${sessionId}`&&res.request().method()==='GET');
  release();delete state.holdSessionRead;
  await (await response).finished();
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  assert.equal(await page.locator('.topbar h1').innerText(),'Keep the saved title');
  assert.equal(await page.title(),'Keep the saved title · Moyai');
});

test('losing ownership clears an open title draft without saving it',async t=>{
  reset();const page=await pageFor(t);const input=await editTitle(page);
  await input.fill('Private unsaved draft');state.ownerId='someone-else';
  await input.waitFor({state:'detached'});
  assert.equal(await page.getByRole('button',{name:/^Rename session:/}).count(),0);
  assert.equal(await page.locator('.topbar h1').innerText(),'Current native work');
  assert.equal(renameRequests().length,0);
});

test('non-owners and historical sessions have no editable header title',async t=>{
  reset({ownerId:'someone-else'});const page=await pageFor(t);
  await page.locator('.topbar h1').waitFor();await page.locator('.topbar h1').click();
  assert.equal(await page.getByRole('button',{name:/^Rename session:/}).count(),0);
  assert.equal(await page.getByRole('textbox',{name:'Session name',exact:true}).count(),0);
  await page.goto(base+'/#run='+legacyId);await page.getByText('Historical saved answer',{exact:true}).waitFor();
  assert.equal(await page.getByRole('button',{name:/^Rename session:/}).count(),0);
  assert.equal(renameRequests().length,0);
});

for(const width of [1440,768,320])test(`header editing fits at ${width}px with a long title`,async t=>{
  reset({titles:{[sessionId]:'A long session name '.repeat(15)}});const page=await pageFor(t,'#'+sessionId,width);
  const input=await editTitle(page);await input.fill('Another long title '.repeat(15));
  const bounds=await input.boundingBox();assert(bounds.width>50&&bounds.x>=0&&bounds.x+bounds.width<=width);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  if(width===320)assert.equal(await input.evaluate(el=>getComputedStyle(el).fontSize),'16px');
  await input.press('Enter');await page.getByRole('button',{name:/^Rename session: Another long title/}).waitFor();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
});

function swarm() {
  return {id:sessionId,status:'active',reason:'',revision:0,generation:1,round:1,goal:'Check the sum with independent methods.',
    budget_seconds:0,ends_at:null,leader_id:'worker-1',can_control:true,sequence:0,posts:[],has_more:false,next_after:0,
    members:[sessionId,'worker-1','worker-2','worker-3'].map((id,index)=>({
      session_id:id,name:index?'Agent '+index:'Coordinator',model:'test-model',coordinator:index===0,
      status:'running',active_turn:'turn-'+index,assignment:'',deleted:false,
    }))};
}

test('swarm Space and Chat retain drafts and worker navigation opens the full conversation',async t=>{
  reset({swarm:swarm()});const page=await pageFor(t);
  const space=page.getByRole('region',{name:'Live swarm space',exact:true});
  await space.waitFor();
  assert.equal(await page.locator('.transcript').isVisible(),false);
  const composer=page.getByRole('textbox',{name:'Message Moyai',exact:true});
  await composer.fill('Keep this unsent direction.');
  await page.getByRole('button',{name:'Chat',exact:true}).click();
  assert.equal(await page.locator('.transcript').isVisible(),true);
  assert.equal(await composer.innerText(),'Keep this unsent direction.');
  await page.getByRole('button',{name:'Space',exact:true}).click();
  assert.equal(await composer.innerText(),'Keep this unsent direction.');
  await page.getByRole('link',{name:'Agent 2 · Working · Open conversation',exact:true}).click();
  await page.waitForURL('**/#worker-2');
  await page.locator('.transcript').waitFor({state:'visible'});
  assert.equal(await space.count(),0);
  await page.getByRole('link',{name:'Open mission',exact:true}).click();
  await page.waitForURL('**/#'+sessionId);
  await space.waitFor();
});

for(const attention of ['question','effect'])test(`swarm reveals a pending ${attention} instead of hiding it behind Space`,async t=>{
  reset({swarm:swarm()});const page=await pageFor(t);
  await page.getByRole('region',{name:'Live swarm space',exact:true}).waitFor();
  if(attention==='question') {
    state.status='waiting';
    state.pending={calls:[{tool_call_id:'question',tool_name:'ask_user_question',args:{questions:[{
      header:'Approach',question:'Which method should I use?',options:[{label:'Iterative addition'},{label:'Pairing'}],
    }]}}],approvals:[]};
  } else {
    state.status='idle';
    state.effects=[{id:'effect-one',turn_id:turnId,tool_name:'shell',arguments:{command:'example requiring review'}}];
  }
  await page.getByRole('heading',{name:attention==='question'?'Your input is needed':'An action needs review',exact:true}).waitFor();
  assert.equal(await page.locator('.transcript').isVisible(),true);
  await page.getByRole('button',{name:'Space',exact:true}).click();
  assert.equal(await page.locator('.transcript').isVisible(),true);
  assert.equal(await page.getByRole('region',{name:'Live swarm space',exact:true}).count(),0);
});

test('home and inventory render while account/folder reads and decoration are held',async t=>{
  let release;const held=new Promise(resolve=>{release=resolve;});t.after(()=>release());
  reset({status:'idle',holds:{'/api/connections':held,'/api/session-folders':held}});
  const page=await browser.newPage();t.after(()=>page.close());
  await page.route('**/frontend/moyai-sculpture.ts',route=>route.abort());
  await page.goto(base+'/');
  await page.getByRole('textbox',{name:'Message Moyai'}).waitFor();
  await page.locator(`[data-session-key="native:${sessionId}"]`).waitFor();
  assert.ok(state.requests.some(req=>req.path==='/api/connections'));
  assert.equal(await page.getByText('Opening workspace…',{exact:true}).count(),0);
});

for(const width of [1440,768,320])test(`space home respects reduced motion and keeps controls reachable at ${width}px`,async t=>{
  reset({status:'idle'});const page=await pageFor(t,'',width);
  await page.getByRole('heading',{name:'What are we working on?',exact:true}).waitFor();
  await page.evaluate(()=>document.fonts.ready);
  assert.deepEqual(await page.locator('.decode-text').allTextContents(),['> what are we','  working on?▌']);
  const geometry=await page.locator('.decode-headline').boundingBox();
  await page.getByRole('textbox',{name:'Message Moyai'}).fill('A draft that must survive animation.');
  assert.equal(await page.locator('.decode-headline').evaluate(el=>el.getBoundingClientRect().height),geometry.height);
  assert.equal(await page.locator('.home').evaluate(el=>el.scrollWidth<=el.clientWidth),true);
  assert.equal(await page.getByRole('checkbox',{name:'Private session · Only you',exact:true}).isVisible(),true);
  assert.equal(await page.getByRole('button',{name:'Swarm',exact:true}).isVisible(),true);
});

test('reference meteor shaders render front, side and back and restore Home after remount',async t=>{
  reset({status:'idle'});
  const page=await browser.newPage({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
  t.after(()=>page.close());
  const errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.on('console',message=>{if(message.type()==='error'&&/WebGL|shader|texture|GLTF/i.test(message.text()))errors.push(message.text());});
  await page.route('**/static/models/moyai.glb',route=>route.fulfill({
    path:path.resolve('native/web/public/moyai.glb'),contentType:'model/gltf-binary',
  }));
  await page.goto(base+'/');
  const canvas=page.locator('.home-sculpture canvas');
  await canvas.waitFor({state:'visible'});
  const painted=()=>page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  await canvas.press('Home');await painted();
  const front=await canvas.screenshot();
  assert.ok(front.length>4000,'The actual model and textures must render, not an empty canvas.');
  for(let index=0;index<9;index++)await canvas.press('ArrowRight');
  await painted();const side=await canvas.screenshot();
  for(let index=0;index<9;index++)await canvas.press('ArrowRight');
  await painted();const back=await canvas.screenshot();
  assert.equal(front.equals(side),false);assert.equal(side.equals(back),false);
  await canvas.press('Home');await painted();
  assert.ok(front.equals(await canvas.screenshot()),'Home must restore the reference view.');
  await page.getByRole('link',{name:'idle Current native work',exact:true}).click();
  await canvas.waitFor({state:'detached'});
  await page.getByRole('button',{name:'New session ⌘⇧O',exact:true}).click();
  await canvas.waitFor({state:'visible'});await painted();
  assert.equal(await canvas.count(),1);
  assert.deepEqual(errors,[]);
});

test('decode animation settles on input and can be paused without losing the draft',async t=>{
  reset({status:'idle'});const page=await pageFor(t,'');
  await page.emulateMedia({reducedMotion:'no-preference'});
  await page.waitForFunction(()=>document.querySelector('.decode-headline')?.classList.contains('decoded'));
  const input=page.getByRole('textbox',{name:'Message Moyai'});
  await input.fill('Keep this draft while the headline pauses.');
  await page.getByRole('button',{name:'Pause headline animation',exact:true}).click();
  await page.waitForTimeout(2700);
  assert.equal(await input.innerText(),'Keep this draft while the headline pauses.');
  assert.deepEqual(await page.locator('.decode-text').allTextContents(),['> what are we','  working on?▌']);
  assert.equal(await page.locator('.decode-caret').evaluate(el=>getComputedStyle(el).animationName),'none');
  await page.getByRole('button',{name:'Resume headline animation',exact:true}).click();
  assert.equal(await input.innerText(),'Keep this draft while the headline pauses.');
});

test('sidebar PR icons retain multi-PR selection without numeric badges',async t=>{
  const pr=(number,state)=>({url:`https://github.com/example/project/pull/${number}`,repository:'example/project',
    number,title:`Review change ${number}`,state,draft:false,accessible:true});
  reset({status:'waiting',prStatuses:{[sessionId]:[pr(12,'open'),pr(13,'open'),pr(14,'merged')]}});
  const page=await pageFor(t,'');
  await page.locator('.session-pr-ready').waitFor();
  assert.equal(await page.locator('.session-pr-ready').innerText(),'PRs ready');
  assert.equal(await page.locator('.session-pr-merged').innerText(),'PR merged');
  assert.equal(await page.locator('.session-pr-count').count(),0);
  assert.equal(await page.locator('.session-pr-ready svg').count(),1);
  assert.equal(await page.locator('.session-meta-status.needs-input').innerText(),'Needs input');
  await page.locator('.session-pr-ready').click();
  await page.getByRole('button',{name:'Review change 12 example/project#12',exact:true}).waitFor();
  await page.getByRole('button',{name:'Review change 13 example/project#13',exact:true}).waitFor();
});

test('workspace overlay preserves chat geometry while the side panel resizes it',async t=>{
  reset({status:'completed',workspaceData:{links:[],files:[],sources:[],subagents_limited:false,
    subagents:[{id:'agent-one',parent_id:'',name:'Newton',avatar:0,status:'failed',can_inspect:true,started_at:'',depth:0}]}});
  const page=await pageFor(t);
  const key=page.getByRole('button',{name:'Open workspace',exact:true});
  await key.locator('.workspace-orbit-alert').waitFor();
  assert.equal(await key.getAttribute('aria-expanded'),'false');
  const before = await Promise.all([page.locator('.chat-content').boundingBox(),page.locator('.composer-wrap').boundingBox()]);
  await key.click();
  const dialog=page.getByRole('dialog',{name:'Workspace resources',exact:true});
  await dialog.waitFor();
  assert.deepEqual(await Promise.all([page.locator('.chat-content').boundingBox(),page.locator('.composer-wrap').boundingBox()]),before);
  assert.equal(await dialog.getByText('Failed',{exact:true}).count(),1);
  await page.keyboard.press('Escape');
  await dialog.waitFor({state:'detached'});
  assert.equal(await page.getByRole('button',{name:'Open workspace',exact:true}).locator('.workspace-orbit-alert').count(),1);
  assert.deepEqual(await page.locator('.chat-content').boundingBox(),before[0]);
  await page.getByRole('button',{name:'Open side panel',exact:true}).click();
  await page.getByRole('separator',{name:'Resize chat and side panel',exact:true}).waitFor();
  assert.ok((await page.locator('.chat-content').boundingBox()).width < before[0].width);
});

test('workspace attention refresh preserves the active skill picker and draft',async t=>{
  let release;const held=new Promise(resolve=>{release=resolve;});t.after(()=>release());
  reset({status:'completed',holds:{[`/api/sessions/${sessionId}/workspace`]:held},
    skills:[{id:'org-team',name:'team',description:'Team workflow',scope:'organization',reference:'org:team',icon:'code'}],
    workspaceData:{links:[],files:[],sources:[],subagents_limited:false,
      subagents:[{id:'agent-one',parent_id:'',name:'Newton',avatar:0,status:'failed',can_inspect:true,started_at:'',depth:0}]}});
  const page=await pageFor(t,'#'+sessionId,768);
  const input=page.getByRole('textbox',{name:'Message Moyai'});
  await input.fill('Use /team');
  const options=page.getByRole('listbox',{name:'Skills'});await options.waitFor();
  release();
  await page.locator('.workspace-orbit-alert').waitFor();
  assert.equal(await input.innerText(),'Use /team');
  assert.equal(await options.isVisible(),true);
  await input.press('Enter');
  await input.getByLabel('/team skill').waitFor();
  assert.match(await input.innerText(),/^Use Team /);
});

test('resolved conversation opens without plan/activity reads; plan loads only when opened',async t=>{
  let release;const held=new Promise(resolve=>{release=resolve;});t.after(()=>release());
  reset({status:'idle',holds:{[`/api/sessions/${sessionId}/plan`]:held},plan:[{id:'p',content:'Review the measurements',status:'pending'}]});
  const page=await pageFor(t);
  await page.getByText('Inspect this repository.',{exact:true}).waitFor();
  assert.equal(state.requests.filter(req=>req.path===`/api/sessions/${sessionId}`).length,1);
  assert.ok(state.requests.some(req=>req.query.includes('resolve=true')&&req.query.includes('include_activity=false')));
  assert.ok(!state.requests.some(req=>req.path===`/api/history/sessions/${sessionId}`||req.path.endsWith('/plan')||req.path.endsWith('/activity')));
  await page.getByRole('button',{name:'Open side panel',exact:true}).click();
  await page.getByRole('button',{name:'Add panel tab',exact:true}).click();
  await page.getByRole('menuitem',{name:/Plan/}).click();
  await page.getByText('Loading plan…',{exact:true}).waitFor();
  assert.equal(await page.getByRole('textbox',{name:'Message Moyai'}).isVisible(),true);
  release();await page.locator('.plan-row').filter({hasText:'Review the measurements'}).waitFor();
});

for(const width of [1440,768,320])test(`inline skills keep surrounding text, icon and scoped identity through send/reload at ${width}px`,async t=>{
  const skills=[
    {id:'personal-team',name:'team',description:'Investigate a bug with a team.',scope:'personal',reference:'personal:team',icon:'👥'},
    {id:'org-team',name:'team',description:'Organization workflow.',scope:'organization',reference:'org:team',icon:'code'},
  ];
  reset({status:'completed',skills});
  const page=await pageFor(t,'#',width), input=page.getByRole('textbox',{name:'Message Moyai'});
  await page.getByRole('checkbox',{name:'Private session · Only you',exact:true}).check();
  await input.fill('Please use /team');
  const options=page.getByRole('listbox',{name:'Skills'});
  await options.waitFor();assert.equal(await options.getByRole('option').count(),2);
  assert.match(await options.innerText(),/Personal/);assert.match(await options.innerText(),/Organization/);
  const box=await options.boundingBox();assert.ok(box.x>=0&&box.x+box.width<=width+1);
  await input.press('Enter');
  const mention=input.getByLabel('/team skill');await mention.waitFor();
  assert.equal(await mention.locator('svg[data-skill-icon="team"]').count(),1);
  assert.match(await input.innerText(),/^Please use Team /);
  assert.equal(state.requests.filter(r=>r.method==='POST'&&r.path.endsWith('/messages')).length,0);
  await input.pressSequentially('to inspect the issue.');
  await input.press('Enter');
  await waitFor(()=>state.requests.some(r=>r.method==='POST'&&r.path.endsWith('/messages')));
  const sent=state.requests.find(r=>r.method==='POST'&&r.path.endsWith('/messages'));
  assert.equal(sent.body.prompt,'Please use [/team](moyai-skill:personal%3Ateam?icon=%F0%9F%91%A5) to inspect the issue.');
  const saved=page.locator('.transcript').getByLabel('/team skill');await saved.waitFor();
  assert.equal(await saved.locator('svg[data-skill-icon="team"]').count(),1);
  state.skills=[];await page.reload();await saved.waitFor();
  assert.equal(await saved.locator('svg[data-skill-icon="team"]').count(),1);
  assert.match(await saved.locator('..').innerText(),/Please use Team to inspect the issue\./);
  state.skills=skills;
  await page.goto(base+'/#'+sessionId);await input.fill('/team');await options.waitFor();
  await input.press('ArrowDown');await input.press('Tab');
  await mention.waitFor();assert.match(await mention.getAttribute('title'),/org:team/);
  await input.press('Backspace');await input.press('Backspace');await input.press('Backspace');
  assert.equal(await mention.count(),0);
  await input.fill('/nomatch');await page.getByRole('status').filter({hasText:'No matching skills'}).waitFor();
  await input.fill('/team');await options.waitFor();await input.press('Escape');await options.waitFor({state:'hidden'});
  assert.equal(await input.innerText(),'/team');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
});

test('inline mention editing preserves middle-of-text insertion, undo, copy/paste and code literals',async t=>{
  reset({status:'completed',skills:[{id:'p',name:'team',description:'Team procedure',scope:'personal',reference:'personal:team',icon:'👥'}]});
  const page=await pageFor(t,'#'+sessionId),input=page.getByRole('textbox',{name:'Message Moyai'}),options=page.getByRole('listbox',{name:'Skills'});
  await input.fill('Before  after');await input.press('Home');for(let i=0;i<7;i++)await input.press('ArrowRight');
  await input.pressSequentially('/team');await options.waitFor();await input.press('Enter');
  await input.getByLabel('/team skill').waitFor();assert.match(await input.innerText(),/^Before Team  after$/);
  await input.press('ControlOrMeta+z');await input.getByLabel('/team skill').waitFor({state:'hidden'});
  assert.equal(await input.innerText(),'Before /team after');
  await input.press('ControlOrMeta+Shift+z');await input.getByLabel('/team skill').waitFor();
  await input.press('ControlOrMeta+a');
  const clipboard=await input.evaluate(el=>{
    const clipboardData=new DataTransfer();el.dispatchEvent(new ClipboardEvent('copy',{clipboardData,bubbles:true,cancelable:true}));
    return {text:clipboardData.getData('text/plain'),rich:clipboardData.getData('application/x-moyai-message')};
  });
  assert.equal(clipboard.text,'Before /team  after');assert.match(clipboard.rich,/moyai-skill:personal%3Ateam/);
  await input.fill('');
  await input.evaluate((el,data)=>{
    const clipboardData=new DataTransfer();clipboardData.setData('application/x-moyai-message',data.rich);clipboardData.setData('text/plain',data.text);
    el.dispatchEvent(new ClipboardEvent('paste',{clipboardData,bubbles:true,cancelable:true}));
  },clipboard);
  await input.getByLabel('/team skill').waitFor();assert.equal(await input.innerText(),'Before Team  after');
  await input.getByLabel('/team skill').click();
  await input.press('Delete');assert.equal(await input.getByLabel('/team skill').count(),0);
  await input.fill('Example: `use /team');await page.waitForTimeout(100);assert.equal(await options.count(),0);
  await input.fill('A new line');await input.press('Shift+Enter');await input.pressSequentially('then /team');await options.waitFor();
  await input.press('Enter');assert.match(await input.innerText(),/A new line\nthen Team/);
  const posts=state.requests.filter(r=>r.method==='POST'&&r.path.endsWith('/messages')).length;
  await input.dispatchEvent('keydown',{key:'Enter',isComposing:true});assert.equal(state.requests.filter(r=>r.method==='POST'&&r.path.endsWith('/messages')).length,posts);
});

test('editing a queued message retains its inline token and restores the unsent draft',async t=>{
  const token='[/team](moyai-skill:personal%3Ateam?icon=%F0%9F%91%A5)';
  reset({messages:[{id:1,turn_id:'queued',role:'user',author_id:'viewer',content:'Use '+token+' here.'}],
    turns:[{id:turnId,actor_id:'viewer',status:'running',connections:[]},{id:'queued',actor_id:'viewer',status:'queued',mode:'when_idle',connections:[]}]});
  const page=await pageFor(t),input=page.getByRole('textbox',{name:'Message Moyai'});
  await input.fill('Keep this unsent draft.');
  await page.locator('.queued-message').getByRole('button',{name:'Edit',exact:true}).click();
  await input.getByLabel('/team skill').waitFor();
  await input.press('End');await input.pressSequentially(' Revised.');
  await page.getByRole('button',{name:'Save queued message'}).click();
  await waitFor(()=>state.requests.some(row=>row.path==='/api/turns/queued'&&row.method==='PATCH'));
  assert.equal(state.requests.find(row=>row.path==='/api/turns/queued'&&row.method==='PATCH').body.prompt,'Use '+token+' here. Revised.');
  assert.equal(state.requests.filter(row=>row.method==='POST'&&row.path.endsWith('/messages')).length,0);
  await page.locator('.queued-message').getByLabel('/team skill').waitFor();
  await input.getByText('Keep this unsent draft.',{exact:true}).waitFor();
  assert.equal(await input.innerText(),'Keep this unsent draft.');
});

test('skill catalog retries and excludes personal skills in shared sessions',async t=>{
  reset({skillError:true,individual:false,skills:[{id:'p',name:'private-only',description:'Private',scope:'personal',reference:'personal:private-only'}]});
  const page=await pageFor(t,'#'),input=page.getByRole('textbox',{name:'Message Moyai'});
  await input.fill('/');await page.getByText('Couldn’t load your skills.').waitFor();
  state.skillError=false;await page.getByRole('button',{name:'Retry skills'}).click();
  await page.getByText('No matching skills available in shared sessions.').waitFor();
  assert.equal(await page.getByRole('option').count(),0);
});

for(const width of [1440,768,320])test(`palette keyboard, escaped body search, focus handoff and historical isolation at ${width}px`,async t=>{
  reset();const page=await pageFor(t,'#'+sessionId,width);await page.getByRole('heading',{name:'Current native work',exact:true}).waitFor();
  const input=page.getByRole('textbox',{name:'Message Moyai'});await input.focus();
  let dialog=await palette(page),search=dialog.getByRole('combobox');
  await dialog.getByRole('option',{name:'Saved earlier investigation',exact:false}).waitFor();
  await search.press('ArrowUp');assert.match(await dialog.locator('[role=option][aria-selected=true]').innerText(),/Current native work/);
  await search.press('ArrowDown');assert.match(await dialog.locator('[role=option][aria-selected=true]').innerText(),/Start session/);
  await search.press('Escape');await dialog.waitFor({state:'hidden'});
  await page.waitForFunction(()=>document.activeElement?.getAttribute('aria-label')==='Message Moyai');
  assert.equal(await input.evaluate(el=>el===document.activeElement),true);
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
  emit({type:'response_start',run_id:turnId,step:1},1);emit({type:'text_start',index:0,text:'I will inspect.',phase:'commentary'},2);
  await page.locator('.turn-progress-latest').getByText('I will inspect.',{exact:true}).waitFor();
  const polls=state.requests.filter(req=>req.path===`/api/sessions/${sessionId}/messages`).length;
  await waitFor(()=>state.requests.filter(req=>req.path===`/api/sessions/${sessionId}/messages`).length>polls,'saved poll');
  assert.equal(await page.locator('.turn-progress-latest').innerText(),'I will inspect.');
  emit({type:'tool_call',id:'call1',name:'read_file',arguments:{path:'private-tool-detail'}},3);
  await page.locator('.live-action').getByText('Using read file…',{exact:true}).waitFor();assert.equal(await page.getByText('private-tool-detail',{exact:true}).count(),0);
  for(const client of clients)client.end();await waitFor(()=>state.streams.length===2,'browser reconnect');assert.equal(state.streams[1].last,'3');
  emit({type:'text_delta',index:0,text:'duplicate'},2);emit({type:'response_start',run_id:turnId,step:2},4);emit({type:'text_start',index:0,text:'Wrong attempt'},5);
  emit({type:'response_start',run_id:turnId,step:2},6);emit({type:'text_start',index:0,text:'Final',phase:'final_answer'},7);emit({type:'text_delta',index:0,text:' answer'},8);
  await page.locator('.live-message').getByText('Final answer',{exact:true}).waitFor();assert.equal(await page.getByText(/Unlabelled child text|Wrong attempt|duplicate/).count(),0);
  state.messages.push({id:2,turn_id:turnId,role:'assistant',author_id:null,content:'Final answer'});state.status='completed';emit({},undefined,'finished');
  await page.waitForFunction(()=>document.querySelectorAll('.message.assistant:not(.live-message)').length===1);
  assert.equal(await page.getByText('Final answer',{exact:true}).count(),1);
  assert.equal(await page.getByText('I will inspect.',{exact:true}).count(),0);
  await page.getByRole('button',{name:'Worked',exact:true}).click();
  await page.getByText('I will inspect.',{exact:true}).waitFor();
  await page.reload();
  await page.getByText('Final answer',{exact:true}).waitFor();
  assert.equal(await page.getByText('I will inspect.',{exact:true}).count(),0);
  await page.getByRole('button',{name:'Worked',exact:true}).click();
  await page.getByText('I will inspect.',{exact:true}).waitFor();
});

test('access revocation removes transcript, draft and panel state before reauthorization',async t=>{
  reset();const page=await pageFor(t);const input=page.getByRole('textbox',{name:'Message Moyai'});await input.waitFor();await input.fill('private draft');await waitFor(()=>clients.size>0);
  emit({type:'response_start',run_id:turnId,step:1},0);emit({type:'text_start',index:0,text:'Sensitive live text'},1);await page.getByText('Sensitive live text',{exact:true}).waitFor();
  state.denied=true;emit({},undefined,'access_revoked');await page.getByText(/Session access revoked\./).waitFor();
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

for(const width of [1440,768,320])test(`ordinary subagent opens a full chat and sends to its own session at ${width}px`,async t=>{
  const childId='d'.repeat(32);
  reset({status:'idle',sessionOverrides:{[childId]:{title:'Curie',parent_session_id:sessionId}},
    messagesBySession:{[childId]:[{id:1,turn_id:'child-turn',role:'system',source:'delegation',author_id:null,content:'Evaluate the triangle side count.'}]},
    workspace:{links:[],files:[],sources:[],subagents:[{id:'child-turn',session_id:childId,parent_id:turnId,
      name:'Curie',avatar:1,status:'idle',can_inspect:true,depth:0,started_at:''}],subagents_limited:false}});
  const page=await pageFor(t,'#'+sessionId);
  await page.setViewportSize({width,height:900});
  await page.getByRole('button',{name:'Open workspace',exact:true}).click();
  await page.getByRole('region',{name:'Subagents',exact:true}).getByRole('button',{name:'Curie idle',exact:true}).click();
  await page.getByRole('heading',{name:'Curie',exact:true}).waitFor();
  assert.equal(new URL(page.url()).hash,'#'+childId);
  await page.getByText('Parent agent',{exact:true}).waitFor();
  await page.getByText('Evaluate the triangle side count.',{exact:true}).waitFor();
  const composer=page.getByRole('textbox',{name:'Message Moyai',exact:true});
  await composer.fill('Follow up with this subagent only.');
  await composer.press('Enter');
  await waitFor(()=>state.requests.some(r=>r.path===`/api/sessions/${childId}/messages`&&r.method==='POST'));
  assert.equal(state.requests.some(r=>r.path===`/api/sessions/${sessionId}/messages`&&r.method==='POST'),false);
  await page.getByRole('link',{name:'Parent chat',exact:true}).click();
  await page.getByRole('heading',{name:'Current native work',exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
});

for(const width of [1440,768,320])test(`sidebar nests three subagents and opens an independent side chat at ${width}px`,async t=>{
  const childIds=['d','e','f'].map(c=>c.repeat(32)),names=['Curie','Kepler','Newton'];
  reset({status:'idle',sessionOverrides:Object.fromEntries(childIds.map((id,i)=>[id,{title:names[i],parent_session_id:sessionId}])),
    messagesBySession:Object.fromEntries(childIds.map(id=>[id,[{id:1,turn_id:'child-turn',role:'system',source:'delegation',author_id:null,content:'Independent child assignment.'}]]))});
  state.inventoryRows=childIds.map(id=>({...session(id),source:'native',key:'native:'+id}));
  // The older parent is authorized context, outside the primary page.
  state.inventoryAncestors=[{...session(),source:'native',key:'native:'+sessionId}];
  const page=await pageFor(t,'#'+sessionId,width);
  if(width===320)await page.getByRole('button',{name:'Toggle conversations',exact:true}).click();
  const group=page.getByRole('group',{name:'Subagents of Current native work',exact:true});
  await group.waitFor();assert.equal(await group.locator('[data-session-key]').count(),3);
  const parent=page.locator(`[data-session-key="native:${sessionId}"]`);
  const child=group.locator(`[data-session-key="native:${childIds[0]}"]`);
  assert.ok((await child.boundingBox()).x>(await parent.boundingBox()).x+12);
  for(const id of childIds)assert.equal(await page.locator(`[data-session-key="native:${id}"]`).count(),1);
  await page.getByRole('button',{name:'Collapse subagents for Current native work',exact:true}).click();
  assert.equal(await group.isVisible(),false);
  await page.reload();
  if(width===320)await page.getByRole('button',{name:'Toggle conversations',exact:true}).click();
  await page.getByRole('button',{name:'Expand subagents for Current native work',exact:true}).click();
  const parentComposer=page.locator('.workspace > .chat [role="textbox"][aria-label="Message Moyai"]');
  if(width===320)await page.getByRole('button',{name:'Toggle conversations',exact:true}).click();
  await parentComposer.fill('Keep the parent draft.');
  if(width===320)await page.getByRole('button',{name:'Toggle conversations',exact:true}).click();
  await child.hover();await child.getByRole('link').press('Tab');
  await child.getByRole('button',{name:'Open Curie in side chat',exact:true}).click();
  const panel=page.getByRole('complementary',{name:'Session panel',exact:true});
  await panel.getByRole('heading',{name:'Curie',exact:true}).waitFor();
  assert.equal(new URL(page.url()).hash,'#'+sessionId);
  assert.equal(await parentComposer.textContent(),'Keep the parent draft.');
  const sideComposer=panel.getByRole('textbox',{name:'Message Moyai',exact:true});
  await sideComposer.fill('Message only the selected child.');await sideComposer.press('Enter');
  await waitFor(()=>state.requests.some(r=>r.path===`/api/sessions/${childIds[0]}/messages`&&r.method==='POST'));
  assert.equal(state.requests.some(r=>r.path===`/api/sessions/${sessionId}/messages`&&r.method==='POST'),false);
  await sideComposer.fill('Retain the side-chat draft.');
  await panel.getByRole('button',{name:'Hide side panel',exact:true}).click();
  if(width===320)await page.getByRole('button',{name:'Toggle conversations',exact:true}).click();
  await child.hover();await child.getByRole('link').press('Tab');await child.getByRole('button',{name:'Open Curie in side chat',exact:true}).click();
  await panel.getByRole('tab',{name:'Curie',exact:true}).waitFor();
  assert.equal(await panel.getByRole('tab',{name:'Curie',exact:true}).count(),1);
  assert.equal(await sideComposer.innerText(),'Retain the side-chat draft.');
  assert.equal(await parentComposer.textContent(),'Keep the parent draft.');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
});

test('workspace subagent action opens the right-panel conversation without leaving the parent',async t=>{
  const childId='d'.repeat(32);
  reset({status:'idle',sessionOverrides:{[childId]:{title:'Curie',parent_session_id:sessionId}},
    messagesBySession:{[childId]:[{id:1,turn_id:'child-turn',role:'assistant',author_id:null,content:'Saved child result.'}]},
    workspace:{links:[],files:[],sources:[],subagents:[{id:'child-turn',session_id:childId,parent_id:turnId,
      name:'Curie',avatar:1,status:'idle',can_inspect:true,depth:0,started_at:''}],subagents_limited:false}});
  const page=await pageFor(t,'#'+sessionId);
  await page.getByRole('button',{name:'Open workspace',exact:true}).click();
  await page.getByRole('region',{name:'Subagents',exact:true}).getByRole('button',{name:'Open Curie in side chat',exact:true}).click();
  const panel=page.getByRole('complementary',{name:'Session panel',exact:true});
  await panel.getByRole('heading',{name:'Curie',exact:true}).waitFor();
  await panel.getByText('Saved child result.',{exact:true}).waitFor();
  assert.equal(new URL(page.url()).hash,'#'+sessionId);
  assert.equal(await panel.getByRole('textbox',{name:'Message Moyai',exact:true}).isVisible(),true);
});
