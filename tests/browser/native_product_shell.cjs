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
  if(url.pathname==='/api/me')return json({user:{id:'viewer',name:'Test viewer',role:'admin',individual:state.individual},csrf:'fixture-csrf',models:state.models||['test-model'],default_model:'test-model',product_mode:state.mode,preferences:state.preferences||{delivery_mode:'when_idle',connection_ids:null}});
  if(url.pathname==='/api/sessions'&&req.method==='POST'){state.createdSession=true;const id='c'.repeat(32);state.sessionOverrides={...state.sessionOverrides,[id]:{...state.sessionOverrides?.[id],model:data.model||'test-model'}};return json(session(id));}
  if(url.pathname==='/api/sessions')return json(state.denied?[]:[session(),...(state.createdSession?[session('c'.repeat(32))]:[])]);
  if(url.pathname==='/api/connections') {
    if(state.connectionError && (url.searchParams.has('shared') || url.searchParams.has('session_id')))return json({detail:'Connections temporarily unavailable.'},503);
    return json(state.connectionsBySession?.[url.searchParams.get('session_id')]||state.connections||[]);
  }
  if(url.pathname==='/api/session-folders')return json(state.folders||[]);
  if(url.pathname==='/api/pull-requests/statuses')return json(state.prStatuses||{});
  if(url.pathname==='/api/history/sessions'||url.pathname==='/api/history/search') {
    if(url.pathname==='/api/history/sessions'&&state.inventoryError)return json({detail:'Inventory temporarily unavailable.'},503);
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
  if(url.pathname==='/api/settings/general') {
    if(req.method==='PATCH') {
      if(state.preferenceError)return json({detail:'Could not save preferences. Try again.'},503);
      state.preferences={delivery_mode:'when_idle',connection_ids:null,...state.preferences,...data};
    }
    return json(state.preferences||{delivery_mode:'when_idle',connection_ids:null});
  }
  if(url.pathname==='/api/settings/overview')return json({counts:{},capabilities:{}});
  if(url.pathname==='/api/skills') {
    if(state.skillError)return json({detail:'Temporary skill outage.'},503);
    return json({skills:(state.skillsBySession?.[url.searchParams.get('session_id')]||state.skills||[]).filter(row=>url.searchParams.get('shared')!=='true'||row.scope!=='personal'),has_more:false,next_offset:null});
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
    if(url.pathname.endsWith('/answers')&&req.method==='POST') {
      if(state.answerHold)await state.answerHold;
      if(state.answerError)return json({detail:'The answer could not be saved. Try again.'},503);
      state.status='idle';state.pending=undefined;state.answerAccepted=true;
      return json({id:'resumed-turn',status:'queued'},202);
    }
    if(url.pathname.endsWith('/workspace'))return json(state.workspace||state.workspaceData||{links:[],files:[],sources:[],subagents:[],subagents_limited:false});
    if(url.pathname.endsWith('/computer'))return json({available:false,has_workspace:true,controlled:false,busy:false});
    if(url.pathname.endsWith('/swarm'))return json(state.swarm);
    if(url.pathname.includes('/actions/')) {
      const key=decodeURIComponent(url.pathname.split('/actions/')[1]);
      if(state.actionHolds?.[key])await state.actionHolds[key];
      if(state.actionError)return json({detail:'Could not load action.'},state.actionError);
      const value=state.actionDetails?.[key];
      return value ? json(value) : json({detail:'Action not found.'},404);
    }
    if(url.pathname.endsWith('/progress/'+turnId))return json(state.workProgress||{messages:[{id:'saved-progress',phase:'commentary',text:'I will inspect.'}]});
    if(url.pathname.endsWith('/events')) {
      res.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-cache','Connection':'keep-alive'});
      res.write('event: connected\ndata: {}\n\n'); clients.add(res);
      state.streams.push({offset:url.searchParams.get('offset'),last:req.headers['last-event-id']});
      res.on('close',()=>clients.delete(res));return;
    }
    if(url.pathname.endsWith('/messages')) {
      const messages=state.messagesBySession?.[url.pathname.split('/')[3]]||state.messages;
      if(req.method==='POST') {
        if(state.goal && data.prompt.startsWith('/goal ')) {
          if(state.goalError)return json({detail:'Goal update temporarily unavailable.'},503);
          state.goalAccepted=true;
          if(data.prompt==='/goal pause')state.goal={...state.goal,status:'paused'};
          else if(data.prompt==='/goal resume')state.goal={...state.goal,status:'active'};
          else if(data.prompt.startsWith('/goal edit '))state.goal={...state.goal,objective:data.prompt.slice(11),generation:state.goal.generation+1};
          return json({goal:state.goal});
        }
        if(state.steeringAt&&data.mode==='asap') {
          const turn={id:'turn-steer',actor_id:'viewer',parent_turn:turnId,status:'accepted',mode:'asap',
            created_at:state.steeringAt,accepted_at:state.steeringAt,connections:[],usage:{}};
          state.turns.push(turn);
          messages.push({id:messages.length+1,turn_id:turn.id,role:'user',author_id:'viewer',
            content:data.prompt,created_at:state.steeringAt});
          return json(turn);
        }
        messages.push({id:messages.length+1,turn_id:'turn-skill',role:'user',author_id:'viewer',content:data.prompt});
        if(data.reply_to){state.status='idle';state.pending=undefined;state.answerAccepted=true;}
        return json({id:'turn-skill',actor_id:'viewer',status:'completed',model:data.model,connections:data.connections||[]});
      }
      return json(state.paginate ? messages.filter(row=>row.id>Number(url.searchParams.get('after')||0)).slice(0,200) : messages);
    }
    if(url.pathname.endsWith('/plan'))return json(state.plan||[]);
    if(url.pathname===`/api/sessions/${sessionId}/debug-trace`&&req.method==='GET')
      return state.traceError?json({detail:'Trace unavailable.'},503):json({schema_version:'ATIF-v1.8',session_id:sessionId,agent:{name:'moyai',version:'0.1.0'},steps:[],notes:'Fixture trace.',final_metrics:{total_steps:0}});
    if(url.pathname.endsWith('/activity'))return json([]);
    if(url.pathname.endsWith('/read'))return state.readError?json({detail:'Read marker unavailable.'},503):json({});
    if(url.pathname.endsWith('/stop')&&req.method==='POST') {
      state.status='stopped';
      if(state.turns)state.turns=state.turns.map(turn=>({...turn,status:'stopped'}));
      return json({status:'stopping'});
    }
    if(req.method==='PATCH') {
      if(state.holdPreference&&(data.pinned!==undefined||data.folder!==undefined))await state.holdPreference;
      if(state.preferenceError&&(data.pinned!==undefined||data.folder!==undefined))return json({detail:'Could not save sidebar preference.'},503);
      if(state.holdRename)await state.holdRename;
      if(state.renameError)return json({detail:'Could not save the session name.'},503);
      const id=url.pathname.split('/')[3];
      if(data.title!==undefined)state.titles={...state.titles,[id]:data.title};
      if(data.pinned!==undefined||data.folder!==undefined) {
        const patch={...(data.pinned===undefined?{}:{pinned:data.pinned}),...(data.folder===undefined?{}:{folder:data.folder})};
        state.sessionOverrides={...state.sessionOverrides,[id]:{...state.sessionOverrides?.[id],...patch}};
        if(state.inventoryRows)state.inventoryRows=state.inventoryRows.map(row=>row.id===id&&row.source!=='legacy'?{...row,...patch}:row);
      }
      return json({updated:true});
    }
    if(state.answerAccepted&&state.answerRefreshError)return json({detail:'Could not refresh the conversation.'},503);
    if(state.goalAccepted&&state.goalRefreshError)return json({detail:'Could not refresh the goal.'},503);
    const row=session(url.pathname.split('/')[3]);
    const result={session:row,reference:{...row,source:'native',url:'/#'+row.id,...state.reference},turns:state.turnsBySession?.[row.id]||state.turns||[{id:turnId,actor_id:'viewer',status:state.status,connections:[],usage:{}}],activity:[],pending:state.pending,can_answer:state.canAnswer,can_reply_with_text:state.canReply,question_deadline:state.questionDeadline,has_unsettled_effects:state.effects?.length>0,effects:state.effects};
    if(url.searchParams.get('include_messages')==='true'&&!state.omitInitialMessages)
      result.messages=(state.messagesBySession?.[row.id]||state.messages).slice(0,state.paginate?200:undefined);
    if(state.slackDelivery)result.slack_delivery=state.slackDelivery;
    if(state.goal)result.goal=state.goal;
    if(state.holdSessionRead)await state.holdSessionRead;
    return json(result);
  }
  return json({detail:'Fixture endpoint not implemented: '+url.pathname},404);
}
async function waitFor(predicate,label='condition') {const end=Date.now()+10000;while(!predicate()){if(Date.now()>end)throw new Error('Timed out: '+label);await new Promise(resolve=>setTimeout(resolve,25));}}
function emit(value,id,event) {for(const client of clients)client.write(`${id===undefined?'':`id: ${id}\n`}${event?`event: ${event}\n`:''}data: ${JSON.stringify(value)}\n\n`);}
async function pageFor(t,route='#'+sessionId,width=1440,initialize) {
  const page=await browser.newPage({viewport:{width,height:1000},reducedMotion:'reduce'});t.after(()=>page.close());
  const errors=[];page.on('pageerror',error=>errors.push(error.message));t.after(()=>assert.deepEqual(errors,[]));
  if(initialize)await initialize(page);
  await page.goto(base+'/'+route);return page;
}
for(const width of [1440,768,320])test(`ordinary question accepts a composer answer at ${width}px`,async t=>{
  reset({status:'waiting',canAnswer:true,canReply:true,questionDeadline:'2099-01-01T12:00:00Z',
    pending:{calls:[{tool_call_id:'color',tool_name:'ask_user_question',args:{questions:[{
      header:'Color',question:'Blue or green?',options:[{label:'Blue'},{label:'Green'}],
    }]}}],approvals:[]}});
  const page=await pageFor(t,'#'+sessionId,width);
  await page.getByText('Your reply will answer the pending question.',{exact:true}).waitFor();
  await page.getByRole('textbox',{name:'Message Moyai'}).fill('Blue, please.');
  const button=page.getByRole('button',{name:'Answer question',exact:true});
  assert.equal(await button.isEnabled(),true);
  await button.click();
  await waitFor(()=>state.requests.some(row=>row.path.endsWith('/messages')&&row.method==='POST'));
  const sent=state.requests.find(row=>row.path.endsWith('/messages')&&row.method==='POST');
  assert.equal(sent.body.reply_to,turnId);
  assert.equal(sent.body.prompt,'Blue, please.');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
});
test('another participant can queue a message without answering the requester question',async t=>{
  reset({status:'waiting',canAnswer:false,canReply:false});
  const page=await pageFor(t);
  await page.getByText('Waiting for the original requester’s response. New messages will be queued.',{exact:true}).waitFor();
  await page.getByRole('textbox',{name:'Message Moyai'}).fill('Additional context for later.');
  await page.getByRole('button',{name:'Send message',exact:true}).click();
  await waitFor(()=>state.requests.some(row=>row.path.endsWith('/messages')&&row.method==='POST'));
  const sent=state.requests.find(row=>row.path.endsWith('/messages')&&row.method==='POST');
  assert.equal(sent.body.reply_to,undefined);
  assert.equal(state.requests.some(row=>row.path.endsWith('/answers')),false);
});
function loginRequest() {
  return {calls:[{tool_call_id:'login-call',tool_name:'request_browser_login',args:{url:'https://portal.example.test',reason:'Inspect the protected report.'}}],
    approvals:[],metadata:{'login-call':{kind:'browser_login',site:'https://portal.example.test',reason:'Inspect the protected report.',workspace_id:'test-computer'}}};
}
for(const width of [1440,768,320])test(`browser sign-in opens Computer and submits an explicit acknowledgement at ${width}px`,async t=>{
  reset({status:'waiting',canAnswer:true,canReply:false,pending:loginRequest()});
  const page=await pageFor(t,'#'+sessionId,width);
  await page.getByRole('tab',{name:'Computer',exact:true}).waitFor();
  if(width<1000) {
    await page.getByRole('region',{name:'Browser sign-in instructions',exact:true}).waitFor();
    await page.getByRole('button',{name:'Back to sign-in request',exact:true}).click();
    await page.waitForFunction(()=>document.activeElement?.textContent==='I’ve signed in');
  }
  await page.getByRole('heading',{name:'Browser sign-in needed',exact:true}).waitFor();
  assert.equal(await page.locator('.browser-login-request').getByText('Inspect the protected report.',{exact:true}).count(),1);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
  await page.getByRole('button',{name:'I’ve signed in',exact:true}).click();
  await waitFor(()=>state.requests.some(row=>row.path.endsWith('/answers')));
  assert.deepEqual(state.requests.find(row=>row.path.endsWith('/answers')).body.browser_logins,{'login-call':'signed_in'});
});
test('browser sign-in respects panel dismissal, can reopen, and restores after reload',async t=>{
  reset({status:'waiting',canAnswer:true,pending:loginRequest()});
  const page=await pageFor(t);
  await page.getByRole('tab',{name:'Computer',exact:true}).waitFor();
  await page.getByRole('button',{name:'Close Computer tab',exact:true}).click();
  const readCount=()=>state.requests.filter(row=>row.path===`/api/sessions/${sessionId}`&&row.method==='GET').length;
  const previous=readCount();await waitFor(()=>readCount()>previous,'session poll after dismissal');
  assert.equal(await page.getByRole('tab',{name:'Computer',exact:true}).count(),0);
  await page.getByRole('button',{name:'Open Computer',exact:true}).click();
  await page.getByRole('tab',{name:'Computer',exact:true}).waitFor();
  await page.reload();
  await page.getByRole('tab',{name:'Computer',exact:true}).waitFor();
  await page.getByRole('button',{name:'Cancel sign-in',exact:true}).click();
  await waitFor(()=>state.requests.some(row=>row.path.endsWith('/answers')));
  assert.deepEqual(state.requests.find(row=>row.path.endsWith('/answers')).body.browser_logins,{'login-call':'cancelled'});
});
test('another viewer cannot open or complete the requester browser handoff',async t=>{
  reset({status:'waiting',canAnswer:false,pending:loginRequest()});
  const page=await pageFor(t);
  await page.getByRole('textbox',{name:'Message Moyai',exact:true}).waitFor();
  assert.equal(await page.getByRole('tab',{name:'Computer',exact:true}).count(),0);
  assert.equal(await page.getByRole('button',{name:'I’ve signed in',exact:true}).count(),0);
});
test('browser sign-in retry keeps its identity and accepted responses cannot be resubmitted',async t=>{
  reset({status:'waiting',canAnswer:true,pending:loginRequest(),answerError:true});
  const page=await pageFor(t);
  await page.getByRole('button',{name:'I’ve signed in',exact:true}).click();
  await page.getByRole('alert').filter({hasText:'The answer could not be saved'}).waitFor();
  const sent=()=>state.requests.filter(row=>row.path.endsWith('/answers'));
  state.answerError=false;state.answerRefreshError=true;
  await page.getByRole('button',{name:'I’ve signed in',exact:true}).click();
  await page.getByText('Your response was sent, but the conversation could not be refreshed.',{exact:true}).waitFor();
  assert.equal(sent().length,2);
  assert.equal(sent()[0].body.request_key,sent()[1].body.request_key);
  assert.equal(await page.getByRole('button',{name:'I’ve signed in',exact:true}).isEnabled(),false);
});
for(const width of [1440,768,320])test(`Activity downloads the session debug trace at ${width}px`,async t=>{
  reset({status:'idle'});
  const page=await pageFor(t,'#'+sessionId,width);
  await page.getByRole('button',{name:'Open side panel'}).click();
  await page.getByRole('button',{name:'Add panel tab'}).click();
  await page.getByRole('menuitem',{name:/Activity/}).click();
  const traceButton=page.getByRole('button',{name:'Get debug trace',exact:true});
  await traceButton.waitFor();
  const bounds=await traceButton.boundingBox();
  assert(bounds&&bounds.x>=0&&bounds.x+bounds.width<=width);
  await traceButton.click();
  await waitFor(()=>state.requests.some(row=>row.path===`/api/sessions/${sessionId}/debug-trace`));
  const request=state.requests.find(row=>row.path===`/api/sessions/${sessionId}/debug-trace`);
  assert.equal(request.method,'GET');
});
for(const width of [1440,768,320])test(`Main header downloads the session debug trace at ${width}px`,async t=>{
  reset({status:'idle'});
  const page=await pageFor(t,'#'+sessionId,width);
  const traceButton=page.getByRole('button',{name:'Debug trace',exact:true});
  await traceButton.waitFor();
  assert.equal(await traceButton.isVisible(),true);
  const bounds=await traceButton.boundingBox();
  assert(bounds&&bounds.x>=0&&bounds.x+bounds.width<=width);
  await traceButton.click();
  await waitFor(()=>state.requests.some(row=>row.path===`/api/sessions/${sessionId}/debug-trace`));
  const request=state.requests.find(row=>row.path===`/api/sessions/${sessionId}/debug-trace`);
  assert.equal(request.method,'GET');
});
test('Main header shows trace export errors without opening Activity',async t=>{
  reset({status:'idle',traceError:true});
  const page=await pageFor(t,'#'+sessionId,1440);
  await page.getByRole('button',{name:'Debug trace',exact:true}).click();
  const alert=page.getByRole('alert');
  await alert.waitFor();
  assert.match(await alert.textContent(),/Trace unavailable/);
});
async function palette(page) {await page.keyboard.press('Control+k');const dialog=page.getByRole('dialog',{name:'Commands and session search'});await dialog.waitFor();return dialog;}
before(async()=>{
  reset();const {createServer}=await import('vite');
  server=await createServer({configFile:path.resolve('native/web/vite.config.ts'),server:{host:'127.0.0.1',port:0,strictPort:false},plugins:[{name:'synthetic-native-api',configureServer(vite){vite.middlewares.use((req,res,next)=>{void handle(req,res,next).catch(error=>{res.writeHead(500);res.end(JSON.stringify({detail:error.message}));});});}}]});
  await server.listen();base=`http://127.0.0.1:${server.httpServer.address().port}`;browser=await chromium.launch({headless:true});
});
after(async()=>{for(const client of clients)client.end();await browser?.close();await server?.close();});

