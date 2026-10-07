const automationSources = {schedule:'Schedule',session:'Moyai sessions',slack:'Slack',github:'GitHub',gitlab:'GitLab',linear:'Linear',jira:'Jira',pylon:'Pylon',pagerduty:'PagerDuty',webhook:'Generic webhook'};
const automationDefaultSchedule = () => ({frequency:'weekdays',time:'09:00',weekday:1,timezone:Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC'});
function automationTriggers(d) {
  return d.triggers || [{id:'default',...(d.event?{event:d.event}:{schedule:d.timing||automationDefaultSchedule()})}];
}
function automationTriggerSummary(d,completed=[]) {
  return automationTriggers(d).map(t=>t.schedule?automationTiming(t.schedule)+(completed.includes(t.id)?' (completed)':''):`${automationSources[t.event.provider]} · ${dictEventLabel(t.event)}${t.event.repository?' · '+t.event.repository:''}`).join(' OR ') + (d.max_runs_per_hour===null?' · No hourly cap':` · ${d.max_runs_per_hour??50} runs/hour shared`);
}
function dictEventLabel(e) {return (automationEventChoices[e.provider]||[]).find(x=>x[0]===e.event)?.[1]||e.event;}
function automationTriggerFields() {
  return '<fieldset class="automation-trigger-group"><legend>Start when any trigger matches</legend><p class="automation-policy">Each trigger can start this workflow independently. An event matching several triggers starts one run.</p><div data-triggers></div><button type="button" class="quiet" data-add-trigger>＋ Add trigger</button></fieldset>';
}
const automationEventFields = {
  session:[['session_id','Session ID (optional)','Leave blank for all shared sessions'],['text_contains','Message contains (optional)','error'],['text_starts_with','Message starts with (optional)','bug:']],
  github:[['repository','Repository','BerriAI/litellm',true],['action','Action (optional)','opened, labeled, closed…'],['label','Label (optional)','bug'],['branch','Branch (optional)','main'],['conclusion','CI conclusion (optional)','failure'],['text_contains','Text contains (optional)','investigate']],
  gitlab:[['repository','Project path','group/project',true],['action','Action (optional)','open, update, merge…'],['status','Status (optional)','failed'],['branch','Branch (optional)','main'],['text_contains','Text contains (optional)','investigate']],
  linear:[['team_id','Team ID','Linear team UUID',true],['assignee_id','Assignee ID (optional)','Linear user UUID'],['label_id','Label ID (optional)','Linear label UUID'],['status','Status ID (optional)','Linear state UUID'],['priority','Priority (optional)','0–4']],
  jira:[['project','Project key (optional)','ENG'],['assignee_id','Assignee account ID (optional)','Account ID'],['label','Label (optional)','moyai'],['status','Status (optional)','In Progress'],['epic','Epic / parent key (optional)','ENG-123'],['text_contains','Text contains (optional)','investigate']],
  pylon:[['label','Tag (optional)','bug'],['status','Status (optional)','open']],
  pagerduty:[['service_id','Service ID (optional)','PABC123'],['urgency','Urgency (optional)','high'],['status','Status (optional)','triggered']],
  slack:[['channel_id','Channel ID (optional with another filter)','C0123456789'],['text_contains','Message contains (optional)','investigate'],['text_starts_with','Message starts with (optional)','!moyai'],['reaction','Reaction name (required for reactions)','rotating_light']],
  webhook:[['payload_pattern','Payload matches regex (optional)','"priority":\\s*"high"']]
};
function automationEventEditor(provider,e={}) {
  if(provider==='github'&&e.event?.includes('.')){const [event,action]=e.event.split('.');e={...e,event,action:e.action||action};}
  const fields=automationEventFields[provider]||[];
  const choices=automationEventChoices[provider]||[];
  const event=provider==='webhook'?`<label>Event name (optional; * matches any)<input name="event" value="${esc(e.event||'*')}" maxlength="80"></label>`:`<label>Event<select name="event">${choices.map(([key,label])=>`<option value="${key}" ${e.event===key?'selected':''}>${esc(label)}</option>`).join('')}</select></label>`;
  return event+`<div class="automation-fields">${fields.map(([key,label,placeholder,required])=>`<label>${label}<input name="${key}" value="${esc(e[key]??'')}" placeholder="${esc(placeholder)}" ${required?'required':''} ${key==='priority'?'type="number" min="0" max="4"':''}></label>`).join('')}</div>`+
    (['slack','github','gitlab'].includes(provider)?`<label>Sender<select name="sender_type">${[['any','Anyone'],['human','People only'],['bot','Bots only']].map(([key,label])=>`<option value="${key}" ${e.sender_type===key?'selected':''}>${label}</option>`).join('')}</select></label>`:'')+
    (provider==='slack'?`<label class="automation-check"><input type="checkbox" name="include_thread_replies" ${e.include_thread_replies?'checked':''}>Include thread replies</label><p class="automation-policy">Uses the installed Slack app and its allowed users. Add the bot to watched channels. Messages require history scopes; reactions require reactions:read and the reaction_added subscription. Moyai’s own messages are excluded.</p>`:'')+
    (provider==='session'?'<p class="automation-policy">Watches human messages in shared sessions in this organization, including teammates’ sessions and ordinary side chats. Only new messages after enabling are eligible; history and messages posted while paused are not replayed. Assistant messages, automation and worker sessions and their descendants are excluded. No webhook or Slack credentials needed. The existing shared hourly cap still applies.</p>':'')+
    (provider==='pylon'?'<p class="automation-policy">Connect Pylon’s Send webhook action for each selected event. Setup includes the payload mapping and authentication.</p>':'');
}
function automationScheduleEditor(t) {
  const freq=t.frequency||'weekdays';
  const at=t.run_at?new Date(t.run_at):null;
  const local=at&&!Number.isNaN(at.valueOf())?new Date(at.valueOf()-at.getTimezoneOffset()*60000).toISOString().slice(0,16):'';
  return `<label>Repeat<select name="frequency">${[['hourly','Hourly'],['daily','Daily'],['weekdays','Weekdays'],['weekly','Weekly'],['cron','Custom cron'],['once','One time']].map(([key,label])=>`<option value="${key}" ${freq===key?'selected':''}>${label}</option>`).join('')}</select></label><div class="automation-fields">`+
    (freq==='once'?`<label>Date and time (${esc(Intl.DateTimeFormat().resolvedOptions().timeZone)})<input name="run_at" type="datetime-local" required value="${local}"></label>`:
    `${freq==='weekly'?`<label>Day<select name="weekday">${['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'].map((day,i)=>`<option value="${i}" ${t.weekday===i?'selected':''}>${day}</option>`).join('')}</select></label>`:''}${freq==='cron'?`<label>Cron expression<input name="cron" required value="${esc(t.cron||'0 9 * * 1-5')}" placeholder="0 9 * * 1-5"></label>`:`<label>${freq==='hourly'?'Minute within each hour (hour is ignored)':'Time'}<input name="time" type="time" required value="${esc(t.time||'09:00')}"></label>`}<label>Timezone<input name="timezone" required value="${esc(t.timezone||'UTC')}"></label>`)+`</div>${freq==='cron'?'<p class="automation-policy">Five fields: minute, hour, day, month, weekday. Supports numbers, *, lists, ranges and steps.</p>':freq==='once'?'<p class="automation-policy">Runs once and then completes this trigger. Other triggers keep listening.</p>':''}`;
}
function readAutomationTrigger(card) {
  const get=name=>{const el=card.querySelector(`[name="${name}"]`);return el&&!el.disabled?el.value:undefined;};
  const provider=get('source'),id=card.dataset.triggerId;
  if(provider==='schedule') {
    const frequency=get('frequency');
    return {id,schedule:{frequency,time:get('time')||'09:00',weekday:Number(get('weekday')??1),timezone:get('timezone')||'UTC',cron:get('cron')||'',run_at:frequency==='once'&&get('run_at')?new Date(get('run_at')).toISOString():null}};
  }
  const event={provider,event:get('event')||'*'};
  for(const [key] of automationEventFields[provider]||[])if(get(key)!==''&&get(key)!==undefined)event[key]=key==='priority'?Number(get(key)):get(key);
  if(provider==='github'&&event.repository===card.dataset.repositoryName&&card.dataset.repositoryId)event.repository_id=Number(card.dataset.repositoryId);
  if(get('sender_type'))event.sender_type=get('sender_type');
  if(provider==='slack')event.include_thread_replies=!card.querySelector('[name=include_thread_replies]').disabled&&card.querySelector('[name=include_thread_replies]').checked;
  return {id,event};
}
function bindAutomationTrigger(form,d) {
  const host=form.querySelector('[data-triggers]'),cap=form.elements.max_runs_per_hour;
  let capEdited=d.max_runs_per_hour!==undefined;
  cap.oninput=()=>{capEdited=true;};
  function refresh() {
    const cards=[...host.children];
    cards.forEach((card,i)=>{card.querySelector('[data-trigger-title]').textContent=`${i?'OR · ':''}Trigger ${i+1}`;card.querySelector('[data-remove-trigger]').disabled=cards.length===1;});
    form.querySelector('[data-add-trigger]').disabled=cards.length>=20;
    if(!capEdited)cap.value=cards.some(c=>c.querySelector('[name=source]').value==='slack'&&c.querySelector('[name=event]')?.value==='message.posted')?150:50;
  }
  function add(t={id:crypto.randomUUID(),schedule:automationDefaultSchedule()}) {
    const card=document.createElement('section');card.className='automation-trigger-card';card.dataset.triggerId=t.id;card.dataset.repositoryName=t.event?.repository||'';card.dataset.repositoryId=t.event?.repository_id||'';
    const provider=t.event?.provider||'schedule';
    card.innerHTML=`<header><strong data-trigger-title></strong><button type="button" class="quiet" data-remove-trigger>Remove</button></header><label>Source<select name="source">${Object.entries(automationSources).map(([key,label])=>`<option value="${key}" ${key===provider?'selected':''}>${label}</option>`).join('')}</select></label><div data-source-fields></div>`;
    const fields=card.querySelector('[data-source-fields]'),source=card.querySelector('[name=source]');
    function draw(value) {
      fields.innerHTML=source.value==='schedule'?automationScheduleEditor(value.schedule||automationDefaultSchedule()):automationEventEditor(source.value,value.event||{});
      const frequency=card.querySelector('[name=frequency]');
      if(frequency)frequency.onchange=()=>{const current=readAutomationTrigger(card);draw(current);};
      const event=card.querySelector('[name=event]');
      function eventFields() {
        if(source.value==='github') {
          const kind=event.value.split('.')[0];
          for(const [name,show] of [['conclusion',kind==='check_run'],['label',!['check_run','push'].includes(kind)],['branch',kind.startsWith('pull_request')||['check_run','push'].includes(kind)]]) {
            const el=card.querySelector(`[name="${name}"]`);el.disabled=!show;el.closest('label').hidden=!show;
          }
        }
        if(source.value==='slack') {
          const reaction=event.value==='reaction.added';
          for(const name of ['text_contains','text_starts_with','sender_type','include_thread_replies','reaction']) {
            const el=card.querySelector(`[name="${name}"]`),hidden=name==='reaction'?!reaction:reaction;
            el.disabled=hidden;el.closest('label').hidden=hidden;
            if(name==='reaction')el.required=reaction;
          }
        }
        refresh();
      }
      if(event)event.onchange=eventFields;
      eventFields();
    }
    source.onchange=()=>draw({});
    card.querySelector('[data-remove-trigger]').onclick=()=>{card.remove();refresh();};
    host.append(card);draw(t);
  }
  automationTriggers(d).forEach(add);
  form.querySelector('[data-add-trigger]').onclick=()=>add();
}
function automationDeliveries(a,opened) {
  if(!automationTriggers(a.definition).some(t=>t.event))return '';
  const rows=a.trigger.deliveries||[],key='events:'+a.id;
  return `<details class="automation-history" data-history="${key}" ${opened.has(key)?'open':''}><summary>Event deliveries <span>${rows.filter(r=>r.status==='pending').length} queued</span></summary>${rows.length?rows.map(r=>`<div class="automation-history-row"><span>${new Date(r.received_at).toLocaleString()}</span><span>${esc(r.status==='pending'?'Queued':r.status)}</span>${r.run_id?`<a href="#run=${r.run_id}">Open session ↗</a>`:`<span>${esc(r.detail)}</span>`}</div>`).join(''):'<p>No events received yet. Test filters with a sample, then enable the automation.</p>'}</details>`;
}
function automationTriggerDialog(title,description,body) {
  const dialog=$('#automation-dialog');
  dialog.innerHTML=`<form class="automation-form"><header><div><h2>${esc(title)}</h2><p>${esc(description)}</p></div><button type="button" class="icon-button" data-close aria-label="Close trigger setup">×</button></header>${body}<p data-error role="alert"></p></form>`;
  dialog.querySelector('[data-close]').onclick=()=>dialog.close();
  dialog.onclose=()=>{dialog.innerHTML='';};
  dialog.showModal();return dialog.querySelector('form');
}
const automationSetupNotes={
  github:'Add this URL in the repository’s Webhooks settings with application/json. Select the events used by your triggers and enter the generated secret. Signature: X-Hub-Signature-256.',
  gitlab:'Add this URL in the project’s Webhooks settings. Paste its whsec_ signing token below to use Standard Webhooks, or generate a legacy secret token and enter it in GitLab. Select the events used by your triggers.',
  linear:'Create an Issues webhook for your team in Linear → Settings → API → Webhooks. Paste its signing secret below.',
  jira:'Register this URL as a Jira webhook for the selected events and configure its secret. Moyai verifies X-Hub-Signature with HMAC-SHA256.',
  pagerduty:'Create a V3 webhook subscription for the selected incident events and paste its signing secret below. Moyai verifies X-PagerDuty-Signature.',
  pylon:'In Pylon Settings → Webhooks, add this URL and Authorization: Bearer <secret> or X-Webhook-Secret. Then create a Pylon Trigger with the matching kickoff and Send webhook action. Template the body with event_type (issue.created, issue.tag_added, or issue.status_changed) and data: {id, title, description, status, tags, url}. Include event_id or occurred_at in the body so separate changes to the same issue remain distinct; keep it stable on retries.',
  webhook:'Send a JSON object using Authorization: Bearer <secret> or X-Webhook-Secret. Include a unique X-Moyai-Event-Id and reuse it on retries. Without an ID, identical payloads deduplicate. You can also use the timestamped HMAC format documented below.'
};
function automationWebhookProviders(a) {return a.trigger.providers.filter(p=>!['slack','session'].includes(p.provider));}
function setupAutomationWebhook(a) {
  const providers=automationWebhookProviders(a);
  if(!providers.length)return;
  const form=automationTriggerDialog('Connect event sources',a.definition.name,`<label>Provider<select name="provider">${providers.map(p=>`<option value="${p.provider}">${automationSources[p.provider]}${p.ready?' · configured':''}</option>`).join('')}</select></label><div data-provider-setup></div><footer><button type="submit" class="primary">Save webhook secret</button></footer>`);
  let revision=a.revision;
  function draw() {
    const provider=form.elements.provider.value,entry=providers.find(p=>p.provider===provider),required=['linear','pagerduty'].includes(provider);
    form.querySelector('[data-provider-setup]').innerHTML=`<label>Webhook URL<input readonly value="${esc(entry.url)}"></label><p class="automation-policy">${esc(automationSetupNotes[provider])}</p><label>Signing secret ${required?'':'(leave blank to generate)'}<input name="secret" type="password" autocomplete="new-password" ${required?'required':''} minlength="16" maxlength="512"></label>${provider==='webhook'?'<p class="automation-policy">HMAC alternative: X-Moyai-Event-Id, X-Moyai-Timestamp (Unix seconds), X-Moyai-Signature: sha256=&lt;hex digest&gt;. Sign timestamp + "." + eventId + "." + rawBody with HMAC-SHA256.</p>':''}<p class="automation-policy">Saving a secret pauses this automation. Enable it after setup. Replacing a secret disables the old one.</p><div data-secret-result></div>`;
    form.querySelector('[type=submit]').disabled=false;
  }
  form.elements.provider.onchange=draw;draw();
  const version=state.pageVersion;
  form.onsubmit=async event=>{
    event.preventDefault();const button=form.querySelector('[type=submit]');button.disabled=true;form.querySelector('[data-error]').textContent='';
    try{
      const result=await api(`/api/automations/${a.id}/webhook`,{method:'POST',body:JSON.stringify({revision,provider:form.elements.provider.value,secret:form.elements.secret.value})});
      revision=result.revision;form.elements.secret.value='';
      form.querySelector('[data-secret-result]').innerHTML=result.secret?`<label>Secret (shown once)<input readonly type="password" value="${esc(result.secret)}" data-generated-secret></label><button type="button" class="quiet" data-copy-secret>Copy secret</button><p class="automation-policy">Store this in your webhook sender. Moyai keeps an encrypted copy.</p>`:'<p>Secret saved.</p>';
      const copy=form.querySelector('[data-copy-secret]');if(copy)copy.onclick=async()=>{try{await navigator.clipboard.writeText(result.secret);toast('Secret copied.');}catch{form.querySelector('[data-generated-secret]').select();toast('Select and copy the secret.');}};
      if(state.pageVersion===version)await renderAutomations();
    }catch(e){form.querySelector('[data-error]').textContent=e.message;button.disabled=false;}
  };
}
function testAutomationFilters(a) {
  const samples=a.trigger.providers.flatMap(p=>p.examples.map(e=>({...e,provider:p.provider})));
  const form=automationTriggerDialog('Test event filters','Checks all triggers for the chosen source without starting a session or charging for inference.',`<label>Sample<select name="sample">${samples.map((s,i)=>`<option value="${i}">${automationSources[s.provider]} · Trigger ${automationTriggers(a.definition).findIndex(t=>t.id===s.trigger_id)+1}</option>`).join('')}</select></label><label data-event-header>GitHub event header<input name="event_header"></label><label>Sample event JSON<textarea name="payload" rows="12" required></textarea></label><div data-test-result role="status"></div><footer><button type="submit" class="primary">Test filters</button></footer>`);
  function sample() {const s=samples[Number(form.elements.sample.value)];form.elements.payload.value=JSON.stringify(s.payload,null,2);form.elements.event_header.value=s.event_header;form.querySelector('[data-event-header]').hidden=s.provider!=='github';form.querySelector('[data-test-result]').textContent='';}
  form.elements.sample.onchange=sample;sample();
  form.onsubmit=async event=>{
    event.preventDefault();const button=form.querySelector('[type=submit]');button.disabled=true;form.querySelector('[data-error]').textContent='';
    try{const s=samples[Number(form.elements.sample.value)];const result=await api(`/api/automations/${a.id}/test-event`,{method:'POST',body:JSON.stringify({provider:s.provider,event_header:form.elements.event_header.value,payload:JSON.parse(form.elements.payload.value)})});form.querySelector('[data-test-result]').textContent=result.matches?`Matched ${result.context.matched_trigger_ids.length} trigger(s). This event would queue one run. No session was started.`:'No match. This event would be ignored. No session was started.';}
    catch(e){form.querySelector('[data-error]').textContent=e.message;}finally{button.disabled=false;}
  };
}
