const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),ts=require('typescript');
function load(file){
  const code=ts.transpileModule(fs.readFileSync(path.join(__dirname,'../native/web',file),'utf8'),{
    compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022},
  }).outputText;
  return import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'));
}
const icons=load('skill-icon-map.ts'),mentions=load('skill-mentions.ts');

test('skill names select meaningful distinct outlines with specific phrases taking priority',async()=>{
  const {skillIconName}=await icons;
  for(const [name,expected] of [
    ['team','team'],['add-issue','tasks'],['litellm-brand-visuals','palette'],
    ['research','search'],['review-pr','pull-request'],['e2e-testwriter','test'],
    ['daily-task','calendar'],['api-security','shield'],['translate-notes','languages'],
    ['spend-analysis','chart'],['google-sheets','table'],['release-notes','rocket'],
    ['hyperframes-audio','music'],['hyperframes-cli','terminal'],['ramp-book-flight','globe'],
    ['notion-spec-to-implementation','workflow'],
    ['contest-research','search'],
  ])assert.equal(skillIconName({name,icon:'auto'}),expected,name);
  // A generic image hint does not hide the clearer brand/design meaning.
  assert.equal(skillIconName({name:'litellm-brand-visuals',icon:'image'}),'palette');
});

test('30-icon fallback is deterministic, distributed and independent of catalog ordering',async()=>{
  const {skillIconName,SKILL_ICON_NAMES}=await icons;
  assert.equal(SKILL_ICON_NAMES.length,30);assert.equal(new Set(SKILL_ICON_NAMES).size,30);
  const skills=Array.from({length:240},(_,index)=>({name:`zorbix-${index}`,icon:'auto'}));
  const original=new Map(skills.map(skill=>[skill.name,skillIconName(skill)]));
  for(const skill of [...skills].reverse())assert.equal(skillIconName(skill),original.get(skill.name));
  assert.ok(new Set(original.values()).size>=20);
  for(const icon of ['constructor','__proto__','not-an-icon','https://example.test/icon.svg'])assert.ok(SKILL_ICON_NAMES.includes(skillIconName({name:'zorbix',icon})));
});

test('saved mentions retain the same icon resolution without a catalog lookup',async()=>{
  const {skillIconName}=await icons,{skillMarker,skillMentions}=await mentions;
  for(const skill of [
    {reference:'personal:team',name:'team',icon:'👥'},
    {reference:'org:litellm-brand-visuals',name:'litellm-brand-visuals',icon:'image'},
    {reference:'personal:zorbix',name:'zorbix',icon:'📝'},
    {reference:'personal:lumora',name:'lumora',icon:'auto'},
  ]){
    const [saved]=skillMentions(skillMarker(skill));
    assert.equal(skillIconName(saved),skillIconName(skill));
  }
  assert.equal(skillIconName({name:'zorbix',icon:'📝'}),'tasks');
});
