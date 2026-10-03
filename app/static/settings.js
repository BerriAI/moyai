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
    {view:'runtime', title:'Runtime', description:'Check cloud setup and session limits.'},
    {view:'environments', title:'Environments', description:'Prepare repositories and workspace tools.', admin:true},
  ]},
  {id:'administration', title:'Administration', admin:true, items:[
    {view:'users', title:'Users', description:'Manage workspace members and roles.', admin:true},
    {view:'spend', title:'Spend', description:'See model usage and costs across your team.', admin:true},
  ]},
];
const settingsViews = new Set(['settings', ...settingsGroups.flatMap(group => group.items.map(item => item.view))]);

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
    <div class="page-heading"><div><h1>Settings</h1><p class="subtext">Manage your workflows, tools, and workspace.</p></div></div>
    <div class="settings-grid">${settingsGroups.filter(group => !group.admin || admin).map(group => `
      <section class="settings-group" aria-labelledby="settings-${group.id}">
        <h2 id="settings-${group.id}">${group.title}${group.admin ? '<span>Admin</span>' : ''}</h2>
        <div class="settings-links">${group.items.filter(item => !item.admin || admin).map(item => `
          <a class="settings-link" href="#${item.view}">
            <span class="settings-icon">${settingsIcon(item.view)}</span>
            <span class="settings-copy"><strong>${item.title}</strong><span>${item.description}</span></span>
            <span class="settings-arrow" aria-hidden="true">›</span>
          </a>`).join('')}</div>
      </section>`).join('')}</div>
  </section>`;
}
