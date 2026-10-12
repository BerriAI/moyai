const assert=require('node:assert/strict');
require('./helpers/ui-vm.cjs');
const {test}=require('node:test');
const {reference,resolve,decorate,preview,reveal,loadCatalog,create}=require('../app/static/saved-files.js');
const file=path=>({path,workspace_path:path,name:path.split('/').at(-1),archive_path:'new-files/'+path,url:'/download/'+encodeURIComponent(path)});

test('captures arrive before a held archive and opening one never waits for that archive',async()=>{
  const capture={...file('moyai-captures/demo.webm'),archive_path:'capture:demo.webm'};
  const media={files:[capture],archive_pending:true},updates=[],requests=[];
  let finish;const archive=new Promise(resolve=>{finish=resolve;});
  const api=(url,options)=>{requests.push({url,options});return url.endsWith('?view=media')?Promise.resolve(media):archive;};
  const pending=loadCatalog(api,'session',{onUpdate:value=>updates.push(value)});
  await new Promise(setImmediate);
  assert.deepEqual(updates,[media]);
  const opened=await loadCatalog(api,'session',{filePath:capture.archive_path});
  assert.equal(opened.files[0],capture);
  assert.equal(requests.filter(r=>!r.url.endsWith('?view=media')).length,1);
  assert(requests.every(r=>r.options.recent&&r.options.timeoutMs===30000));
  finish({files:[capture,file('report.md')]});
  assert.equal((await pending).files.length,2);
  assert.equal(updates.length,2);
});

test('an archive timeout retains captures with a retryable state, but access errors propagate',async()=>{
  const media={files:[file('demo.webm')],archive_pending:true};
  let error=Object.assign(Error('timed out'),{name:'TimeoutError'});
  const api=async url=>{if(url.endsWith('?view=media'))return media;throw error;};
  const result=await loadCatalog(api,'session');
  assert.equal(result.files,media.files);assert.equal(result.archive_pending,false);
  assert.equal(result.archive_error,true);assert.match(result.error,/Retry/);
  for(const status of [401,403,404,409]){
    error=Object.assign(Error('unavailable'),{status});
    await assert.rejects(loadCatalog(api,'session'),err=>err.status===status);
  }
});

test('chat hydration and Files share both in-flight catalog requests; explicit refresh fetches again',async()=>{
  const vm=require('./helpers/ui-vm.cjs'),{readFileSync}=require('node:fs');
  const context={structuredClone};vm.createContext(context);
  vm.runInContext(readFileSync('app/static/navigation-cache.js','utf8'),context);
  const cache=context.MoyaiNavigationCache.create(),calls=[];
  let finish;const full=new Promise(resolve=>{finish=resolve;});
  const api=(url,options)=>cache.read(url,()=>{calls.push(url);return url.endsWith('?view=media')?Promise.resolve({files:[],archive_pending:true}):full;},options);
  const id='a'.repeat(32),chat=loadCatalog(api,id),panel=loadCatalog(api,id);
  await new Promise(setImmediate);assert.equal(calls.length,2);
  finish({files:[file('report.md')]});await Promise.all([chat,panel]);
  await loadCatalog(api,id);assert.equal(calls.length,2);
  await loadCatalog(api,id,{refresh:true});assert.equal(calls.length,4);
});

test('a changed checkpoint bypasses a recently cached archive index',async()=>{
  const requests=[];
  const api=async(url,options)=>{
    requests.push(options);
    if(url.endsWith('?view=media'))return {files:[],archive_pending:true,archive_revision:'new'};
    return options.recent?{files:[file('old.md')],revision:'old'}:{files:[file('new.md')],revision:'new'};
  };
  const result=await loadCatalog(api,'session');
  assert.equal(result.files[0].path,'new.md');assert.equal(requests.length,3);
  assert.equal(requests[2].recent,undefined);
});

test('a cached full catalog cannot hide a newly published capture',async()=>{
  const capture={...file('moyai-captures/new.png'),archive_path:'capture:new.png'},requests=[];
  const api=async(url,options)=>{
    requests.push(url);
    if(url.endsWith('?view=media'))return {files:[capture],archive_pending:true,archive_revision:'same'};
    return {files:options.recent?[file('report.md')]:[capture,file('report.md')],revision:'same'};
  };
  const updates=[],result=await loadCatalog(api,'session',{onUpdate:value=>updates.push(value)});
  assert.equal(requests.length,3);assert.equal(result.files[0],capture);
  assert(updates.every(value=>value.files.includes(capture)));
});

