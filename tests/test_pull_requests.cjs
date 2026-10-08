const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const panelPath=process.env.PANEL_SOURCE||'app/static/workspace-panel.js';
const {prUrl,restore}=require(require('node:path').resolve(panelPath));
const url='https://github.com/BerriAI/moyai/pull/145';
test('panel defaults leave most space for chat and preserve saved resize choices',()=>{
  for(const value of [null, '{', '{}', JSON.stringify({width:0})])
    assert.equal(restore(value).width,40);
  for(const [width,expected] of [[30,30],[48,48],[60,60],[70,70],[10,30],[90,70]])
    assert.equal(restore(JSON.stringify({width})).width,expected);
});

test('only canonical PR destinations survive tab restore',()=>{
  for(const repository of ['moyai','.github','_config','-tools','Mixed.Case-repo_1']){
    const accepted=url.replace('moyai',repository);
    assert.equal(prUrl(accepted),accepted);
    assert.equal(restore(JSON.stringify({tabs:[{id:'pr:'+accepted,kind:'pr',url:accepted}]})).tabs[0].url,accepted);
  }
  for(const bad of ['javascript:alert(1)',url+'/files',url+'?token=x',url+'#x',url.replace('github.com','github.com.evil.test'),url.replace('145','0'),url.replace('moyai','x'.repeat(512)),'https://user@github.com/BerriAI/moyai/pull/145']){
    assert.equal(prUrl(bad),null,bad);
    assert.deepEqual(restore(JSON.stringify({tabs:[{id:'pr:unsafe',kind:'pr',url:bad}]})).tabs,[]);
  }
  assert.equal(restore(JSON.stringify({tabs:[{id:'pr:'+url,kind:'pr',url}]})).tabs[0].url,url);
});
test('saved captures restore as a session tab with their selected state',()=>{
  const saved=restore(JSON.stringify({visible:true,active:'captures',tabs:[
    {id:'computer',kind:'computer',title:'Computer'},
    {id:'captures',kind:'captures',title:'Saved captures'}
  ]}));
  assert.deepEqual(saved.tabs.map(tab=>tab.kind),['computer','captures']);
  assert.equal(saved.active,'captures');assert.equal(saved.visible,true);
});
test('only session PR links in the owning chat use the native panel; modified and embedded links stay native',()=>{
  const source=fs.readFileSync(panelPath,'utf8'),opened=[];
  const ctx={prUrl,openPullRequest:link=>{if(link!==url)return false;opened.push(link);return true;}};
  vm.createContext(ctx);vm.runInContext(source.slice(source.indexOf('    function followPullRequest('),source.indexOf("    layout.addEventListener('click',followPullRequest)")),ctx);
  let prevented=0;const event=(href=url,scope=true,extra={})=>({target:{closest:()=>({closest:()=>scope,getAttribute:()=>href})},preventDefault(){prevented++;},...extra});
  ctx.followPullRequest(event());assert.deepEqual(opened,[url]);assert.equal(prevented,1);
  for(const modifier of ['ctrlKey','metaKey','shiftKey','altKey'])ctx.followPullRequest(event(url,true,{[modifier]:true}));
  ctx.followPullRequest(event(url,false));ctx.followPullRequest(event(url.replace('145','999')));ctx.followPullRequest(event(url,true,{button:1}));
  assert.equal(opened.length,1);assert.equal(prevented,1);
});
function closingPanel({active='pr:a',mode='modal'}={}){
  const events=[],tabs=new Map();
  for(const id of ['pr:a','pr:b'])tabs.set(id,{id,kind:'pr',url:id==='pr:a'?url:url.replace('145','135'),loaded:true,
    element:{hidden:id!==active,remove(){events.push('remove:'+id);}},
    activate(){events.push('activate:'+id);},deactivate(){events.push('deactivate:'+id);}});
  const closingStatus={hidden:true};
  const ctx={tabs,active,visible:true,disposed:false,closingStatus,run:{id:'session',mode},
    computer:{closeTab(id,tab){events.push('close:'+tab);throw Error('Native PR close must not use Computer');}},
    draw(){},save(){},toast:message=>events.push(message),q:()=>({scrollIntoView(){}}),
    document:{querySelector:()=>null},setVisible(value){ctx.visible=value;},
    pullRequests:[{url,title:'Review'}],make:()=>tabs.get('pr:a')};
  const source=fs.readFileSync(panelPath,'utf8');
  vm.createContext(ctx);
  vm.runInContext(source.slice(source.indexOf('    function select('),source.indexOf('    function error(')),ctx);
  vm.runInContext(source.slice(source.indexOf('    function hide('),source.indexOf('    function menu(')),ctx);
  vm.runInContext(source.slice(source.indexOf('    function openPullRequest('),source.indexOf('    function syncPullRequests(')),ctx);
  return {ctx,tabs,events,closingStatus};
}
test('cloud PR close is local and never asks the computer to close a browser',async()=>{
  for(const hidden of [false,true]){
    const f=closingPanel();if(hidden)f.ctx.hide();
    await f.ctx.remove('pr:a');
    assert.equal(f.events.some(e=>e.startsWith('close:')),false);
    assert.equal(f.tabs.has('pr:a'),false);assert.equal(f.ctx.active,'pr:b');
    assert.equal(f.ctx.visible,!hidden);
    assert.equal(f.events.includes('activate:pr:b'),!hidden);
    await f.ctx.remove('pr:b');assert.equal(f.tabs.size,0);assert.equal(f.ctx.visible,false);
  }
});
test('closing an inactive PR preserves the selected file tab',async()=>{
  const f=closingPanel();
  f.tabs.set('files',{id:'files',kind:'files',loaded:true,element:{hidden:true},activate(){f.events.push('activate:files');}});
  f.ctx.select('files');await f.ctx.remove('pr:a');
  assert.equal(f.ctx.active,'files');assert.equal(f.ctx.visible,true);
  assert.equal(f.events.filter(e=>e==='activate:files').length,1);
  assert.equal(f.events.some(e=>e.startsWith('close:')),false);
});
test('PR mount delegates to native details for every run mode without touching Computer',async()=>{
  for(const mode of ['modal','demo']){
    const source=fs.readFileSync(panelPath,'utf8'),calls=[],element={};
    const ctx={run:{id:'session',mode},good:()=>true,markdown:String,esc:String,encodeURIComponent,
      api:async path=>{calls.push(path);return {};},
      computer:new Proxy({},{get(){throw Error('PR viewing must never access Computer');}}),
      MoyaiPullRequest:{mount(options){assert.equal(options.element,element);return {activate:options.load};}}};
    vm.createContext(ctx);vm.runInContext(source.slice(source.indexOf('    async function mount('),source.indexOf('    function mountCaptures(')),ctx);
    const tab={kind:'pr',url,element};await ctx.mount(tab);await tab.activate();
    assert.deepEqual(calls,['/api/runs/session/pull-request?url='+encodeURIComponent(url)]);
  }
});
test('switching between live views and captures runs each owning tab lifecycle',()=>{
  for(const id of ['computer','pr:a']){
    const f=closingPanel({active:id});
    if(id==='computer')f.tabs.set(id,{id,kind:'computer',loaded:true,element:{hidden:false},
      activate(){f.events.push('activate:'+id);},deactivate(){f.events.push('deactivate:'+id);}});
    f.tabs.set('captures',{id:'captures',kind:'captures',loaded:true,element:{hidden:true},
      activate(){f.events.push('activate:captures');},deactivate(){f.events.push('deactivate:captures');}});
    f.ctx.select('captures');
    assert.equal(f.ctx.active,'captures');assert.equal(f.tabs.get(id).element.hidden,true);
    assert.deepEqual(f.events,['deactivate:'+id,'activate:captures']);
    f.ctx.select(id);
    assert.deepEqual(f.events,['deactivate:'+id,'activate:captures','deactivate:captures','activate:'+id]);
    assert.equal(f.events.some(event=>event.startsWith('close:')),false);
  }
});
test('selecting Computer creates one discoverable captures sibling without selecting it',()=>{
  const f=closingPanel({active:''}),source=fs.readFileSync(panelPath,'utf8');
  let serial=0;f.ctx.crypto={randomUUID:()=>String(++serial)};f.ctx.views={append(){}};
  f.ctx.document.createElement=()=>({hidden:true,setAttribute(){}});
  vm.runInContext(source.slice(source.indexOf('    function make('),source.indexOf('    function open(')),f.ctx);
  const computer=f.ctx.make('computer');computer.loaded=true;computer.activate=()=>f.events.push('activate:computer');
  f.ctx.select(computer.id);
  assert.equal(f.ctx.active,'computer');assert.equal(f.tabs.get('captures')?.kind,'captures');
  assert.equal(f.tabs.get('captures').element.hidden,true);
  const sibling=f.tabs.get('captures');f.ctx.select(computer.id);
  assert.equal(f.tabs.get('captures'),sibling);assert.deepEqual(f.events,['activate:computer']);
  f.tabs.delete('captures');f.ctx.select(computer.id);
  assert.equal(f.tabs.has('captures'),false,'Reselecting the active tab must not create an undrawn sibling');
});

