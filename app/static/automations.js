let automationRefresh;
let automationEventChoices = {};
const automationRunKeys = new Map();
const automationFilters = {scope:'mine', search:'', status:'all'};
const automationRunStatus = r => r.status==='idle'?'Completed':(r.status||r.outcome).replaceAll('_',' ');
function automationTiming(t) {
  if(t.frequency==='once')return 'Once · '+new Date(t.run_at).toLocaleString();
  if(t.frequency==='cron')return t.cron+' · '+t.timezone;
  const days=['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
  return (t.frequency==='hourly'?`Every hour at :${t.time.slice(3)}`:t.frequency==='weekdays'?`Weekdays at ${t.time}`:t.frequency==='weekly'?`${days[t.weekday]} at ${t.time}`:`Every day at ${t.time}`)+` · ${t.timezone}`;
}

async function renderAutomations(background = false) {
  clearTimeout(automationRefresh);
  if (background && state.view !== 'automations') return;
  if (background && settingsInteractionActive()) {
    automationRefresh=setTimeout(()=>renderAutomations(true).catch(showError),5000);
    return;
  }
  const openHistory=new Set([...document.querySelectorAll('.automation-history[open],.automation-workflow[open]')].map(e=>e.dataset.history));
  const version=state.pageVersion;
  const data=await api('/api/automations');
  if(version!==state.pageVersion)return;
  automationEventChoices=data.event_choices||{};
  const content=$('#content');
  content.innerHTML=`<section class="automations-page"><div class="page-heading"><div><h1>Automations</h1><p class="subtext">Run a workflow on a schedule or when an event happens.</p></div>${automationCreateMenu()}</div>
    ${!data.enabled?'<p class="automation-notice">You can save workflows and test them now. Automatic runs require Temporal.</p>':!data.connected?'<p class="automation-notice">Reconnecting to the worker. Received events and workflows stay saved.</p>':''}
    <div class="automation-discover"><div><strong>Let Moyai find new ways to help</strong><p>Turn repetitive work into an automation you can review and enable.</p></div><button id="suggest-automation">Suggest for me</button></div>
    <div class="settings-toolbar automation-toolbar"><div class="automation-scopes" role="group" aria-label="Automation ownership"><button data-automation-scope="mine">Mine <span>${data.automations.filter(a=>a.can_edit).length}</span></button><button data-automation-scope="all">All <span>${data.automations.length}</span></button></div><label class="sr-only" for="automation-search">Search automations</label><input type="search" id="automation-search" placeholder="Search automations…"><label class="sr-only" for="automation-status">Automation status</label><select id="automation-status"><option value="all">All statuses</option><option value="enabled">Enabled</option><option value="paused">Paused</option></select><span data-automation-count class="settings-result-count" role="status"></span></div>
    <div class="automation-list">${data.automations.length?data.automations.map(a=>{
      const d=a.definition,last=a.history[0],synced=a.revision===a.synced_revision&&!a.schedule_sync_pending,hasEvents=automationTriggers(d).some(t=>t.event),done=automationTriggers(d).every(t=>(a.completed_triggers||[]).includes(t.id));
      return `<article class="automation-card" data-automation-id="${a.id}"><header><div><h2>${esc(d.name)}</h2><p>${esc(automationTriggerSummary(d,a.completed_triggers||[]))}</p></div><span class="badge ${a.environment_blocker?'settings-status-warning':''}">${a.paused?'Paused':done?'Completed':a.environment_blocker?'Blocked':!synced?'Syncing':hasEvents?'Listening':'Scheduled'}</span></header><details class="automation-workflow" data-history="workflow-${a.id}" ${openHistory.has('workflow-'+a.id)?'open':''}><summary>Workflow instructions</summary><p class="automation-prompt">${esc(d.prompt)}</p></details><div class="automation-meta"><span>Runs as ${esc(a.owner)}</span><span>${esc(modelName(d.model))}</span>${d.mode==='demo'?'<span>Simulated preview</span>':''}</div>
      ${a.sync_error?`<p role="status" class="automation-notice">${esc(a.sync_error)}</p>`:''}
      ${a.environment_blocker?`<p role="status" class="automation-notice">${esc(a.environment_blocker)} ${hasEvents&&!a.paused?'New events stay queued for up to 24 hours.':''} <a href="#environments">View environments</a></p>`:''}
      <footer><span>${last?`Last run: ${esc(automationRunStatus(last))}`:'No runs yet'}</span><div>${a.can_edit?`<button class="quiet" data-edit-automation="${a.id}">Edit</button><button class="quiet" data-run-automation="${a.id}">Run now</button>${hasEvents?`<button class="quiet" data-test-event="${a.id}">Test filters</button>${automationWebhookProviders(a).length?`<button class="quiet" data-setup-event="${a.id}">${a.trigger.ready?'Webhook settings':'Set up webhook'}</button>`:''}`:''}`:''}${a.can_edit||(state.role==='admin'&&!a.paused)?`<button class="quiet" data-toggle-automation="${a.id}" ${a.paused&&!data.enabled?'disabled':''}>${a.paused?'Enable':'Pause'}</button>`:''}</div></footer>
      <details class="automation-history" data-history="${a.id}" ${openHistory.has(a.id)?'open':''}><summary>Run history <span>${a.history.length}</span></summary>${a.history.length?a.history.map(r=>`<div class="automation-history-row"><span>${new Date(r.created_at).toLocaleString()}</span><span>${esc(automationRunStatus(r))}</span>${r.run_id?`<a href="#run=${r.run_id}">Open session ↗</a>`:`<span>${esc(r.detail)}</span>`}</div>`).join(''):'<p>No runs yet. Use Run now to test the workflow.</p>'}</details>${automationDeliveries(a,openHistory)}</article>`;
    }).join(''):'<div class="automation-empty"><span aria-hidden="true">◷</span><h2>Choose what starts the work</h2><p>Start with your Linear tickets, or write a workflow of your own.</p><p>Schedules and event triggers start paused so you can review them first.</p></div>'}</div><div class="settings-empty" data-automation-empty hidden><h2>No matching automations</h2><p>Try another search or show all automations.</p><button data-clear-automations>Clear filters</button></div></section>`;
  bindAutomationLibrary(content,data);
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
  if(data.enabled&&data.automations.some(a=>a.environment_blocker||a.schedule_sync_pending||a.revision!==a.synced_revision||a.trigger?.deliveries?.some(e=>e.status==='pending')||a.history.some(r=>r.run_id&&!['idle','completed','failed','cancelled','interrupted'].includes(r.status))))automationRefresh=setTimeout(()=>renderAutomations(true).catch(showError),5000);
}
