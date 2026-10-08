const assert=require('node:assert/strict');
const {test}=require('node:test');
const {reference,resolve,decorate,create}=require('../app/static/saved-files.js');
const file=path=>({path,workspace_path:path,name:path.split('/').at(-1),archive_path:'new-files/'+path,url:'/download/'+encodeURIComponent(path)});

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
      getAttribute(name){return this[name]??null;},setAttribute(name,value){this[name]=value;},removeAttribute(name){delete this[name];}};
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

test('missing file is explained and becomes clickable when saved later',()=>{
  const view=linkFixture('/workspace/demo.zip',false);
  decorate(view.container,[]);
  assert.equal(view.link.href,undefined);
  assert.equal(view.link.getAttribute('aria-disabled'),'true');
  assert.equal(view.link.classList.contains('saved-file-unavailable'),true);
  assert.match(view.link.title,/not in the saved archive/);
  const demo=file('demo.zip');
  decorate(view.container,[demo]);
  assert.equal(view.link.href,demo.url);
  assert.equal(view.link.getAttribute('aria-disabled'),null);
  assert.equal(view.link.classList.contains('saved-file-unavailable'),false);
});
