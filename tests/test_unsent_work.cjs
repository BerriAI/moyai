const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const {readFileSync}=require('node:fs');
const source=readFileSync('app/static/app.js','utf8');
function harness(){
  const state={authenticated:true,newDraft:{},drafts:{},queueDrafts:{}};
  const attachmentDrafts=new Map();let handler;
  const context={state,attachmentDrafts,window:{addEventListener:(_,fn)=>handler=fn}};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('function hasUnsentWork(){'),source.indexOf('boot();',source.indexOf('function hasUnsentWork(){'))),context);
  return {state,attachmentDrafts,unload(){let prevented=false;const event={preventDefault(){prevented=true;}};handler(event);return prevented;}};
}
test('reload warns for new text, other-session replies, queue edits and unsent files',()=>{
  const h=harness();assert.equal(h.unload(),false);
  h.state.newDraft.prompt='unfinished';assert.equal(h.unload(),true);
  h.state.newDraft={};h.state.drafts.other='reply';assert.equal(h.unload(),true);
  h.state.drafts={};h.state.queueDrafts.other=new Map([[1,{content:'edit'}]]);assert.equal(h.unload(),true);
  h.state.queueDrafts={};h.attachmentDrafts.set('other',{items:[{status:'uploading'}]});assert.equal(h.unload(),true);
  h.attachmentDrafts.get('other').items=[];assert.equal(h.unload(),false);
});
test('completed sign-out never traps a user on an invalid session',()=>{
  const h=harness();h.state.newDraft.prompt='draft';h.state.authenticated=false;assert.equal(h.unload(),false);
});
