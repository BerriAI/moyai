const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');
const source = readFileSync('app/static/session-actions.js', 'utf8');
function harness() {
  const notices = [], calls = [];
  const state = {runs:[{id:'one'},{id:'two'}],runsRefresh:1,chatRefresh:1,chatRun:{id:'one',archived:false}};
  const context = {state,renderSidebar:()=>{},toast:message=>notices.push(message),
    refreshRuns:async()=>calls.push('refresh'),api:async(path,options)=>calls.push({path,options})};
  vm.createContext(context);vm.runInContext(readFileSync('app/static/credentials.js','utf8'),context);vm.runInContext(source,context);
  return {context,state,notices,calls};
}
function menuHarness(chat) {
  const h=harness(),c=h.context,nodes=new Map();let button,pending;
  const node=selector=>{if(!nodes.has(selector))nodes.set(selector,{focus(){},setAttribute(){},addEventListener(){},closest:()=>null,getBoundingClientRect:()=>({right:300,bottom:50})});return nodes.get(selector);};
  const menu={innerHTML:'',style:{},offsetWidth:200,offsetHeight:120,querySelector:node,matches:()=>false,
    hidePopover(){},showPopover(){},insertAdjacentHTML(_,html){this.innerHTML+=html;}};
  const detail={id:'one',chat_enabled:chat,archived:false};
  Object.assign(h.state,{selected:'one',runs:[{...detail},{id:'two'}],chatRun:chat?{...detail}:null});
  Object.assign(c,{document:{createElement:()=>node('header')},$:selector=>selector==='#session-actions'?menu:{append:value=>button=value},
    innerWidth:800,innerHeight:600,showError:error=>h.notices.push(error.message)});
  const folders=readFileSync('app/static/session-folders.js','utf8');
  vm.runInContext(folders.slice(folders.indexOf('function showSessionActions('),folders.indexOf('function renameSession(')),c);
  const change=c.changeSessionArchive;c.changeSessionArchive=run=>pending=change(run);
  c.bindSessionHeaderActions(detail);
  return {...h,menu,detail,button,archive:()=>{node('[data-archive-session]').onclick();return pending;}};
}
test('sidebar menu opens beside its session row, aligned with the row top',()=>{
  const {context:c,menu,detail,button}=menuHarness(true);
  const row={left:8,right:280,top:200,bottom:260};
  button.closest=selector=>selector==='.parent-session'?{getBoundingClientRect:()=>row}:null;
  button.getBoundingClientRect=()=>({right:276,bottom:233});
  c.showSessionActions(detail,button);
  assert.equal(Number.parseFloat(menu.style.top),row.top);
  assert.ok(Number.parseFloat(menu.style.left)>row.right,'Menu must not cover the session row');
  assert.ok(Number.parseFloat(menu.style.left)+menu.offsetWidth<=c.innerWidth-8);
});
test('narrow sidebar menu falls back below the trigger and remains within the viewport',()=>{
  const {context:c,menu,detail,button}=menuHarness(true);
  c.innerWidth=320;
  button.closest=()=>({getBoundingClientRect:()=>({right:288,top:200,bottom:260})});
  button.getBoundingClientRect=()=>({right:280,bottom:233});
  c.showSessionActions(detail,button);
  assert.ok(Number.parseFloat(menu.style.top)>=233);
  assert.ok(Number.parseFloat(menu.style.left)>=8);
  assert.ok(Number.parseFloat(menu.style.left)+menu.offsetWidth<=c.innerWidth-8);
});
test('a sidebar menu near the bottom shifts up to keep every action visible',()=>{
  const {context:c,menu,detail,button}=menuHarness(true);
  const row={right:280,top:540,bottom:600};
  button.closest=()=>({getBoundingClientRect:()=>row});
  button.getBoundingClientRect=()=>({right:276,bottom:573});
  c.showSessionActions(detail,button);
  assert.ok(Number.parseFloat(menu.style.left)>row.right);
  assert.ok(Number.parseFloat(menu.style.top)<row.top);
  assert.equal(Number.parseFloat(menu.style.top)+menu.offsetHeight,c.innerHeight-8);
});
test('session-header menu stays below its trigger and inside the right edge',()=>{
  const {context:c,menu,button}=menuHarness(true);
  button.getBoundingClientRect=()=>({right:798,bottom:40});
  button.onclick();
  assert.ok(Number.parseFloat(menu.style.top)>=40);
  assert.equal(Number.parseFloat(menu.style.left)+menu.offsetWidth,c.innerWidth-8);
});
for(const chat of [false,true])for(const origin of ['header','sidebar'])test(`${chat?'chat':'legacy'} header tracks archive and restore started from the ${origin}`,async()=>{
  const {context:c,state,menu,button,archive,calls}=menuHarness(chat);
  if(origin==='header')button.onclick();else c.showSessionActions(state.runs[0],button);
  assert.match(menu.innerHTML,/Archive session/);
  await archive();assert.equal(state.runs.some(run=>run.id==='one'),false);
  button.onclick();assert.match(menu.innerHTML,/Restore session/);
  await archive();button.onclick();assert.match(menu.innerHTML,/Archive session/);
  assert.deepEqual(calls.filter(call=>call.options).map(call=>JSON.parse(call.options.body)),[{archived:true},{archived:false}]);
});
test('archive invalidates earlier reads and updates only the selected session metadata', async () => {
  const {context:c,state,notices,calls} = harness();
  await c.changeSessionArchive({id:'one',archived:false});
  assert.equal(calls[0].path,'/api/runs/one/archive');
  assert.deepEqual(JSON.parse(calls[0].options.body),{archived:true});
  assert.deepEqual(state.runs.map(run=>run.id),['two']);
  assert.equal(state.runsRefresh,2);assert.equal(state.chatRefresh,2);
  assert.equal(state.sessionEdits,1);
  assert.equal(state.chatRun.archived,true);assert.equal(state.sessionMutation,null);
  assert.match(notices[0],/Ask Moyai to find it/);
});
test('rejected archive retains the row and permits retry without false success', async () => {
  const {context:c,state,notices,detail,menu,button,archive} = menuHarness(true);
  c.api=async()=>{throw Error('Save unavailable');};
  await assert.rejects(c.changeSessionArchive(detail),/Save unavailable/);
  assert.equal(state.runs.length,2);assert.equal(state.chatRun.archived,false);
  assert.equal(detail.archived,false);button.onclick();assert.match(menu.innerHTML,/Archive session/);
  assert.equal(state.runsRefresh,1);assert.equal(state.sessionMutation,null);assert.equal(notices.length,0);
  c.api=async()=>{};await archive();button.onclick();assert.match(menu.innerHTML,/Restore session/);
});
test('a pending restore keeps its original intent and never changes a different open session or header', async () => {
  const {context:c,state,notices,calls} = menuHarness(true);let finish;
  c.api=async(path,options)=>{calls.push({path,options});await new Promise(resolve=>finish=resolve);};
  const item={id:'one',archived:true},pending=c.changeSessionArchive(item);
  state.chatRun={id:'two',archived:true};item.archived=false;
  c.bindSessionHeaderActions({id:'two',archived:true});
  await c.changeSessionArchive({id:'one',archived:true});
  assert.equal(calls.length,1);finish();await pending;
  assert.equal(state.chatRun.archived,true);
  assert.equal(state.sessionHeaderRun.archived,true);
  assert.deepEqual(JSON.parse(calls[0].options.body),{archived:false});
  assert.match(notices[0],/restored/);
});