function capturePanel(){
  const source=fs.readFileSync(panelPath,'utf8');
  const start=source.indexOf('    function mountCaptures(');
  assert.ok(start>=0,'The workspace owns a saved-captures lifecycle');
  const end=source.indexOf('\n    function ',start+1);
  const nodes=new Map(),requests=[],timers=new Map(),listeners=new Map(),opened=[];let serial=0,disposed=false;
  const video={pauses:0,pause(){this.pauses++;}};
  function node(selector){
    if(!nodes.has(selector))nodes.set(selector,{html:'',writes:0,get innerHTML(){return this.html;},set innerHTML(value){this.html=value;this.writes++;},textContent:'',hidden:false,disabled:false,dataset:{},isConnected:true,
      querySelector:node,querySelectorAll:kind=>kind==='video'?[video]:[],
      addEventListener(name,fn){this[name]=fn;},setAttribute(){}});
    return nodes.get(selector);
  }
  const tab={id:'captures',kind:'captures',element:node('tab')};
  const context={run:{id:'session'},esc:value=>String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    good:t=>!disposed&&t===tab,openFile:file=>opened.push(file),size:n=>n+' B',
    api:(path,options)=>new Promise((resolve,reject)=>requests.push({path,options,resolve,reject})),
    document:{hidden:false,addEventListener:(name,fn)=>listeners.set(name,fn),removeEventListener:name=>listeners.delete(name)},
    setTimeout:(fn,delay)=>{timers.set(++serial,{fn,delay});return serial;},clearTimeout:id=>timers.delete(id)};
  vm.createContext(context);vm.runInContext(source.slice(start,end<0?undefined:end),context);context.mountCaptures(tab);
  return {tab,node,requests,timers,listeners,video,opened,
    async poll(){const [id,timer]=timers.entries().next().value;timers.delete(id);timer.fn();await new Promise(setImmediate);},
    dispose(){disposed=true;tab.dispose();}};
}
const captured=(name='proof.png',kind='image')=>({name,kind,path:'moyai-captures/'+name,archive_path:'capture:'+name,
  inline_url:'/captures/'+name,url:'/captures/'+name+'?download=true',size:12});
