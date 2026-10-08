async function renderSandboxConnection(version) {
  const host = document.createElement('section');
  host.className = 'card sandbox-connection';
  MoyaiUI.render(host, '<h2>Sandbox provider</h2><p role="status">Loading connection…</p>');
  $('#content .page-heading').after(host);
  try {
    let saved = await api('/api/settings/sandboxes');
    if (version !== state.pageVersion) return;
    const admin = state.role === 'admin';
    if (!admin) {
      MoyaiUI.render(host, `<h2>Sandbox provider</h2><p>${saved.provider === 'substrate' ? 'Substrate' : 'Modal'} supplies new sandboxes. An administrator manages this connection.</p>`);
      return;
    }
    const fields = {
      modal: [['modal_token_id','Token ID','password'], ['modal_token_secret','Token secret','password'], ['modal_app_name','App name','text']],
      substrate: [['substrate_api_url','Control API URL','url'], ['substrate_router_url','Router URL','url'],
        ['substrate_api_token','API token','password'], ['substrate_atespace','Atespace','text'],
        ['substrate_template','Moyai actor template','text'], ['substrate_egress_hosts','Allowed outbound hosts','text'],
        ['substrate_ca_cert','CA certificate (optional)','textarea']],
    };
    const drafts = Object.fromEntries(Object.entries(saved.providers).map(([name, value]) => [name, {...value.values}]));
    let selected = saved.provider;
    MoyaiUI.render(host, `<h2>Sandbox provider</h2><p class="subtext">Choose where new sessions run. Existing sessions keep their original provider.</p>
      <form id="sandbox-connection-form"><div class="field"><label for="sandbox-provider">Provider</label><select id="sandbox-provider"><option value="modal">Modal</option><option value="substrate">Substrate</option></select></div>
      <div id="sandbox-connection-fields"></div><p id="sandbox-connection-status" role="status"></p>
      <div class="credential-actions"><button type="submit" class="primary">Connect and use</button><span class="subtext">Tests the connection before saving.</span></div></form>`);
    const picker = host.querySelector('#sandbox-provider');
    picker.value = selected;
    const draw = () => {
      MoyaiUI.render(host.querySelector('#sandbox-connection-fields'), fields[selected].map(([key,label,type]) => {
        const value = drafts[selected][key] || '';
        const attributes = `id="${key}" name="${key}" autocomplete="${type === 'password' ? 'new-password' : 'off'}"`;
        return `<div class="field ${type === 'textarea' ? 'sandbox-field-wide' : ''}"><label for="${key}">${label}</label>${type === 'textarea' ? `<textarea ${attributes} rows="3">${esc(value)}</textarea>` : `<input ${attributes} type="${type}" value="${esc(value)}" placeholder="${type === 'password' && saved.providers[selected].secrets?.[key] ? 'Saved · leave blank to keep' : ''}">`}</div>`;
      }).join('') + (selected === 'substrate' ? `<details><summary>Set up the Moyai actor template</summary><p>Install the Moyai sandbox image and template on your cluster using the <a href="https://github.com/BerriAI/moyai/blob/main/docs/substrate.md" target="_blank" rel="noopener">Substrate setup guide</a>. Use this public key in the template:</p><pre class="sandbox-public-key">${esc(saved.public_key)}</pre><p>The connection test starts a small sandbox, runs a command, and deletes it.</p></details>` : ''));
    };
    const collect = () => {
      fields[selected].forEach(([key]) => { drafts[selected][key] = host.querySelector('#' + key).value.trim(); });
    };
    picker.onchange = () => { collect(); selected = picker.value; draw(); host.querySelector('#sandbox-connection-status').textContent = ''; };
    draw();
    host.querySelector('form').onsubmit = async event => {
      event.preventDefault(); collect();
      const status = host.querySelector('#sandbox-connection-status');
      const button = host.querySelector('button[type=submit]');
      button.disabled = picker.disabled = true;
      status.textContent = 'Testing the connection…';
      try {
        saved = await api('/api/settings/sandboxes', {method:'PUT', body:JSON.stringify({provider:selected, revision:saved.revision, values:drafts[selected]})});
        if (version !== state.pageVersion) return;
        fields[selected].filter(([, ,type]) => type === 'password').forEach(([key]) => { drafts[selected][key] = ''; });
        state.config = await api('/api/config');
        if (version !== state.pageVersion) return;
        draw();
        status.textContent = saved.message + ' New sessions will use ' + (selected === 'substrate' ? 'Substrate.' : 'Modal.');
        const badge = $('#runtime-readiness');
        if (badge) badge.textContent = state.config.cloud_ready ? 'Cloud ready' : 'Setup needed';
      } catch (error) {
        if (version === state.pageVersion) status.textContent = error.message;
      } finally {
        button.disabled = picker.disabled = false;
        if (version === state.pageVersion) button.focus();
      }
    };
  } catch (error) {
    if (version === state.pageVersion) MoyaiUI.render(host, `<h2>Sandbox provider</h2><p role="alert">${esc(error.message)}</p><button id="sandbox-settings-retry">Try again</button>`);
    host.querySelector('#sandbox-settings-retry')?.addEventListener('click', () => { host.remove(); renderSandboxConnection(version); });
  }
}
