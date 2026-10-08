/* Extend the shared session menu; Rename and Move keep their existing owners. */
function bindSessionLifecycleActions(menu, run) {
  const icon = name => globalThis.MoyaiIcon?.(name, 16) || '';
  MoyaiUI.insert(menu, 'beforeend', `<button type="button" data-archive-session>${icon(run.archived ? 'restore' : 'archive')}${run.archived ? 'Restore session' : 'Archive session'}</button>
    ${run.can_delete ? `<button type="button" data-delete-session class="session-delete">${icon('trash')}Delete session…</button>` : ''}`);
  menu.querySelector('[data-archive-session]').onclick = () => { menu.hidePopover(); changeSessionArchive(run).catch(showError); };
  menu.querySelector('[data-delete-session]')?.addEventListener('click', () => { menu.hidePopover(); deleteSessionDialog(run); });
}

async function changeSessionArchive(run) {
  if (state.sessionMutation) return;
  state.sessionMutation = run.id;
  const archived = !run.archived;
  try {
    await api(`/api/runs/${run.id}/archive`, {method:'POST', body:JSON.stringify({archived})});
    // Invalidate older polls before they can restore the removed row.
    state.runsRefresh++;
    state.chatRefresh = (state.chatRefresh || 0) + 1;
    state.sessionEdits = (state.sessionEdits || 0) + 1;
    state.runs = state.runs.filter(item => item.id !== run.id);
    for (const item of [run, state.chatRun, state.sessionHeaderRun]) {
      if (item?.id === run.id) item.archived = archived;
    }
    renderSidebar();
    toast(archived ? 'Session archived. Ask Moyai to find it when you need it.' : 'Session restored to your sidebar.');
    await refreshRuns();
  } finally { state.sessionMutation = null; }
}

function deleteSessionDialog(run) {
  const dialog = $('#session-delete-dialog');
  MoyaiUI.render(dialog, `<form class="folder-form"><h2 id="session-delete-title">Delete session?</h2>
    <p class="folder-session-name">${esc(sessionTitle(run))}</p>
    <p>This removes the session and its agents for everyone. It cannot be reopened or continued.</p>
    <p class="subtext">Stored conversation data, files, billing records, and backups are retained. Side chats, GitHub work, and messages already sent to Slack stay unchanged.</p>
    <p class="folder-error" role="alert"></p><div class="folder-dialog-actions"><button type="button" data-stop-session hidden>Stop session and agents</button><button type="button" data-cancel>Cancel</button><button type="submit" class="danger">Delete session</button></div></form>`);
  dialog.querySelector('[data-cancel]').onclick = () => dialog.close();
  dialog.querySelector('[data-stop-session]').onclick = async () => {
    const buttons = [...dialog.querySelectorAll('button')];
    buttons.forEach(button => button.disabled = true);
    try {
      await api(`/api/runs/${run.id}/cancel`, {method:'POST'});
      dialog.querySelector('.folder-error').textContent = 'Stop requested. Wait for cleanup, then try Delete session again.';
    } catch (error) { dialog.querySelector('.folder-error').textContent = error.message; }
    finally { buttons.forEach(button => button.disabled = false); }
  };
  dialog.onkeydown = event => { if (event.key === 'Escape') event.stopPropagation(); };
  dialog.querySelector('form').onsubmit = async event => {
    event.preventDefault();
    const buttons = [...dialog.querySelectorAll('button')];
    buttons.forEach(button => button.disabled = true);
    dialog.oncancel = event => event.preventDefault();
    dialog.querySelector('.folder-error').textContent = '';
    try {
      await api(`/api/runs/${run.id}`, {method:'DELETE'});
    } catch (error) {
      dialog.querySelector('.folder-error').textContent = error.message;
      dialog.querySelector('[data-stop-session]').hidden = error.status !== 409;
      buttons.forEach(button => button.disabled = false);
      return;
    } finally { dialog.oncancel = null; }
    dialog.close();
    state.runsRefresh++;
    state.runs = state.runs.filter(item => item.id !== run.id);
    renderSidebar();
    // A completed request must not navigate away from a different session.
    if (state.selected && (state.selected === run.id || state.activeParentId === run.id)) {
      await navigate('tasks').catch(showError);
    } else await refreshRuns().catch(showError);
    toast('Session deleted.');
  };
  dialog.showModal();
  dialog.querySelector('[data-cancel]').focus();
}

function bindSessionHeaderActions(run) {
  state.sessionHeaderRun = run;
  if (run.parent_run_id) return;
  const button = MoyaiUI.createElement('button', document);
  button.className = 'quiet details-toggle header-icon';
  button.setAttribute('aria-label', 'Session actions');
  button.setAttribute('aria-expanded', 'false');
  button.setAttribute('aria-controls', 'session-actions');
  MoyaiUI.render(button, globalThis.MoyaiIcon?.('more', 17) || '⋯');
  button.onclick = () => showSessionActions(state.chatRun?.id === run.id ? state.chatRun : run, button);
  $('#header-actions').append(button);
}