const settleCaptures=()=>new Promise(setImmediate);

test('captures read saved media without a live computer and discover newly completed captures',async()=>{
  const f=capturePanel();f.tab.activate();
  assert.equal(f.requests.length,1);assert.equal(f.requests[0].path,'/api/runs/session/files');assert.equal(f.requests[0].options,undefined);
  f.requests[0].resolve({files:[]});await settleCaptures();
  assert.match(f.node('[data-captures]').innerHTML,/No saved captures yet/);
  assert.equal([...f.timers.values()][0].delay,3000);
  await f.poll();
  f.requests[1].resolve({files:[captured(),captured('flow.webm','video'),{...captured('unrelated.png'),archive_path:'new-files/unrelated.png'}]});
  await settleCaptures();
  const gallery=f.node('[data-captures]').innerHTML;
  assert.match(gallery,/proof\.png/);assert.match(gallery,/<video[^>]+flow\.webm/);assert.match(gallery,/download="proof\.png"/);
  assert.doesNotMatch(gallery,/unrelated\.png/);
  const writes=f.node('[data-captures]').writes;await f.poll();
  f.requests[2].resolve({files:[captured(),captured('flow.webm','video')]});await settleCaptures();
  assert.equal(f.node('[data-captures]').writes,writes,'Unchanged catalog refresh must not restart video playback');
  assert.ok(f.requests.every(request=>request.path.endsWith('/files')&&!request.options));
  f.dispose();
});
test('capture previews open the saved file while downloads and modified clicks stay native',async()=>{
  const f=capturePanel(),file=captured();f.tab.activate();f.requests[0].resolve({files:[file]});await settleCaptures();
  function click(extra={},download=false,href=file.inline_url){
    const link={hasAttribute:name=>name==='download'&&download,getAttribute:()=>href};
    const event={target:{closest:()=>link},preventDefault(){this.prevented=true;},...extra};
    f.node('[data-captures]').onclick(event);return event;
  }
  assert.equal(click().prevented,true);assert.deepEqual(f.opened,[file]);
  for(const modifier of ['metaKey','ctrlKey','shiftKey','altKey'])assert.equal(click({[modifier]:true}).prevented,undefined);
  assert.equal(click({},true,file.url).prevented,undefined);assert.equal(click({},false,'/not-a-capture').prevented,undefined);
  assert.deepEqual(f.opened,[file]);f.dispose();
});

