const settingsGroups = [
  {id:'workflows', title:'Workflows', items:[
    ...(typeof renderAutomations === 'function' ? [{view:'automations', title:'Automations', description:'Schedule recurring work and review past runs.'}] : []),
    {view:'skills', title:'Skills', description:'Save instructions for yourself or your team.'},
    {view:'memory', title:'Memory', description:'Review what Moyai remembers for your future sessions.'},
  ]},
  {id:'integrations', title:'Integrations', items:[
    {view:'connections', title:'Connections', description:'Connect GitHub, Slack, Linear, and Notion.'},
    {view:'secrets', title:'Secrets', description:'Manage personal and shared service access.'},
  ]},
  {id:'workspace', title:'Workspace', items:[
    {view:'spend', title:'Spend', description:'See your own LLM usage and costs.', adminDescription:'See model usage and costs across your team.'},
    {view:'runtime', title:'Runtime', description:'Check cloud setup and session limits.'},
    {view:'environments', title:'Environments', description:'Prepare repositories and workspace tools.', admin:true},
  ]},
  {id:'administration', title:'Administration', admin:true, items:[
    {view:'users', title:'Users', description:'Manage workspace members and roles.', admin:true},
    {view:'adoption', title:'Usage analytics', description:'Track human requests and active teammates over time.', admin:true},
  ]},
];
const settingsViews = new Set(['settings', ...settingsGroups.flatMap(group => group.items.map(item => item.view))]);

// The same destinations drive the overview and the persistent settings rail.
function settingsNavigation(view, role) {
  const link = (target, title, icon) => `<a href="#${target}"${view === target ? ' aria-current="page"' : ''}>${icon ? settingsIcon(target) : '<span class="settings-overview-icon" aria-hidden="true">⊞</span>'}<span>${title}</span></a>`;
  return `<a class="settings-back" href="#tasks"><span aria-hidden="true">←</span> Back to workspace</a>
    <div class="settings-nav-title">Settings</div>${link('settings', 'Overview')}
    ${settingsGroups.filter(group => !group.admin || role === 'admin').map(group => `<div class="settings-nav-group"><h2>${group.title}</h2>${group.items.filter(item => !item.admin || role === 'admin').map(item => link(item.view, item.title, true)).join('')}</div>`).join('')}`;
}

function updateSettingsNavigation() {
  const nav = $('#settings-navigation');
  if (nav) nav.innerHTML = settingsNavigation(state.view, state.role);
}

function settingsLoadError(title, error, retry) {
  $('#content').innerHTML = `<section class="settings-load-error"><h1>${esc(title)}</h1><div role="alert"><h2>Unable to load this page</h2><p>${esc(error.message)}</p><p>Check your connection and try again.</p></div><button id="settings-retry">Try again</button></section>`;
  $('#settings-retry').onclick = retry;
}

function confirmSettingsAction(title, description, action) {
  return new Promise(resolve => {
    const dialog = document.createElement('dialog');
    dialog.setAttribute('aria-label', title);
    dialog.innerHTML = `<h2>${esc(title)}</h2><p>${esc(description)}</p><div class="credential-actions"><button class="quiet" data-cancel>Cancel</button><button class="danger" data-confirm>${esc(action)}</button></div>`;
    document.body.append(dialog);
    dialog.querySelector('[data-cancel]').onclick = () => dialog.close();
    dialog.querySelector('[data-confirm]').onclick = () => dialog.close('confirmed');
    dialog.addEventListener('close', () => {
      const confirmed = dialog.returnValue === 'confirmed';
      dialog.remove();
      resolve(confirmed);
    }, {once:true});
    dialog.showModal();
    dialog.querySelector('[data-cancel]').focus();
  });
}

// Background polling must not replace the control someone is using.
function settingsInteractionActive() {
  return !!document.querySelector('dialog[open]') ||
    !!(document.querySelector('#content')?.contains(document.activeElement) &&
      document.activeElement?.matches('button,a,input,select,textarea,summary,[tabindex="0"]'));
}

// Filtering hides existing rows so their action handlers and disclosure state survive.
const settingsFilterState = new Map();
function bindSettingsFilter({input, select, rows, count, empty}) {
  const search = document.querySelector(input), filter = select && document.querySelector(select);
  const items = [...document.querySelectorAll(rows)];
  const saved = settingsFilterState.get(input);
  if (saved) { search.value = saved.query; if (filter) filter.value = saved.filter; }
  const draw = () => {
    settingsFilterState.set(input, {query:search.value, filter:filter?.value || ''});
    const query = search.value.trim().toLocaleLowerCase();
    let visible = 0;
    for (const item of items) {
      const matches = item.textContent.toLocaleLowerCase().includes(query) && (!filter || !filter.value || item.dataset.filter === filter.value);
      item.hidden = !matches;
      if (matches) visible++;
    }
    document.querySelector(count).textContent = `${visible} of ${items.length}`;
    document.querySelector(empty).hidden = visible > 0 || (!query && !filter?.value);
  };
  search.oninput = draw;
  if (filter) filter.onchange = draw;
  document.querySelector(empty)?.querySelector('button')?.addEventListener('click', () => {
    search.value = ''; if (filter) filter.value = ''; draw(); search.focus();
  });
  draw();
}

