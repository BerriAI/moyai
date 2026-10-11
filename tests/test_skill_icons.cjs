const {test}=require('node:test');
const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const vm=require('./helpers/ui-vm.cjs');
const ctx=vm.createContext({});
for(const file of ['icons','skill-icons'])vm.runInContext(readFileSync(`app/static/${file}.js`,'utf8'),ctx);
test('automatic icons use bounded name words and default to a cube',()=>{
  for(const [name,key] of [['team','team'],['video','video'],['linkedin-post','chat'],['benchmark','chart'],['code-review','review'],['unknown','cube'],['steam','cube']])
    assert.equal(ctx.skillIconKey({name}),key);
  assert.equal(ctx.skillIconKey({name:'unknown',description:'team video review'}),'cube');
});
test('explicit icons win and all choices render decorative local SVG',()=>{
  const choices=vm.runInContext('skillIconChoices',ctx);
  for(const [key] of choices){
    assert.equal(ctx.skillIconKey({name:'team',icon:key}),key);
    assert.match(ctx.skillIcon({icon:key}),/<svg.*aria-hidden="true"/);
  }
  assert.equal(ctx.skillIconKey({builtin:true,reference:'goal',icon:'video'}),'target');
  assert.equal(ctx.skillIconKey({name:'goal',reference:'personal:goal',icon:'video'}),'video');
});
test('untrusted icon values cannot inject markup or URLs',()=>{
  for(const icon of ['<img src=x onerror=alert(1)>','https://example.test/a.svg','__proto__','constructor']){
    assert.equal(ctx.skillIconKey({icon}),'cube');
    assert.doesNotMatch(ctx.skillIcon({icon}),/img|https:|onerror/);
  }
});