test('capture refresh errors preserve saved media and retry successfully',async()=>{
  const f=capturePanel();f.tab.activate();f.requests[0].resolve({files:[captured()]});await settleCaptures();
  const gallery=f.node('[data-captures]').innerHTML;
  await f.poll();f.requests[1].reject(Object.assign(Error('Temporarily unavailable'),{status:503}));await settleCaptures();
  assert.equal(f.node('[data-captures]').innerHTML,gallery);
  assert.match(f.node('[data-status]').textContent,/Temporarily unavailable/);assert.equal(f.timers.size,1);
  await f.poll();f.requests[2].resolve({files:[captured(),captured('later.png')]});await settleCaptures();
  assert.match(f.node('[data-captures]').innerHTML,/later\.png/);assert.doesNotMatch(f.node('[data-status]').textContent,/Temporarily unavailable/);
  f.dispose();
});

test('missing saved files show an empty captures view that can refresh later',async()=>{
  const f=capturePanel();f.tab.activate();
  f.requests[0].reject(Object.assign(Error('No saved files are available yet.'),{status:404}));await settleCaptures();
  assert.match(f.node('[data-captures]').innerHTML,/No saved captures yet/);
  assert.doesNotMatch(f.node('[data-status]').textContent,/No saved files are available/);assert.equal(f.timers.size,1);
  await f.poll();f.requests[1].resolve({files:[captured()]});await settleCaptures();
  assert.match(f.node('[data-captures]').innerHTML,/proof\.png/);f.dispose();
});

test('hidden and disposed captures pause playback and reject late reads',async()=>{
  for(const dispose of [false,true]){
    const f=capturePanel();f.tab.activate();
    if(dispose)f.dispose();else f.tab.deactivate();
    assert.ok(f.video.pauses>0);assert.equal(f.timers.size,0);
    const hidden=f.node('[data-captures]').innerHTML;
    f.requests[0].resolve({files:[captured('stale.png')]});await settleCaptures();
    assert.equal(f.node('[data-captures]').innerHTML,hidden);assert.equal(f.timers.size,0);
    if(!dispose){
      f.tab.activate();assert.equal(f.requests.length,2);
      f.requests[1].resolve({files:[captured('current.png')]});await settleCaptures();
      assert.match(f.node('[data-captures]').innerHTML,/current\.png/);f.dispose();
    }
  }
});
test('returning to captures invalidates an earlier read before its delayed response arrives',async()=>{
  const f=capturePanel();f.tab.activate();f.tab.deactivate();f.tab.activate();
  assert.equal(f.requests.length,2);
  f.requests[1].resolve({files:[captured('current.png')]});await settleCaptures();
  const current=f.node('[data-captures]').innerHTML;
  f.requests[0].resolve({files:[captured('stale.png')]});await settleCaptures();
  assert.equal(f.node('[data-captures]').innerHTML,current);assert.match(current,/current\.png/);
  assert.equal(f.timers.size,1);f.dispose();
});

const native=require('../app/static/pull-request.js');
const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const details={number:145,title:'Native review',state:'open',draft:false,merged:false,base:'main',head:'abcdef0',head_ref:'feature',author:'alex',
  additions:2,deletions:1,changed_files:2,body:'Description',files:[{filename:'src/view.js',status:'modified',additions:2,deletions:1,patch:'@@ -4,2 +4,3 @@\n-old\n+new\n+added\n context'},
  {filename:'logo.png',status:'added',additions:0,deletions:0,patch:''}]};