const renameRequests=()=>state.requests.filter(row=>row.method==='PATCH'&&row.body?.title!==undefined);
const preferenceRequests=()=>state.requests.filter(row=>row.method==='PATCH'&&(row.body?.pinned!==undefined||row.body?.folder!==undefined));
async function clickSidebarPin(page, name) {
  await page.locator('.session-row').filter({has:page.getByRole('button',{name,exact:true})}).hover();
  await page.getByRole('button',{name,exact:true}).click({timeout:5000});
}

async function groupSidebarByFolder(page) {
  await page.getByRole('combobox',{name:'Group conversations',exact:true}).click();
  await page.getByRole('option',{name:'By folder',exact:true}).click();
}

test('status groups reflect current work and keep filed, pinned and participated sessions reachable once',async t=>{
  reset({status:'idle',folders:[{id:'today',name:'Today'}]});
  const waiting='waiting',working='working',review='review',draft='draft',shared='shared',hidden='hidden',child='child';
  state.inventoryRows=[
    {...session(),source:'native',folder:'Today'},
    {...session(waiting),source:'native',status:'waiting'},
    {...session(working),source:'native',status:'queued'},
    {...session(review),source:'native',pinned:true},
    {...session(draft),source:'native'},
    {...session(shared),source:'native',owner_id:'teammate',participated:true},
    {...session(hidden),source:'native',owner_id:'teammate'},
    {...session(child),source:'native',status:'failed',parent_session_id:sessionId},
  ];
  const pr=(draft=false)=>({url:'https://github.com/example/project/pull/1',repository:'example/project',number:1,title:'Review the change',state:'open',draft,accessible:true});
  state.prStatuses={[review]:[pr()],[draft]:[pr(true)]};
  const page=await pageFor(t,'#');
  const group=key=>page.locator(`[data-status-group="${key}"]`);
  const row=id=>page.locator(`[data-session-key="native:${id}"]`);
  await group('in-review').locator(`[data-session-key="native:${review}"]`).waitFor();
  assert.equal(await group('needs-you').locator(`[data-session-key="native:${waiting}"]`).isVisible(),true);
  assert.equal(await group('needs-you').locator(`[data-session-key="native:${sessionId}"]`).isVisible(),true);
  assert.equal(await group('in-progress').locator(`[data-session-key="native:${working}"]`).isVisible(),true);
  for(const id of [draft,shared])assert.equal(await group('backlog').locator(`[data-session-key="native:${id}"]`).isVisible(),true);
  assert.equal(await row(hidden).count(),0);
  for(const id of [sessionId,waiting,working,review,draft,shared,child])assert.equal(await row(id).count(),1);
  await groupSidebarByFolder(page);
  await page.getByRole('button',{name:/^Today /}).click();
  assert.equal(await page.locator('#sessions-today').locator(`[data-session-key="native:${sessionId}"]`).isVisible(),true);
  assert.equal(await row(sessionId).count(),1);
  await page.getByRole('combobox',{name:'Group conversations',exact:true}).click();
  await page.getByRole('option',{name:'By status',exact:true}).click();
  state.inventoryRows.find(item=>item.id===waiting).status='running';
  await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
  await group('in-progress').locator(`[data-session-key="native:${waiting}"]`).waitFor();
  assert.equal(await row(waiting).count(),1);
});

function sidebarDefaultsFixture() {
  const child='d'.repeat(32),pinned='e'.repeat(32),filed='f'.repeat(32),shared='1'.repeat(32);
  reset({status:'idle',folders:[{id:'today',name:'Today'}],sessionOverrides:{
    [child]:{title:'Curie',parent_session_id:sessionId},
  }});
  state.inventoryRows=[
    {...session(),source:'native'},
    {...session(child),source:'native'},
    {...session(pinned),title:'Pinned work',pinned:true,source:'native'},
    {...session(filed),title:'Filed work',folder:'Today',source:'native'},
    {...session(shared),title:'Shared work',owner_id:'teammate',participated:true,source:'native'},
  ];
  return child;
}

for(const width of [1440,768,320])test(`sidebar starts with status groups open and keeps child routes collapsed at ${width}px`,async t=>{
  const child=sidebarDefaultsFixture();
  const page=await pageFor(t,'#',width);
  if(width===320)await page.getByRole('button',{name:'Toggle conversations',exact:true}).click();
  for(const name of [/^Needs you /,/^In progress /,/^In review /,/^Backlog /]) {
    const group=page.getByRole('button',{name});await group.waitFor();
    assert.equal(await group.getAttribute('aria-expanded'),'true');
  }
  assert.equal(await page.getByRole('button',{name:/^Recent /}).count(),0);
  assert.equal(await page.locator('[data-session-key]').count(),5);
  const expand=page.getByRole('button',{name:'Expand subagents for Current native work',exact:true});
  await expand.waitFor();
  assert.equal(await page.locator(`[data-session-key="native:${child}"]`).isVisible(),false);
  await expand.press('Enter');
  assert.equal(await page.locator(`[data-session-key="native:${child}"]`).isVisible(),true);
  await page.getByRole('button',{name:'Collapse subagents for Current native work',exact:true}).press('Space');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.goto(base+'/#'+child);
  if(width===320)await page.getByRole('button',{name:'Toggle conversations',exact:true}).click();
  await page.getByRole('heading',{name:'Curie',exact:true}).waitFor();
  await expand.waitFor();
  assert.equal(await page.locator(`[data-session-key="native:${child}"]`).isVisible(),false);
  const sidebar=await page.getByRole('navigation',{name:'Conversations',exact:true}).boundingBox();
  assert.ok(sidebar&&sidebar.x>=0&&sidebar.x+sidebar.width<=width);
});

test('explicit sidebar expansion survives reload and polling without opening new branches',async t=>{
  sidebarDefaultsFixture();
  const page=await pageFor(t,'#');
  await groupSidebarByFolder(page);
  await page.getByRole('button',{name:'Expand subagents for Current native work',exact:true}).click();
  await page.getByRole('button',{name:/^Participated /}).click();
  await page.getByRole('button',{name:/^Today /}).click();
  await page.getByRole('button',{name:/^Recent /}).click();
  await page.reload();
  await page.getByRole('button',{name:/^Today /}).waitFor();
  assert.equal(await page.getByRole('button',{name:/^Recent /}).getAttribute('aria-expanded'),'false');
  assert.equal(await page.getByRole('button',{name:/^Participated /}).getAttribute('aria-expanded'),'true');
  assert.equal(await page.getByRole('button',{name:/^Today /}).getAttribute('aria-expanded'),'true');
  await page.getByRole('button',{name:/^Recent /}).click();
  await page.getByRole('button',{name:'Collapse subagents for Current native work',exact:true}).waitFor();
  const newParent='2'.repeat(32),newChild='3'.repeat(32);
  state.inventoryRows.push({...session(newParent),source:'native',title:'New parent'},
    {...session(newChild),source:'native',title:'New child',parent_session_id:newParent});
  await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
  await page.getByRole('button',{name:'Expand subagents for New parent',exact:true}).waitFor();
  assert.equal(await page.locator(`[data-session-key="native:${newChild}"]`).isVisible(),false);
  await page.getByRole('button',{name:'Collapse subagents for Current native work',exact:true}).waitFor();
});

for(const stored of ['legacy','malformed','unavailable'])test(`sidebar safely adopts collapsed defaults with ${stored} storage`,async t=>{
  sidebarDefaultsFixture();
  const page=await pageFor(t,'#',1440,async page=>page.addInitScript(kind=>{
    if(kind==='unavailable'){
      Storage.prototype.getItem=()=>{throw new Error('Storage unavailable');};
      Storage.prototype.setItem=()=>{throw new Error('Storage unavailable');};
    }else{
      localStorage.setItem('moyai-native-folders:viewer',kind==='legacy'?JSON.stringify({version:2,closed:[]}):'{bad json');
      localStorage.setItem('moyai-subagents:viewer',kind==='legacy'?JSON.stringify(['native:'+'a'.repeat(32)]):JSON.stringify({version:1,expanded:{}}));
      localStorage.setItem('moyai-subagents:another-user',JSON.stringify({version:1,expanded:['native:'+'a'.repeat(32)]}));
    }
  },stored));
  await groupSidebarByFolder(page);
  await page.getByRole('button',{name:'Expand subagents for Current native work',exact:true}).click();
  await page.getByRole('button',{name:'Collapse subagents for Current native work',exact:true}).waitFor();
  assert.equal(await page.getByRole('button',{name:/^Recent /}).getAttribute('aria-expanded'),'true');
  assert.equal(await page.getByRole('button',{name:/^Participated /}).getAttribute('aria-expanded'),'false');
});

test('background inventory refresh stays quiet while explicit pagination shows loading and recovers from failure',async t=>{
  reset();
  state.inventoryPages=[[{...session(),source:'native'}],[{...session('4'.repeat(32)),source:'native',title:'Older work'}]];
  const page=await pageFor(t,'#');
  const more=page.getByRole('button',{name:'Show more sessions',exact:true});
  await more.waitFor();
  let release;const held=new Promise(resolve=>{release=resolve;});t.after(()=>release());
  state.holds={'/api/history/sessions':held};
  const reads=()=>state.requests.filter(r=>r.path==='/api/history/sessions').length;
  let count=reads();
  await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
  await waitFor(()=>reads()>count,'held background inventory read');
  assert.equal(await more.isVisible(),true);
  assert.equal(await more.isEnabled(),true);
  assert.equal(await page.getByRole('button',{name:'Loading…',exact:true}).count(),0);
  count=reads();await more.click();
  await waitFor(()=>reads()>count,'manual pagination replaces background read');
  const loading=page.getByRole('button',{name:'Loading…',exact:true});
  await loading.waitFor();assert.equal(await loading.isEnabled(),false);
  state.inventoryError=true;release();
  await more.waitFor();assert.equal(await more.isEnabled(),true);
  state.inventoryError=false;state.holds={};
  await more.click();
  await page.getByRole('link',{name:/Older work/}).waitFor();
  assert.equal(await loading.count(),0);
});

test('an empty sidebar shows initial loading but remains settled during later background reads',async t=>{
  let release;const held=new Promise(resolve=>{release=resolve;});t.after(()=>release());
  reset({status:'idle',inventoryRows:[],holds:{'/api/history/sessions':held}});
  const page=await pageFor(t,'#');
  await page.locator('.session-list').getByText('Loading sessions…',{exact:true}).waitFor();
  release();state.holds={};
  await page.getByText('No sessions here yet.',{exact:true}).waitFor();
  let releaseRefresh;const refreshHeld=new Promise(resolve=>{releaseRefresh=resolve;});t.after(()=>releaseRefresh());
  state.holds={'/api/history/sessions':refreshHeld};
  const count=state.requests.filter(r=>r.path==='/api/history/sessions').length;
  await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
  await waitFor(()=>state.requests.filter(r=>r.path==='/api/history/sessions').length>count);
  assert.equal(await page.getByText('Loading sessions…',{exact:true}).count(),0);
  assert.equal(await page.getByText('No sessions here yet.',{exact:true}).isVisible(),true);
});

