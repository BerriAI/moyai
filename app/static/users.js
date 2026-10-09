const userDirectoryState = {query:'', filter:'all'};
const userRoleLabel = role => role === 'admin' ? 'Admin' : 'Internal user';

function userRows(users, query, filter) {
  const needle = query.trim().toLowerCase();
  return users.filter(user => (filter === 'all' || user.role === filter) &&
    `${user.name} ${user.email}`.toLowerCase().includes(needle));
}

function userTable(users, query, filter) {
  const rows = userRows(users, query, filter);
  return rows.length ? `<table class="users-table"><thead><tr><th scope="col">User</th><th scope="col">Role</th><th scope="col">Workspace access</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead><tbody>${rows.map(user => `<tr>
    <td><div class="user-person"><span class="user-avatar" aria-hidden="true">${esc((user.name || user.email).slice(0,1).toUpperCase())}</span><div><strong>${esc(user.name || user.email.split('@')[0])}${user.email === state.identity?.email ? ' <span class="user-you">You</span>' : ''}</strong><span>${esc(user.email)}</span></div></div></td>
    <td><span class="user-role ${user.role === 'admin' ? 'is-admin' : ''}">${userRoleLabel(user.role)}</span></td>
    <td><span class="user-access">${user.has_signed_in ? 'Signed in with Google' : 'Awaiting first sign-in'}</span></td>
    <td><button class="quiet small" data-edit-user="${esc(user.email)}" aria-label="Change role for ${esc(user.email)}">Change role</button></td>
  </tr>`).join('')}</tbody></table>` : '<div class="users-empty">No users match your search.</div>';
}

function applyUserSession(session) {
  const changed=state.role!==(session.role||'member')||state.userId!==session.user_id||state.authenticated!==session.authenticated;
  state.role = session.role || 'member'; state.identity = session.identity; state.csrf = session.csrf;
  state.authenticated = session.authenticated; state.userId = session.user_id;
  state.preferences = {send_immediately: session.preferences?.send_immediately === true,
    omit_private_tool_payloads: session.preferences?.omit_private_tool_payloads === true};
  restoreSessionScope();
  if(changed){++state.runsRefresh;state.runs=[];state.folders=[];renderSidebar();}
  if (session.identity) $('.rail-foot small').title = state.role === 'admin' ? 'Organization admin' : 'Internal user';
}

async function renderUsers() {
  const version = state.pageVersion;
  const session = await api('/api/session');
  if (version !== state.pageVersion) return;
  applyUserSession(session);
  if (state.role !== 'admin') {
    MoyaiUI.render($('#content'), '<div class="page-heading"><div><h1>Users</h1><p class="subtext">Only administrators can manage workspace roles.</p></div></div>');
    return;
  }
  const data = await api('/api/admin/users');
  if (version !== state.pageVersion) return;
  const admins = data.users.filter(user => user.role === 'admin').length;
  MoyaiUI.render($('#content'), `<section class="users-page">
    <div class="page-heading"><div><h1>Users</h1><p class="subtext">Manage workspace members and their access.</p></div><button class="primary" id="add-user">Add user</button></div>
    <div class="users-overview"><span><strong>${data.users.length}</strong> users</span><span><strong>${admins}</strong> admins</span><span><strong>${data.users.length - admins}</strong> internal users</span></div>
    <div class="users-panel"><div class="users-toolbar"><label class="users-search"><span class="sr-only">Search users</span><input id="user-search" type="search" placeholder="Search by name or email" autocomplete="off"></label><label><span class="sr-only">Filter by role</span><select id="user-role-filter"><option value="all">All roles</option><option value="admin">Admins</option><option value="member">Internal users</option></select></label></div><div id="user-table" class="users-table-scroll" role="region" aria-label="Workspace users" tabindex="0"></div><p class="users-domain">Sign-in is restricted to ${data.domains.map(domain => '@' + esc(domain)).join(', ')} Google accounts. New teammates join as internal users by default.</p></div>
    <div class="user-role-guide"><div><h2>Admin</h2><p>Manage users, shared connections and organization settings. View team spend and manage allowed actions.</p></div><div><h2>Internal user</h2><p>Start sessions, chat with agents and use shared tools and skills. Create Linear tickets and GitHub PRs directly. Access follows each connection’s permissions.</p></div></div>
    <details class="user-history"><summary>Role activity <span>${data.activity.length ? 'Recent changes' : 'No changes yet'}</span></summary><ul>${data.activity.map(entry => `<li><div><strong>${esc(entry.email)}</strong><span>${entry.previous_role ? userRoleLabel(entry.previous_role) + ' → ' : 'Assigned '}${userRoleLabel(entry.role)}</span><small>By ${esc(entry.actor)}</small></div><time datetime="${esc(entry.created_at)}">${esc(new Date(entry.created_at).toLocaleString())}</time></li>`).join('') || '<li class="subtext">Role changes will appear here with the administrator who made them.</li>'}</ul></details>
  </section>`);
  $('#user-search').value = userDirectoryState.query;
  $('#user-role-filter').value = userDirectoryState.filter;
  const redraw = () => {
    MoyaiUI.render($('#user-table'), userTable(data.users, userDirectoryState.query, userDirectoryState.filter));
    document.querySelectorAll('[data-edit-user]').forEach(button => button.onclick = () =>
      editUserRole(data, data.users.find(user => user.email === button.dataset.editUser)));
  };
  $('#user-search').oninput = event => {userDirectoryState.query = event.target.value; redraw();};
  $('#user-role-filter').onchange = event => {userDirectoryState.filter = event.target.value; redraw();};
  $('#add-user').onclick = () => editUserRole(data);
  redraw();
}

