const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const {test}=require('node:test');
const vm=require('node:vm');
const flush=()=>new Promise(resolve=>setImmediate(resolve));

function fixture(){
  class Element {
    constructor(){this.listeners={};this.classList={add(){},remove(){}};this.innerHTML='';this.children=[];}
    setAttribute(){} addEventListener(name,fn){this.listeners[name]=fn;} removeEventListener(name){delete this.listeners[name];}
    prepend(el){this.children.unshift(el);} append(el){this.children.push(el);} focus(){} contains(){return true;}
    querySelector(){return this.toolbar ||= new Element();}
  }
  const form=new Element(),input=new Element(),requests=[],revoked=[];
  input.value='My unsent text';input.required=true;
  const ctx={console,AbortController,Map,Set,Array,URL:{createObjectURL:()=>'blob:test',revokeObjectURL:url=>revoked.push(url)},
    crypto:{randomUUID:()=>String(requests.length+1).padStart(32,'0')},
    document:Object.assign(new Element(),{createElement:()=>new Element()}),
    esc:value=>String(value).replaceAll('<','&lt;').replaceAll('"','&quot;'),toast:()=>{},
    api:(path,options)=>new Promise((resolve,reject)=>requests.push({path,options,resolve,reject})),
  };
  vm.createContext(ctx);vm.runInContext(readFileSync('app/static/attachments.js','utf8'),ctx);
  const controller=ctx.bindAttachments(input,form,'session-one');
  const file={name:'screenshot.png',type:'image/png',size:123};
  const paste=(files=[file])=>{const event={clipboardData:{files},preventDefault(){this.prevented=true;}};form.listeners.paste(event);return event;};
  return {ctx,form,input,requests,revoked,controller,file,paste};
}

test('pasted files show immediately, preserve text, and block send until upload completes',async()=>{
  const f=fixture();assert.equal(f.paste().prevented,true);
  assert.equal(f.input.value,'My unsent text');assert.equal(f.input.required,false);
  assert.match(f.form.children[0].innerHTML,/blob:test/);
  assert.throws(()=>f.controller.ids(),/Wait for uploads/);
  f.requests[0].resolve({id:'1'.padStart(32,'0'),name:f.file.name,size:123,preview_url:'/safe/preview'});await flush();
  assert.deepEqual(Array.from(f.controller.ids()),['1'.padStart(32,'0')]);
  assert.match(f.form.children[0].innerHTML,/123 B/);
  f.controller.clear(f.controller.ids());assert.equal(f.input.required,true);assert.equal(f.revoked.length,1);
});

test('ordinary text paste remains native; failed uploads and navigation retain the draft',async()=>{
  const f=fixture();assert.equal(f.paste([]).prevented,undefined);
  f.paste();f.requests[0].reject(new Error('Network unavailable'));await flush();
  assert.match(f.form.children[0].innerHTML,/Upload failed/);
  assert.throws(()=>f.controller.ids(),/retry\/remove/);
  f.controller.destroy();
  const other=f.ctx.bindAttachments(f.input,f.form,'session-one');
  assert.match(f.form.children[0].innerHTML,/screenshot.png/);
  assert.throws(()=>other.ids(),/retry\/remove/);
  assert.equal(f.revoked.length,0);
});

test('drop uses the same upload flow and attached-file removal never edits the text',async()=>{
  const f=fixture();const event={dataTransfer:{types:['Files'],files:[f.file]},preventDefault(){this.prevented=true;},stopPropagation(){}};
  f.form.listeners.drop(event);assert.equal(event.prevented,true);
  const id='1'.padStart(32,'0');f.requests[0].resolve({id,name:f.file.name,size:123});await flush();
  f.form.children[0].onclick({target:{closest:()=>({dataset:{remove:id}})}});
  assert.deepEqual(Array.from(f.controller.ids()),[]);assert.equal(f.input.value,'My unsent text');
  assert.equal(f.requests[1].options.method,'DELETE');assert.equal(f.revoked.length,1);
});

test('file limits and names cannot inject markup into cards',()=>{
  const f=fixture();
  assert.match(f.ctx.attachmentError([], {...f.file,size:11*1024*1024}),/10 MB/);
  assert.match(f.ctx.attachmentError(Array(5).fill(f.file),f.file),/5 files/);
  assert.match(f.ctx.attachmentError([{size:20*1024*1024}],f.file),/20 MB/);
  const html=f.ctx.messageAttachments([{id:'safe',name:'<img src=x onerror="bad()">',size:3}]);
  assert.doesNotMatch(html,/<img src=x/);assert.match(html,/&lt;img/);
});