test('sidebar pin and unpin render before a held save without blocking other actions or refreshing catalogs',async t=>{
  let release;const held=new Promise(resolve=>{release=resolve;});t.after(()=>release());
  reset({status:'idle',holdPreference:held});
  const page=await pageFor(t);
  await groupSidebarByFolder(page);
  await page.getByRole('button',{name:'Pin Current native work',exact:true}).waitFor();
  const catalogReads=()=>state.requests.filter(row=>['/api/connections','/api/session-folders','/api/history/sessions'].includes(row.path)).length;
  const reads=catalogReads();
  await clickSidebarPin(page,'Pin Current native work');
  await page.getByRole('button',{name:/^Pinned /}).click();
  const unpin=page.getByRole('button',{name:'Unpin Current native work',exact:true});
  await unpin.waitFor({timeout:500});
  assert.equal(await unpin.isEnabled(),true);
  assert.equal(await page.getByRole('button',{name:'New folder',exact:true}).isEnabled(),true);
  await clickSidebarPin(page,'Unpin Current native work');
  await page.getByRole('button',{name:'Pin Current native work',exact:true}).waitFor({timeout:500});
  assert.equal(preferenceRequests().length,1,'the second save waits for the first');
  release();
  await waitFor(()=>preferenceRequests().length===2);
  await page.getByText('Session unpinned.',{exact:true}).waitFor();
  assert.deepEqual(preferenceRequests().map(row=>row.body),[{pinned:true},{pinned:false}]);
  assert.equal(catalogReads(),reads,'personal edits do not refetch the inventory or catalogs');
  await page.reload();
  await page.getByRole('button',{name:'Pin Current native work',exact:true}).waitFor();
});

test('sidebar drag into and out of a folder renders while saves are held and preserves the pin',async t=>{
  let release;const held=new Promise(resolve=>{release=resolve;});t.after(()=>release());
  const id='11111111-1111-4111-8111-111111111111';
  reset({status:'idle',holdPreference:held,folders:[{id:'today',name:'Today'}],
    inventoryRows:[{...session(id),title:'Folder latency test',pinned:true,folder:'',source:'native'}]});
  const page=await pageFor(t,'#');
  await groupSidebarByFolder(page);
  await page.getByRole('button',{name:/^Pinned /}).click();
  const row=page.locator(`[data-session-key="native:${id}"]`);
  await row.waitFor();
  const today=page.locator('.session-group').filter({has:page.getByRole('button',{name:/^Today/})});
  const recent=page.locator('.session-group').filter({has:page.getByRole('button',{name:/^Recent/})});
  await row.dragTo(today.locator('.session-group-heading'));
  await today.locator(`[data-session-key="native:${id}"]`).waitFor({timeout:500});
  assert.equal(await row.getByRole('button',{name:'Unpin Folder latency test',exact:true}).isEnabled(),true);
  await row.dragTo(recent.locator('.session-group-heading'));
  await page.locator('#sessions-pinned').locator(`[data-session-key="native:${id}"]`).waitFor({timeout:500});
  release();
  await waitFor(()=>preferenceRequests().length===2);
  await page.getByText('Moved to Pinned.',{exact:true}).waitFor();
  assert.deepEqual(preferenceRequests().map(row=>row.body),[{folder:'Today'},{folder:''}]);
  await page.reload();
  await page.locator('#sessions-pinned').locator(`[data-session-key="native:${id}"]`).waitFor();
});

test('a rejected sidebar save restores placement and shows an error',async t=>{
  let release;const held=new Promise(resolve=>{release=resolve;});t.after(()=>release());
  reset({status:'idle',holdPreference:held,preferenceError:true});
  const page=await pageFor(t);
  await groupSidebarByFolder(page);
  await clickSidebarPin(page,'Pin Current native work');
  await page.getByRole('button',{name:/^Pinned /}).click();
  await page.getByRole('button',{name:'Unpin Current native work',exact:true}).waitFor({timeout:500});
  release();
  await page.getByRole('alert').filter({hasText:'Could not save sidebar preference.'}).waitFor();
  await page.getByRole('button',{name:'Pin Current native work',exact:true}).waitFor();
  assert.equal(await page.locator('#sessions-pinned').count(),0);
});

test('header and command palette see the current pin even when their conversation read is older',async t=>{
  let release;const held=new Promise(resolve=>{release=resolve;});t.after(()=>release());
  reset({status:'idle',holdPreference:held});
  const page=await pageFor(t);
  await clickSidebarPin(page,'Pin Current native work');
  await page.getByRole('button',{name:'Session actions',exact:true}).click();
  await page.getByRole('menuitem',{name:'Unpin',exact:true}).waitFor({timeout:500});
  await page.keyboard.press('Escape');
  const dialog=await palette(page);
  await dialog.getByRole('option',{name:'Unpin session',exact:true}).waitFor({timeout:500});
  release();
});
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
  await page.getByRole('heading',{name:attention==='question'?'Question':'An action needs review',exact:true}).waitFor();
  assert.equal(await page.locator('.transcript').isVisible(),true);
  await page.getByRole('button',{name:'Space',exact:true}).click();
  assert.equal(await page.locator('.transcript').isVisible(),true);
  assert.equal(await page.getByRole('region',{name:'Live swarm space',exact:true}).count(),0);
});

test('home and inventory render while account and folder reads are held',async t=>{
  let release;const held=new Promise(resolve=>{release=resolve;});t.after(()=>release());
  reset({status:'idle',holds:{'/api/connections':held,'/api/session-folders':held}});
  const page=await browser.newPage();t.after(()=>page.close());
  await page.goto(base+'/');
  await page.getByRole('textbox',{name:'Message Moyai'}).waitFor();
  await page.locator(`[data-session-key="native:${sessionId}"]`).waitFor();
  assert.ok(state.requests.some(req=>req.path==='/api/connections'));
  assert.equal(await page.getByText('Opening workspace…',{exact:true}).count(),0);
});

for(const width of [1440,768,320])test(`home keeps the draft and controls reachable at ${width}px`,async t=>{
  reset({status:'idle'});const page=await pageFor(t,'',width);
  const heading=page.getByRole('heading',{name:'What are we building?',exact:true});
  await heading.waitFor();
  await page.evaluate(()=>document.fonts.ready);
  const geometry=await heading.boundingBox();
  const input=page.getByRole('textbox',{name:'Message Moyai'});
  await input.fill('A draft that must stay ready to edit.');
  assert.deepEqual(await heading.boundingBox(),geometry,'Typing must not shift the home layout.');
  assert.equal(await input.innerText(),'A draft that must stay ready to edit.');
  assert.equal(await page.locator('.home').evaluate(el=>el.scrollWidth<=el.clientWidth),true);
  assert.equal(await page.getByRole('button',{name:'Private session',exact:true}).isVisible(),true);
  assert.equal(await page.getByRole('button',{name:'Swarm',exact:true}).count(),0);
  assert.equal(await page.locator('.home .account-selection, .home .suggestions, .home-mode-options').count(),0);
  assert.equal(await page.getByText('Drop files or paste images · Enter to send',{exact:true}).isVisible(),true);
  assert.equal(await page.locator('.home-composer').getByRole('button',{name:'Private session',exact:true}).isVisible(),true);
  assert.equal(await page.getByRole('button',{name:'Review code',exact:true}).isVisible(),false);
  await input.fill('');
  assert.equal(await page.getByRole('button',{name:'Review code',exact:true}).isVisible(),true);
});

for(const [label,prompt] of [
  ['Review code','Review my changes for bugs and improvements.'],
  ['Fix a bug','Help me investigate and fix this bug: '],
  ['Plan a feature','Help me plan and implement this feature: '],
])test(`home starter "${label}" opens an editable draft without sending`,async t=>{
  reset({status:'idle'});const page=await pageFor(t,'');
  await page.getByRole('button',{name:label,exact:true}).click();
  const input=page.getByRole('textbox',{name:'Message Moyai'});
  await page.waitForFunction(expected=>document.querySelector('.skill-editor')?.textContent===expected,prompt);
  assert.equal(await input.evaluate(el=>el===document.activeElement),true);
  assert.equal(state.requests.some(row=>row.method==='POST'&&(row.path==='/api/sessions'||row.path.endsWith('/messages'))),false);
  await page.keyboard.type(' Please focus on the current branch.');
  await page.getByRole('button',{name:'Send message',exact:true}).click();
  await waitFor(()=>state.requests.some(row=>row.method==='POST'&&row.path.endsWith('/messages')));
  assert.equal(state.requests.find(row=>row.method==='POST'&&row.path.endsWith('/messages')).body.prompt,prompt+' Please focus on the current branch.');
});

test('home draft survives model and privacy changes before sending',async t=>{
  reset({status:'idle',models:['test-model','second-model']});const page=await pageFor(t,'');
  await page.getByRole('button',{name:'Plan a feature',exact:true}).click();
  const input=page.getByRole('textbox',{name:'Message Moyai'});
  await input.fill('Plan a feature that exports the selected report.');
  await page.getByRole('button',{name:'Private session',exact:true}).click();
  await page.getByRole('combobox',{name:'Model',exact:true}).click();
  await page.getByRole('option',{name:'second-model',exact:true}).click();
  assert.equal(await input.innerText(),'Plan a feature that exports the selected report.');
  await page.getByRole('button',{name:'Send message',exact:true}).click();
  await waitFor(()=>state.requests.some(row=>row.method==='POST'&&row.path.endsWith('/messages')));
  const sent=state.requests.find(row=>row.method==='POST'&&row.path.endsWith('/messages'));
  assert.equal(sent.body.prompt,'Plan a feature that exports the selected report.');
  assert.equal(sent.body.model,'second-model');
  assert.equal(state.requests.find(row=>row.method==='POST'&&row.path==='/api/sessions').body.visibility,'private');
});

for(const width of [1440,320])test(`selected chat keeps its highlight while actions hide at ${width}px`,async t=>{
  reset({status:'idle'});const page=await pageFor(t,'#'+sessionId,width);
  if(width<760)await page.getByRole('button',{name:'Toggle conversations',exact:true}).click();
  const row=page.locator(`[data-session-key="native:${sessionId}"]`);
  await row.getByRole('link').click();
  if(width<760 && !(await row.isVisible()))await page.getByRole('button',{name:'Toggle conversations',exact:true}).click();
  await page.mouse.move(width-2,2);
  assert.ok((await row.getAttribute('class')).includes('selected'));
  assert.notEqual(await row.evaluate(el=>getComputedStyle(el).backgroundColor),'rgba(0, 0, 0, 0)');
  assert.equal(await row.locator('.session-row-actions').evaluate(el=>getComputedStyle(el).opacity),'0');
  await row.hover();
  assert.equal(await row.locator('.session-row-actions').evaluate(el=>getComputedStyle(el).opacity),'1');
  await row.getByRole('link').press('Tab');
  await page.mouse.move(width-2,2);
  assert.equal(await row.locator('.session-row-actions').evaluate(el=>getComputedStyle(el).opacity),'1');
});

