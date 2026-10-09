const assert = require('node:assert/strict');
const {test} = require('node:test');
const {readFileSync} = require('node:fs');
const vm = require('./helpers/ui-vm.cjs');

function fixture(api) {
  const nodes=new Map();
  const context={
    api, state:{pageVersion:1,view:'memory'},
    $:key=>{if(!nodes.has(key))nodes.set(key,{value:'',innerHTML:'',disabled:false});return nodes.get(key);},
    document:{querySelectorAll:()=>[]},showError:()=>{},
    relative:()=> 'Just now',
    esc:value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),
  };
  vm.createContext(context);
  vm.runInContext(readFileSync('app/static/memory.js','utf8'),context);
  return {context,nodes};
}

test('private notes render as text even when a saved note contains markup',()=>{
  const {context}=fixture();
  const html=context.memoryCard({id:'abc',kind:'feedback',title:'<img src=x onerror="bad()">',
    content:'<script>bad()</script>',repo_url:'',updated_at:'2026-10-02',source:{type:'manual'}});
  assert.doesNotMatch(html,/<img|<script/);
  assert.match(html,/&lt;img/);
  assert.match(html,/Review & edit/);
});

test('observations show their source and escaped supporting evidence',()=>{
  const {context,nodes}=fixture();
  const note={id:'observed',kind:'project',title:'Harness gotcha',content:'Scoped context',repo_url:'',updated_at:'2026-10-08',
    source:{type:'observation',run_id:'session',scope:'Harness <img src=x>',evidence:'Initialization failed with <script>bad()</script>'}};
  assert.match(context.memoryCard(note),/Observed during work/);
  assert.doesNotMatch(context.memoryCard(note),/Added by you/);
  context.crypto={randomUUID:()=> 'request-id'};
  nodes.set('#memory-dialog',{innerHTML:'',querySelector:()=> ({}),showModal:()=>{},close:()=>{}});
  nodes.set('#memory-title',{focus:()=>{}});
  context.openMemoryEditor(note);
  const html=nodes.get('#memory-dialog').innerHTML;
  assert.match(html,/Harness &lt;img/);
  assert.match(html,/Initialization failed with &lt;script/);
  assert.doesNotMatch(html,/<img|<script/);
  assert.match(html,/#run=session/);
});

test('late personal-memory response never replaces a different page',async()=>{
  const f=fixture(async()=>{f.context.state.pageVersion++;f.context.$('#content').innerHTML='Different page';return {memories:[],preferences:{}};});
  await f.context.renderMemory();
  assert.equal(f.nodes.get('#content').innerHTML,'Different page');
});

test('changing capture mode preserves pause state and sends the current revision',async()=>{
  const calls=[];
  const prefs={enabled:true,auto_save:true,revision:7};
  const f=fixture(async(url,options)=>{if(options?.method)calls.push({url,body:JSON.parse(options.body)});return {memories:[],preferences:prefs,limit:200};});
  await f.context.renderMemory();
  await f.nodes.get('#memory-learning').onchange({target:{value:'manual'}});
  assert.deepEqual(calls,[{url:'/api/memory/preferences',body:{enabled:true,auto_save:false,revision:7}}]);
});

test('failed private-library load is escaped and never displays an earlier library',async()=>{
  const f=fixture(async()=>{throw new Error('<img src=x>');});
  f.context.$('#content').innerHTML='Earlier user memory';
  await f.context.renderMemory();
  assert.doesNotMatch(f.nodes.get('#content').innerHTML,/Earlier user memory|<img/);
  assert.match(f.nodes.get('#content').innerHTML,/&lt;img/);
});

test('review status distinguishes pending, empty results, failures and manual mode',()=>{
  const {context}=fixture();
  const prefs={enabled:true,auto_save:true};
  const review={enabled:true,configured:true,pending:2};
  assert.match(context.memoryReviewSummary(review,prefs),/2 finished turns are waiting/);
  assert.match(context.memoryReviewSummary({...review,pending:0,latest:{status:'completed',saved_count:0}},prefs),/no new lasting context/);
  assert.match(context.memoryReviewSummary({...review,latest:{status:'completed',saved_count:1}},prefs),/saved 1 memory\./);
  assert.match(context.memoryReviewSummary({...review,latest:{status:'failed'}},prefs),/could not finish/);
  assert.equal(context.memoryReviewSummary(review,{...prefs,auto_save:false}),'');
});
