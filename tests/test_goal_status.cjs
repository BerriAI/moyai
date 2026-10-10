const {test}=require('node:test');
const assert=require('node:assert/strict');
const goal=require('../app/static/goal-status.js');
test('draft selection is not confirmation and commands are anchored',()=>{
  assert.match(goal.draft('/goal '),/Add an objective/);
  assert.match(goal.draft('/goal ship the fix'),/Not running yet/);
  assert.equal(goal.draft('please /goal fix'),null);
  assert.equal(goal.draft('/goals fix'),null);
  assert.equal(goal.current({messages:[{content:'/goal fix',status:'queued'}]}),null);
});
test('goal timer survives pauses and resumes without counting paused time',()=>{
  let run={status:'running',goal:{status:'active',active_since:100,elapsed_seconds:15}};
  assert.equal(goal.elapsed(goal.current(run),run,110000),25);
  run.goal={status:'paused',active_since:null,elapsed_seconds:25};
  assert.equal(goal.elapsed(goal.current(run),run,150000),25);
  run.goal={status:'active',active_since:150,elapsed_seconds:25};
  assert.equal(goal.elapsed(goal.current(run),run,160000),35);
  run.status='interrupted';run.updated_at=new Date(155000).toISOString();
  assert.equal(goal.current(run).status,'interrupted');
  assert.equal(goal.elapsed(goal.current(run),run,200000),30);
});
test('waiting and verified completion are distinct from running',()=>{
  assert.equal(goal.current({status:'waiting_credential',goal:{status:'active'}}).status,'waiting');
  assert.equal(goal.current({status:'idle',goal:{status:'completed'}}).status,'completed');
});

test('budget exhaustion and reporting never imply completion',()=>{
  for(const status of ['wrapping','budget_exhausted','needs_input','interrupted']) assert.equal(goal.current({status:'idle',goal:{status}}).status,status);
  assert.equal(goal.elapsed({elapsed_seconds:10800},{status:'idle'},999999999),10800);
});
