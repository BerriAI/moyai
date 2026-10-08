const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const panelPath=process.env.PANEL_SOURCE||'app/static/workspace-panel.js';
const {prUrl,restore}=require(require('node:path').resolve(panelPath));
const url='https://github.com/BerriAI/moyai/pull/145';
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
test('only session PR links in the owning chat use the side browser; modified and embedded links stay native',()=>{
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
  const events=[],pending=new Map(),tabs=new Map();
  for(const id of ['pr:a','pr:b'])tabs.set(id,{id,kind:'pr',url:id==='pr:a'?url:url.replace('145','135'),loaded:true,
    element:{hidden:id!==active,remove(){events.push('remove:'+id);}},
    activate(){events.push('activate:'+id);},deactivate(){events.push('deactivate:'+id);}});
  const closingStatus={hidden:true};
  const ctx={tabs,active,visible:true,disposed:false,closingStatus,run:{id:'session',mode},
    computer:{closeTab(id,tab){events.push('close:'+tab);return new Promise((resolve,reject)=>pending.set(tab,{resolve,reject}));}},
    draw(){},save(){},toast:message=>events.push(message),q:()=>({scrollIntoView(){}}),
    document:{querySelector:()=>null},setVisible(value){ctx.visible=value;},
    pullRequests:[{url,title:'Review'}],make:()=>tabs.get('pr:a')};
  const source=fs.readFileSync(panelPath,'utf8');
  vm.createContext(ctx);
  vm.runInContext(source.slice(source.indexOf('    function select('),source.indexOf('    function error(')),ctx);
  vm.runInContext(source.slice(source.indexOf('    function hide('),source.indexOf('    function menu(')),ctx);
  vm.runInContext(source.slice(source.indexOf('    function openPullRequest('),source.indexOf('    function syncPullRequests(')),ctx);
  return {ctx,tabs,events,closingStatus,settle(id,reject=false){const p=pending.get(id==='pr:a'?url:url.replace('145','135'));reject?p.reject(new Error('Stop recording, then retry.')):p.resolve();}};
}
test('close waits for acknowledgment and keeps rejected recording controls reachable',async()=>{
  const f=closingPanel(),a=f.tabs.get('pr:a'),closing=f.ctx.remove('pr:a');
  await f.ctx.remove('pr:a');
  assert.equal(f.events.filter(e=>e.startsWith('close:')).length,1);
  assert.equal(f.tabs.size,2);assert.equal(f.ctx.active,'pr:a');assert.equal(a.element.hidden,false);
  f.settle('pr:a',true);await closing;
  assert.equal(f.tabs.get('pr:a'),a);assert.equal(a.closing,false);
  assert.equal(f.events.some(e=>e.startsWith('deactivate:')),false);
  const retry=f.ctx.remove('pr:a');f.settle('pr:a');await retry;
  assert.equal(f.tabs.has('pr:a'),false);assert.equal(f.ctx.active,'pr:b');
  assert.equal(f.events.filter(e=>e==='activate:pr:b').length,1);
});
test('every close order and rejection combination reconciles actual selection and pending status',async()=>{
  for(const order of [['pr:a','pr:b'],['pr:b','pr:a']])for(const failA of [false,true])for(const failB of [false,true]){
    const f=closingPanel(),jobs={'pr:a':f.ctx.remove('pr:a'),'pr:b':f.ctx.remove('pr:b')};
    const failed={'pr:a':failA,'pr:b':failB};
    for(const id of order){
      f.settle(id,failed[id]);await jobs[id];
      const selected=f.tabs.get(f.ctx.active);
      assert.ok(!f.ctx.visible||selected||!f.closingStatus.hidden,'A visible panel has a view or a pending-close status');
      if(selected)assert.equal(selected.element.hidden,false);
    }
    assert.equal(f.tabs.size,Number(failA)+Number(failB));
    assert.equal(f.ctx.visible,failA||failB);
    if(f.tabs.size){assert.ok(f.tabs.has(f.ctx.active));assert.equal(f.tabs.get(f.ctx.active).closing,false);}
    else assert.equal(f.ctx.active,'');
    assert.equal(f.closingStatus.hidden,true);
  }
});
test('close settlement preserves hidden intent and never steals unrelated selection',async()=>{
  for(const hidden of [false,true])for(const reject of [false,true]){
    const f=closingPanel();
    f.tabs.set('files',{id:'files',kind:'files',loaded:true,element:{hidden:true},activate(){f.events.push('activate:files');}});
    const closing=f.ctx.remove('pr:a');f.ctx.select('files');if(hidden)f.ctx.hide();
    f.settle('pr:a',reject);await closing;
    assert.equal(f.ctx.active,'files');assert.equal(f.ctx.visible,!hidden);
    assert.equal(f.events.filter(e=>e==='activate:files').length,1);
  }
  const f=closingPanel(),a=f.ctx.remove('pr:a'),b=f.ctx.remove('pr:b');
  f.settle('pr:a');await a;f.ctx.hide();f.settle('pr:b',true);await b;
  assert.equal(f.ctx.visible,false);assert.equal(f.ctx.active,'pr:b');
  assert.equal(f.events.includes('activate:pr:b'),false,'Failure may restore selection but cannot reopen a hidden panel');
  f.ctx.show();assert.equal(f.ctx.visible,true);assert.equal(f.events.includes('activate:pr:b'),true);
});
test('reopening the panel shows pending status without reactivating closing tabs',async()=>{
  const f=closingPanel(),a=f.ctx.remove('pr:a'),b=f.ctx.remove('pr:b');
  f.settle('pr:a');await a;f.ctx.hide();f.ctx.show();
  assert.equal(f.ctx.visible,true);assert.equal(f.closingStatus.hidden,false);
  assert.equal(f.events.includes('activate:pr:b'),false);
  f.settle('pr:b',true);await b;
  assert.equal(f.ctx.active,'pr:b');assert.equal(f.closingStatus.hidden,true);
  assert.equal(f.events.filter(e=>e==='activate:pr:b').length,1);
});
test('late close completion ignores disposed or replaced tab instances',async()=>{
  for(const replaced of [false,true]){
    const f=closingPanel(),closing=f.ctx.remove('pr:a');
    if(replaced)f.tabs.set('pr:a',{id:'pr:a'});else f.ctx.disposed=true;
    f.settle('pr:a');await closing;
    assert.equal(f.tabs.size,2);assert.equal(f.events.some(e=>e.startsWith('remove:')),false);
  }
});
test('pending closes reject every activation path while other tabs stay reachable',async()=>{
  for(const active of ['pr:a','pr:b'])for(const rejected of [false,true]){
    const f=closingPanel({active}),a=f.tabs.get('pr:a'),closing=f.ctx.remove('pr:a');
    assert.equal(f.ctx.openPullRequest(url),true,'The pending close consumes its PR link');
    f.ctx.select('pr:b');assert.equal(f.ctx.active,'pr:b');
    f.ctx.select('pr:a');f.ctx.openPullRequest(url);
    assert.equal(f.ctx.active,'pr:b');assert.equal(f.events.includes('activate:pr:a'),false);assert.equal(a.autoload,undefined);
    f.settle('pr:a',rejected);await closing;
    if(rejected){f.ctx.openPullRequest(url);assert.equal(f.ctx.active,'pr:a');assert.equal(f.events.includes('activate:pr:a'),true);}
    else{assert.equal(f.tabs.has('pr:a'),false);assert.equal(f.ctx.active,'pr:b');}
  }
});
test('non-cloud close never requests a browser resource and hidden close stays hidden',async()=>{
  const f=closingPanel({mode:'demo'});f.ctx.hide();await f.ctx.remove('pr:a');
  assert.equal(f.events.some(e=>e.startsWith('close:')),false);
  assert.equal(f.ctx.visible,false);assert.equal(f.ctx.active,'pr:b');
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