test('sidebar PR icons retain multi-PR selection without numeric badges',async t=>{
  const pr=(number,state)=>({url:`https://github.com/example/project/pull/${number}`,repository:'example/project',
    number,title:`Review change ${number}`,state,draft:false,accessible:true});
  reset({status:'waiting',prStatuses:{[sessionId]:[pr(12,'open'),pr(13,'open'),pr(14,'merged')]}});
  const page=await pageFor(t,'');
  await page.locator(`[data-session-key="native:${sessionId}"]`).hover();
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

test('conversation bootstrap renders messages without a second transcript request',async t=>{
  let release;const held=new Promise(resolve=>{release=resolve;});t.after(()=>release());
  reset({status:'idle',holds:{[`/api/sessions/${sessionId}/messages`]:held}});
  const page=await pageFor(t);
  await page.getByText('Inspect this repository.',{exact:true}).waitFor({timeout:3000});
  assert.equal(await page.getByRole('textbox',{name:'Message Moyai'}).isVisible(),true);
  const reads=state.requests.filter(req=>req.method==='GET'&&req.path.startsWith(`/api/sessions/${sessionId}`));
  assert.equal(reads.filter(req=>req.path===`/api/sessions/${sessionId}`).length,1);
  assert.ok(reads.some(req=>req.query.includes('resolve=true')&&req.query.includes('include_messages=true')));
  assert.equal(reads.filter(req=>req.path.endsWith('/messages')).length,0);
});

test('an empty bootstrap transcript does not trigger a redundant history read',async t=>{
  reset({status:'idle',messages:[]});
  const page=await pageFor(t);
  await page.getByText('Start with a task or a question.',{exact:true}).waitFor();
  assert.equal(state.requests.filter(req=>req.method==='GET'&&req.path.endsWith('/messages')).length,0);
});

test('an older server without bootstrap messages still opens the conversation',async t=>{
  reset({status:'idle',omitInitialMessages:true});
  const page=await pageFor(t);
  await page.getByText('Inspect this repository.',{exact:true}).waitFor();
  assert.equal(state.requests.filter(req=>req.method==='GET'&&req.path.endsWith('/messages')).length,1);
});

function savedComposer(overrides={}) {
  reset({status:'idle',models:['test-model','saved-model','edited-model'],
    sessionOverrides:{[sessionId]:{model:'saved-model'}},
    turns:[{id:turnId,actor_id:'viewer',status:'completed',connections:['saved'],usage:{}}],
    connections:[{id:'saved',name:'Saved account',owner_id:'viewer',kind:'linear',enabled:true,shared:true,read_only:true}],
    ...overrides});
}

for(const entry of ['resolved','older-server','standalone'])for(const failure of [false,true])
test(`saved composer settings precede ${failure?'failed':'pending'} read receipt: ${entry}`,async t=>{
  let release;const held=new Promise(resolve=>{release=resolve;});t.after(()=>release());
  savedComposer({mode:entry==='standalone'?'standalone':'existing',omitInitialMessages:entry==='older-server',
    holds:failure?{}:{[`/api/sessions/${sessionId}/read`]:held},readError:failure});
  const page=await pageFor(t);
  await page.getByRole('textbox',{name:'Message Moyai'}).waitFor();
  await waitFor(()=>state.requests.some(req=>req.path.endsWith('/read')));
  if(failure)await page.getByText(/Read marker unavailable/).waitFor();
  assert.equal(await page.getByRole('combobox',{name:'Model',exact:true}).innerText(),'saved-model');
  assert.equal(await page.locator('.account-selection').count(),0);
  await page.getByRole('textbox',{name:'Message Moyai'}).fill('Send with the saved settings.');
  await page.getByRole('button',{name:'Send message',exact:true}).click();
  await waitFor(()=>state.requests.some(req=>req.path.endsWith('/messages')&&req.method==='POST'));
  const sent=state.requests.find(req=>req.path.endsWith('/messages')&&req.method==='POST');
  assert.equal(sent.body.model,'saved-model');assert.deepEqual(sent.body.connections,['saved']);
});

test('read receipt completion cannot overwrite manual composer choices',async t=>{
  let release;const held=new Promise(resolve=>{release=resolve;});t.after(()=>release());
  savedComposer({preferences:{delivery_mode:'when_idle',connection_ids:[]},holds:{[`/api/sessions/${sessionId}/read`]:held}});
  const page=await pageFor(t),model=page.getByRole('combobox',{name:'Model',exact:true});
  await model.waitFor();await waitFor(()=>state.requests.some(req=>req.path.endsWith('/read')));
  await model.click();await page.getByRole('option',{name:'edited-model',exact:true}).click();
  const response=page.waitForResponse(r=>r.url().endsWith('/read'));
  release();await (await response).finished();
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  assert.equal(await model.innerText(),'edited-model');
  await page.getByRole('textbox',{name:'Message Moyai'}).fill('Keep my preferences.');
  await page.getByRole('button',{name:'Send message',exact:true}).click();
  await waitFor(()=>state.requests.some(req=>req.path.endsWith('/messages')&&req.method==='POST'&&req.body.prompt==='Keep my preferences.'));
  const sent=state.requests.find(req=>req.body?.prompt==='Keep my preferences.');
  assert.equal(sent.body.model,'edited-model');assert.deepEqual(sent.body.connections,[]);
});

test('resolved composer never inherits another author’s selected accounts',async t=>{
  savedComposer({turns:[{id:turnId,actor_id:'teammate',status:'completed',connections:['saved'],usage:{}}]});
  const page=await pageFor(t);
  await page.getByRole('textbox',{name:'Message Moyai'}).waitFor();
  await page.getByRole('textbox',{name:'Message Moyai'}).fill('Use my own connections.');
  await page.getByRole('button',{name:'Send message',exact:true}).click();
  await waitFor(()=>state.requests.some(req=>req.path.endsWith('/messages')&&req.method==='POST'));
  assert.deepEqual(state.requests.find(req=>req.path.endsWith('/messages')&&req.method==='POST').body.connections,[]);
  assert.equal(await page.getByRole('combobox',{name:'Model',exact:true}).innerText(),'saved-model');
});

test('side-chat composer initializes before its read receipt completes',async t=>{
  const childId='d'.repeat(32);
  let release;const held=new Promise(resolve=>{release=resolve;});t.after(()=>release());
  savedComposer({sessionOverrides:{[childId]:{title:'Curie',model:'saved-model',parent_session_id:sessionId}},
    holds:{[`/api/sessions/${childId}/read`]:held},
    workspace:{links:[],files:[],sources:[],subagents:[{id:'child-turn',session_id:childId,parent_id:turnId,
      name:'Curie',avatar:1,status:'idle',can_inspect:true,depth:0,started_at:''}],subagents_limited:false}});
  const page=await pageFor(t);
  await page.getByRole('button',{name:'Open workspace',exact:true}).click();
  await page.getByRole('region',{name:'Subagents',exact:true}).getByRole('button',{name:'Open Curie in side chat',exact:true}).click();
  const panel=page.getByRole('complementary',{name:'Session panel',exact:true});
  await panel.getByRole('textbox',{name:'Message Moyai'}).waitFor();
  await waitFor(()=>state.requests.some(req=>req.path===`/api/sessions/${childId}/read`));
  assert.equal(await panel.getByRole('combobox',{name:'Model',exact:true}).innerText(),'saved-model');
  await panel.getByRole('textbox',{name:'Message Moyai'}).fill('Continue the side chat.');
  await panel.getByRole('button',{name:'Send message',exact:true}).click();
  await waitFor(()=>state.requests.some(req=>req.path===`/api/sessions/${childId}/messages`&&req.method==='POST'));
  assert.deepEqual(state.requests.find(req=>req.path===`/api/sessions/${childId}/messages`&&req.method==='POST').body.connections,['saved']);
});

test('saved composer blocks a fast send until its selected account catalog is authorized',async t=>{
  let release;const held=new Promise(resolve=>{release=resolve;});t.after(()=>release());
  savedComposer({holds:{'/api/connections':held,[`/api/sessions/${sessionId}/read`]:held}});
  const page=await pageFor(t);
  await page.getByRole('textbox',{name:'Message Moyai'}).fill('Do not drop my saved account.');
  await waitFor(()=>state.requests.some(req=>req.path.endsWith('/read')));
  assert.equal(await page.getByRole('button',{name:'Send message',exact:true}).isEnabled(),false);
  assert.equal(state.requests.some(req=>req.path.endsWith('/messages')&&req.method==='POST'),false);
  release();
  await page.getByRole('button',{name:'Send message',exact:true}).click();
  await waitFor(()=>state.requests.some(req=>req.path.endsWith('/messages')&&req.method==='POST'));
  assert.deepEqual(state.requests.find(req=>req.path.endsWith('/messages')&&req.method==='POST').body.connections,['saved']);
});

test('newly accepted composer preserves choices across its first read receipt',async t=>{
  const created='c'.repeat(32);
  let release;const held=new Promise(resolve=>{release=resolve;});t.after(()=>release());
  savedComposer({holds:{[`/api/sessions/${created}/read`]:held}});
  const page=await pageFor(t,'#'),model=page.getByRole('combobox',{name:'Model',exact:true});
  await model.click();await page.getByRole('option',{name:'saved-model',exact:true}).click();
  await page.getByRole('textbox',{name:'Message Moyai'}).fill('Start with these settings.');
  await page.getByRole('button',{name:'Send message',exact:true}).click();
  await page.waitForURL(base+'/#'+created);
  await waitFor(()=>state.requests.some(req=>req.path===`/api/sessions/${created}/read`));
  assert.equal(await model.innerText(),'saved-model');
  await model.click();await page.getByRole('option',{name:'edited-model',exact:true}).click();

  const response=page.waitForResponse(r=>r.url().endsWith('/read'));
  release();await (await response).finished();
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  assert.equal(await model.innerText(),'edited-model');
  await page.getByRole('textbox',{name:'Message Moyai'}).fill('Keep the accepted connections.');
  await page.getByRole('button',{name:'Send message',exact:true}).click();
  await waitFor(()=>state.requests.some(req=>req.body?.prompt==='Keep the accepted connections.'));
  assert.deepEqual(state.requests.find(req=>req.body?.prompt==='Keep the accepted connections.').body.connections,['saved']);
});

test('revoked composer clears protected state and reinitializes on an authorized retry',async t=>{
  let release;const held=new Promise(resolve=>{release=resolve;});t.after(()=>release());
  savedComposer({holds:{[`/api/sessions/${sessionId}/read`]:held}});
  const page=await pageFor(t);
  await page.getByRole('textbox',{name:'Message Moyai'}).fill('Private draft');
  await waitFor(()=>state.requests.some(req=>req.path.endsWith('/read')));
  state.denied=true;release();
  await page.getByRole('status').getByText(/Session access revoked/).waitFor();
  assert.equal(await page.getByRole('textbox',{name:'Message Moyai'}).count(),0);
  assert.equal(await page.getByText('Inspect this repository.',{exact:true}).count(),0);
  state.denied=false;state.sessionOverrides[sessionId].model='edited-model';state.turns[0].connections=[];
  await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
  await page.getByRole('textbox',{name:'Message Moyai'}).waitFor();
  assert.equal(await page.getByRole('combobox',{name:'Model',exact:true}).innerText(),'edited-model');
  assert.equal((await page.getByRole('textbox',{name:'Message Moyai'}).innerText()).trim(),'');
  await page.getByRole('textbox',{name:'Message Moyai'}).fill('Authorized retry.');
  await page.getByRole('button',{name:'Send message',exact:true}).click();
  await waitFor(()=>state.requests.some(req=>req.body?.prompt==='Authorized retry.'));
  assert.deepEqual(state.requests.find(req=>req.body?.prompt==='Authorized retry.').body.connections,[]);
});

for(const width of [1440,768,320])test(`inline skills share one icon and keep surrounding text and scoped identity through send/reload at ${width}px`,async t=>{
  const skills=[
    {id:'personal-team',name:'team',description:'Investigate a bug with a team.',scope:'personal',reference:'personal:team',icon:'👥'},
    {id:'org-team',name:'team',description:'Organization workflow.',scope:'organization',reference:'org:team',icon:'code'},
  ];
  reset({status:'completed',skills,sessionOverrides:{['c'.repeat(32)]:{title:'Skill draft session'}}});
  const page=await pageFor(t,'#',width), input=page.getByRole('textbox',{name:'Message Moyai'});
  await page.getByRole('button',{name:'Private session',exact:true}).click();
  await input.fill('Please use /team');
  const options=page.getByRole('listbox',{name:'Skills'});
  await options.waitFor();assert.equal(await options.getByRole('option').count(),2);
  assert.match(await options.innerText(),/Personal/);assert.match(await options.innerText(),/Organization/);
  assert.equal(await options.locator('svg[data-skill-icon="document"]').count(),2);
  assert.deepEqual(await options.locator('svg').evaluateAll(icons=>icons.map(icon=>icon.innerHTML)),Array(2).fill(await options.locator('svg').first().innerHTML()));
  const box=await options.boundingBox();assert.ok(box.x>=0&&box.x+box.width<=width+1);
  await input.press('Enter');
  const mention=input.getByLabel('/team skill');await mention.waitFor();
  assert.equal(await mention.locator('svg[data-skill-icon="document"]').count(),1);
  assert.match(await input.innerText(),/^Please use Team /);
  assert.equal(state.requests.filter(r=>r.method==='POST'&&r.path.endsWith('/messages')).length,0);
  await input.pressSequentially('to inspect the issue.');
  await input.press('Enter');
  await waitFor(()=>state.requests.some(r=>r.method==='POST'&&r.path.endsWith('/messages')));
  const sent=state.requests.find(r=>r.method==='POST'&&r.path.endsWith('/messages'));
  assert.equal(sent.body.prompt,'Please use [/team](moyai-skill:personal%3Ateam?icon=%F0%9F%91%A5) to inspect the issue.');
  const saved=page.locator('.transcript').getByLabel('/team skill');await saved.waitFor();
  assert.equal(await saved.locator('svg[data-skill-icon="document"]').count(),1);
  state.skills=[];await page.reload();await saved.waitFor();
  assert.equal(await saved.locator('svg[data-skill-icon="document"]').count(),1);
  assert.match(await saved.locator('..').innerText(),/Please use Team to inspect the issue\./);
  state.skills=skills;
  await page.goto(base+'/#'+sessionId);
  await page.getByRole('heading',{name:'Current native work',exact:true}).waitFor();
  await input.fill('/team');await options.waitFor();
  await input.press('ArrowDown');await input.press('Tab');
  await mention.waitFor();assert.match(await mention.getAttribute('title'),/org:team/);
  await input.press('Backspace');await input.press('Backspace');await input.press('Backspace');
  assert.equal(await mention.count(),0);
  assert.equal(await input.textContent(),'');
  await input.pressSequentially('/nomatch');await page.getByRole('status').filter({hasText:'No matching skills'}).waitFor();
  assert.equal(await input.innerText(),'/nomatch');
  await input.fill('/team');await options.waitFor();await input.press('Escape');await options.waitFor({state:'hidden'});
  assert.equal(await input.innerText(),'/team');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
});

for(const width of [1440,768,320])test(`assistant skills match the composer in streaming, progress and reloaded replies at ${width}px`,async t=>{
  const marker='[/team](moyai-skill:personal%3Ateam?icon=users)';
  reset({skills:[{id:'team',name:'team',description:'Team procedure',scope:'personal',reference:'personal:team',icon:'users'}],
    messages:[{id:1,turn_id:turnId,role:'user',author_id:'viewer',content:`Use ${marker} here.`}],
    workProgress:{messages:[]}});
  const page=await pageFor(t,'#'+sessionId,width),input=page.getByRole('textbox',{name:'Message Moyai'});
  await input.fill('/team');await page.getByRole('listbox',{name:'Skills'}).waitFor();await input.press('Enter');
  const composer=input.getByLabel('/team skill');await composer.waitFor();
  const icon=await composer.locator('svg').innerHTML();
  await waitFor(()=>clients.size>0,'SSE connected');
  emit({type:'response_start',run_id:turnId,step:1},1);
  emit({type:'text_start',index:0,text:'Using /te',phase:'commentary'},2);
  await page.locator('.turn-progress-history').getByText('Using /te',{exact:true}).waitFor();
  assert.equal(await page.locator('.turn-progress-history .skill-mention').count(),0);
  emit({type:'text_delta',index:0,text:'am to inspect.'},3);
  const progress=page.locator('.turn-progress-history').getByLabel('/team skill');
  await progress.waitFor();assert.equal(await progress.locator('svg').innerHTML(),icon);
  await page.locator('.turn-progress-toggle').click();
  await page.locator('.turn-progress-latest').getByLabel('/team skill').waitFor();
  emit({type:'response_start',run_id:turnId,step:2},4);
  emit({type:'text_start',index:0,text:'Finished with **/team** and `/personal:team`.',phase:'final_answer'},5);
  await page.locator('.live-message .skill-mention').nth(1).waitFor();
  assert.deepEqual(await page.locator('.live-message .skill-mention svg').evaluateAll(nodes=>nodes.map(node=>node.innerHTML)),[icon,icon]);
  state.messages.push({id:2,turn_id:turnId,role:'assistant',author_id:null,content:`Finished with **/team** and \`/personal:team\`.\n\n${marker}`});
  state.workProgress={messages:[{id:'saved',phase:'commentary',text:'Using /team to inspect.'}]};
  state.status='completed';emit({},undefined,'finished');
  await page.locator('.message.assistant:not(.live-message) .skill-mention').nth(2).waitFor();
  state.skills=[];await page.reload();
  await page.locator('.message.assistant .skill-mention').nth(2).waitFor();
  await page.locator('.turn-progress-toggle').click();
  await page.locator('.turn-progress-history').getByLabel('/team skill').waitFor();
  assert.deepEqual(await page.locator('.message.assistant .skill-mention svg').evaluateAll(nodes=>nodes.map(node=>node.innerHTML)),[icon,icon,icon]);
  assert.equal(await page.locator('.message.assistant strong .skill-mention').innerText(),'Team');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  assert.equal(state.requests.filter(row=>row.path==='/api/skills').length,1,'transcript rendering needs no catalog request');
});

test('assistant skill rendering preserves literals, rejects ambiguous names and stays within the conversation',async t=>{
  const marker=scope=>`[/team](moyai-skill:${scope}%3Ateam?icon=auto)`;
  const literals=['/team/file.txt','https://example.test/team','--path=/team','/unknown','\\/team',
    '`run /team`','```sh\n/team\n```','[link /team](https://example.test/team)','[reference /team][ref]','<span>/team</span>'];
  reset({status:'completed',messages:[
    {id:1,turn_id:turnId,role:'user',author_id:'viewer',content:`Use ${marker('personal')} and ${marker('org')}.`},
    {id:2,turn_id:turnId,role:'assistant',author_id:null,content:
      '/team stays ambiguous. /personal:team and /org:team are scoped.\n\n'+literals.join('\n\n')+'\n\n[ref]: https://example.test/team'},
  ]});
  const page=await pageFor(t),answer=page.locator('.message.assistant');
  await answer.getByLabel('/team skill').nth(1).waitFor();
  assert.equal(await answer.locator('.skill-mention').count(),2);
  assert.equal(await answer.locator('pre code').innerText(),'/team\n');
  assert.match(await answer.locator('code:not(pre code)').innerText(),/run \/team/);
  assert.equal(await answer.locator('a .skill-mention').count(),0);
  assert.equal(await answer.locator('a').first().getAttribute('href'),'https://example.test/team');
  assert.match(await answer.innerText(),/\/team stays ambiguous/);
  // Remove ambiguity to exercise literal protection when /team is resolvable.
  state.messages[0].content=`Use ${marker('personal')}.`;
  state.messages[1].content='/team. **/team**, and \\/team; /team &amp; more.\n\n'+literals.join('\n\n')+'\n\n[ref]: https://example.test/team';
  await page.reload();await answer.locator('.skill-mention').nth(2).waitFor();
  assert.equal(await answer.locator('.skill-mention').count(),3);
  assert.match(await answer.innerText(),/Team\. Team, and \/team; Team & more/);
  const child='d'.repeat(32);
  state.sessionOverrides={[child]:{title:'Unrelated conversation'}};
  state.messagesBySession={[child]:[{id:3,turn_id:turnId,role:'assistant',author_id:null,content:'No selected skill: /team.'}]};
  await page.goto(base+'/#'+child);await page.getByRole('heading',{name:'Unrelated conversation',exact:true}).waitFor();
  await page.getByText('No selected skill: /team.',{exact:true}).waitFor();
  assert.equal(await page.locator('.skill-mention').count(),0);
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
  await page.getByText('Couldn’t load your skills.').waitFor({state:'hidden'});
  await page.getByRole('option',{name:/Goal/}).waitFor();
  assert.equal(await page.getByRole('option').count(),1);
  assert.equal(await page.getByRole('option',{name:/private-only/}).count(),0);
});

const testGoal=()=>({objective:'Verify the original result.',status:'active',generation:1,owner_id:'viewer',
  tokens_used:0,token_budget:null,turns:0,blocked_count:0,reason:'',created_at:''});
test('chat goal retries keep their request identity and preserve the edit after a failure',async t=>{
  reset({mode:'standalone',status:'idle',goal:testGoal(),goalError:true});
  const page=await pageFor(t);
  await page.getByRole('button',{name:'Edit goal',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'Edit goal',exact:true});
  await dialog.getByLabel('Objective',{exact:true}).fill('Verify the new criterion.');
  await dialog.getByRole('button',{name:'Save goal',exact:true}).click();
  await dialog.getByRole('alert').filter({hasText:'Goal update temporarily unavailable'}).waitFor();
  assert.equal(await dialog.getByLabel('Objective',{exact:true}).inputValue(),'Verify the new criterion.');
  state.goalError=false;
  await dialog.getByRole('button',{name:'Save goal',exact:true}).click();
  await dialog.waitFor({state:'hidden'});
  const requests=state.requests.filter(row=>row.method==='POST'&&row.body?.prompt==='/goal edit Verify the new criterion.');
  assert.equal(requests.length,2);
  assert.deepEqual(requests[0].body,requests[1].body);
});
test('confirmed chat goal edits are not resubmitted when conversation refresh fails',async t=>{
  reset({mode:'standalone',status:'idle',goal:testGoal(),goalRefreshError:true});
  const page=await pageFor(t);
  await page.getByRole('button',{name:'Edit goal',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'Edit goal',exact:true});
  await dialog.getByLabel('Objective',{exact:true}).fill('The confirmed new objective.');
  await dialog.getByRole('button',{name:'Save goal',exact:true}).click();
  await dialog.waitFor({state:'hidden'});
  await page.getByRole('alert').filter({hasText:'goal was updated'}).waitFor();
  assert.equal(state.requests.filter(row=>row.method==='POST'&&row.body?.prompt?.startsWith('/goal ')).length,1);
  assert.equal(state.goal.objective,'The confirmed new objective.');
});
test('typed goal controls preserve staged files and work while connection selection is unavailable',async t=>{
  reset({mode:'standalone',status:'idle',goal:testGoal(),connectionError:true,
    preferences:{delivery_mode:'when_idle',connection_ids:['missing']}});
  const page=await pageFor(t);
  await page.route('**/api/uploads?**',route=>route.fulfill({json:{id:'staged-file',name:'draft.txt',media_type:'text/plain',size:5}}));
  await page.locator('input[type="file"]').setInputFiles({name:'draft.txt',mimeType:'text/plain',buffer:Buffer.from('draft')});
  await page.locator('.image-preview[aria-busy="false"]').waitFor();
  const input=page.getByRole('textbox',{name:'Message Moyai',exact:true});
  await input.fill('/goal pause');await input.press('Enter');
  await page.getByText('Goal paused',{exact:true}).waitFor();
  const request=state.requests.find(row=>row.method==='POST'&&row.body?.prompt==='/goal pause');
  assert.equal(request.body.attachments,undefined);
  assert.equal(request.body.connections,undefined);
  await page.getByRole('button',{name:'Remove draft.txt',exact:true}).waitFor();
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

for(const width of [1440,768,320])test(`queued turn stays below its preceding saved answer at ${width}px`,async t=>{
  const nextId='turn-next';
  reset({
    turns:[{id:turnId,actor_id:'viewer',status:'running',connections:[],usage:{}},
      {id:nextId,actor_id:'viewer',status:'queued',mode:'when_idle',connections:[],usage:{}}],
    messages:[{id:1,turn_id:turnId,role:'user',author_id:'viewer',content:'hello'},
      {id:2,turn_id:nextId,role:'user',author_id:'viewer',content:'Check the next task.'}],
  });
  const page=await pageFor(t,'#'+sessionId,width);
  await page.locator('.queued-messages').getByText('Check the next task.',{exact:true}).waitFor();
  await waitFor(()=>clients.size>0,'SSE connected');
  emit({type:'response_start',run_id:turnId,step:1},0);
  emit({type:'text_start',index:0,text:'Hi! What would you like to work on?',phase:'final_answer'},1);
  await page.locator('.live-message').getByText('Hi! What would you like to work on?',{exact:true}).waitFor();
  assert.deepEqual(await page.locator('.transcript .message .message-content').allTextContents(),['hello','Hi! What would you like to work on?']);
  state.messages.push({id:3,turn_id:turnId,role:'assistant',author_id:null,content:'Hi! What would you like to work on?'});
  state.turns[0].status='completed';state.turns[1].status='running';
  state.sessionOverrides={[sessionId]:{active_turn:nextId}};
  emit({},undefined,'finished');
  await page.locator('.transcript').getByText('Check the next task.',{exact:true}).waitFor();
  const expected=['hello','Hi! What would you like to work on?','Check the next task.'];
  assert.deepEqual(await page.locator('.transcript .message .message-content').allTextContents(),expected);
  assert.equal(await page.locator('.queued-messages').count(),0);
  assert.equal(await page.locator('.live-message').count(),0,'saved answer replaces its stream exactly once');
  await page.reload();
  await page.locator('.transcript').getByText('Check the next task.',{exact:true}).waitFor();
  assert.deepEqual(await page.locator('.transcript .message .message-content').allTextContents(),expected);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
});

test('a streaming answer stays above another requester’s already visible queued input',async t=>{
  reset({
    turns:[{id:turnId,actor_id:'viewer',status:'running',connections:[],usage:{}},
      {id:'other',actor_id:'teammate',status:'queued',mode:'when_idle',connections:[],usage:{}}],
    messages:[{id:1,turn_id:turnId,role:'user',author_id:'viewer',content:'hello'},
      {id:2,turn_id:'other',role:'user',author_id:'teammate',content:'My next task.'}],
  });
  const page=await pageFor(t);
  await page.locator('.transcript').getByText('My next task.',{exact:true}).waitFor();
  await waitFor(()=>clients.size>0,'SSE connected');
  emit({type:'response_start',run_id:turnId,step:1},0);
  emit({type:'text_start',index:0,text:'Hi there.',phase:'final_answer'},1);
  await page.locator('.live-message').getByText('Hi there.',{exact:true}).waitFor();
  assert.deepEqual(await page.locator('.transcript .message .message-content').allTextContents(),['hello','Hi there.','My next task.']);
  assert.equal(await page.locator('.turn-progress-toggle').getAttribute('aria-expanded'),'true');
});

test('root streaming survives saved-state polling, reconnect replay, per-round retry and final settlement',async t=>{
  reset();const page=await pageFor(t);await page.getByRole('textbox',{name:'Message Moyai'}).waitFor();await waitFor(()=>clients.size>0,'SSE connected');
  emit({type:'text_start',index:0,text:'Unlabelled child text'},0);
  emit({type:'response_start',run_id:turnId,step:1},1);emit({type:'text_start',index:0,text:'I will inspect.',phase:'commentary'},2);
  await page.locator('.turn-progress-history').getByText('I will inspect.',{exact:true}).waitFor();
  const savedPolls=()=>state.requests.filter(req=>req.method==='GET'&&req.path===`/api/sessions/${sessionId}`&&req.query.includes('include_messages=true')).length;
  const polls=savedPolls();
  await waitFor(()=>savedPolls()>polls,'saved poll');
  assert.equal(await page.locator('.turn-progress-history').getByText('I will inspect.',{exact:true}).count(),1);
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

for(const width of [1440,768,320])test(`live work and subagents survive steering and final collapse at ${width}px`,async t=>{
  const start=Date.now()-60000;
  const at=seconds=>new Date(start+seconds*1000).toISOString();
  const update=(step,text,seconds)=>({id:`commentary:${turnId}:${step}:0`,phase:'commentary',text,created_at:at(seconds)});
  reset({
    steeringAt:at(30),preferences:{delivery_mode:'asap',connection_ids:null},
    turns:[{id:turnId,actor_id:'viewer',status:'running',created_at:at(0),connections:[],usage:{}}],
    messages:[{id:1,turn_id:turnId,role:'user',author_id:'viewer',content:'Inspect the renderer.',created_at:at(0)}],
    workProgress:{messages:[update(1,'First inspection.',5),update(2,'Checking recorded behavior.',10)],
      tools:[{id:'read',name:'read_file',status:'completed',created_at:at(6),finished_at:at(7)}],
      subagents:[{id:'child',parent_id:turnId,parent_name:null,name:'Kepler',avatar:0,depth:0,status:'running',
        created_at:at(8),updated_at:at(12),messages:[{id:'child-update',phase:'commentary',text:'I found the renderer.',created_at:at(9)}],
        tools:[{id:'inspect',name:'read_file',status:'completed',created_at:at(8),finished_at:at(9)}],
        messages_limited:false,tools_limited:false}]},
  });
  const page=await pageFor(t,'#'+sessionId,width);
  const progress=page.locator('.turn-progress');
  await progress.getByText('First inspection.',{exact:true}).waitFor();
  await progress.getByText('Checking recorded behavior.',{exact:true}).waitFor();
  await progress.getByText('I found the renderer.',{exact:true}).waitFor();
  assert.equal(await progress.locator('.turn-progress-toggle').getAttribute('aria-expanded'),'true');
  const input=page.getByRole('textbox',{name:'Message Moyai'});
  await input.fill('Also check keyboard use.');
  await page.getByRole('button',{name:'Send message',exact:true}).click();
  await page.getByText('Also check keyboard use.',{exact:true}).waitFor();
  await page.locator('.turn-progress').nth(1).waitFor();
  assert.equal(await progress.nth(0).getByRole('button',{name:'Activity',exact:true}).getAttribute('aria-expanded'),'true');
  await progress.nth(0).getByText('First inspection.',{exact:true}).waitFor();
  state.workProgress.messages.push(update(3,'Keyboard behavior is verified.',35));
  state.workProgress.subagents[0].status='completed';
  state.workProgress.subagents[0].updated_at=at(36);
  state.workProgress.subagents[0].messages.push({id:'child-final',phase:'final_answer',text:'Subagent checks passed.',created_at:at(36)});
  await progress.nth(1).getByText('Keyboard behavior is verified.',{exact:true}).waitFor();
  assert.equal(await progress.nth(1).locator('.turn-progress-toggle').getAttribute('aria-expanded'),'true','child completion does not complete the parent');
  state.messages.push({id:3,turn_id:turnId,role:'assistant',author_id:null,content:'Verified final answer.',created_at:at(45)});
  state.status='completed';state.turns[0].status='completed';state.turns[0].finished_at=at(45);
  emit({},undefined,'finished');
  await page.getByText('Verified final answer.',{exact:true}).waitFor();
  assert.equal(await progress.nth(0).locator('.turn-progress-toggle').getAttribute('aria-expanded'),'true');
  await waitFor(()=>state.requests.some(req=>req.path.endsWith('/progress/'+turnId)),'final progress request');
  await progress.nth(1).locator('.turn-progress-toggle[aria-expanded="false"]').waitFor();
  assert.equal(await page.getByText('Verified final answer.',{exact:true}).count(),1);
  await progress.nth(1).locator('.turn-progress-toggle').click();
  await progress.nth(1).getByText('Keyboard behavior is verified.',{exact:true}).waitFor();
  await page.reload();
  await page.getByText('Verified final answer.',{exact:true}).waitFor();
  await progress.nth(0).getByText('First inspection.',{exact:true}).waitFor();
  assert.equal(await progress.nth(1).locator('.turn-progress-toggle').getAttribute('aria-expanded'),'false');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
});

test('stopped work without a final answer keeps its updates expanded',async t=>{
  reset({workProgress:{messages:[{id:`commentary:${turnId}:1:0`,phase:'commentary',text:'Observed before interruption.'}]}});
  const page=await pageFor(t);
  await page.getByText('Observed before interruption.',{exact:true}).waitFor();
  await page.getByRole('button',{name:'Stop task',exact:true}).click();
  const progress=page.locator('.turn-progress');
  await progress.getByRole('button',{name:'Work stopped',exact:true}).waitFor();
  assert.equal(await progress.locator('.turn-progress-toggle').getAttribute('aria-expanded'),'true');
  await page.getByText('Observed before interruption.',{exact:true}).waitFor();
  await page.reload();
  await page.getByText('Observed before interruption.',{exact:true}).waitFor();
  assert.equal(await page.locator('.message.assistant').count(),0);
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
  const privateChoice=page.getByRole('button',{name:'Private session',exact:true});await privateChoice.waitFor();assert.equal(await privateChoice.isEnabled(),false);assert.equal(await privateChoice.getAttribute('aria-pressed'),'false');
  await page.getByRole('textbox',{name:'Message Moyai'}).fill('A shared task');await page.getByRole('button',{name:'Send message',exact:true}).click();
  await waitFor(()=>state.requests.some(req=>req.path==='/api/sessions'&&req.method==='POST'));assert.equal(state.requests.find(req=>req.path==='/api/sessions'&&req.method==='POST').body.visibility,'shared');
  const login=state.requests.find(req=>req.path==='/api/login');assert.equal(login.body.username,undefined);
  reset({signedIn:false,mode:'standalone',google:false});await page.goto(base+'/');await page.getByLabel('Username',{exact:true}).waitFor();assert.equal(await page.getByRole('button',{name:'Continue with Google'}).count(),0);
});

for(const privateSession of [false,true])test(`individual home defaults to shared with private opt-in: ${privateSession}`,async t=>{
  reset({connections:[
    {id:'organization',name:'Team account',enabled:true,shared:true,read_only:true},
    {id:'personal',name:'Personal account',enabled:true,shared:false,read_only:true},
    {id:'disabled',name:'Disabled account',enabled:false,shared:true,read_only:true},
  ]});const page=await pageFor(t,'');
  const privateChoice=page.getByRole('button',{name:'Private session',exact:true});
  await privateChoice.waitFor();assert.equal(await privateChoice.isEnabled(),true);assert.equal(await privateChoice.getAttribute('aria-pressed'),'false');
  assert.equal(await page.getByText(/Workspace members can see messages/).count(),0);
  const input=page.getByRole('textbox',{name:'Message Moyai'});
  await input.fill('A new task');
  await privateChoice.press('Space');assert.equal(await privateChoice.getAttribute('aria-pressed'),'true');
  await privateChoice.press('Space');assert.equal(await privateChoice.getAttribute('aria-pressed'),'false');
  if(privateSession)await privateChoice.click();
  assert.equal(await input.innerText(),'A new task');
  assert.equal(await privateChoice.innerText(),privateSession?'Private':'Public');
  await page.getByRole('button',{name:'Send message',exact:true}).click();
  await waitFor(()=>state.requests.some(req=>req.path==='/api/sessions'&&req.method==='POST'));
  assert.equal(state.requests.find(req=>req.path==='/api/sessions'&&req.method==='POST').body.visibility,privateSession?'private':'shared');
  await waitFor(()=>state.requests.some(req=>req.path.endsWith('/messages')&&req.method==='POST'));
  assert.deepEqual(state.requests.find(req=>req.path.endsWith('/messages')&&req.method==='POST').body.connections,privateSession?['organization','personal']:['organization']);
});

for(const recovery of ['retry','skip'])test(`home connection failures support ${recovery} without a picker`,async t=>{
  reset({connectionError:true,connections:[{id:'organization',name:'Team account',enabled:true,shared:true,read_only:true}]});
  const page=await pageFor(t,'');
  const input=page.getByRole('textbox',{name:'Message Moyai'});
  await input.fill('Keep my draft while connections recover.');
  const send=page.getByRole('button',{name:'Send message',exact:true});
  await page.getByRole('alert').getByText('Error: Connections temporarily unavailable.',{exact:true}).waitFor();
  assert.equal(await send.isEnabled(),false);
  await input.press('Enter');
  assert.equal(state.requests.some(req=>req.path==='/api/sessions'&&req.method==='POST'),false);
  if(recovery==='retry') {
    state.connectionError=false;
    await page.getByRole('button',{name:'Retry connections',exact:true}).click();
  } else {
    await page.getByRole('button',{name:'Continue without connections',exact:true}).click();
  }
  assert.equal(await input.innerText(),'Keep my draft while connections recover.');
  await send.click();
  await waitFor(()=>state.requests.some(req=>req.path.endsWith('/messages')&&req.method==='POST'));
  assert.deepEqual(state.requests.find(req=>req.path.endsWith('/messages')&&req.method==='POST').body.connections,recovery==='retry'?['organization']:[]);
});

for(const width of [1440,768])test(`sidebar collapses to an accessible rail without losing drafts at ${width}px`,async t=>{
  reset({status:'idle'});const page=await pageFor(t,'',width);
  const draft=page.getByRole('textbox',{name:'Message Moyai'}), sidebar=page.locator('.app > .sidebar');
  await draft.fill('Keep this draft while I hide the sidebar.');
  const expanded=await sidebar.boundingBox();
  await page.getByRole('button',{name:'Close sidebar',exact:true}).click();
  const reopen=page.getByRole('button',{name:'Open sidebar',exact:true});
  assert.equal((await sidebar.boundingBox()).width,52);
  assert.equal(await reopen.getAttribute('aria-expanded'),'false');
  assert.equal(await page.getByRole('button',{name:'New session ⌘⇧O',exact:true}).isVisible(),false);
  assert.equal(await draft.innerText(),'Keep this draft while I hide the sidebar.');
  assert.equal(await reopen.evaluate(el=>el===document.activeElement),true);
  await reopen.press('Enter');
  assert.equal((await sidebar.boundingBox()).width,expanded.width);
  assert.equal(await draft.innerText(),'Keep this draft while I hide the sidebar.');
  if(width>850) {
    await page.getByRole('button',{name:'Settings',exact:true}).click();
    await page.getByRole('heading',{name:'Settings',exact:true}).waitFor();
    await page.getByRole('button',{name:'Close sidebar',exact:true}).click();
    assert.equal((await page.locator('.ns-rail').boundingBox()).width,52);
    assert.equal(await page.getByRole('button',{name:'Back to workspace',exact:true}).isVisible(),false);
    await page.getByRole('button',{name:'Open sidebar',exact:true}).press('Enter');
    assert.equal((await page.locator('.ns-rail').boundingBox()).width,expanded.width);
    await page.getByRole('button',{name:'Back to workspace',exact:true}).click();
    await draft.waitFor();
  }
  await page.getByRole('button',{name:'Close sidebar',exact:true}).click();
  await page.setViewportSize({width:320,height:1000});
  await page.getByRole('button',{name:'Toggle conversations',exact:true}).click();
  assert.equal(await page.getByRole('button',{name:'New session ⌘⇧O',exact:true}).isVisible(),true);
  assert.equal(await page.getByRole('button',{name:'Open sidebar',exact:true}).isVisible(),false);
  await page.getByRole('button',{name:'Toggle conversations',exact:true}).click();
  assert.equal(await sidebar.isVisible(),false);
});

for(const width of [1440,768,320])test(`home and Settings share the same wordmark position and typography at ${width}px`,async t=>{
  reset();const page=await pageFor(t,'',width);
  if(width<760)await page.getByRole('button',{name:'Toggle conversations',exact:true}).click();
  const brand=page.locator('.moyai-brand');await brand.waitFor();await page.evaluate(()=>document.fonts.ready);
  const metrics=()=>brand.evaluate(el=>{
    const bounds=el.querySelector('span').getBoundingClientRect(), style=getComputedStyle(el), subtitle=getComputedStyle(el.querySelector('small'));
    return {x:bounds.x,y:bounds.y,height:bounds.height,font:style.font,spacing:style.letterSpacing,transform:style.textTransform,subtitle:subtitle.font};
  });
  const home=await metrics();
  await page.getByRole('button',{name:'Settings',exact:true}).click();
  await page.getByRole('heading',{name:'Settings',exact:true}).waitFor();
  assert.equal(await brand.count(),1);assert.equal(await brand.locator('img').count(),0);
  assert.deepEqual(await metrics(),home);
  await brand.click();await page.locator('.home-composer').waitFor();
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
  await row.hover();
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
  const page=await pageFor(t,'#run='+legacyId);await groupSidebarByFolder(page);const history=page.locator(`[data-session-key="legacy:${collision}"]`);
  await page.getByRole('button',{name:/^Original folder/}).click();
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

function colorQuestion({multi=false,approvals=[]}={}) {
  return {calls:[{tool_call_id:'color-call',tool_name:'ask_user_question',args:{questions:[{
    header:'Color',question:'Which color should I use?',multi_select:multi,
    options:[
      {label:'Blue',description:'A calm blue accent for the workspace.'},
      {label:'Green',description:'A clear green accent with good contrast.'},
      {label:'Purple',description:'A muted purple accent that matches the existing theme.'},
    ],
  }]}}],approvals};
}
const answerRequests=()=>state.requests.filter(row=>row.path.endsWith('/answers')&&row.method==='POST');

for(const width of [1440,768,320])test(`compact numbered choices submit directly at ${width}px`,async t=>{
  reset({status:'waiting',pending:colorQuestion()});
  const page=await pageFor(t,'#'+sessionId,width);
  await page.getByRole('heading',{name:'Question',exact:true}).waitFor();
  assert.equal(await page.getByRole('button',{name:'Send',exact:true}).count(),0);
  const list=page.locator('.question-choice-list');
  const choices=list.getByRole('button');
  assert.equal(await choices.count(),3);
  const boxes=await Promise.all([0,1,2].map(index=>choices.nth(index).boundingBox()));
  assert.ok(boxes[0].y+boxes[0].height<=boxes[1].y&&boxes[1].y+boxes[1].height<=boxes[2].y);
  assert.ok(boxes.every(box=>Math.abs(box.x-boxes[0].x)<1));
  for(const number of ['1','2','3'])assert.equal(await list.getByText(number,{exact:true}).count(),1);
  const sizes=await page.evaluate(()=>['.message-content','.question-choice-field legend','.question-choice-row','.question-custom-input'].map(selector=>getComputedStyle(document.querySelector(selector)).fontSize));
  assert.equal(new Set(sizes).size,1);
  await choices.nth(0).focus();await choices.nth(0).press('ArrowDown');
  assert.equal(await page.getByRole('button',{name:'Green',exact:true}).evaluate(element=>element===document.activeElement),true);
  assert.equal(answerRequests().length,0);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
  await page.getByRole('button',{name:'Green',exact:true}).press('3');
  await waitFor(()=>answerRequests().length===1);
  assert.deepEqual(answerRequests()[0].body.answers['color-call'].answers,[{header:'Color',selected:['Purple'],custom_answer:null}]);
});

test('Enter sends written answers while digits and Shift Enter remain text',async t=>{
  reset({status:'waiting',pending:colorQuestion()});
  const page=await pageFor(t);
  const custom=page.getByRole('textbox',{name:'Custom answer for Color',exact:true});
  await custom.fill('2 shades of orange');await custom.press('3');
  assert.equal(await custom.inputValue(),'2 shades of orange3');
  await custom.press('Shift+Enter');await custom.pressSequentially('With contrast');
  assert.equal(answerRequests().length,0);
  await custom.press('Enter');await waitFor(()=>answerRequests().length===1);
  assert.deepEqual(answerRequests()[0].body.answers['color-call'].answers,[{header:'Color',selected:[],custom_answer:'2 shades of orange3\nWith contrast'}]);
});

test('multi-select choices remain checkboxes and returning to choices clears custom text',async t=>{
  reset({status:'waiting',pending:colorQuestion({multi:true})});
  const page=await pageFor(t);
  await page.getByRole('checkbox',{name:'Blue',exact:true}).click();
  await page.getByRole('checkbox',{name:'Green',exact:true}).click();
  for(const checkbox of await page.getByRole('checkbox').all())assert.equal(await checkbox.getAttribute('aria-pressed'),null);
  const custom=page.getByRole('textbox',{name:'Custom answer for Color',exact:true});
  await custom.fill('Amber');
  assert.equal(await page.getByRole('checkbox',{name:'Blue',exact:true}).isChecked(),false);
  await page.getByRole('checkbox',{name:'Purple',exact:true}).click();
  assert.equal(await custom.inputValue(),'');
  await page.getByRole('checkbox',{name:'Blue',exact:true}).click();
  assert.equal(answerRequests().length,0);
  await page.getByRole('checkbox',{name:'Blue',exact:true}).press('Enter');await waitFor(()=>answerRequests().length===1);
  assert.deepEqual(answerRequests()[0].body.answers['color-call'].answers[0].selected,['Purple','Blue']);
});

test('choosing an answer never implicitly resolves a pending approval',async t=>{
  reset({status:'waiting',pending:colorQuestion({approvals:[{tool_call_id:'publish',tool_name:'publish_document',args:{title:'Test document'}}]})});
  const page=await pageFor(t);
  await page.getByRole('button',{name:'Blue',exact:true}).click();
  assert.equal(await page.getByRole('button',{name:'Blue',exact:true,pressed:true}).count(),1);
  assert.equal(await page.getByRole('button',{name:'Continue',exact:true}).isEnabled(),false);
  await page.getByRole('textbox',{name:'Custom answer for Color',exact:true}).press('Enter');
  assert.equal(answerRequests().length,0);
  await page.getByRole('button',{name:'Decline',exact:true}).click();
  await page.getByRole('button',{name:'Continue',exact:true}).click();await waitFor(()=>answerRequests().length===1);
  assert.deepEqual(answerRequests()[0].body.approvals,{publish:false});
});

test('failed answer submission preserves text and retries the same request identity',async t=>{
  reset({status:'waiting',pending:colorQuestion(),answerError:true});
  const page=await pageFor(t);
  const custom=page.getByRole('textbox',{name:'Custom answer for Color',exact:true});
  await custom.fill('Soft orange');
  await custom.press('Enter');
  await page.getByRole('alert').filter({hasText:'The answer could not be saved.'}).waitFor();
  assert.equal(await custom.inputValue(),'Soft orange');
  assert.equal(await custom.evaluate(element=>element===document.activeElement),true);
  state.answerError=false;
  await page.keyboard.press('Enter');await waitFor(()=>answerRequests().length===2);
  assert.deepEqual(answerRequests()[0].body,answerRequests()[1].body);
});

test('an accepted answer stays locked when conversation refresh fails and refresh never resubmits',async t=>{
  reset({status:'waiting',pending:colorQuestion(),answerRefreshError:true,canAnswer:true,canReply:true});
  const page=await pageFor(t);
  await page.getByRole('button',{name:'Blue',exact:true}).click();
  await page.getByRole('alert').waitFor();
  assert.equal(await page.getByRole('button',{name:'Blue',exact:true}).isEnabled(),false);
  assert.equal(await page.getByRole('textbox',{name:'Custom answer for Color',exact:true}).isEnabled(),false);
  assert.equal(await page.getByRole('button',{name:'Answer question',exact:true}).count(),0);
  const composer=page.getByRole('textbox',{name:'Message Moyai',exact:true});
  await composer.fill('Additional context after my answer.');await composer.press('Enter');
  await waitFor(()=>state.requests.some(row=>row.path.endsWith('/messages')&&row.method==='POST'));
  assert.equal(state.requests.find(row=>row.path.endsWith('/messages')&&row.method==='POST').body.reply_to,undefined);
  await page.locator('.question-form').evaluate(form=>form.requestSubmit());
  assert.equal(answerRequests().length,1);
  // Keep background reads from removing the button before the explicit retry.
  let release;state.holdSessionRead=new Promise(resolve=>{release=resolve;});t.after(()=>release());
  state.answerRefreshError=false;
  await page.getByRole('button',{name:'Refresh conversation',exact:true}).click();
  release();
  await page.locator('.question-form').waitFor({state:'hidden'});
  assert.equal(answerRequests().length,1);
});

test('an answer sent through the composer also locks the question card after a failed refresh',async t=>{
  reset({status:'waiting',pending:colorQuestion(),answerRefreshError:true,canAnswer:true,canReply:true});
  const page=await pageFor(t);
  const composer=page.getByRole('textbox',{name:'Message Moyai',exact:true});
  await composer.fill('Orange');await composer.press('Enter');
  await page.getByRole('alert').waitFor();
  assert.match(await page.getByRole('alert').innerText(),/Your response was sent/);
  assert.equal(await page.getByRole('button',{name:'Blue',exact:true}).isEnabled(),false);
  assert.equal(await page.getByRole('button',{name:'Answer question',exact:true}).count(),0);
  await page.locator('.question-form').evaluate(form=>form.requestSubmit());
  assert.equal(answerRequests().length,0);
  // Keep background reads from removing the button before the explicit retry.
  let release;state.holdSessionRead=new Promise(resolve=>{release=resolve;});t.after(()=>release());
  state.answerRefreshError=false;
  await page.getByRole('button',{name:'Refresh conversation',exact:true}).click();
  release();
  await page.locator('.question-form').waitFor({state:'hidden'});
  assert.equal(state.requests.filter(row=>row.path.endsWith('/messages')&&row.method==='POST').length,1);
});

test('IME confirmation cannot submit a written answer',async t=>{
  reset({status:'waiting',pending:colorQuestion()});
  const page=await pageFor(t);
  const custom=page.getByRole('textbox',{name:'Custom answer for Color',exact:true});
  await custom.fill('青');
  await custom.dispatchEvent('keydown',{key:'Enter',code:'Enter',isComposing:true});
  assert.equal(answerRequests().length,0);
  await custom.dispatchEvent('keydown',{key:'Enter',code:'Enter',keyCode:229,isComposing:false});
  assert.equal(answerRequests().length,0);
  await custom.press('Enter');await waitFor(()=>answerRequests().length===1);
  assert.equal(answerRequests()[0].body.answers['color-call'].answers[0].custom_answer,'青');
});

test('a failed answer does not steal focus from another control',async t=>{
  let release;reset({status:'waiting',pending:colorQuestion(),answerError:true,answerHold:new Promise(resolve=>{release=resolve})});
  t.after(()=>release());const page=await pageFor(t);
  const custom=page.getByRole('textbox',{name:'Custom answer for Color',exact:true});
  await custom.fill('Orange');await custom.press('Enter');await waitFor(()=>answerRequests().length===1);
  const composer=page.getByRole('textbox',{name:'Message Moyai',exact:true});await composer.focus();
  release();await page.getByRole('alert').waitFor();
  assert.equal(await composer.evaluate(element=>element===document.activeElement),true);
});

test('a failed choice restores keyboard focus and retries the same answer',async t=>{
  reset({status:'waiting',pending:colorQuestion(),answerError:true});
  const page=await pageFor(t),blue=page.getByRole('button',{name:'Blue',exact:true});
  await blue.press('Enter');await page.getByRole('alert').waitFor();
  assert.equal(await blue.evaluate(element=>element===document.activeElement),true);
  assert.equal(await page.getByRole('button',{name:'Blue',exact:true,pressed:true}).count(),1);
  state.answerError=false;
  await page.keyboard.press('Enter');await waitFor(()=>answerRequests().length===2);
  assert.deepEqual(answerRequests()[0].body,answerRequests()[1].body);
});

test('answer controls are disabled while submission is in progress',async t=>{
  let release;reset({status:'waiting',pending:colorQuestion(),answerHold:new Promise(resolve=>{release=resolve})});
  t.after(()=>release());
  const page=await pageFor(t);
  await page.getByRole('button',{name:'Blue',exact:true}).click();await waitFor(()=>answerRequests().length===1);
  await page.getByRole('status').filter({hasText:'Sending…'}).waitFor();
  assert.equal(await page.getByRole('textbox',{name:'Custom answer for Color',exact:true}).isEnabled(),false);
  for(const button of await page.locator('.question-choice-list').getByRole('button').all())assert.equal(await button.isEnabled(),false);
  await page.locator('.question-form').evaluate(form=>{form.requestSubmit();form.requestSubmit()});
  assert.equal(answerRequests().length,1);
  release();
});

test('a filtered sidebar refresh preserves the selected conversation pin when its row is absent',async t=>{
  reset({status:'idle'});
  const page=await pageFor(t);
  await clickSidebarPin(page,'Pin Current native work');
  await page.getByText('Session pinned.',{exact:true}).waitFor();
  state.inventoryRows=[];
  await page.getByRole('button',{name:'Show archived conversations',exact:true}).click();
  await waitFor(()=>state.requests.some(row=>row.path==='/api/history/sessions'&&row.query.includes('archived=true')));
  await page.getByText('No sessions here yet.',{exact:true}).first().waitFor();
  await page.getByRole('button',{name:'Session actions',exact:true}).click();
  await page.getByRole('menuitem',{name:'Unpin',exact:true}).waitFor({timeout:500});
});

test('the last answer submits a complete multi-question snapshot',async t=>{
  const pending=colorQuestion();
  pending.calls[0].args.questions.push({header:'Format',question:'Which format?',options:[{label:'Text'},{label:'JSON'}]});
  reset({status:'waiting',pending});const page=await pageFor(t);
  await page.getByRole('button',{name:'Blue',exact:true}).click();
  assert.equal(await page.getByRole('button',{name:'Blue',exact:true,pressed:true}).count(),1);
  await page.getByRole('button',{name:'Green',exact:true}).click();
  assert.equal(await page.getByRole('button',{name:'Blue',exact:true,pressed:false}).count(),1);
  assert.equal(await page.getByRole('button',{name:'Green',exact:true,pressed:true}).count(),1);
  assert.equal(answerRequests().length,0);
  await page.getByRole('textbox',{name:'Custom answer for Color',exact:true}).fill('Orange');
  assert.equal(await page.locator('.question-choice-list').getByRole('button',{pressed:false}).count(),5);
  await page.getByRole('button',{name:'JSON',exact:true}).click();await waitFor(()=>answerRequests().length===1);
  assert.deepEqual(answerRequests()[0].body.answers['color-call'].answers,[
    {header:'Color',selected:[],custom_answer:'Orange'},
    {header:'Format',selected:['JSON'],custom_answer:null},
  ]);
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
  await page.getByRole('button',{name:'Expand subagents for Current native work',exact:true}).click();
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

test('standalone child pickers use its server-scoped catalogs instead of private presentation',async t=>{
  const childId='d'.repeat(32);
  reset({mode:'standalone',status:'idle',
    sessionOverrides:{[childId]:{title:'Curie',visibility:'private',parent_session_id:sessionId}},
    connections:[{id:'personal',name:'Personal test connector',kind:'linear',enabled:true,shared:false,read_only:true}],
    connectionsBySession:{[childId]:[]},
    skills:[{id:'personal',name:'personal-test',description:'Personal test skill',scope:'personal',reference:'personal:personal-test'}],
    skillsBySession:{[childId]:[]}});
  const page=await pageFor(t,'#'+childId);
  await page.getByRole('heading',{name:'Curie',exact:true}).waitFor();
  await waitFor(()=>state.requests.some(r=>r.path==='/api/connections'&&new URLSearchParams(r.query).get('session_id')===childId));
  assert.equal(await page.getByText('Personal test connector',{exact:true}).count(),0);
  await page.getByRole('textbox',{name:'Message Moyai',exact:true}).fill('/personal');
  await page.getByText(/No matching skills/).waitFor();
  assert.ok(state.requests.some(r=>r.path==='/api/skills'&&new URLSearchParams(r.query).get('session_id')===childId));
  assert.equal(await page.getByRole('option',{name:/Personal test skill/}).count(),0);
});

for(const width of [1440,768,320])test(`General preferences persist and simplify the session composer at ${width}px`,async t=>{
  const shared={id:'team',name:'Team GitHub',enabled:true,shared:true,read_only:true};
  const personal={id:'mine',name:'Personal Slack',enabled:true,shared:false,read_only:true};
  reset({connections:[shared,personal],connectionsBySession:{[sessionId]:[shared]},sessionOverrides:{[sessionId]:{visibility:'shared'}}});
  const page=await pageFor(t,'#settings/general',width);
  await page.getByRole('heading',{name:'General',exact:true}).waitFor();
  assert.equal(await page.locator('.ns-page-heading').count(),1);
  await page.getByRole('combobox',{name:'Message delivery',exact:true}).click();
  await page.getByRole('option',{name:'Send now',exact:true}).click();
  await waitFor(()=>state.preferences?.delivery_mode==='asap');
  await page.getByRole('combobox',{name:'Connection preference'}).click();
  await page.getByRole('option',{name:'Choose connections',exact:true}).click();
  await page.getByRole('checkbox',{name:'Team GitHub',exact:true}).click();
  await page.locator('[role="checkbox"][aria-label="Team GitHub"][aria-checked="true"]').waitFor();
  await waitFor(()=>state.preferences.connection_ids.includes('team'));
  await page.getByRole('checkbox',{name:'Personal Slack',exact:true}).click();
  await page.locator('[role="checkbox"][aria-label="Personal Slack"][aria-checked="true"]').waitFor();
  await waitFor(()=>state.preferences.connection_ids.includes('mine'));
  await page.reload();
  await page.getByRole('checkbox',{name:'Personal Slack',exact:true}).waitFor();
  assert.equal(await page.getByRole('checkbox',{name:'Team GitHub',exact:true}).isChecked(),true);
  assert.equal(await page.getByRole('checkbox',{name:'Personal Slack',exact:true}).isChecked(),true);
  assert.equal(await page.getByRole('combobox',{name:'Message delivery',exact:true}).innerText(),'Send now');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
  await page.goto(base+'/#'+sessionId);
  await page.getByRole('textbox',{name:'Message Moyai'}).fill('Use my saved preferences.');
  assert.equal(await page.locator('.account-selection').count(),0);
  assert.equal(await page.getByRole('combobox',{name:'Delivery timing'}).count(),0);
  assert.equal(await page.getByText(/Drop files or paste images/).count(),0);
  await page.getByRole('button',{name:'Stop task',exact:true}).waitFor();
  await page.getByRole('button',{name:'Send message',exact:true}).click();
  await waitFor(()=>state.requests.some(req=>req.body?.prompt==='Use my saved preferences.'));
  const sent=state.requests.find(req=>req.body?.prompt==='Use my saved preferences.');
  assert.equal(sent.body.mode,'asap');assert.deepEqual(sent.body.connections,['team']);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
});

test('failed General preference save preserves the previous setting and can be retried',async t=>{
  reset({preferenceError:true});const page=await pageFor(t,'#settings/general');
  async function chooseNow(){await page.getByRole('combobox',{name:'Message delivery'}).click();await page.getByRole('option',{name:'Send now',exact:true}).click();}
  await chooseNow();await page.getByRole('alert').filter({hasText:'Could not save preferences.'}).waitFor();
  assert.equal(await page.getByRole('combobox',{name:'Message delivery'}).innerText(),'Queue next');
  state.preferenceError=false;await chooseNow();
  await waitFor(()=>state.preferences?.delivery_mode==='asap');
  await page.reload();
  await page.getByRole('combobox',{name:'Message delivery'}).waitFor();
  assert.equal(await page.getByRole('combobox',{name:'Message delivery'}).innerText(),'Send now');
});

for(const isPrivate of [false,true])test(`saved connection choices respect ${isPrivate?'private':'shared'} Home scope`,async t=>{
  reset({mode:'standalone',status:'idle',preferences:{delivery_mode:'when_idle',connection_ids:['team','mine','unavailable']},
    connections:[{id:'team',name:'Team',enabled:true,shared:true},{id:'mine',name:'Mine',enabled:true,shared:false},{id:'disabled',name:'Disabled',enabled:false,shared:true}]});
  const page=await pageFor(t,'#');
  if(isPrivate)await page.getByRole('button',{name:'Private session',exact:true}).click();
  await page.getByRole('textbox',{name:'Message Moyai'}).fill('Start with my connections.');
  await page.getByRole('button',{name:'Send message',exact:true}).click();
  await waitFor(()=>state.requests.some(req=>req.path.endsWith('/messages')&&req.method==='POST'));
  assert.deepEqual(state.requests.find(req=>req.path.endsWith('/messages')&&req.method==='POST').body.connections,isPrivate?['mine','team']:['team']);
});

for(const retry of [false,true])test(`session connection errors allow ${retry?'retry':'continuing without connections'}`,async t=>{
  savedComposer({connectionError:true});const page=await pageFor(t);
  await page.getByRole('textbox',{name:'Message Moyai'}).fill('Recover my draft.');
  await page.getByRole('alert').filter({hasText:'Connections temporarily unavailable.'}).waitFor();
  assert.equal(await page.getByRole('button',{name:'Send message',exact:true}).isEnabled(),false);
  if(retry){state.connectionError=false;await page.getByRole('button',{name:'Retry connections',exact:true}).click();}
  else await page.getByRole('button',{name:'Continue without connections',exact:true}).click();
  await page.getByRole('button',{name:'Send message',exact:true}).click();
  await waitFor(()=>state.requests.some(req=>req.body?.prompt==='Recover my draft.'));
  assert.deepEqual(state.requests.find(req=>req.body?.prompt==='Recover my draft.').body.connections,retry?['saved']:[]);
});

test('chat links remain accented and underlined while plain inline code stays neutral',async t=>{
  reset({status:'completed',messages:[{id:1,turn_id:turnId,role:'assistant',content:'[BerriAI/moyai](https://github.com/BerriAI/moyai) contains the workspace.\n\n| Path | Description |\n| --- | --- |\n| `native/web/` | Frontend |\n\nStart with [`docs/architecture.md`](https://github.com/BerriAI/moyai/blob/main/docs/architecture.md).'}]});
  const page=await pageFor(t);
  const link=page.getByRole('link',{name:'BerriAI/moyai',exact:true});await link.waitFor();
  const code=page.locator('.message-content td code');
  const linkedCode=page.locator('.message-content a code');
  const linkColor=await link.evaluate(el=>getComputedStyle(el).color);
  assert.notEqual(await code.evaluate(el=>getComputedStyle(el).color),linkColor);
  assert.equal(await linkedCode.evaluate(el=>getComputedStyle(el).color),linkColor);
  assert.equal(await link.evaluate(el=>getComputedStyle(el).textDecorationLine),'underline');
});

test('highlighted code preserves literal markup and copies the original text',async t=>{
  const source=[
    `const markup = '<img src=x onerror="window.__codeExecuted=true">';`,
    'const entity = "&lt;literal&gt;";',
    '\tconsole.log(markup, entity);',
    '',
  ].join('\n');
  reset({status:'completed',messages:[{id:1,turn_id:turnId,role:'assistant',content:'```javascript\n'+source+'```'}]});
  const page=await pageFor(t,'#'+sessionId,1440,page=>page.addInitScript(()=>{
    Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.__copiedCode=text;}}});
  }));
  const block=page.getByRole('group',{name:'javascript code block',exact:true});await block.waitFor();
  assert.ok(await block.locator('.hljs-keyword').count()>0);
  assert.equal(await block.locator('code').textContent(),source);
  assert.equal(await block.locator('img, script, iframe').count(),0);
  assert.equal(await page.evaluate(()=>window.__codeExecuted),undefined);
  await block.getByRole('button',{name:'Copy code',exact:true}).click();
  await block.getByRole('button',{name:'Code copied',exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>window.__copiedCode),source);
  assert.equal(await block.locator('.code-line-numbers').getAttribute('aria-hidden'),'true');
});

test('unsupported code language remains literal plaintext and exposes copy failures',async t=>{
  const source='<script>window.__codeExecuted=true</script>\n  &literal;\n';
  reset({status:'completed',messages:[{id:1,turn_id:turnId,role:'assistant',content:'```moyai-unknown\n'+source+'```'}]});
  const page=await pageFor(t,'#'+sessionId,1440,page=>page.addInitScript(()=>{
    Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw new Error('Clipboard unavailable');}}});
  }));
  const block=page.getByRole('group',{name:'moyai-unknown code block',exact:true});await block.waitFor();
  assert.equal(await block.locator('code').textContent(),source);
  assert.equal(await block.locator('code span, code script').count(),0);
  await block.getByRole('button',{name:'Copy code',exact:true}).click();
  await block.getByRole('status').filter({hasText:'Couldn’t copy.'}).waitFor();
  assert.equal(await block.getByRole('button',{name:'Code copied',exact:true}).count(),0);
});

for(const width of [1440,320])for(const surface of ['chat','output'])test(`long code lines scroll by keyboard inside ${surface} at ${width}px`,async t=>{
  const source='console.log("'+('long_value_'.repeat(140))+'");\n';
  const markdown='```javascript\n'+source+'```';
  reset({status:'completed',messages:[{id:1,turn_id:turnId,role:'assistant',content:surface==='chat'?markdown:'[Code note](/api/files/code-note)'}]});
  const page=await pageFor(t,'#'+sessionId,width,async page=>{
    if(surface==='output')await page.route('**/api/sessions/'+sessionId+'/file-preview/artifact/code-note',route=>route.fulfill({
      json:{id:'code-note',kind:'artifact',name:'Code note.md',size:markdown.length,media_type:'text/markdown',preview_kind:'markdown',
        text:markdown,language:null,truncated:false,notice:null,text_byte_limit:100000,text_line_limit:1000,download_url:'/api/files/code-note',content_url:null},
    }));
  });
  if(surface==='output')await page.getByRole('link',{name:'Code note',exact:true}).click();
  const block=page.getByRole('group',{name:'javascript code block',exact:true});await block.waitFor();
  assert.equal(await block.locator('code').textContent(),source);
  const pre=block.locator('pre');
  assert.equal(await pre.evaluate(el=>el.scrollWidth>el.clientWidth),true);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
  await block.getByRole('button',{name:'Copy code',exact:true}).focus();
  await page.keyboard.press('Tab');
  assert.equal(await pre.evaluate(el=>el===document.activeElement),true);
  assert.notEqual(await pre.evaluate(el=>getComputedStyle(el).outlineWidth),'0px');
  await page.keyboard.press('ArrowRight');
  await page.waitForFunction(()=>document.activeElement?.tagName==='PRE'&&document.activeElement.scrollLeft>0);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
});

test('a pending copy does not mark newer streaming code as copied',async t=>{
  reset();const page=await pageFor(t,'#'+sessionId,1440,page=>page.addInitScript(()=>{
    Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:text=>new Promise(resolve=>{
      window.__copiedCode=text;window.__finishCodeCopy=resolve;
    })}});
  }));
  await waitFor(()=>clients.size>0,'SSE connected');
  emit({type:'response_start',run_id:turnId,step:1},0);
  emit({type:'text_start',index:0,text:'```javascript\nconst first = 1;\n',phase:'final_answer'},1);
  const block=page.getByRole('group',{name:'javascript code block',exact:true});await block.waitFor();
  await block.getByRole('button',{name:'Copy code',exact:true}).click();
  emit({type:'text_delta',index:0,text:'const second = 2;\n```'},2);
  await page.waitForFunction(()=>document.querySelector('.code-block code')?.textContent.includes('const second'));
  await page.evaluate(()=>window.__finishCodeCopy());
  assert.equal(await block.getByRole('button',{name:'Code copied',exact:true}).count(),0);
  assert.equal(await block.getByRole('button',{name:'Copy code',exact:true}).isVisible(),true);
  assert.equal(await page.evaluate(()=>window.__copiedCode),'const first = 1;\n');
});