function editUserRole(data, user) {
  const dialog = $('#user-role-dialog');
  const lastAdmin = user?.role === 'admin' && data.users.filter(row => row.role === 'admin').length === 1;
  MoyaiUI.render(dialog, `<form id="user-role-form"><button class="dialog-close" type="button" aria-label="Close">×</button><div class="eyebrow">WORKSPACE ACCESS</div><h2>${user ? 'Change role' : 'Add a user'}</h2><p class="subtext">${user ? 'Changes apply on their next request. No sign-out or redeploy needed.' : 'Set a role before their first sign-in. They will still need a verified work Google account; no invitation email is sent.'}</p>
    <div class="field"><label for="role-email">Work email</label><input id="role-email" type="email" maxlength="254" required autocomplete="off" ${user ? 'readonly' : ''} placeholder="name@${esc(data.domains[0] || 'company.com')}" value="${esc(user?.email || '')}"></div>
    <div class="field"><label for="role-choice">Role</label><select id="role-choice"><option value="member" ${lastAdmin ? 'disabled' : ''}>Internal user</option><option value="admin">Admin</option></select></div>
    <p id="role-description" class="role-description"></p>${lastAdmin ? '<p class="role-notice">This is the last admin. Promote another user before changing this role.</p>' : ''}<p id="role-error" class="signin-error" role="alert"></p><div class="role-actions"><button type="button" id="role-cancel">Cancel</button><button type="submit" class="primary">${user ? 'Save role' : 'Add user'}</button></div></form>`);
  $('#role-choice').value = user?.role || 'member';
  const describe = () => {$('#role-description').textContent = $('#role-choice').value === 'admin' ?
    'Can manage workspace users and shared connections, view team spend, and manage allowed actions.' :
    'Can use the workspace and shared tools. Cannot manage workspace users or shared connection settings.';};
  $('#role-choice').onchange = describe; describe();
  dialog.querySelector('.dialog-close').onclick = () => dialog.close();
  $('#role-cancel').onclick = () => dialog.close();
  $('#user-role-form').onsubmit = async event => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('[type="submit"]'); button.disabled = true;
    const email = $('#role-email').value.trim().toLowerCase();
    const existing = data.users.find(row => row.email === email);
    try {
      await api('/api/admin/users/role', {method:'PUT', body:JSON.stringify({email, role:$('#role-choice').value, revision:existing?.revision || 0})});
    } catch (error) {
      $('#role-error').textContent = error.message; button.disabled = false; return;
    }
    dialog.close();
    try {
      applyUserSession(await api('/api/session'));
      if (state.role !== 'admin') await navigate('tasks'); else await renderUsers();
      toast('Workspace role saved.');
    } catch (error) {toast('Role saved. Refresh the page to see the updated user list.');}
  };
  dialog.showModal();
}
