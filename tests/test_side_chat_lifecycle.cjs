const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const {test}=require('node:test');
const vm=require('node:vm');

function browser(chatId='side'){
  const script=readFileSync('app/static/workspace-panel.js','utf8');
  const nodes=new Map(),requests=[],timers=new Map(),listeners=new Map();let timerId=0,disposed=false;
  const node=selector=>{if(!nodes.has(selector))nodes.set(selector,{value:'',disabled:false,hidden:false,textContent:'',innerHTML:'',scrollHeight:0,scrollTop:0,clientHeight:0,querySelector:node,querySelectorAll:()=>[],focus(){}});return nodes.get(selector);};
  const tab={id:'chat:side',chatId,uid:'side',draft:'Keep my unsent draft',element:node('tab')};
  const context={models:[],run:{id:'parent',model:'model-a'},esc:x=>x,markdown:x=>x,good:t=>!disposed&&t===tab,save(){},draw(){},syncTitles(){},sideChats:[],crypto:{randomUUID:()=> 'submission-id'},
    api:(path,options)=>new Promise((resolve,reject)=>requests.push({path,options,resolve,reject})),
    MoyaiQueue:{presentation:data=>({transcript:data.messages})},MoyaiActivity:{sync(){},tick(){},current:()=>({headline:'Working'})},
    document:{hidden:false,addEventListener:(name,fn)=>listeners.set(name,fn),removeEventListener:name=>listeners.delete(name)},
    setTimeout:fn=>{timers.set(++timerId,fn);return timerId;},clearTimeout:id=>timers.delete(id),
    navigate(){throw Error('Side chat must not navigate the parent');},stopStream(){throw Error('Side chat must not stop the parent');},
  };
  vm.createContext(context);
  vm.runInContext(script.slice(script.indexOf('function mountChat(t)'),script.indexOf('const {titleFor=')),context);
  context.mountChat(tab);
  node('[role="log"]').innerHTML='<article>Previously visible conversation</article>';
  return {tab,requests,timers,listeners,node,context,
    submit:()=>node('form').onsubmit({preventDefault(){}}),cancel:()=>node('[data-stop]').onclick(),
    retry(){const [id,fn]=timers.entries().next().value;timers.delete(id);return fn();},
    dispose(){disposed=true;tab.dispose();},
  };
}
const failure=status=>Object.assign(Error('Session not found.'),{status});
const flush=async()=>{await Promise.resolve();await Promise.resolve();};
const ready={id:'side',status:'idle',messages:[],active:false};
function assertUnavailable(b){
  assert.match(b.node('[role="log"]').innerHTML,/This side chat is no longer available\./);
  assert.doesNotMatch(b.node('[role="log"]').innerHTML,/Previously visible conversation/);
  for(const selector of ['textarea','select','[type="submit"]','[data-stop]'])assert.equal(b.node(selector).disabled,true,selector);
  assert.equal(b.node('[data-stop]').hidden,true);
  assert.equal(b.node('[data-full]').hidden,true);
  assert.equal(b.timers.size,0);
  assert.equal(b.tab.draft,'Keep my unsent draft');
  assert.equal(b.node('textarea').value,b.tab.draft);
}

for(const action of ['poll','send','cancel','create'])test(`${action} 404 disables only the side chat and returning to it cannot restart polling`,async()=>{
  const b=browser(action==='create'?null:'side');
  const pending=action==='poll'?b.tab.activate():action==='cancel'?b.cancel():b.submit();
  assert.equal(b.requests.length,1);
  b.requests[0].reject(failure(404));await pending;await flush();
  assertUnavailable(b);
  b.tab.deactivate();b.tab.activate();b.listeners.get('visibilitychange')();await flush();
  assert.equal(b.requests.length,1);
  assertUnavailable(b);
});

test('a send completing after a missing-session poll preserves the draft and disabled controls',async()=>{
  const b=browser();b.tab.activate();const sent=b.submit();
  assert.equal(b.requests.length,2);
  b.requests[0].reject(failure(404));await flush();
  assertUnavailable(b);
  b.requests[1].resolve({});await sent;
  assertUnavailable(b);
});

test('a late successful poll cannot revive a side chat whose send returned 404',async()=>{
  const b=browser();b.tab.activate();const sent=b.submit();
  b.requests[1].reject(failure(404));await sent;
  b.requests[0].resolve(ready);await flush();
  assertUnavailable(b);
});

test('temporary poll and send errors preserve drafts and remain retryable',async()=>{
  const b=browser();b.tab.activate();b.requests[0].reject(failure(503));await flush();
  assert.equal(b.timers.size,1);assert.equal(b.node('textarea').disabled,false);
  const retry=b.retry();b.requests[1].resolve(ready);await retry;
  assert.equal(b.node('[data-status]').textContent,'');assert.equal(b.timers.size,1);
  const sent=b.submit();b.requests[2].reject(failure(503));await sent;
  assert.equal(b.node('[type="submit"]').disabled,false);assert.equal(b.node('textarea').disabled,false);
  assert.equal(b.tab.draft,'Keep my unsent draft');assert.equal(b.node('textarea').value,b.tab.draft);
});

test('a disposed tab ignores a late missing-session response',async()=>{
  const b=browser();b.tab.activate();b.dispose();b.requests[0].reject(failure(404));await flush();
  assert.equal(b.node('[data-status]').textContent,'');assert.equal(b.timers.size,0);assert.equal(b.listeners.size,0);
});