test('local file references resolve only to the exact saved workspace file',()=>{
  const root=file('design.md'),nested=file('repo/design.md'),files=[root,nested,file('repo/other.txt')];
  for(const value of ['design.md','./design.md','/workspace/design.md','sandbox:/workspace/design.md'])assert.equal(resolve(value,files),root);
  assert.equal(resolve('/workspace/repo/design.md',files),nested);
  assert.equal(resolve('other.txt',files),files[2]);
  assert.equal(resolve('missing/design.md',files),null);
  assert.equal(resolve('DESIGN.md',files),null);
  assert.equal(resolve('design.md',[nested,file('second/design.md')]),null);
  assert.equal(resolve('result.md',[{name:'result.md',path:'result.md',workspace_path:null}]),null);
  assert.equal(resolve('space%20here.md',[file('space here.md')]).name,'space here.md');
});

test('references cannot open arbitrary URLs, paths, or archive traversal',()=>{
  for(const value of ['https://evil.test/design.md','javascript:alert(1)','data:text/html,hi','file:///etc/passwd',
    '//evil.test/design.md','/etc/passwd','../design.md','repo/../design.md','./../design.md','a\\b',
    '/api/runs/other/artifact','design.md?key=x','design.md#x','%2e%2e/design.md','%2f%2fevil.test/x',
    'bad%zz','evil\nfile.md','a//b','a/./b'])assert.equal(reference(value),null,value);
});

test('source locations preserve canonical file identity and reject unsafe or ambiguous references',()=>{
  const source=file('app/spend.js'),files=[source,file('other/spend.js')];
  for(const [value,path,line,endLine] of [
    ['spend.js','spend.js',null,null],['app/spend.js:38','app/spend.js',38,38],
    ['./app/spend.js:38:4','app/spend.js',38,38],['/workspace/app/spend.js#L38','app/spend.js',38,38],
    ['sandbox:/workspace/app/spend.js#L38-L41','app/spend.js',38,41],
    ['space%20here.js%3A3','space here.js',3,3],['app/spend.js%23L3-L4','app/spend.js',3,4],
  ])assert.deepEqual(reference(value),{path,line,endLine},value);
  for(const suffix of [':38',':38:4','#L38','#L38-L41']){
    assert.equal(resolve('/workspace/app/spend.js'+suffix,files),source);
    assert.equal(resolve('spend.js'+suffix,files),null,'Ambiguous basenames must not guess');
    assert.equal(resolve('missing/spend.js'+suffix,files),null);
    assert.equal(resolve('spend.js'+suffix,[source]),source);
  }
  for(const suffix of [':0',':01',':-1',':1.5',':2:0',':2:',':2:3:4',':9007199254740992',':2:9007199254740992',
    '#L0','#L-1','#L2-L1','#L1-L0','#L2-L3x','#L2-3','#L2:4','#L1-L9007199254740992',':2#L3','?line=2']){
    assert.equal(reference('app/spend.js'+suffix),null,suffix);
  }
  for(const value of ['https://evil.test/x.js:2','javascript:alert(1):2','../x.js:2','/etc/passwd:2',
    'a%2f..%2fx.js%23L2','a\\x.js:2','x.js%00:2'])assert.equal(reference(value),null,value);
  assert.deepEqual(reference('x.js%253A2:3'),{path:'x.js%3A2',line:3,endLine:3},'Decode only once');
});