function actionFixture() {
  const created_at='2026-10-17T10:00:00Z', finished_at='2026-10-17T10:00:02Z';
  const tools=[{id:'shell-call',name:'shell',status:'completed',created_at,finished_at},
    {id:'read-call',name:'read_file',status:'running',created_at,finished_at:null}];
  const detail=(tool,input,output)=>({...tool,input,output,restricted:false,input_truncated:false,output_truncated:false});
  return {
    workProgress:{messages:[],tools,subagents:[{id:'child:run',parent_id:turnId,name:'Kepler',avatar:0,depth:0,status:'completed',created_at,updated_at:finished_at,
      tools:[{...tools[0],id:'child-call'}],messages:[],messages_limited:false,tools_limited:false}]},
    actionDetails:{'shell-call':detail(tools[0],JSON.stringify({command:'ls -la\nprintf "<script>alert(1)</script>"'}),'README.md\nnative/\nfrontend/'),
      'read-call':detail(tools[1],JSON.stringify({path:'README.md'}),null),
      'child-call':detail({...tools[0],id:'child-call'},JSON.stringify({command:'pwd'}),'/workspace/moyai')},
  };
}

for(const width of [1440,768,320])test(`action count opens actual input and output in the shared panel at ${width}px`,async t=>{
  reset(actionFixture());const page=await pageFor(t,'#'+sessionId,width);
  const group=page.getByRole('button',{name:'2 actions',exact:true});
  await group.waitFor();assert.equal(await group.innerText(),'2 actions');
  assert.equal(state.requests.some(row=>row.path.includes('/actions/')),false,'details are lazy');
  await group.click();
  const shell=page.getByRole('button',{name:'Open Run command action',exact:true});
  await shell.focus();await page.keyboard.press('Enter');
  const panel=page.getByRole('region',{name:'Action details',exact:true});
  await panel.getByText('README.md\nnative/\nfrontend/',{exact:true}).waitFor();
  assert.match(await panel.locator('pre[aria-label="Input"]').innerText(),/ls -la/);
  assert.equal(await page.getByRole('tab',{name:'Action',exact:true}).count(),1);
  assert.equal(await page.locator('.work-tool-action[aria-label="Open Run command action"]').getAttribute('aria-pressed'),'true');
  assert.equal(await panel.locator('script').count(),0);
  await page.locator('.panel-tab-actions').getByRole('button',{name:'Hide side panel',exact:true}).click();
  await page.getByRole('button',{name:'Open Read file action',exact:true}).click();
  await panel.getByText('Running…',{exact:true}).waitFor();
  state.actionDetails['read-call']={...state.actionDetails['read-call'],status:'completed',finished_at:'2026-10-17T10:00:03Z',output:'# Moyai\nA workspace for agents.'};
  await panel.getByText('# Moyai\nA workspace for agents.',{exact:true}).waitFor();
  assert.equal(await panel.getByText('README.md\nnative/\nfrontend/',{exact:true}).count(),0);
  assert.equal(await page.getByRole('tab',{name:'Action',exact:true}).count(),1,'reuse the action tab');
  if(process.env.MOYAI_ACTION_SCREENSHOTS)await page.screenshot({path:path.resolve(`.data/actions-${width}.png`)});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
});

