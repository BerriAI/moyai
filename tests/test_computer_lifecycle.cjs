const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
function node(){
  const children=new Map();
  return {dataset:{},classList:{toggle(){}},setAttribute(){},addEventListener(){},remove(){this.removed=true;},
    querySelector(s){if(!children.has(s))children.set(s,node());return children.get(s);},querySelectorAll(){return [];},append(el){this.child=el;}};
}
function browser(api){
  const context={window:{},document:{hidden:false,createElement:node,addEventListener(){}},setTimeout:()=>1,clearTimeout(){},setInterval:()=>1,clearInterval(){}};
  vm.createContext(context);vm.runInContext(fs.readFileSync('app/static/computer.js','utf8'),context);
  return context.window.MoyaiComputer.create({api,escape:s=>s});
}
const tick=()=>new Promise(setImmediate);
const frame=url=>({url,has_sandbox:true,available:true,frame:'image',width:1280,height:720,captures:[],actor:'owner',controller:''});

test('hiding while a claim is in flight releases after the claim completes',async()=>{
  const actions=[];let finishClaim;
  const view=browser(async(path,options)=>{
    if(!options)return frame('https://example.com');
    const action=JSON.parse(options.body).action;actions.push(action);
    if(action==='claim')await new Promise(resolve=>finishClaim=resolve);
    return {};
  });
  const host=node();await view.open('one',host);
  const claim=host.child.querySelector('[data-control]').onclick();
  view.close();await tick();assert.deepEqual(actions,['claim']);
  finishClaim();await claim;await tick();
  assert.deepEqual(actions,['claim','release']);assert.equal(host.child.removed,true);
});

test('late frames cannot overwrite a replacement computer view',async()=>{
  let finishOld;
  const view=browser(async(path,options)=>{
    if(options)return {};
    if(path.includes('/old/'))return new Promise(resolve=>finishOld=resolve);
    return frame('https://new.example');
  });
  const old=node(),current=node();const opening=view.open('old',old);await tick();
  await view.open('new',current);
  finishOld(frame('https://old.example'));await opening;
  assert.equal(current.child.querySelector('[data-address]').textContent,'https://new.example');
  assert.equal(old.child.removed,true);view.close();
});