function deletionHarness() {
  const h=harness();let nodes,buttons;
  const dialog={open:false,set innerHTML(value){
    this.html=value;nodes=new Map();
    for(const selector of ['form','[data-cancel]','.folder-error','[type="submit"]'])
      nodes.set(selector,{disabled:false,textContent:'',focus(){this.focused=true;}});
    nodes.get('[type="submit"]').textContent='Delete session';
    buttons=['[data-cancel]','[type="submit"]'].map(selector=>nodes.get(selector));
  },querySelector:selector=>nodes.get(selector),querySelectorAll:()=>buttons,
    showModal(){this.open=true;},close(){this.open=false;}};
  Object.assign(h.context,{$:()=>dialog,esc:value=>value,sessionTitle:run=>run.id,
    api:async(path,options)=>{h.calls.push({path,options});return {deleted:true};},updateChatStatus(){},
    navigate:async view=>h.calls.push({navigate:view}),showError:error=>h.notices.push(error.message)});
  h.state.selected='one';
  h.context.deleteSessionDialog({id:'one'});
  return {...h,dialog,get nodes(){return nodes;},get buttons(){return buttons;},
    submit:()=>nodes.get('form').onsubmit({preventDefault(){}})};
}
test('failed deletion keeps the session and dialog, then permits a successful retry',async()=>{
  const {context:c,state,dialog,nodes,buttons,submit,calls,notices}=deletionHarness();
  assert.equal(nodes.get('[data-cancel]').focused,true);
  c.api=async()=>{throw Error('Delete unavailable');};
  await submit();
  assert.equal(dialog.open,true);assert.equal(state.runs.length,2);
  assert.equal(nodes.get('.folder-error').textContent,'Delete unavailable');
  assert.equal(buttons.every(button=>!button.disabled),true);assert.equal(notices.length,0);
  assert.equal(nodes.get('[type="submit"]').textContent,'Delete session');
  c.api=async()=>({deleted:true});await submit();
  assert.equal(dialog.open,false);assert.deepEqual(state.runs.map(run=>run.id),['two']);
  assert.equal(calls.at(-1).navigate,'tasks');assert.equal(notices.at(-1),'Session deleted.');
});
test('accepted deletion closes confirmation while server cleanup continues automatically',async()=>{
  const {context:c,state,dialog,submit,calls,notices}=deletionHarness();
  assert.match(dialog.html,/automatically stops the session and its agents/);
  assert.doesNotMatch(dialog.html,/data-stop-session/);
  c.api=async(path,options)=>{calls.push({path,options});return {deleted:false,deleting:true};};
  await submit();
  assert.equal(calls[0].path,'/api/runs/one');assert.equal(calls[0].options.method,'DELETE');
  assert.deepEqual(calls.slice(1),['refresh']);
  assert.equal(dialog.open,false);assert.equal(state.runs.length,2);assert.equal(state.selected,'one');
  assert.equal(state.runs[0].status,'deleting');assert.equal(state.chatRun.status,'deleting');
  assert.equal(state.runsRefresh,2);assert.equal(state.chatRefresh,2);assert.equal(state.sessionEdits,1);
  assert.match(notices[0],/Deleting session.*automatically/);assert.doesNotMatch(notices[0],/Session deleted/);
});
test('one confirmation submits once and shows progress until the server acknowledges it',async()=>{
  const {context:c,dialog,nodes,buttons,submit}=deletionHarness();let finish,calls=0;
  c.api=()=>{calls++;return new Promise(resolve=>finish=resolve);};
  const pending=submit();await submit();
  assert.equal(calls,1);assert.equal(nodes.get('[type="submit"]').textContent,'Deleting…');
  assert.equal(buttons.every(button=>button.disabled),true);
  let prevented=false;dialog.oncancel({preventDefault(){prevented=true;}});
  nodes.get('[data-cancel]').onclick();assert.equal(prevented,true);assert.equal(dialog.open,true);
  finish({deleted:false,deleting:true});await pending;assert.equal(dialog.open,false);
});
test('cancel before confirming does not submit deletion',async()=>{
  const {dialog,nodes,submit,calls}=deletionHarness();
  nodes.get('[data-cancel]').onclick();await submit();
  assert.equal(dialog.open,false);assert.deepEqual(calls,[]);
});
test('delayed deletion never navigates away from a newly selected session',async()=>{
  const {context:c,state,dialog,submit,calls}=deletionHarness();let finish;
  c.api=()=>new Promise(resolve=>finish=resolve);
  const pending=submit();
  state.selected='two';state.activeParentId='two';
  finish({deleted:true});await pending;
  assert.equal(dialog.open,false);assert.equal(state.selected,'two');
  assert.deepEqual(calls,['refresh']);
});
for(const result of ['deleted','pending','error'])test(`a late ${result} response cannot change a reopened confirmation`,async()=>{
  const h=deletionHarness(),{context:c,dialog,submit,calls}=h;let finish,reject;
  c.api=()=>new Promise((resolve,fail)=>{finish=resolve;reject=fail;});
  const pending=submit();dialog.close();c.deleteSessionDialog({id:'one'});
  const newForm=h.nodes.get('form');
  if(result==='error')reject(Error('Old request failed'));
  else finish({deleted:result==='deleted',deleting:result==='pending'});
  await pending;
  assert.equal(dialog.open,true);assert.equal(h.nodes.get('form'),newForm);
  assert.equal(h.nodes.get('.folder-error').textContent,'');assert.equal(h.buttons.every(button=>!button.disabled),true);
  assert.equal(h.nodes.get('[type="submit"]').textContent,'Delete session');assert.equal(dialog.oncancel,null);
  assert.equal(calls.some(call=>call.navigate),false);
});
function composerHarness(){
  const app=readFileSync('app/static/app.js','utf8'),nodes=new Map(),locks=[],queue=[];
  const node=selector=>{if(!nodes.has(selector))nodes.set(selector,{disabled:false,inert:false,value:'Keep this draft',contentEditable:'true',dataset:{},setAttribute(name,value){this[name]=value;},removeAttribute(name){delete this[name];},dispatchEvent(){},querySelectorAll:()=>[],querySelector:node});return nodes.get(selector);};
  const send=node('#message-form [type="submit"]');nodes.set('#message-form .send-button',send);
  const form=node('#message-form'),input=node('#followup'),model=node('#chat-model'),stop=node('#stop-response'),now=node('[data-send-now]');
  form.querySelectorAll=selector=>selector==='[type="submit"]'?[send,now]:[send,now,model,stop];
  const conversation=node('#conversation');conversation.dataset.messages='[]';conversation.scrollHeight=conversation.scrollTop=conversation.clientHeight=0;
  const state={selected:'one',sending:new Set(),modelDrafts:{},drafts:{one:input.value},config:{},pendingMessages:{},attachments:{ids:()=>[],clear(){},lock:value=>locks.push(value)},messageQueue:{render:run=>queue.push(run.status)},preferences:{}};
  const c={state,$:node,terminal:new Set(['idle','completed','failed','cancelled','interrupted']),document:{},statusLabel:status=>status,crypto:{randomUUID:()=> 'message-one'},toast(){},Event:class{},autoSize(){},refreshChat:async()=>{},refreshRuns:async()=>{},bottom(){},
    MoyaiQueue:{presentation:()=>({transcript:[]})},MoyaiActivity:{sync(){}},savedFiles:{decorate(){},sync(){}},renderMarkdown(){},copyText(){},
    renderChatWorking(){},syncRunSummary(){},renderCredentialRequests(){},renderApprovals(){},renderPrWriteAccess(){},renderSlackContext(){},renderAgentDetails(){}};
  vm.createContext(c);
  const controls=app.indexOf('function syncChatComposer(');
  vm.runInContext(app.slice(controls<0?app.indexOf('function updateChatStatus('):controls,app.indexOf('function renderChatWorking('))+
    app.slice(app.indexOf('function updateChat('),app.indexOf('function renderLiveWork(')),c);
  return {c,state,form,input,model,stop,send,now,locks,queue,nodes,node,refresh:status=>c.updateChat({id:'one',status,messages:[],events:[]}),bindSubmit(){
    const start=app.indexOf("  $('#message-form').onsubmit=async e=>{");
    vm.runInContext("(()=>{const id='one',run={};"+app.slice(start,app.indexOf('\n  updateChat(run,true);',start))+'})();',c);
    return ()=>form.onsubmit({preventDefault(){},currentTarget:form});
  }};
}