test('subagent actions use their identity and failures retry without stale output',async t=>{
  reset(actionFixture());const page=await pageFor(t);
  const agent=page.locator('.work-agent');
  await agent.locator('.work-agent-heading .work-detail-toggle').click();
  await agent.getByRole('button',{name:'1 action',exact:true}).click();
  state.actionError=503;
  await agent.getByRole('button',{name:'Open Run command action',exact:true}).click();
  const panel=page.getByRole('region',{name:'Action details',exact:true});
  await panel.getByRole('alert').waitFor();
  assert.equal(await panel.locator('pre').count(),0);
  state.actionError=undefined;await panel.getByRole('button',{name:'Retry',exact:true}).click();
  await panel.getByText('/workspace/moyai',{exact:true}).waitFor();
  assert.ok(state.requests.some(row=>row.path.endsWith('/actions/child-call')&&row.query==='?agent_id=child%3Arun'));
  state.actionError=403;
  await panel.getByRole('button',{name:'Refresh action',exact:true}).click();
  await panel.waitFor({state:'hidden'});
  assert.equal(await page.getByText('/workspace/moyai',{exact:true}).count(),0);
});

test('switching action discards a late response and saved work remains inspectable',async t=>{
  const fixture=actionFixture();
  reset({...fixture,status:'completed',messages:[{id:1,turn_id:turnId,role:'user',author_id:'viewer',content:'Inspect.'},
    {id:2,turn_id:turnId,role:'assistant',content:'Finished inspection.'}]});
  let release;state.actionHolds={'shell-call':new Promise(resolve=>{release=resolve;})};t.after(()=>release());
  const page=await pageFor(t);
  await page.getByRole('button',{name:'Worked',exact:true}).click();
  await page.getByRole('button',{name:'2 actions',exact:true}).click();
  await page.getByRole('button',{name:'Open Run command action',exact:true}).click();
  await waitFor(()=>state.requests.some(row=>row.path.endsWith('/actions/shell-call')));
  await page.getByRole('button',{name:'Open Read file action',exact:true}).click();
  const panel=page.getByRole('region',{name:'Action details',exact:true});
  await panel.getByRole('heading',{name:'Read file',exact:true}).waitFor();
  await panel.getByText('Running…',{exact:true}).waitFor();
  release();delete state.actionHolds;
  await page.waitForTimeout(150);
  assert.equal(await panel.getByText('README.md\nnative/\nfrontend/',{exact:true}).count(),0);
  await page.reload();
  await page.getByRole('button',{name:'Worked',exact:true}).click();
  await page.getByRole('button',{name:'2 actions',exact:true}).click();
  await page.getByRole('button',{name:'Open Run command action',exact:true}).click();
  await panel.getByText('README.md\nnative/\nfrontend/',{exact:true}).waitFor();
});