function settingsIcon(view) {
  const paths = {
    automations:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    skills:'<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z"/>',
    memory:'<path d="M8 3h10v18H8a3 3 0 0 1-3-3V6a3 3 0 0 1 3-3Zm0 0v18M11 8h4m-4 4h4m-4 4h2"/>',
    connections:'<path d="m10 13 4-4M8 15l-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m2 3 1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0" transform="translate(1 0)"/>',
    secrets:'<circle cx="8" cy="9" r="5"/><path d="m12 13 8 8m-4-4 3-3m-6 3 3-3"/>',
    runtime:'<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4M7 8l3 3-3 3m6 0h4"/>',
    environments:'<path d="M3 8h18v12H3Zm0 0V4h7l3 4m-5 6 2 2-2 2m5-2h4"/>',
    users:'<circle cx="9" cy="8" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3m2-15a3 3 0 0 1 0 6m4 9v-3a6 6 0 0 0-3-5"/>',
    spend:'<path d="M4 20V10m6 10V4m6 16v-7m5 7H2"/>',
    adoption:'<path d="m3 17 6-6 4 4L21 7m-6 0h6v6"/>',
  };
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[view] || ''}</svg>`;
}

async function renderSettings() {
  const version = state.pageVersion;
  $('#content').innerHTML = '<p class="subtext" role="status">Loading settings…</p>';
  const session = await api('/api/session');
  if (version !== state.pageVersion) return;
  applyUserSession(session);
  if (!session.authenticated) { await boot(); return; }
  const admin = state.role === 'admin';
  $('#content').innerHTML = `<section class="settings-page">
    <div class="page-heading"><div><h1>Settings</h1><p class="subtext">Make Moyai work the way your team does.</p></div></div>
    <section class="settings-group" aria-labelledby="personal-preferences-title">
      <h2 id="personal-preferences-title">Personal preferences</h2>
      <div class="chat-preference">
        <label for="send-immediately"><input id="send-immediately" type="checkbox" ${state.preferences.send_immediately?'checked':''} aria-describedby="send-immediately-description send-immediately-status"><span>Send messages immediately</span></label>
        <p id="send-immediately-description" class="subtext">While Moyai is working, send follow-ups into the active response instead of queueing them. Saved for your account across chats.${session.identity?'':' Shared password and local sign-ins use a shared profile.'}</p>
        <p id="send-immediately-status" class="subtext" role="status">${state.preferences.send_immediately?'On · Enter and the send button send immediately.':'Off · Follow-ups queue while Moyai is working.'}</p>
      </div>
    </section>
    <div class="settings-grid">${settingsGroups.filter(group => !group.admin || admin).map(group => `
      <section class="settings-group" aria-labelledby="settings-${group.id}">
        <h2 id="settings-${group.id}">${group.title}${group.admin ? '<span>Admin</span>' : ''}</h2>
        <div class="settings-links">${group.items.filter(item => !item.admin || admin).map(item => `
          <a class="settings-link" href="#${item.view}">
            <span class="settings-icon">${settingsIcon(item.view)}</span>
            <span class="settings-copy"><strong>${item.title}</strong><span>${admin && item.adminDescription || item.description}</span></span>
            <span class="settings-arrow" aria-hidden="true">›</span>
          </a>`).join('')}</div>
      </section>`).join('')}</div>
    <section class="card settings-form-section" id="title-model-settings"><div><h2>Session titles</h2><p class="subtext">Choose the model that names new chats. Existing titles and the chat model stay the same.</p></div><form id="title-model-form"><label for="title-model">Gateway model ID</label><input id="title-model" maxlength="200" required placeholder="openai/gpt-4.1-nano" aria-describedby="title-model-status" ${admin?'':'disabled'}><button type="submit" ${admin?'':'disabled'}>Save model</button><p id="title-model-status" role="status">Loading…</p></form></section>
  </section>`;
  const preferenceInput=$('#send-immediately'),preferenceStatus=$('#send-immediately-status'),userId=state.userId;
  preferenceInput.onchange=async()=>{
    const previous=state.preferences.send_immediately;
    preferenceInput.disabled=true;preferenceStatus.textContent='Saving…';
    try{
      const saved=await api('/api/settings/preferences',{method:'PUT',body:JSON.stringify({send_immediately:preferenceInput.checked})});
      if(state.userId===userId)state.preferences=saved;
      if(version!==state.pageVersion)return;
      preferenceInput.checked=saved.send_immediately;
      preferenceStatus.textContent=saved.send_immediately?'Saved · Messages now go straight into the active response.':'Saved · Follow-ups will queue while Moyai is working.';
    }catch(error){
      if(version!==state.pageVersion)return;
      preferenceInput.checked=previous;
      preferenceStatus.textContent='Could not confirm the change. '+error.message+' Reload settings to check, or try again.';
    }finally{if(version===state.pageVersion)preferenceInput.disabled=false;}
  };
  try{
    const saved=await api('/api/settings/session-titles');if(version!==state.pageVersion)return;
    $('#title-model').value=saved.model;
    $('#title-model-status').textContent=!saved.enabled?'Title generation is disabled by the server.':!saved.gateway_configured?'Gateway access must be configured on the server.':admin?'Enter the exact model ID enabled on your gateway.':'Only administrators can change the workspace title model.';
    $('#title-model-form').onsubmit=async event=>{
      event.preventDefault();const button=event.currentTarget.querySelector('button');button.disabled=true;
      try{
        await api('/api/settings/session-titles',{method:'PUT',body:JSON.stringify({model:$('#title-model').value.trim()})});
        const confirmed=await api('/api/settings/session-titles');if(version!==state.pageVersion)return;
        $('#title-model').value=confirmed.model;$('#title-model-status').textContent='Saved. New title attempts use '+confirmed.model+'.';
      }catch(error){if(version===state.pageVersion)$('#title-model-status').textContent=error.message;}
      finally{if(version===state.pageVersion)button.disabled=false;}
    };
  }catch(error){if(version===state.pageVersion)$('#title-model-status').textContent=error.message;}
}