function nativePanel(){
  const nodes=new Map(),requests=[];
  function node(selector){
    if(!nodes.has(selector))nodes.set(selector,{innerHTML:'',textContent:'',className:'',disabled:false,scrollTop:0,dataset:{},setAttribute(){},querySelector:node,querySelectorAll(){return [];}});
    return nodes.get(selector);
  }
  const lifecycle=native.mount({element:node('root'),url,escape,markdown:escape,
    load:()=>new Promise((resolve,reject)=>requests.push({resolve,reject}))});
  return {node,requests,...lifecycle};
}
test('native diffs show source line numbers, missing patches and truncation without interpreting code as HTML',()=>{
  assert.deepEqual(native.diffRows(details.files[0].patch).map(r=>[r.old,r.next]),[['',''],[4,''],['',4],['',5],[5,6]]);
  const html=native.changes({...details,files_truncated:true,files:[{...details.files[0],filename:'<script>.js',patch:'+<img onerror=x>',patch_truncated:true},details.files[1]]},escape,new Set());
  assert.match(html,/&lt;script&gt;\.js/);assert.match(html,/&lt;img onerror=x&gt;/);assert.doesNotMatch(html,/<img/);
  assert.match(html,/first 100 files/);assert.match(html,/diff is truncated/);assert.match(html,/did not provide a text diff/);
});
test('native PR refresh keeps prior data on temporary failure and clears it on access denial',async()=>{
  const f=nativePanel(),first=f.activate();f.requests[0].resolve(details);await first;
  const body=f.node('[data-content]').innerHTML;assert.match(body,/src\/view.js/);assert.match(f.node('[data-heading]').innerHTML,/Native review/);
  const refresh=f.node('[data-refresh]').onclick();f.requests[1].reject(Object.assign(Error('Offline'),{status:502}));await refresh;
  assert.equal(f.node('[data-content]').innerHTML,body);assert.match(f.node('[data-status]').textContent,/previous version/);
  const denied=f.node('[data-refresh]').onclick();f.requests[2].reject(Object.assign(Error('Access removed'),{status:403}));await denied;
  assert.equal(f.node('[data-content]').innerHTML,'');assert.equal(f.node('[data-heading]').innerHTML,'');assert.equal(f.node('[data-state]').textContent,'Unavailable');
  const retry=f.node('[data-refresh]').onclick();f.requests[3].resolve(details);await retry;
  assert.match(f.node('[data-content]').innerHTML,/src\/view.js/);assert.equal(f.node('[data-status]').textContent,'');
});
test('hidden, closed and reactivated PR tabs reject stale detail responses',async()=>{
  for(const ending of ['deactivate','dispose']){
    const f=nativePanel(),pending=f.activate();f[ending]();f.requests[0].resolve(details);await pending;
    assert.equal(f.node('[data-heading]').innerHTML,'');
  }
  const f=nativePanel(),old=f.activate();f.deactivate();const current=f.activate();
  f.requests[1].resolve({...details,title:'Current'});await current;
  f.requests[0].resolve({...details,title:'Stale'});await old;
  assert.match(f.node('[data-heading]').innerHTML,/Current/);assert.doesNotMatch(f.node('[data-heading]').innerHTML,/Stale/);
});

