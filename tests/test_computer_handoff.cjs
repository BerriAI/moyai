const {test}=require('node:test');
const assert=require('node:assert/strict');
const {handoffFor,restore}=require('../app/static/workspace-panel.js');
const event={id:20,run_id:'one',kind:'computer',data:{phase:'handoff',version:1,turn_id:10,user_id:'alice',reason:'Sign in.'}};
const run={id:'one',mode:'modal',status:'idle',messages:[{id:10,role:'user',status:'completed'}],events:[event]};

test('latest handoff reaches its requester once, including after a completed turn',()=>{
  assert.equal(handoffFor(run,'alice'),event);
  assert.equal(handoffFor(run,'alice',20),null);
  assert.equal(handoffFor(run,'bob'),null);
  assert.equal(handoffFor({...run,id:'two'},'alice'),null);
  assert.equal(handoffFor({...run,messages:[...run.messages,{id:11,role:'user'}]},'alice'),null);
  assert.equal(restore(JSON.stringify({tabs:[],handoff:20})).handoff,20);
});

test('old, malformed and stopped handoffs cannot open a computer',()=>{
  for(const status of ['cancelled','failed','stopping','deleting'])assert.equal(handoffFor({...run,status},'alice'),null);
  assert.equal(handoffFor({...run,events:[{...event,data:{...event.data,version:2}}]},'alice'),null);
  assert.equal(handoffFor({...run,mode:'demo'},'alice'),null);
  assert.equal(handoffFor({...run,messages:[]},'alice'),null);
});