test('a complete detail refresh keeps a deleting composer disabled',()=>{
  const b=composerHarness();b.refresh('deleting');
  assert.equal(b.send.disabled,true,'detail refresh must not re-enable Send');
  assert.equal(b.now.disabled,true);assert.equal(b.form.inert,true);
  assert.equal(b.input.contentEditable,'false');assert.equal(b.model.disabled,true);assert.equal(b.locks.at(-1),true);
});

test('late detail and stream status cannot revive a deleting composer or its queue',()=>{
  const b=composerHarness();b.refresh('deleting');b.refresh('running');b.c.updateChatStatus({status:'idle',active:false});
  assert.equal(b.state.chatRun.status,'deleting');assert.equal(b.send.disabled,true);assert.equal(b.form.inert,true);
  assert.ok(b.queue.every(status=>status==='deleting'));assert.equal(b.locks.at(-1),true);
});

for(const outcome of ['success','error'])test(`an in-flight message ${outcome} cannot unlock a deleting session or permit another send`,async()=>{
  const b=composerHarness();b.refresh('running');let finish,calls=0;
  b.c.api=()=>{calls++;return new Promise((resolve,reject)=>finish=()=>outcome==='success'?resolve({}):reject(Error('Session is being deleted')));};
  const submit=b.bindSubmit(),pending=submit();assert.equal(b.send.disabled,true);assert.equal(b.now.disabled,true);
  b.refresh('deleting');finish();await pending;await submit();
  assert.equal(calls,1);assert.equal(b.send.disabled,true);assert.equal(b.form.inert,true);assert.equal(b.locks.at(-1),true);
  if(outcome==='error')assert.equal(b.input.value,'Keep this draft');
});

