const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const source = ts.transpileModule(fs.readFileSync('native/web/api.ts','utf8'),{
  compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022},
}).outputText;
const loaded = import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

test('non-JSON errors retain HTTP status without showing raw proxy or backend contents',async t=>{
  const {api,ApiError}=await loaded;
  for(const body of ['Internal Server Error','<html>private backend details</html>','', 'null','[]','{"detail":42}']){
    t.mock.method(global,'fetch',async()=>new Response(body,{status:503}));
    await assert.rejects(api('/sessions'),error=>error instanceof ApiError
      && error.status===503 && error.message==='Moyai is temporarily unavailable (HTTP 503). Please try again.');
  }
});

test('JSON validation errors and successful JSON values keep their existing meaning',async t=>{
  const {api}=await loaded;
  for(const detail of ['Please choose a model.',[{msg:'Please choose a model.'}]]){
    t.mock.method(global,'fetch',async()=>new Response(JSON.stringify({detail}),{status:422}));
    await assert.rejects(api('/sessions'),{status:422,message:'Please choose a model.'});
  }
  t.mock.method(global,'fetch',async()=>new Response('{"ok":true}'));
  assert.deepEqual(await api('/sessions'),{ok:true});
});

test('empty successful responses are accepted only when the HTTP contract permits them',async t=>{
  const {api}=await loaded;
  for(const status of [204,205]){
    t.mock.method(global,'fetch',async()=>new Response(null,{status}));
    assert.equal(await api('/turns/queued','DELETE'),undefined);
  }
  for(const body of ['', '<html>login page</html>', '{broken']){
    t.mock.method(global,'fetch',async()=>new Response(body));
    await assert.rejects(api('/sessions'),{status:200,message:'Moyai returned an unreadable response. Please try again.'});
  }
});

test('network failures get a useful message and never automatically replay a write',async t=>{
  const {api}=await loaded;
  const request=t.mock.method(global,'fetch',async()=>{throw new TypeError('Failed to fetch');});
  await assert.rejects(api('/sessions/session/messages','POST',{prompt:'hello'}),{
    status:0,message:'Could not reach Moyai. Check your connection and try again.',
  });
  assert.equal(request.mock.callCount(),1);
});

test('abort errors remain aborts during fetch and response body consumption',async t=>{
  const {api}=await loaded;
  const abort=new DOMException('Aborted','AbortError');
  t.mock.method(global,'fetch',async()=>{throw abort;});
  await assert.rejects(api('/sessions'),error=>error===abort);
  t.mock.method(global,'fetch',async()=>new Response(new ReadableStream({start(controller){controller.error(abort);}})));
  await assert.rejects(api('/sessions'),error=>error===abort);
});

test('non-JSON 401 signs out only the session that issued the request',async t=>{
  const {api,setCsrf}=await loaded;
  const previous=global.window;global.window=new EventTarget();
  t.after(()=>{global.window=previous;});
  let signedOut=0;window.addEventListener('moyai:signed-out',()=>signedOut++);
  setCsrf('original');
  t.mock.method(global,'fetch',async()=>new Response('Unauthorized',{status:401}));
  await assert.rejects(api('/me'),{status:401});
  assert.equal(signedOut,1);
  t.mock.method(global,'fetch',async()=>{setCsrf('new-session');return new Response('',{status:401});});
  await assert.rejects(api('/me'),{status:401});
  assert.equal(signedOut,1,'a stale request must not sign out the newer session');
});

test('binary uploads use the same error policy without changing their body or media type',async t=>{
  const {apiRequest,setCsrf}=await loaded;
  setCsrf('upload-session');
  const body=new Uint8Array([0,1,255]);
  t.mock.method(global,'fetch',async(url,options)=>{
    assert.equal(url,'/api/uploads?name=image.png');
    assert.equal(options.body,body);
    assert.equal(options.headers.get('Content-Type'),'image/png');
    assert.equal(options.headers.get('X-Moyai-CSRF'),'upload-session');
    assert.equal(options.headers.get('X-CSRF-Token'),'upload-session');
    return new Response('Internal Server Error',{status:500});
  });
  await assert.rejects(apiRequest('/uploads?name=image.png',{method:'POST',body,headers:{'Content-Type':'image/png'}}),{status:500});
});
