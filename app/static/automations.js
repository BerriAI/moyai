let automationRefresh;
let automationEventChoices = {};
const automationRunKeys = new Map();
const automationRunStatus = r => r.status==='idle'?'Completed':(r.status||r.outcome).replaceAll('_',' ');
function automationTiming(t) {
  if(t.frequency==='once')return 'Once · '+new Date(t.run_at).toLocaleString();
  if(t.frequency==='cron')return t.cron+' · '+t.timezone;
  const days=['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
  return (t.frequency==='hourly'?`Every hour at :${t.time.slice(3)}`:t.frequency==='weekdays'?`Weekdays at ${t.time}`:t.frequency==='weekly'?`${days[t.weekday]} at ${t.time}`:`Every day at ${t.time}`)+` · ${t.timezone}`;
}

async function renderAutomations() {
  clearTimeout(automationRefresh);
  const openHistory=new Set([...document.querySelectorAll('.automation-history[open]')].map(e=>e.dataset.history));
  const version=state.pageVersion;
  const data=await api('/api/automations');
  if(version!==state.pageVersion)return;
  automationEventChoices=data.event_choices||{};
  const content=$('#content');
  content.innerHTML=`<section class="automations-page"><div class="page-heading"><div><div class="eyebrow">RECURRING WORK</div><h1>Automations</h1><p class="subtext">Run a workflow on a schedule or when an event happens.</p></div><button class="primary" id="new-automation">＋ New automation</button></div>
    ${!data.enabled?'<p class="automation-notice">You can save workflows and test them now. Automatic runs require Temporal.</p>':!data.connected?'<p class="automation-notice">Reconnecting to the worker. Received events and workflows stay saved.</p>':''}
    <div class="automation-template"><div><span aria-hidden="true">↗</span><div><strong>My Linear tickets → PR</strong><p>Pick a ticket, implement a fix, run tests, and prepare a PR.</p></div></div><button class="quiet" id="linear-automation">Use template</button></div>
    <div class="automation-list">${data.automations.length?data.automations.map(a=>{
      const d=a.definition,last=a.history[0],synced=a.revision===a.synced_revision,hasEvents=automationTriggers(d).some(t=>t.event),done=automationTriggers(d).every(t=>(a.completed_triggers||[]).includes(t.id));
      return `<article class="automation-card"><header><div><h2>${esc(d.name)}</h2><p>${esc(automationTriggerSummary(d,a.completed_triggers||[]))}</p></div><span class="badge">${a.paused?'Paused':done?'Completed':a.environment_blocker?'Blocked':!synced?'Syncing':hasEvents?'Listening':'Scheduled'}</span></header><p class="automation-prompt">${esc(d.prompt)}</p><div class="automation-meta"><span>Runs as ${esc(a.owner)}</span><span>${esc(modelName(d.model))}</span>${d.mode==='demo'?'<span>Simulated preview</span>':''}</div>
      ${a.sync_error?`<p role="status" class="automation-notice">${esc(a.sync_error)}</p>`:''}
      ${a.environment_blocker?`<p role="status" class="automation-notice">${esc(a.environment_blocker)} ${hasEvents&&!a.paused?'New events stay queued for up to 24 hours.':''} <a href="#environments">View environments</a></p>`:''}
      <footer><span>${last?`Last run: ${esc(automationRunStatus(last))}`:'No runs yet'}</span><div>${a.can_edit?`<button class="quiet" data-edit-automation="${a.id}">Edit</button><button class="quiet" data-run-automation="${a.id}">Run now</button>${hasEvents?`<button class="quiet" data-test-event="${a.id}">Test filters</button>${automationWebhookProviders(a).length?`<button class="quiet" data-setup-event="${a.id}">${a.trigger.ready?'Webhook settings':'Set up webhook'}</button>`:''}`:''}`:''}${a.can_edit||(state.role==='admin'&&!a.paused)?`<button class="quiet" data-toggle-automation="${a.id}" ${a.paused&&!data.enabled?'disabled':''}>${a.paused?'Enable':'Pause'}</button>`:''}</div></footer>
      <details class="automation-history" data-history="${a.id}" ${openHistory.has(a.id)?'open':''}><summary>Run history <span>${a.history.length}</span></summary>${a.history.length?a.history.map(r=>`<div class="automation-history-row"><span>${new Date(r.created_at).toLocaleString()}</span><span>${esc(automationRunStatus(r))}</span>${r.run_id?`<a href="#run=${r.run_id}">Open session ↗</a>`:`<span>${esc(r.detail)}</span>`}</div>`).join(''):'<p>No runs yet. Use Run now to test the workflow.</p>'}</details>${automationDeliveries(a,openHistory)}</article>`;
    }).join(''):'<div class="automation-empty"><span aria-hidden="true">◷</span><h2>Choose what starts the work</h2><p>Start with your Linear tickets, or write a workflow of your own.</p><p>Schedules and event triggers start paused so you can review them first.</p></div>'}</div></section>`;
  $('#new-automation').onclick=()=>editAutomation(null,null);
  $('#linear-automation').onclick=()=>editAutomation(null,data.templates[0]);
  content.querySelectorAll('[data-edit-automation]').forEach(b=>b.onclick=()=>editAutomation(data.automations.find(a=>a.id===b.dataset.editAutomation)));
  content.querySelectorAll('[data-toggle-automation]').forEach(b=>b.onclick=async()=>{
    const a=data.automations.find(a=>a.id===b.dataset.toggleAutomation);b.disabled=true;
    try{await api(`/api/automations/${a.id}/state`,{method:'POST',body:JSON.stringify({revision:a.revision,paused:!a.paused})});if(state.pageVersion===version)await renderAutomations();}
    catch(e){toast(e.message);b.disabled=false;}
  });
  content.querySelectorAll('[data-run-automation]').forEach(b=>b.onclick=async()=>{
    const a=data.automations.find(a=>a.id===b.dataset.runAutomation);b.disabled=true;
    // Retain this ID on a transport error; a retry must not create another session.
    const key=a.id+':'+a.revision;if(!automationRunKeys.has(key))automationRunKeys.set(key,crypto.randomUUID());
    try{const r=await api(`/api/automations/${a.id}/run`,{method:'POST',body:JSON.stringify({revision:a.revision,client_id:automationRunKeys.get(key)})});automationRunKeys.delete(key);if(state.pageVersion!==version)return;if(r.run_id){await refreshRuns();await openRun(r.run_id);}else{toast('Run '+r.outcome+'. See the run history for details.');await renderAutomations();}}
    catch(e){toast(e.message);b.disabled=false;}
  });
  content.querySelectorAll('[data-setup-event]').forEach(b=>b.onclick=()=>setupAutomationWebhook(data.automations.find(a=>a.id===b.dataset.setupEvent)));
  content.querySelectorAll('[data-test-event]').forEach(b=>b.onclick=()=>testAutomationFilters(data.automations.find(a=>a.id===b.dataset.testEvent)));
  if(data.enabled&&data.automations.some(a=>a.environment_blocker||a.revision!==a.synced_revision||a.trigger?.deliveries?.some(e=>e.status==='pending')||a.history.some(r=>r.run_id&&!['idle','completed','failed','cancelled','interrupted'].includes(r.status))))automationRefresh=setTimeout(()=>{if(state.view==='automations'&&!$('#automation-dialog').open)renderAutomations().catch(showError);},5000);
}

async function editAutomation(existing,template) {
  const version=state.pageVersion;
  let connections,environments;
  try{[connections,environments]=await Promise.all([api('/api/connections'),api('/api/environments')]);}
  catch(e){toast(e.message);return;}
  if(state.pageVersion!==version)return;
  const d=existing?.definition||{name:template?.name||'',prompt:template?.prompt||'',repo_url:template?'https://github.com/BerriAI/litellm':'',mode:state.config.cloud_ready?'modal':'demo',model:state.config.model,harness:state.config.harness,plugins:template?.plugins||[],environment_id:'auto',triggers:[{id:crypto.randomUUID(),schedule:automationDefaultSchedule()}]};
  const dialog=$('#automation-dialog');
  dialog.onclose=()=>{dialog.innerHTML='';};
  dialog.innerHTML=`<form class="automation-form"><header><div><h2>${existing?'Edit automation':'New automation'}</h2><p>Runs as you, with your selected organization connections.</p></div><button type="button" class="icon-button" data-close aria-label="Close automation editor">×</button></header>
    <label>Name<input name="name" required minlength="2" maxlength="100" placeholder="Tackle my Linear tickets" value="${esc(d.name)}"></label>
    ${automationTriggerFields(d)}
    <label>Workflow<textarea name="prompt" required minlength="3" maxlength="14000" rows="8" placeholder="Tell Moyai what to do each time…">${esc(d.prompt)}</textarea></label>
    <label>Maximum runs per hour (shared by all triggers)<input name="max_runs_per_hour" type="number" min="1" placeholder="No limit" value="${d.max_runs_per_hour===null?'':d.max_runs_per_hour??50}"></label><p class="automation-policy">Default: 50 runs/hour, or 150 for Slack message watching. Clear the field for no per-automation cap. This limits new runs, not how long work can continue.</p>
    <div class="automation-fields"><label>Repository<input name="repo_url" type="url" placeholder="https://github.com/owner/repository" value="${esc(d.repo_url)}"></label><label>Agent harness<select name="harness">${(state.config.harnesses||[]).map(h=>`<option value="${esc(h.id)}" ${h.id===(d.harness||'hermes')?'selected':''}>${esc(h.name)}</option>`).join('')}</select></label><label>Model<select name="model">${harnessModels(d.harness||'hermes').map(m=>`<option value="${esc(m.id)}" ${m.id===d.model?'selected':''}>${esc(m.name)}</option>`).join('')}</select></label></div>
    <label>Project environment<select name="environment_id">${environmentOptions(environments,d.environment_id)}</select></label>
    <fieldset><legend>Organization connections</legend>${connections.map(c=>`<label class="automation-check"><input type="checkbox" name="plugin" value="${esc(c.id)}" ${d.plugins.includes(c.id)?'checked':''}>${esc(providerNames[c.id])}${!c.connected||!c.enabled?' · not connected':''}</label>`).join('')}</fieldset>
    <p class="automation-policy">Runs follow the same connection permissions and tool approval requirements as interactive sessions. Runs never approve or merge PRs. Scheduled runs skip overlap. Events queue until the previous run finishes, for up to 24 hours. Pausing stops future runs; stop an active run from its session.</p>
    <p class="automation-policy">${existing?'Saving edits pauses the automation.':'Saved automations start paused.'} Review and test before enabling. Event runs use your identity, connections, and spend; source content is visible to signed-in teammates. ${d.mode==='demo'?'This local preview runs a simulation without AI or cloud usage.':''}</p>
    <p data-error role="alert"></p><footer><button type="button" class="quiet" data-close>Cancel</button><button type="submit" class="primary">Save paused</button></footer></form>`;
  dialog.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>dialog.close());
  const form=dialog.querySelector('form');
  bindAutomationTrigger(form,d);
  form.elements.harness.onchange=()=>{
    const picker=form.elements.model,selected=picker.value;
    picker.innerHTML=harnessModels(form.elements.harness.value).map(m=>`<option value="${esc(m.id)}" ${m.id===selected?'selected':''}>${esc(m.name)}</option>`).join('');
  };
  form.onsubmit=async event=>{
    event.preventDefault();const button=form.querySelector('[type="submit"]');button.disabled=true;
    try {
      const f=new FormData(form),body={revision:existing?.revision||0,definition:{name:f.get('name'),prompt:f.get('prompt'),repo_url:f.get('repo_url'),github_repository_id:f.get('repo_url')===d.repo_url?d.github_repository_id:null,model:f.get('model'),harness:f.get('harness'),mode:d.mode,plugins:f.getAll('plugin'),environment_id:f.get('environment_id'),triggers:[...form.querySelector('[data-triggers]').children].map(readAutomationTrigger),max_runs_per_hour:f.get('max_runs_per_hour')===''?null:Number(f.get('max_runs_per_hour'))}};
      await api('/api/automations'+(existing?'/'+existing.id:''),{method:existing?'PUT':'POST',body:JSON.stringify(body)});dialog.close();if(state.pageVersion===version)await renderAutomations();toast('Saved paused. Review connections and test before enabling.');
    }
    catch(e){form.querySelector('[data-error]').textContent=e.message;button.disabled=false;}
  };
  dialog.showModal();form.elements.name.focus();
}
