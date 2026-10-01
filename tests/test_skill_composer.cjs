const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');

const catalog = [
  {id:'org',name:'benchmark-review',reference:'org:benchmark-review',scope:'organization',description:'Review benchmark coverage'},
  {id:'mine',name:'benchmark-review',reference:'personal:benchmark-review',scope:'personal',description:'My benchmark review'},
  {id:'old',name:'old-review',reference:'org:old-review',scope:'organization',archived:true,description:'Old workflow'},
];
const flush = () => new Promise(resolve => setImmediate(resolve));

function fixture(api = async () => ({skills:catalog})) {
  const listeners = {}, attrs = {}, globals = new Map();
  const input = {id:'prompt',value:'',selectionStart:0,selectionEnd:0,maxLength:16000,isConnected:true,
    setAttribute:(key,value)=>{attrs[key]=value;},removeAttribute:key=>{delete attrs[key];},
    addEventListener:(name,fn)=>{(listeners[name] ||= []).push(fn);},
    setSelectionRange(start,end){this.selectionStart=start;this.selectionEnd=end;},
    focus(){ctx.document.activeElement=this;},
    dispatchEvent(event){for(const fn of listeners[event.type] || [])fn(event);},
  };
  const popup = {hidden:true,style:{},classList:{toggle(){}},innerHTML:'',
    querySelector:()=>null,addEventListener(){},remove(){this.removed=true;}};
  const form = {append(){},getBoundingClientRect:()=>({top:400,bottom:500}),addEventListener(){}};
  const ctx = {api,document:{activeElement:input,createElement:()=>popup,getElementById:()=>null},
    window:{innerHeight:800,addEventListener:(name,fn)=>globals.set(name,fn),removeEventListener:name=>globals.delete(name)},
    skillToken:skill=>'/'+skill.reference,
    esc:value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),
    toast:()=>{},Event:class{constructor(type){this.type=type;}},
  };
  vm.createContext(ctx);
  vm.runInContext(readFileSync('app/static/skill-composer.js','utf8'),ctx);
  const controller = ctx.bindInlineSkillPicker(input,form);
  const type = (value,caret=value.length) => {input.value=value;input.setSelectionRange(caret,caret);input.dispatchEvent({type:'input'});};
  const key = (key,extra={}) => {const event={key,preventDefault(){this.prevented=true;},...extra};return {handled:controller.keydown(event),event};};
  return {ctx,input,popup,attrs,controller,type,key,globals};
}

test('slash completion ignores URLs, paths, code and selected text',()=>{
  const {ctx} = fixture();
  for(const value of ['https://example.com/review','/tmp/file','`/review','```\n/review','./review'])
    assert.equal(ctx.slashSkillQuery(value,value.length),null,value);
  assert.equal(ctx.slashSkillQuery('/review',7,0),null);
  assert.equal(ctx.slashSkillQuery('/skill',6).query,'');
  assert.equal(ctx.slashSkillQuery('Please /skill bench',19).query,'bench');
});

test('same-named skills keep explicit scope, archived skills stay hidden',()=>{
  const {ctx} = fixture();
  assert.deepEqual(Array.from(ctx.matchingSkills(catalog,'benchmark'),s=>s.id),['mine','org']);
  assert.deepEqual(Array.from(ctx.matchingSkills(catalog,'org:'),s=>s.id),['org']);
});

test('Enter selects rather than sends; caret and surrounding draft survive',async()=>{
  const b=fixture();
  b.type('Please /bench then check costs',13);
  await flush();
  b.key('ArrowDown'); // organization version, after the personal version
  const result=b.key('Enter');
  assert.equal(result.handled,true);
  assert.equal(result.event.prevented,true);
  assert.equal(b.input.value,'Please /org:benchmark-review then check costs');
  assert.equal(b.input.selectionStart,'Please /org:benchmark-review '.length);
  assert.equal(b.popup.hidden,true);
  assert.equal(b.key('Enter').handled,false); // normal composer can now submit
});

test('loading and empty menus cannot accidentally submit, Escape and IME work',async()=>{
  let resolve;
  const b=fixture(()=>new Promise(r=>{resolve=r;}));
  b.type('/');
  assert.equal(b.key('Enter').handled,true);
  assert.equal(b.input.value,'/');
  assert.equal(b.key('Enter',{isComposing:true}).handled,false);
  assert.equal(b.key('Escape').handled,true);
  resolve({skills:[]});await flush();
  assert.equal(b.popup.hidden,true);
  b.type('/missing');
  assert.equal(b.key('Enter').handled,true);
  assert.match(b.popup.innerHTML,/No skills yet/);
  assert.equal(b.key('Enter',{shiftKey:true}).handled,false);
  assert.equal(b.popup.hidden,true);
});

test('slow catalog replies cannot reopen a picker after navigation',async()=>{
  let resolve;
  const b=fixture(()=>new Promise(r=>{resolve=r;}));
  b.type('/');b.controller.destroy();resolve({skills:catalog});await flush();
  assert.equal(b.popup.hidden,true);
  assert.equal(b.popup.removed,true);
  assert.equal(b.globals.size,0);
});

test('provider failure is visible and untrusted descriptions stay text',async()=>{
  const unavailable=fixture(async()=>{throw new Error('Unavailable');});
  unavailable.type('/');await flush();
  assert.match(unavailable.popup.innerHTML,/Couldn’t load skills/);
  const b=fixture(async()=>({skills:[{...catalog[0],description:'<img src=x onerror="bad()">'}]}));
  b.type('/');await flush();
  assert.doesNotMatch(b.popup.innerHTML,/<img/);
  assert.match(b.popup.innerHTML,/&lt;img/);
});