const escapeHTML=value=>String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const renderPreview=(result,ref)=>preview(result,ref,{escape:escapeHTML,markdown:text=>'<p>'+escapeHTML(text)+'</p>'});
test('preview shares plain rendering but highlights only validated, available source lines',()=>{
  const source={text:'one\n<script>two</script>\nthree\nfour\n',format:'text',truncated:false};
  assert.match(renderPreview(source,null),/<pre[^>]*>one\n&lt;script&gt;two&lt;\/script&gt;\nthree\nfour\n<\/pre>/);
  assert.doesNotMatch(renderPreview(source,null),/data-source-line/);
  for(const ref of ['spend.js:2','spend.js#L2-L3']){
    const html=renderPreview(source,ref);assert.match(html,/<mark[^>]*data-source-line="2"[^>]*>/);
    assert.match(html,/&lt;script&gt;two&lt;\/script&gt;/);assert.doesNotMatch(html,/<script>/);
    assert.equal((html.match(/<mark\b/g)||[]).length,1,'One bounded selection for the full range');
    assert.match(html,ref.includes('-')?/two&lt;\/script&gt;\nthree[^<]*<\/mark>/:/two&lt;\/script&gt;[^<]*<\/mark>/);
  }
  assert.match(renderPreview({...source,format:'markdown'},null),/<div class="markdown"><p>/);
  assert.doesNotMatch(renderPreview({...source,format:'markdown'},'note.md:2'),/<div class="markdown">/);
  for(const [result,ref] of [[source,'spend.js:8'],[source,'spend.js#L2-L8'],
    [{...source,text:'one\npartial',truncated:true},'spend.js:2']]){
    const html=renderPreview(result,ref);assert.doesNotMatch(html,/data-source-line/);assert.match(html,/saved-file-notice/);
  }
  assert.match(renderPreview({...source,text:'one\ntwo\n',truncated:true},'spend.js:2'),/data-source-line="2"/);
  assert.match(renderPreview({...source,text:null},'spend.js:2'),/Download/);
  const calls=[],selection={scrollIntoView:()=>calls.push('scroll'),focus:()=>calls.push('focus')};
  reveal({querySelector:selector=>selector==='[data-source-line]'?selection:null});
  assert.deepEqual(calls.sort(),['focus','scroll']);
  reveal({querySelector:()=>null});
});

test('archive requests are deduplicated and stale responses cannot change another session',async()=>{
  const requests=[],pending=[];
  const button={};const area={innerHTML:'',querySelector:()=>null};
  global.document={addEventListener(){},querySelector:selector=>selector==='#files-button'?button:selector==='#artifact-area'?area:null};
  const view=create({api:url=>{requests.push(url);return new Promise(resolve=>pending.push(resolve));},markdown:s=>s,escape:s=>s,size:s=>String(s)});
  const first={id:'first',has_artifact:true,events:[{id:1,kind:'artifact'}]};
  view.sync(first);view.sync(first);assert.equal(requests.length,1);
  view.reset();view.sync({...first,id:'second'});assert.equal(requests.length,2);
  pending[1]({files:[file('second.md')],revision:'two'});await new Promise(setImmediate);
  pending[0]({files:[file('wrong.md'),file('wrong2.md')],revision:'one'});await new Promise(setImmediate);
  assert.equal(button.textContent,'Files · 1');assert.match(area.innerHTML,/runs\/second\/artifact/);
  view.sync({...first,id:'second',events:[{id:2,kind:'artifact'}]});assert.equal(requests.length,3);
  pending[2]({files:[],revision:'three'});await new Promise(setImmediate);
  assert.equal(button.textContent,'Files · 0');
  delete global.document;
});

// Small DOM seam for reference binding; real Markdown/sanitizer and panel
// navigation are exercised against the local app in browser verification.
function linkFixture(ref,image=true){
  const doc={createElement:tag=>{
    const classes=new Set();let text='';
    return {tagName:tag.toUpperCase(),ownerDocument:doc,dataset:{},children:[],
      get textContent(){return text;},set textContent(value){text=value;this.children=[];},
      classList:{add(...names){names.forEach(n=>classes.add(n));},remove(...names){names.forEach(n=>classes.delete(n));},contains:n=>classes.has(n)},
      closest:()=>null,matches:selector=>selector==='[data-file-ref]',
      append(node){this.children.push(node);},replaceChildren(...nodes){this.children=nodes;},
      querySelector(selector){return this.children.find(n=>n.tagName===selector.toUpperCase())||null;},
      getAttribute(name){return this[name]??null;},setAttribute(name,value){this[name]=value;},removeAttribute(name){delete this[name];},
      replaceWith(node){this.replacement=node;}};
  }};
  const link=doc.createElement('a');link.dataset.fileRef=ref;if(image)link.dataset.fileImage='true';link.textContent='Lens landing page';
  return {link,container:{querySelectorAll:()=>[link]}};
}

