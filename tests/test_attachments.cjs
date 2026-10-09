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
  const form=new Element(),input=new Element(),requests=[],revoked=[],errors=[];let nextId=0;
  input.value='My unsent text';input.required=true;
  const ctx={console,AbortController,Map,Set,Array,URL:{createObjectURL:()=>'blob:test',revokeObjectURL:url=>revoked.push(url)},
    state:{csrf:'signed-in-session'},attachmentContentType:'application/vnd.moyai.attachment-v1',
    sealAttachment:async(file,id,name,csrf)=>{assert.equal(csrf,'signed-in-session');assert.equal(name,file.name);return 'sealed file bytes';},
    crypto:{randomUUID:()=>String(++nextId).padStart(32,'0')},
    document:Object.assign(new Element(),{createElement:()=>new Element()}),
    esc:value=>String(value).replaceAll('<','&lt;').replaceAll('"','&quot;'),toast:message=>errors.push(message),
    api:(path,options)=>new Promise((resolve,reject)=>requests.push({path,options,resolve,reject})),
  };
  vm.createContext(ctx);vm.runInContext(readFileSync('app/static/attachments.js','utf8'),ctx);
  const controller=ctx.bindAttachments(input,form,'session-one');
  const file={name:'screenshot.png',type:'image/png',size:123};
  const paste=(files=[file])=>{const event={clipboardData:{files},preventDefault(){this.prevented=true;}};form.listeners.paste(event);return event;};
  return {ctx,form,input,requests,revoked,controller,file,paste,errors};
}

test('pasted files show immediately, preserve text, and block send until upload completes',async()=>{
  const f=fixture();assert.equal(f.paste().prevented,true);
  assert.equal(f.input.value,'My unsent text');assert.equal(f.input.required,false);
  assert.match(f.form.children[0].innerHTML,/blob:test/);
  assert.throws(()=>f.controller.ids(),/Wait for uploads/);
  await flush();
  assert.equal(f.requests[0].options.body,'sealed file bytes');
  assert.equal(f.requests[0].options.headers['Content-Type'],'application/vnd.moyai.attachment-v1');
  f.requests[0].resolve({id:'1'.padStart(32,'0'),name:f.file.name,size:123,preview_url:'/safe/preview'});await flush();
  assert.deepEqual(Array.from(f.controller.ids()),['1'.padStart(32,'0')]);
  assert.match(f.form.children[0].innerHTML,/123 B/);
  f.controller.clear(f.controller.ids());assert.equal(f.input.required,true);assert.equal(f.revoked.length,1);
});

test('ordinary text paste remains native; failed uploads and navigation retain the draft',async()=>{
  const f=fixture();assert.equal(f.paste([]).prevented,undefined);
  f.paste();await flush();f.requests[0].reject(new Error('Network unavailable'));await flush();
  assert.match(f.form.children[0].innerHTML,/Upload failed/);
  assert.match(f.form.children[0].innerHTML,/Network unavailable/);
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
  await flush();
  const id='1'.padStart(32,'0');f.requests[0].resolve({id,name:f.file.name,size:123});await flush();
  f.form.children[0].onclick({target:{closest:()=>({dataset:{remove:id}})}});
  assert.deepEqual(Array.from(f.controller.ids()),[]);assert.equal(f.input.value,'My unsent text');
  assert.equal(f.requests[1].options.method,'DELETE');assert.equal(f.revoked.length,1);
});

test('failed uploads keep the file ID and encrypt again on retry',async()=>{
  const f=fixture();let encryptions=0;
  f.ctx.sealAttachment=async()=>`sealed-${++encryptions}`;
  f.paste();await flush();f.requests[0].reject(new Error('Temporary service error'));await flush();
  const id='1'.padStart(32,'0');
  f.form.children[0].onclick({target:{closest:()=>({dataset:{retry:id}})}});await flush();
  assert.equal(f.requests[1].path,f.requests[0].path);
  assert.equal(f.requests[1].options.body,'sealed-2');
  f.requests[1].resolve({id,name:f.file.name,size:123});await flush();
  assert.deepEqual(Array.from(f.controller.ids()),[id]);
  assert.doesNotMatch(f.form.children[0].innerHTML,/Temporary service error/);
});

