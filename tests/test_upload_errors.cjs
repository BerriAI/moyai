const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const {test}=require('node:test');
const vm=require('./helpers/ui-vm.cjs');

function client(status,detail){
  const script=readFileSync('app/static/app.js','utf8');
  const ctx={state:{csrf:'test'},fetch:async()=>({ok:false,status,json:async()=>{
    if(detail===undefined)throw Error('Not JSON');
    return {detail};
  }})};
  vm.createContext(ctx);
  vm.runInContext(script.slice(script.indexOf('async function api('),script.indexOf('function toast(')),ctx);
  return ()=>ctx.api('/api/attachments/example',{method:'PUT'});
}

test('HTML edge failures explain the HTTP status without showing untrusted markup',async()=>{
  await assert.rejects(client(403),/firewall blocked.*HTTP 403/);
  await assert.rejects(client(502),/temporarily unavailable.*HTTP 502/);
  await assert.rejects(client(429),/Other files are uploading/);
});

test('server validation messages retain their specific explanation',async()=>{
  await assert.rejects(client(422,'The upload could not be verified. Retry this file.'),/could not be verified/);
  await assert.rejects(client(403,'Refresh the page and try again.'),/Refresh the page and try again/);
});
