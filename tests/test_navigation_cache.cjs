const assert=require('node:assert/strict');
const {test}=require('node:test');
const {readFileSync}=require('node:fs');
const vm=require('./helpers/ui-vm.cjs');
const context={URLSearchParams,structuredClone};
vm.createContext(context);
vm.runInContext(readFileSync('app/static/navigation-cache.js','utf8'),context);
const {create,pathsFor}=context.MoyaiNavigationCache;
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};

test('recent reads reuse isolated responses, expire, and explicit refresh bypasses them',async()=>{
  let time=0,calls=0;const cache=create({now:()=>time}),load=async()=>({users:[{name:'User '+ ++calls}]});
  const first=await cache.read('/api/admin/users',load,{recent:true});first.users[0].name='Controller mutation';
  assert.equal((await cache.read('/api/admin/users',load,{recent:true})).users[0].name,'User 1');
  time=15000;await cache.read('/api/admin/users',load,{recent:true});assert.equal(calls,2);
  await cache.read('/api/admin/users',load);assert.equal(calls,3);
});
test('hover and click share an in-flight read but receive independent values',async()=>{
  const cache=create(),pending=deferred();let calls=0;
  const load=()=>{calls++;return pending.promise;};
  const hover=cache.read('/api/config',load,{recent:true}),click=cache.read('/api/config',load,{recent:true});
  pending.resolve({model:'original'});const [a,b]=await Promise.all([hover,click]);
  a.model='changed';assert.equal(b.model,'original');assert.equal(calls,1);
});
test('mutation and identity invalidation fence off older reads and do not reuse their promises',async()=>{
  const cache=create(),old=deferred(),fresh=deferred();
  const reading=cache.read('/api/config',()=>old.promise,{recent:true});
  cache.clear();const next=cache.read('/api/config',()=>fresh.promise,{recent:true});
  fresh.resolve({model:'new account'});await next;old.resolve({model:'old account'});await reading;
  assert.equal(cache.get('/api/config').model,'new account');
});
test('a later refresh wins even if an older response finishes last',async()=>{
  const cache=create(),old=deferred();const first=cache.read('/api/config',()=>old.promise);
  await cache.read('/api/config',async()=>({model:'latest'}));old.resolve({model:'outdated'});await first;
  assert.equal(cache.get('/api/config').model,'latest');
});
test('cache is bounded by both entry count and retained bytes',()=>{
  const cache=create({maxEntries:2,maxBytes:1200});
  cache.put('/api/session',{x:1});cache.put('/api/config',{x:2});cache.get('/api/session');cache.put('/api/organization',{x:3});
  assert.equal(cache.get('/api/config'),undefined);
  cache.put('/api/organization',{text:'x'.repeat(400)});assert.equal(cache.get('/api/organization'),undefined,'oversized responses are not retained');
  const bytes=create({maxBytes:200});
  for(const path of ['/api/session','/api/config','/api/organization'])bytes.put(path,{text:'x'.repeat(30)});
  assert.equal(bytes.get('/api/session'),undefined);assert(bytes.get('/api/organization'));
});
test('permission failures discard all cached data; deleted sessions discard their snapshot',async()=>{
  const cache=create(),path='/api/runs/'+'a'.repeat(32)+'?activity=summary';
  cache.put(path,{id:'a'});cache.put('/api/config',{model:'saved'});
  await assert.rejects(cache.read(path,async()=>{throw Object.assign(Error('gone'),{status:404});}));
  assert.equal(cache.get(path),undefined);assert(cache.get('/api/config'));
  await assert.rejects(cache.read('/api/admin/users',async()=>{throw Object.assign(Error('denied'),{status:403});}));
  assert.equal(cache.get('/api/config'),undefined);
});
test('secrets, credential forms and unlisted reads are never cached',async()=>{
  const cache=create();let calls=0;
  for(const path of ['/api/secrets','/api/runs/abc/credentials','/api/skills/private']){
    await cache.read(path,async()=>({secret:++calls}),{recent:true});assert.equal(cache.get(path),undefined);
  }
});
test('navigation preloads only the requested route and keys analytics by exact dates',()=>{
  assert.deepEqual(Array.from(pathsFor({view:'users',role:'member'})),['/api/session']);
  assert.deepEqual(Array.from(pathsFor({view:'spend',role:'admin',start:'2026-10-01',end:'2026-10-07'})),['/api/spend?start=2026-10-01&end=2026-10-07']);
  assert.equal(pathsFor({view:'secrets',role:'admin'}).length,0);
});

test('the real API invalidates before and after a write, including reads overlapping it',async()=>{
  const cache=create(),write=deferred(),get=deferred();
  const c={state:{csrf:'test',navigationCache:cache},fetch:(path,options)=>options.method==='PUT'?write.promise:get.promise};
  vm.createContext(c);const script=readFileSync('app/static/app.js','utf8');
  vm.runInContext(script.slice(script.indexOf('async function api('),script.indexOf('function toast(')),c);
  cache.put('/api/admin/users',{users:['before']});
  const saving=c.api('/api/admin/users/role',{method:'PUT'});assert.equal(cache.get('/api/admin/users'),undefined);
  const reading=c.api('/api/admin/users',{recent:true});
  get.resolve({ok:true,json:async()=>({users:['overlap']})});await reading;
  write.resolve({ok:true,json:async()=>({saved:true})});await saving;
  assert.equal(cache.get('/api/admin/users'),undefined);
});
