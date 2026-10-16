const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const compiled = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../native/web/live-transcript.ts'), 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022},
}).outputText;
const loaded = import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);

test('only root-marked rounds enter the transcript; retries replace their round and preserve earlier commentary', async () => {
  const {emptyLiveTranscript, applyLiveEvent: apply, liveCommentary: text} = await loaded;
  let state = emptyLiveTranscript('root');
  state = apply(state, {type:'text_start',index:0,text:'unlabelled child'}, 0);
  assert.equal(text(state,false), '');
  state = apply(state, {type:'response_start',run_id:'root',step:1}, 1);
  state = apply(state, {type:'text_start',index:0,text:'I will inspect.'}, 2);
  state = apply(state, {type:'response_start',run_id:'root',step:2}, 3);
  state = apply(state, {type:'text_start',index:0,text:'Failed attempt'}, 4);
  state = apply(state, {type:'text_start',index:1,text:'stale second part'}, 5);
  state = apply(state, {type:'response_start',run_id:'root',step:2}, 6);
  state = apply(state, {type:'text_start',index:0,text:'Final'}, 7);
  state = apply(state, {type:'text_delta',index:0,text:' answer'}, 8);
  assert.equal(text(state,false), 'I will inspect.\n\nFinal answer');
  assert.equal(text(state,true), 'I will inspect.', 'saved final assistant replaces only the last model round');
  state = apply(state, {type:'response_start',run_id:'child',step:3}, 9);
  state = apply(state, {type:'text_delta',index:0,text:'child secret'}, 10);
  assert.equal(text(state,false), 'I will inspect.\n\nFinal answer');
});

test('SSE offsets deduplicate reconnect replay without losing replacement semantics', async () => {
  const {emptyLiveTranscript, applyLiveEvent: apply, liveCommentary: text} = await loaded;
  let state = apply(emptyLiveTranscript('root'), {type:'response_start',run_id:'root',step:1}, 0);
  state = apply(state, {type:'text_start',index:0,text:'Hello'}, 1);
  state = apply(state, {type:'text_delta',index:0,text:' world'}, 2);
  assert.equal(apply(state, {type:'text_delta',index:0,text:' world'}, 2), state);
  assert.equal(apply(state, {type:'text_start',index:0,text:'Hello'}, 1), state);
  assert.equal(apply(state, {type:'text_delta',index:0,text:'bad'}, NaN), state);
  state = apply(state, {type:'text_start',index:0,text:'Replacement'}, 3);
  assert.equal(text(state,false), 'Replacement');
  assert.equal(apply({...state,finished:true}, {type:'text_delta',index:0,text:'late'}, 4).rounds, state.rounds);
});

test('tool details merge by call and event type, while the compact action stays separate', async () => {
  const {emptyLiveTranscript, applyLiveEvent: apply, mergeActivity} = await loaded;
  const call = {type:'tool_call',id:'call',name:'read_file',arguments:{path:'src/app.ts'}};
  const result = {type:'tool_result',id:'call',name:'read_file',content:'contents'};
  const state = apply(emptyLiveTranscript('root'), call, 0);
  assert.equal(state.action, 'Using read file…');
  assert.deepEqual(mergeActivity([call,result],state.activity),[call,result]);
});
