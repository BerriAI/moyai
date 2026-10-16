const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),ts=require('typescript');
const code=ts.transpileModule(fs.readFileSync(path.join(__dirname,'../native/web/skill-mentions.ts'),'utf8'),{
  compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022},
}).outputText;
const helpers=import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'));
test('inline serialized mentions preserve scoped identity, surrounding prose and snapshot icons',async()=>{
  const {skillMarker,skillMentions,skillDisplayText}=await helpers;
  const value='Please use '+skillMarker({reference:'personal:team',name:'team',icon:'👥'})+' and '+skillMarker({reference:'org:team',name:'team',icon:'code'})+' here.';
  assert.deepEqual(skillMentions(value).map(({reference,icon})=>({reference,icon})),[{reference:'personal:team',icon:'👥'},{reference:'org:team',icon:'code'}]);
  assert.equal(skillDisplayText(value),'Please use /team and /team here.');
  assert.equal(skillMentions(skillMarker({reference:'personal:team',name:'team',icon:"unrecognized ()!*'"}))[0].icon,"unrecognized ()!*'");
});
test('escaped markers, code, malformed URIs and ordinary slash words stay literal',async()=>{
  const {skillMarker,skillMentions,skillQueryAllowed}=await helpers;
  const token=skillMarker({reference:'personal:team',name:'team',icon:'users'});
  for(const text of ['\\'+token,'`'+token+'`','```\n'+token+'\n```','~~~\n'+token+'\n~~~','`unclosed '+token,'    '+token,'\t'+token,'Explain /team','[/team](https://example.test)','[/team](moyai-skill:personal%ZZteam?icon=auto)'])assert.deepEqual(skillMentions(text),[],text);
  assert.equal(skillQueryAllowed('Example `use /team'),false);
  assert.equal(skillQueryAllowed('Please use /team'),true);
});
test('older scoped-prefix messages retain a readable icon and short label without a catalog',async()=>{
  const {transcriptSkills}=await helpers;
  assert.deepEqual(transcriptSkills('/personal:team investigate').map(({reference,name,icon})=>({reference,name,icon})),[{reference:'personal:team',name:'team',icon:'auto'}]);
  assert.deepEqual(transcriptSkills('Explain /personal:team'),[]);
});
