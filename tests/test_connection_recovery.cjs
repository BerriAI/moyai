const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');

test('connection recheck clears stale errors and preserves failure feedback',async()=>{
  const elements=new Map();
  const $=s=>{if(!elements.has(s))elements.set(s,{textContent:'',disabled:false,showModal(){},close(){}});return elements.get(s);};
  const responses=[new Error('HTTP 403'),new Error('HTTP 429'),null];
  let refreshes=0;
  const ctx={$,state:{role:'admin',connections:[{id:'linear',connected:true,enabled:true,identity:'Fixture',tools:[]}]},providerNames:{linear:'Linear'},esc:s=>s,
    api:async()=>{const err=responses.shift();if(err)throw err;return {healthy:true};},toast:()=>{},renderConnections:async()=>{refreshes++;},githubRepositoriesDialog:()=>{}};
  vm.createContext(ctx);
  const src=fs.readFileSync('app/static/app.js','utf8');
  vm.runInContext(src.slice(src.indexOf('function manageConnection('),src.indexOf('async function githubRepositoriesDialog(')),ctx);
  ctx.manageConnection('linear');
  const b=$('#connection-check');
  for(const message of ['HTTP 403','HTTP 429','']){
    await b.onclick();
    assert.equal($('#connection-error').textContent,message);
    assert.equal(b.disabled,false);
  }
  assert.equal(refreshes,1);
});