test('sending completion preserves a replacement mount and recomputes its own deletion lock',async()=>{
  const b=composerHarness();b.refresh('running');let finish;
  b.c.api=()=>new Promise(resolve=>finish=resolve);
  const pending=b.bindSubmit()();
  const nextForm=b.node('replacement-form'),nextInput=b.node('replacement-input'),nextSend=b.node('replacement-send'),nextLocks=[];
  nextForm.querySelectorAll=()=>[nextSend];nextForm.inert=true;
  b.nodes.set('#message-form',nextForm);b.nodes.set('#followup',nextInput);
  b.state.attachments={lock:value=>nextLocks.push(value)};b.state.chatRun={id:'one',status:'deleting'};
  finish({});await pending;
  assert.equal(nextInput.value,'Keep this draft');assert.equal(b.state.drafts.one,'Keep this draft');
  assert.equal(nextSend.disabled,true);assert.equal(nextForm.inert,true);assert.equal(nextLocks.at(-1),true);
  assert.equal(b.locks.at(-1),true,'the old attachment controller must not unlock a replacement mount');
});

test('ordinary status and new mounts remain editable while sending only locks submission',()=>{
  const b=composerHarness();b.refresh('running');assert.equal(b.form.inert,false);assert.equal(b.send.disabled,false);
  b.state.sending.add('one');b.refresh('running');assert.equal(b.send.disabled,true);assert.equal(b.now.disabled,true);
  assert.equal(b.form.inert,false);assert.equal(b.input.contentEditable,'true');assert.equal(b.model.disabled,false);
  b.state.sending.clear();b.refresh('idle');assert.equal(b.send.disabled,false);assert.equal(b.locks.at(-1),false);
  b.refresh('deleting');const other=composerHarness();other.refresh('idle');assert.equal(other.form.inert,false);assert.equal(other.send.disabled,false);
});