test('side-chat actions open in the parent panel',async t=>{
  const fixture=actionFixture(),child='c'.repeat(32);
  reset({...fixture,createdSession:true,sessionOverrides:{[child]:{title:'Delegate chat'}},
    workspace:{links:[],files:[],sources:[],subagents:[{id:'delegate',session_id:child,name:'Kepler',avatar:0,status:'completed',can_inspect:true}]}});
  const page=await pageFor(t);
  await page.getByRole('button',{name:'Open workspace',exact:true}).click();
  await page.getByRole('button',{name:'Open Kepler in side chat',exact:true}).click();
  const side=page.locator('.side-conversation');
  await side.getByRole('button',{name:'2 actions',exact:true}).click();
  await side.getByRole('button',{name:'Open Run command action',exact:true}).click();
  await page.getByRole('region',{name:'Action details',exact:true}).getByText('README.md\nnative/\nfrontend/',{exact:true}).waitFor();
  assert.ok(state.requests.some(row=>row.path===`/api/sessions/${child}/progress/${turnId}/actions/shell-call`));
});

for(const width of [1440,768,320])test(`Slack attribution, original files and delivery recovery at ${width}px`,async t=>{
  reset({status:'completed',ownerId:'slack:TTEST:UME',
    sessionOverrides:{[sessionId]:{surface:'slack',visibility:'shared',can_manage:true}},
    slackDelivery:{pending:0,sending:1,rejected:0},
    messages:[{id:1,turn_id:turnId,role:'user',author_id:'slack:TTEST:UME',author_name:'Avery Chen',is_self:true,
      source:'slack',source_timestamp:'1792281607.232469',source_url:'/api/sessions/source-test',content:'Please review the launch notes.',
      attachments:[{id:'original-pdf',name:'Launch notes.pdf',media_type:'application/pdf',size:321}]},
      {id:2,turn_id:'other',role:'user',author_id:'slack:TTEST:UOTHER',author_name:'Jordan Lee',is_self:false,
       source:'slack',content:'The updated dates are ready.',edited:true,edited_after_start:true}]});
  const page=await pageFor(t,'#'+sessionId,width);
  await page.locator('.message-author').filter({hasText:'Avery Chen · You'}).waitFor();
  await page.getByText('Jordan Lee',{exact:true}).waitFor();
  await page.getByText('Launch notes.pdf',{exact:true}).waitFor();
  assert.equal(await page.locator('.message-author img[alt="Slack"]').count(),2);
  assert.equal(await page.getByRole('link',{name:'Open message in Slack'}).count(),1);
  assert.equal(await page.locator('.message-author time').count(),1);
  assert.equal(await page.getByText('Teammate',{exact:true}).count(),0);
  await page.getByText('Confirming Slack delivery. Your message will not be sent twice.',{exact:true}).waitFor();
  assert.equal(await page.getByRole('button',{name:'Retry delivery'}).count(),0);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
  await page.screenshot({path:`/tmp/moyai-slack-ui-${width}.png`});
  state.slackDelivery={pending:0,sending:0,rejected:1};
  await page.reload();
  await page.getByRole('button',{name:'Retry delivery'}).waitFor();
});