test('removing a file during encryption prevents a late upload',async()=>{
  const f=fixture();let finish;
  f.ctx.sealAttachment=()=>new Promise(resolve=>{finish=resolve;});
  f.paste();
  const id='1'.padStart(32,'0');
  f.form.children[0].onclick({target:{closest:()=>({dataset:{remove:id}})}});
  finish('sealed');await flush();
  assert.equal(f.requests.length,1);assert.equal(f.requests[0].options.method,'DELETE');
  assert.deepEqual(Array.from(f.controller.ids()),[]);
});

test('file limits and names cannot inject markup into cards',()=>{
  const f=fixture();
  assert.match(f.ctx.attachmentError([], {...f.file,size:11*1024*1024}),/10 MB/);
  assert.match(f.ctx.attachmentError(Array(8).fill(f.file),f.file),/8 files/);
  assert.match(f.ctx.attachmentError([{size:20*1024*1024}],f.file),/20 MB/);
  const html=f.ctx.messageAttachments([{id:'safe',name:'<img src=x onerror="bad()">',size:3}]);
  assert.doesNotMatch(html,/<img src=x/);assert.match(html,/&lt;img/);
});

test('draft audio inserts an editable transcript without replacing typed text',async()=>{
  const f=fixture();let changeEvents=0;
  f.ctx.Event=class Event{constructor(type){this.type=type;}};
  f.input.dispatchEvent=event=>{if(event.type==='input')changeEvents++;};
  let useTranscript;
  f.ctx.showAttachment=(file,insert)=>{useTranscript=()=>insert(file.transcript);};
  f.paste([{name:'voice.wav',type:'audio/wav',size:123}]);await flush();
  const id='1'.padStart(32,'0');f.requests[0].resolve({id,name:'voice.wav',size:123,media_type:'audio/wav',transcript:'Fix the health check.'});await flush();
  f.form.children[0].onclick({target:{closest:()=>({dataset:{preview:id}})}});
  assert.equal(useTranscript(),true);assert.equal(f.input.value,'My unsent text\n\nFix the health check.');assert.equal(changeEvents,1);
  f.controller.lock(true);assert.equal(useTranscript(),false);
  f.controller.lock(false);f.controller.destroy();assert.equal(useTranscript(),false);
});

test('a locked attachment owner cancels recording and late uploads keep their controls locked',async()=>{
  const f=fixture();let cancelled=0;
  f.controller.destroy();
  f.ctx.bindAudioRecorder=()=>({busy:()=>false,render(){},cancel(){cancelled++;},destroy(){}});
  const controller=f.ctx.bindAttachments(f.input,f.form,'session-one');
  f.paste();await flush();controller.lock(true);
  const id='1'.padStart(32,'0');f.requests[0].resolve({id,name:f.file.name,size:123});await flush();
  assert.equal(cancelled,1);assert.equal(f.form.toolbar.children[0].disabled,true);
  assert.match(f.form.children[0].innerHTML,/data-remove="[^"]+"[^>]+disabled/);
  f.paste();await flush();assert.equal(f.requests.length,1);assert.equal(f.input.value,'My unsent text');
});


for(const source of ['picker','paste','drop'])test(`${source} accepts eight, rejects ninth, and permits replacement`,async()=>{
  const f=fixture(),files=Array.from({length:9},(_,i)=>({...f.file,name:`file-${i}.png`}));
  if(source==='paste')f.paste(files);
  else if(source==='picker'){const picker=f.form.children[1];picker.files=files;picker.onchange();}
  else f.form.listeners.drop({dataTransfer:{types:['Files'],files},preventDefault(){},stopPropagation(){}});
  for(let i=0;i<8;i++){
    await flush();assert.equal(f.requests.length,i+1);
    f.requests[i].resolve({id:String(i+1).padStart(32,'0'),name:files[i].name,size:123});
  }
  await flush();assert.equal(f.requests.length,8);assert.equal(f.controller.ids().length,8);
  assert.deepEqual(f.errors,['Attach up to 8 files per message.']);
  const removed=f.controller.ids()[0];
  f.form.children[0].onclick({target:{closest:()=>({dataset:{remove:removed}})}});
  f.paste([files[8]]);await flush();
  assert.equal(f.requests[8].options.method,'DELETE');assert.equal(f.requests.length,10);
  f.requests[9].resolve({id:'9'.padStart(32,'0'),name:files[8].name,size:123});await flush();
  assert.equal(f.controller.ids().length,8);assert.ok(!f.controller.ids().includes(removed));
});