test('saved image gets a catalog thumbnail and opens the same file in the panel',()=>{
  const image={...file('shots/lens.png'),kind:'image',inline_url:'/api/runs/one/files/content?revision=1&inline=true'};
  const view=linkFixture('/workspace/shots/lens.png'),opened=[];
  decorate(view.container,[image],{onOpen:f=>opened.push(f)});
  const thumbnail=view.link.querySelector('img');
  assert.equal(thumbnail.src,image.inline_url);assert.equal(thumbnail.alt,'Lens landing page');assert.equal(thumbnail.loading,'lazy');
  assert.equal(view.link.href,image.url);assert.equal(view.link.classList.contains('saved-image-link'),true);
  let prevented=false;view.link.onclick({preventDefault(){prevented=true;}});
  assert.equal(prevented,true);assert.deepEqual(opened,[image]);
  view.link.onclick({ctrlKey:true,preventDefault(){assert.fail('Modified click keeps browser behavior');}});
  assert.equal(opened.length,1);
  decorate(view.container,[image],{runId:'one'});assert.equal(view.link.querySelector('img'),thumbnail,'Polling must not reload the same image');
  assert.equal(view.link.dataset.fileRun,'one');
  const revised={...image,inline_url:image.inline_url.replace('revision=1','revision=2')};
  decorate(view.container,[revised],{runId:'one'});assert.equal(view.link.querySelector('img').src,revised.inline_url);
  decorate(view.container,[],{runId:'one'});
  assert.equal(view.link.href,undefined);assert.equal(view.link.querySelector('img'),null);assert.equal(view.link.dataset.savedFile,undefined);
  assert.equal(view.link.textContent,'Lens landing page');
});

test('image hydration waits for exact catalog matches and preserves ordinary links',()=>{
  const image={...file('shots/lens.png'),kind:'image',inline_url:'/saved-image'};
  for(const ref of ['https://outside.test/lens.png','/workspace/missing.png','lens.png']){
    const view=linkFixture(ref);
    decorate(view.container,[image,{...image,...file('second/lens.png')}]);
    assert.equal(view.link.querySelector('img'),null);assert.equal(view.link.href,undefined);
  }
  const pending=linkFixture('/workspace/shots/lens.png');
  decorate(pending.container,[]);assert.equal(pending.link.textContent,'Lens landing page');
  decorate(pending.container,[image]);assert.equal(pending.link.querySelector('img').src,image.inline_url);
  const plain=linkFixture('/workspace/shots/lens.png',false);decorate(plain.container,[image]);
  assert.equal(plain.link.href,image.url);assert.equal(plain.link.querySelector('img'),null);
  const broken=pending.link.querySelector('img');broken.onerror();decorate(pending.container,[image]);
  assert.equal(pending.link.querySelector('img'),broken);assert.equal(broken.hidden,true);assert.equal(pending.link.href,image.url);
});

test('source links retain their requested line when saved files arrive or disappear',()=>{
  const source=file('app/spend.js'),view=linkFixture('/workspace/app/spend.js:38',false),opened=[];
  decorate(view.container,[],{runId:'one'});assert.equal(view.link.href,undefined);assert.equal(view.link['aria-disabled'],'true');
  decorate(view.container,[source],{runId:'one',onOpen:(...args)=>opened.push(args)});
  assert.equal(view.link.href,source.url);assert.equal(view.link.dataset.fileRun,'one');
  assert.equal(view.link['aria-disabled'],undefined);
  view.link.onclick({preventDefault(){}});assert.deepEqual(opened,[[source,'/workspace/app/spend.js:38']]);
  decorate(view.container,[],{runId:'one'});
  assert.equal(view.link.href,undefined);assert.equal(view.link.onclick,null);
  assert.equal(view.link.classList.contains('saved-file-link'),false);
  assert.equal(view.link['aria-disabled'],'true');
});

test('a parent file catalog cannot decorate another agent’s message references',()=>{
  const view=linkFixture('design.md',false);
  view.link.closest=selector=>selector==='[data-message-run]'?{dataset:{messageRun:'child'}}:null;
  decorate(view.container,[file('design.md')],{runId:'parent'});
  assert.equal(view.link.href,undefined);
  decorate(view.container,[file('design.md')],{runId:'child'});
  assert.equal(view.link.href,'/download/design.md');
});
