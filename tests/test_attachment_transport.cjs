const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const {test}=require('node:test');
const vm=require('node:vm');
const {webcrypto,createHash}=require('node:crypto');
function transport() {
 const context={crypto:webcrypto,Blob,TextEncoder,Uint8Array,DataView,Date,Array,Number,BigInt,Error};
 vm.createContext(context);vm.runInContext(readFileSync('app/static/attachment-transport.js','utf8'),context);return context;
}
test('WebCrypto transfers complete files in independent authenticated parts',async()=>{
 const ctx=transport(),bytes=Buffer.alloc(11*1024*1024+39,137),source=new Blob([bytes]);
 const original=source.arrayBuffer.bind(source);source.arrayBuffer=()=>{throw Error('Whole-file buffering');};
 const calls=[];let part=0;
 const send=async(path,options)=>{
   calls.push({path,options});
   if(path.endsWith('/upload'))return {part_size:1024*1024,size:source.size};
   if(path.includes('/parts/')){
     const packet=options.body,stamp=new DataView(packet.buffer).getBigUint64(0);
     const name='référence.bin',type='application/vnd.moyai.attachment-v1';
     const keyBytes=await webcrypto.subtle.digest('SHA-256',new TextEncoder().encode('moyai-attachment-v1\0csrf'));
     const key=await webcrypto.subtle.importKey('raw',keyBytes,'AES-GCM',false,['decrypt']);
     const raw=Buffer.from(await webcrypto.subtle.decrypt({name:'AES-GCM',iv:packet.slice(8,20),
       additionalData:new TextEncoder().encode(`${type}\0upload:${part}\0${name}\0${stamp}`)},key,packet.slice(20)));
     assert.deepEqual(raw,bytes.subarray(part*1024*1024,(part+1)*1024*1024));
     assert.ok(raw.length<=1024*1024);return {part:part++,sha256:createHash('sha256').update(raw).digest('hex')};
   }
   return {id:'upload',size:bytes.length};
 };
 const progress=[];
 const result=await ctx.uploadAttachment(source,'upload','référence.bin','csrf',send,new AbortController().signal,(done,total)=>progress.push([done,total]));
 assert.equal(result.size,bytes.length);assert.equal(part,12);assert.equal(calls.length,14);
 assert.deepEqual(progress.at(-1),[bytes.length,bytes.length]);
});
test('mismatched receipts and cancellation prevent completion',async()=>{
 const ctx=transport(),source=new Blob(['hello']);const paths=[];
 await assert.rejects(ctx.uploadAttachment(source,'upload','f.txt','csrf',async path=>{
   paths.push(path);return path.endsWith('/upload')?{part_size:2,size:5}:{part:0,sha256:'wrong'};
 },new AbortController().signal),/receipt did not match/);
 assert.equal(paths.length,2);assert.ok(!paths.some(p=>p.endsWith('/complete')));
 const cancelled=new AbortController();cancelled.abort();
 await assert.rejects(ctx.uploadAttachment(source,'upload','f.txt','csrf',async()=>({part_size:2,size:5}),cancelled.signal),{name:'AbortError'});
});
