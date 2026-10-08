const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const {test}=require('node:test');
const vm=require('./helpers/ui-vm.cjs');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function setup(){
  class Element{setAttribute(){}remove(){this.removed=true;}prepend(...items){this.children=items;}}
  const toolbar=new Element(), form={querySelector:()=>toolbar},files=[],errors=[],requests=[],recorders=[],tracks=[];
  class Recorder{
    static isTypeSupported(type){return type.includes('webm');}
    constructor(stream,options){this.mimeType=options.mimeType;this.state='inactive';recorders.push(this);}
    start(){this.state='recording';}
    stop(){this.state='inactive';queueMicrotask(()=>{this.ondataavailable({data:new Blob(['audio'])});this.onstop();});}
  }
  const ctx={Blob,File,console,clearInterval,setInterval,MediaRecorder:Recorder,
    navigator:{mediaDevices:{getUserMedia:()=>new Promise((resolve,reject)=>requests.push({resolve,reject}))}},
    document:{createElement:()=>new Element()},toast:value=>errors.push(value)};
  vm.createContext(ctx);vm.runInContext(readFileSync('app/static/audio-recorder.js','utf8'),ctx);
  const controller=ctx.bindAudioRecorder(form,file=>files.push(file),()=>false);
  const acquire=(i=0)=>{const track={stopped:false,stop(){this.stopped=true;}};tracks.push(track);requests[i].resolve({getTracks:()=>[track]});};
  return {controller,button:toolbar.children[0],cancel:toolbar.children[1],files,errors,requests,recorders,tracks,acquire};
}
test('recording requests permission only after a click and stops all tracks',async()=>{
  const f=setup();assert.equal(f.requests.length,0);
  const start=f.button.onclick();assert.equal(f.controller.busy(),true);f.acquire();await start;
  assert.equal(f.button.textContent,'Stop recording');await f.button.onclick();await flush();
  assert.equal(f.files.length,1);assert.equal(f.files[0].name,'Voice message.webm');assert.equal(f.tracks[0].stopped,true);
  assert.equal(f.controller.busy(),false);f.controller.destroy();
});
test('cancelled permission requests cannot reopen a microphone or create a draft',async()=>{
  const f=setup();const start=f.button.onclick();f.cancel.onclick();
  const next=f.button.onclick();f.acquire(0);await start;assert.equal(f.recorders.length,0);
  f.acquire(1);await next;assert.equal(f.recorders.length,1);f.cancel.onclick();await flush();
  assert.equal(f.files.length,0);assert.ok(f.tracks.every(t=>t.stopped));f.controller.destroy();
});
test('navigation discards recordings and releases microphone',async()=>{
  const f=setup();const start=f.button.onclick();f.acquire();await start;f.controller.destroy();await flush();
  assert.equal(f.files.length,0);assert.ok(f.tracks[0].stopped);
});
test('denied microphone permission is actionable and retryable',async()=>{
  const f=setup();const start=f.button.onclick();f.requests[0].reject(Object.assign(new Error(),{name:'NotAllowedError'}));await start;
  assert.match(f.errors[0],/permission was denied/);assert.equal(f.controller.busy(),false);assert.equal(f.button.disabled,false);f.controller.destroy();
});
