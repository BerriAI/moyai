const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const {prUrl,restore}=require('../app/static/workspace-panel.js');
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
test('only session PR links in the owning chat use the side browser; modified and embedded links stay native',()=>{
  const source=fs.readFileSync('app/static/workspace-panel.js','utf8'),opened=[];
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
  const source=fs.readFileSync('app/static/workspace-panel.js','utf8');
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
