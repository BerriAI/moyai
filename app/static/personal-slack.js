/* Requester-owned Slack settings. The server owns credential precedence and eligibility. */
async function renderPersonalSlack() {
  const version = state.pageVersion;
  const connection = await api('/api/connections/slack/personal');
  if (version !== state.pageVersion) return;
  const healthy = connection.connected && connection.health === 'healthy';
  const status = !connection.connected ? 'Not connected' : healthy ? 'Connected' : 'Needs attention';
  const source = {personal:'Your personal Slack account', organization:'Organization Slack connection', none:'No Slack connection'}[connection.effective_source] || 'Unavailable';
  const focusId = document.activeElement?.id;
  $('#content').innerHTML = `<section class="settings-page personal-slack-page">
    <div class="page-heading"><div><h1>Personal Slack</h1><p class="subtext">Optionally connect your own Slack account for conversations you can access.</p></div></div>
    <section class="card"><div class="section-header"><h2>Your connection</h2><span class="connection-state ${healthy?'healthy':connection.connected?'needs-attention':''}">${status}</span></div>
      <dl class="connection-facts"><div><dt>Account</dt><dd>${esc(connection.label || 'No personal account connected')}</dd></div><div><dt>Effective read identity</dt><dd>${esc(source)}</dd></div><div><dt>Health</dt><dd>${esc(connection.health || 'Not checked')}</dd></div></dl>
      <p>Your personal connection is preferred when configured. If it fails, Moyai asks you to retry or reconnect; it never switches to the organization account. Without a personal connection, organization access applies.</p>
      ${connection.description?`<p>${esc(connection.description)}</p>`:''}
      ${!connection.available?'<p class="note">Sign in with your individual workspace account to connect personal Slack. A shared sign-in cannot own this connection.</p>':''}
      ${!connection.oauth_configured?'<p class="note">An administrator must configure Slack OAuth before you can connect.</p>':''}
      <div class="credential-actions">
        <button id="personal-slack-oauth" class="primary" ${connection.available&&connection.oauth_configured?'':'disabled'}>${connection.connected?'Reconnect Slack':'Connect Slack'}</button>
        ${connection.connected?'<button id="personal-slack-check">Check connection</button><button id="personal-slack-disconnect" class="danger">Disconnect…</button>':''}
      </div><p id="personal-slack-result" role="status"></p><p id="personal-slack-error" role="alert"></p>
    </section>
    <section class="card"><h2>Use a private web chat</h2><p>Personal Slack reads require an owner-only web session. They are unavailable in shared chats, Slack threads, and automations. Existing shared chats cannot become private.</p>
      ${connection.limitation?`<p>${esc(connection.limitation)}</p>`:''}
      <p>Only you can open a private session. It stays private after disconnecting Slack and cannot be shared, delegated, or exported to a side chat. Organization connection policies still apply. Bot messages keep the organization bot identity.</p>
      <button id="personal-slack-new-chat" ${connection.available?'':'disabled'}>Start private web chat</button>
    </section></section>`;
  if (focusId) document.getElementById(focusId)?.focus({preventScroll:true});
  const current = () => version === state.pageVersion;
  let busy = false;
  const action = async work => {
    if (busy || !current()) return;
    busy = true;
    const buttons = [...document.querySelectorAll('.personal-slack-page button')];
    const disabled = buttons.map(button => button.disabled);
    buttons.forEach(button => button.disabled = true);
    $('#personal-slack-error').textContent = '';
    try { await work(); }
    catch (error) { if (current()) $('#personal-slack-error').textContent = error.message; }
    finally { busy = false; buttons.forEach((button,i) => button.disabled = disabled[i]); }
  };
  $('#personal-slack-oauth').onclick = () => action(async () => {
    const result = await api('/api/connections/slack/personal/oauth', {method:'POST'});
    if (current()) window.location.assign(result.url);
  });
  if (connection.connected) {
    $('#personal-slack-check').onclick = () => action(async () => {
      await api('/api/connections/slack/personal/check', {method:'POST'});
      if (current()) { await renderPersonalSlack(); if (current()) $('#personal-slack-result').textContent = 'Connection checked.'; }
    });
    $('#personal-slack-disconnect').onclick = () => action(async () => {
      const confirmed = await confirmSettingsAction('Disconnect personal Slack?', 'Future Slack reads will use the organization connection when available. Existing private sessions stay private. This only removes your personal connection.', 'Disconnect');
      if (!confirmed || !current()) return;
      await api('/api/connections/slack/personal', {method:'DELETE'});
      if (current()) await renderPersonalSlack();
    });
  }
  $('#personal-slack-new-chat').onclick = () => startPrivateWebChat().catch(showError);
}

async function startPrivateWebChat() {
  // An empty draft and a new attachment bucket prevent importing another chat's context.
  state.newDraft = {private_session:true, attachment_key:'private-new-'+crypto.randomUUID()};
  state.pendingNew = null;
  await navigate('tasks');
  $('#prompt')?.focus();
}
