const assert=require('node:assert/strict');
const {test}=require('node:test');
const {readFileSync}=require('node:fs');
const vm=require('./helpers/ui-vm.cjs');
function context(){const c={Intl,Date,Map,esc:s=>String(s).replace(/[&<>"']/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]))};vm.createContext(c);for(const file of ['automation-triggers.js','automations.js'])vm.runInContext(readFileSync('app/static/'+file,'utf8'),c);return c;}
test('session source roundtrips filters, escapes values and explains scope',()=>{
  const c=context();vm.runInContext('automationEventChoices={session:[["message.posted","New session message"]]}',c);
  const saved=c.readAutomationTrigger(card({source:'session',event:'message.posted',session_id:'a'.repeat(32),text_contains:'broken',text_starts_with:'bug:'},'native'));
  assert.deepEqual(JSON.parse(JSON.stringify(saved)),{id:'native',event:{provider:'session',event:'message.posted',session_id:'a'.repeat(32),text_contains:'broken',text_starts_with:'bug:'}});
  const html=c.automationEventEditor('session',{text_contains:'"><script>x</script>'});
  assert.doesNotMatch(html,/<script>|name="sender_type"|name="channel_id"/);
  assert.match(html,/New session message/);assert.match(html,/ordinary side chats/);
  assert.match(html,/messages posted while paused are not replayed/);
  assert.match(c.automationTriggerSummary({triggers:[saved]}),/Moyai sessions/);
});
test('native webhook setup is unavailable and mixed sources retain only external setup',()=>{
  const c=context();c.automationTriggerDialog=()=>assert.fail('Native source opened webhook setup');
  const native={trigger:{providers:[{provider:'session'},{provider:'slack'}]}};
  assert.equal(c.automationWebhookProviders(native).length,0);c.setupAutomationWebhook(native);
  assert.deepEqual(Array.from(c.automationWebhookProviders({trigger:{providers:[...native.trigger.providers,{provider:'github'}]}}),p=>p.provider),['github']);
});
function card(values,id='stable-id'){return {dataset:{triggerId:id},querySelector(selector){const name=selector.match(/name=["']?([^"'\]]+)/)?.[1];return values[name]===undefined?null:{value:values[name],disabled:false,checked:values[name]===true};}};}
test('multiple triggers retain identifiers and per-trigger configuration on save',()=>{const c=context();const schedule=c.readAutomationTrigger(card({source:'schedule',frequency:'cron',cron:'*/15 * * * *',timezone:'UTC'},'timer'));const event=c.readAutomationTrigger(card({source:'linear',event:'issue.priority_changed',team_id:'uuid',priority:'0'},'linear'));assert.equal(schedule.id,'timer');assert.equal(schedule.schedule.cron,'*/15 * * * *');assert.equal(event.id,'linear');assert.equal(event.event.priority,0);assert.equal(event.schedule,undefined);});
test('reaction save omits hidden message fields',()=>{const c=context();const item=card({source:'slack',event:'reaction.added',channel_id:'C12345678',reaction:'eyes',include_thread_replies:false});const saved=c.readAutomationTrigger(item);assert.equal(saved.event.reaction,'eyes');assert.equal(saved.event.text_contains,undefined);});
test('one-time dates are serialized with a timezone and displayed as one-time',()=>{const c=context();const saved=c.readAutomationTrigger(card({source:'schedule',frequency:'once',run_at:'2030-01-02T09:30'}));assert.match(saved.schedule.run_at,/Z$/);assert.match(c.automationTiming(saved.schedule),/^Once/);});
test('summaries explain OR and distinguish unlimited from a shared cap',()=>{const c=context(),d={triggers:[{schedule:{frequency:'cron',cron:'0 9 * * *',timezone:'UTC'}},{event:{provider:'webhook',event:'deploy.done'}}],max_runs_per_hour:null};assert.match(c.automationTriggerSummary(d),/ OR /);assert.match(c.automationTriggerSummary(d),/No hourly cap/);assert.match(c.automationTriggerSummary({...d,max_runs_per_hour:150}),/150 runs\/hour shared/);});
test('provider text and schedule values are escaped in editor HTML',()=>{const c=context();assert.doesNotMatch(c.automationEventEditor('github',{repository:'"><img onerror=x>'}),/<img/);assert.doesNotMatch(c.automationScheduleEditor({frequency:'cron',cron:'"><script>',timezone:'UTC'}),/<script>/);});

test('editing legacy GitHub action triggers preserves the action restriction',()=>{const c=context();const html=c.automationEventEditor('github',{event:'issues.labeled',repository:'BerriAI/litellm'});assert.match(html,/name="action" value="labeled"/);});