test('archive while opening a session rereads its metadata before rendering the header',async()=>{
  const app=readFileSync('app/static/app.js','utf8');let finish,rendered,reads=0;
  const state={runs:[{id:'one'}],pageVersion:0,expandedParents:new Set()};
  const c={state,stopStream(){},refreshRuns:async()=>{},document:{hidden:true,querySelector:()=>null},
    setView(){},sessionTitle:()=>'',history:{replaceState(){}},renderChat:run=>rendered=run,
    api:async()=>{if(++reads===1)return new Promise(resolve=>finish=resolve);return {id:'one',chat_enabled:true,archived:true};}};
  vm.createContext(c);vm.runInContext(readFileSync('app/static/credentials.js','utf8'),c);
  vm.runInContext(app.slice(app.indexOf('async function openRun('),app.indexOf('function renderChat(')),c);
  const pending=c.openRun('one');state.sessionEdits=1;
  finish({id:'one',chat_enabled:true,archived:false});await pending;
  assert.equal(reads,2);assert.equal(rendered.archived,true);
});

function availabilityHarness(){
  const h=harness(),c=h.context,app=readFileSync('app/static/app.js','utf8');
  const content={innerHTML:'Old conversation'},source={close(){this.closed=true;}};
  Object.assign(h.state,{pageVersion:1,selected:'one',source,drafts:{one:'Unsent reply'}});
  Object.assign(c,{document:{querySelector:()=>null},history:{replaceState(){}},$:()=>content,computer:{close(){}},savedFiles:{reset(){}},clearTimeout(){},
    navigate:async view=>{c.stopStream();h.state.selected=null;h.state.pageVersion++;h.calls.push({navigate:view});}});
  vm.runInContext(app.slice(app.indexOf('function stopStream()'),app.indexOf('function sessionTitle('))+
    app.slice(app.indexOf('async function openRun('),app.indexOf('function renderChat('))+
    app.slice(app.indexOf('async function refreshChat('),app.indexOf('function eventHTML(')),c);
  return {...h,content,source};
}
test('leaving a session releases its header metadata',()=>{
  const {context:c,state}=availabilityHarness();
  state.sessionHeaderRun={id:'one',archived:true};
  c.stopStream();assert.equal(state.sessionHeaderRun,null);
});
for(const method of ['openRun','refreshChat']){
  test(`${method} clears a confirmed unavailable session and returns home`,async()=>{
    const {context:c,state,content,source,calls}=availabilityHarness();
    c.api=async()=>{throw Object.assign(Error('Session not found.'),{status:404});};
    await c[method]('one');
    assert.equal(source.closed,true);assert.equal(state.selected,null);assert.equal(state.chatRun,null);
    assert.match(content.innerHTML,/no longer available/);assert.deepEqual(calls,[{navigate:'tasks'}]);
    assert.equal(state.drafts.one,'Unsent reply');
  });
  test(`${method} ignores a late missing-session response after navigation`,async()=>{
    const {context:c,state,content,calls}=availabilityHarness();let reject;
    c.api=()=>new Promise((_,fail)=>reject=fail);
    const pending=c[method]('one');
    state.pageVersion++;state.selected='two';content.innerHTML='New conversation';
    reject(Object.assign(Error('Session not found.'),{status:404}));await pending;
    assert.equal(state.selected,'two');assert.equal(content.innerHTML,'New conversation');assert.deepEqual(calls,[]);
  });
  test(`${method} does not treat a temporary error as deletion`,async()=>{
    const {context:c,content,calls}=availabilityHarness();
    c.api=async()=>{throw Object.assign(Error('Temporarily unavailable'),{status:503});};
    await assert.rejects(c[method]('one'),/Temporarily unavailable/);
    assert.equal(content.innerHTML,'Old conversation');assert.deepEqual(calls,[]);
  });
}

test('legacy task streams also close on the terminal deletion marker',async()=>{
  const {context:c,state,content}=availabilityHarness();
  Object.assign(c,{document:{hidden:true,querySelector:()=>null},setView(){},sessionTitle:()=>'',esc:value=>value,
    history:{replaceState(){}},terminal:new Set(['completed']),renderApprovals(){},renderPrWriteAccess(){},bindSessionHeaderActions(){},eventHTML:()=>'',statusLabel:value=>value,
    savedFiles:{reset(){},sync(){}},showError:error=>{throw error;},
    EventSource:class{constructor(){this.handlers={};}addEventListener(name,handler){this.handlers[name]=handler;}close(){this.closed=true;}},
    api:async()=>({id:'one',mode:'demo',status:'running',events:[],plugins:[]})});
  state.expandedParents=new Set();
  await c.openRun('one');const source=state.source;
  await source.handlers.deleted();
  assert.equal(source.closed,true);assert.equal(state.selected,null);
  assert.match(content.innerHTML,/no longer available/);
});