test('Slack history pages beyond 200 and navigation discards the old transcript while loading',async t=>{
  const nextId='d'.repeat(32);
  reset({status:'completed',paginate:true,messages:Array.from({length:205},(_,i)=>({id:i+1,turn_id:turnId,
    role:'user',author_id:'viewer',content:`Authored message ${i+1}`})),
    sessionOverrides:{[nextId]:{title:'Next conversation'}},
    messagesBySession:{[nextId]:[{id:206,turn_id:turnId,role:'user',author_id:'viewer',content:'The next conversation body.'}]}});
  const page=await pageFor(t);
  await page.getByText('Authored message 205',{exact:true}).waitFor();
  assert(state.requests.some(row=>row.path.endsWith('/messages')&&row.query==='?after=200'));
  let release;state.holds={[`/api/sessions/${nextId}`]:new Promise(resolve=>{release=resolve})};
  t.after(()=>release());
  await page.evaluate(id=>{location.hash=id},nextId);
  await waitFor(()=>state.requests.some(row=>row.path===`/api/sessions/${nextId}`));
  assert.equal(await page.getByText('Authored message 205',{exact:true}).count(),0);
  release();
  await page.getByText('The next conversation body.',{exact:true}).waitFor();
  assert.equal(await page.getByText('Authored message 205',{exact:true}).count(),0);
});
