const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('app/static/app.js','utf8');
const functions=source.slice(source.indexOf('function harnessLogo('),source.indexOf('\nfunction setSidebar('));
function setup(){
  const context={state:{config:{harness:'claude-agent-sdk',harnesses:[{id:'hermes',name:'Hermes'},{id:'claude-agent-sdk',name:'Claude Code',model_prefix:'anthropic/claude-'}],models:[{id:'openai/gpt-6-astra',name:'Astra'},{id:'anthropic/claude-opus-5-5',name:'Opus'},{id:'anthropic/claude-sonnet-5-5',name:'Claude Sonnet 5.5'}]}},esc:s=>String(s),MoyaiProviderLogos:require('../app/static/provider-logos.js')};
  vm.createContext(context);vm.runInContext(functions,context);return context;
}
test('harness picker uses the configured Claude default and retains explicit Hermes',()=>{
  const c=setup();assert.match(c.harnessPicker('hermes'),/value="hermes" selected/);
  assert.match(c.harnessPicker('claude-agent-sdk'),/value="claude-agent-sdk" selected/);
  assert.match(c.harnessPicker(),/value="claude-agent-sdk" selected/);
});
test('every harness offers all configured models even with stale provider metadata',()=>{
  const c=setup();assert.equal(c.harnessModels('hermes').length,3);
  for(const harness of ['hermes','claude-agent-sdk','codex','opencode','deepagents','tool-loop']){
    assert.equal(c.harnessModels(harness).length,3);
    const html=c.modelPicker('model','openai/gpt-6-astra',false,harness);
    assert.match(html,/Opus/);assert.match(html,/value="openai\/gpt-6-astra" selected/);
    for(const id of ['new-model','chat-model']){
      const sonnet=c.modelPicker(id,'anthropic/claude-sonnet-5-5',false,harness);
      assert.match(sonnet,/value="anthropic\/claude-sonnet-5-5" selected>Claude Sonnet 5.5<\/option>/);
      assert.match(sonnet,/src="\/static\/provider-logos\/anthropic.svg"/);
      assert.doesNotMatch(sonnet,/disabled/);
    }
  }
});
test('harness picker shows the selected harness logo and hides it for harnesses without one',()=>{
  const c=setup();
  assert.match(c.harnessPicker('claude-agent-sdk'),/<img class="provider-logo harness-logo"[^>]*src="\/static\/harness-logos\/claude-code.svg"/);
  assert.match(c.harnessPicker('hermes'),/<img class="provider-logo harness-logo"[^>]*hidden>/);
});