function agentToolbox(){
  const source=fs.readFileSync(panelPath,'utf8');
  const host=()=>({hidden:false,writes:0,html:'',contains:()=>false,querySelector:()=>null,
    set innerHTML(value){this.html=value;this.writes++;},get innerHTML(){return this.html;}});
  const card=host(),pullsSection=host(),agentsSection=host(),agentTab=host(),classes=new Set(),toggle={hidden:true};
  const ctx={disposed:false,run:{id:'parent'},agents:[],agentSignature:'',pullRequests:[],card,pullsSection,agentsSection,
    tabs:new Map([['agents',{kind:'agents',element:agentTab}]]),
    document:{activeElement:null,querySelector:()=>toggle},ico:()=>'',
    esc:value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),
    layout:{classList:{toggle:(key,value)=>value?classes.add(key):classes.delete(key)}}};
  vm.createContext(ctx);
  const app=fs.readFileSync('app/static/app.js','utf8');
  vm.runInContext(app.slice(app.indexOf('function sessionStatus('),app.indexOf('function sessionRepository(')),ctx);
  ctx.statusFor=ctx.sessionStatus;
  vm.runInContext(source.slice(source.indexOf('    function syncToolboxVisibility('),source.indexOf('    function renderPulls(')),ctx);
  return {ctx,card,pullsSection,agentsSection,agentTab,classes,toggle};
}
const childId='a'.repeat(32);
function team(children){return {id:'parent',agents:{groups:[{status:'completed',children}]}};}
test('agents alone show in the toolbox and tab with current status, safe links and escaped labels',()=>{
  const f=agentToolbox();
  f.ctx.syncAgents(team([{id:childId,agent_label:'<Explore> "design"',status:'running',session_url:'https://evil.test'},
    {id:'javascript:bad',status:'running'}]));
  assert.equal(f.card.hidden,false);assert.equal(f.pullsSection.hidden,true);assert.equal(f.toggle.hidden,false);
  assert.ok(f.classes.has('has-session-tools'));
  assert.match(f.agentsSection.html,/#run=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/);
  assert.match(f.agentsSection.html,/&lt;Explore> &quot;design&quot;/);
  assert.match(f.agentsSection.html,/Working now/);assert.match(f.agentsSection.html,/0 of 1 ready/);
  assert.doesNotMatch(f.agentsSection.html,/evil.test|javascript:|<Explore>/);
  assert.equal(f.agentTab.html,f.agentsSection.html);
});
test('agent changes do not depend on PR changes and preserve unchanged rows',()=>{
  const f=agentToolbox();f.ctx.pullRequests=[{url}];
  for(const [status,label,ready] of [['queued','Queued',0],['running','Working now',0],['completed','Completed',1],['failed','Failed',0],['running','Working now',0],['idle','Ready',1],['waiting_credential','Needs access',0],['cancelled','Stopped',0],['new-status','Status unknown',0]]){
    const data=team([{id:childId,agent_label:'Scout',status}]);
    f.ctx.syncAgents(data);assert.match(f.agentsSection.html,new RegExp(label));
    assert.match(f.agentsSection.html,new RegExp(`${ready} of 1 ready`));
    const writes=f.agentsSection.writes;f.ctx.syncAgents(data);assert.equal(f.agentsSection.writes,writes);
    assert.equal(f.pullsSection.hidden,false);
  }
  f.ctx.syncAgents(team([]));assert.equal(f.agentsSection.hidden,true);assert.equal(f.card.hidden,false);
  f.ctx.pullRequests=[];f.ctx.syncToolboxVisibility();assert.equal(f.card.hidden,true);
  assert.match(f.agentTab.html,/Subagents assigned to this session/);
});
test('agent updates reject old sessions and disposal, deduplicate membership, and restore the agents tab',()=>{
  const f=agentToolbox(),child={id:childId,status:'completed'};
  const data=team([child]);data.agents.groups.push({children:[child]});f.ctx.syncAgents(data);
  assert.match(f.agentsSection.html,/1 of 1 ready/);
  const writes=f.agentsSection.writes;
  f.ctx.syncAgents({...team([]),id:'another-session'});f.ctx.syncAgents({id:'parent'});
  f.ctx.disposed=true;f.ctx.syncAgents(team([]));assert.equal(f.agentsSection.writes,writes);
  const saved=restore(JSON.stringify({visible:true,active:'agents',tabs:[{id:'agents',kind:'agents',title:'Subagents'}]}));
  assert.equal(saved.tabs[0].kind,'agents');assert.equal(saved.active,'agents');
});
test('terminal children still reconcile after the sidebar sees completion first',()=>{
  const script=fs.readFileSync('app/static/app.js','utf8'),polls=[],callbacks=[];
  const ctx={state:{authenticated:true,selected:'parent',chatRun:{agents:{groups:[{}]}},runs:[{id:'parent',children:[{status:'completed'}]}]},
    document:{hidden:false},setInterval:fn=>callbacks.push(fn),refreshChat:id=>{polls.push(id);return Promise.resolve();}};
  vm.createContext(ctx);
  const start=script.indexOf('setInterval(()=>{if(state.authenticated&&!document.hidden&&state.selected');
  vm.runInContext(script.slice(start,script.indexOf('\n',start)),ctx);
  callbacks[0]();assert.deepEqual(polls,['parent']);
  ctx.state.chatRun=null;ctx.state.runs[0].children=[];callbacks[0]();assert.equal(polls.length,1);
  ctx.document.hidden=true;ctx.state.runs[0].children=[{status:'running'}];callbacks[0]();assert.equal(polls.length,1);
});
