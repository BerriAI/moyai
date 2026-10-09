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
    <p>This automatically stops the session and its agents, closes their sandboxes, and removes the session for everyone. It cannot be reopened or continued.</p>
    <p class="subtext">Stored conversation data, files, billing records, and backups are retained. Side chats, GitHub work, and messages already sent to Slack stay unchanged.</p>
    <p class="folder-error" role="alert"></p><div class="folder-dialog-actions"><button type="button" data-cancel>Cancel</button><button type="submit" class="danger">Delete session</button></div></form>`);
  const form = dialog.querySelector('form'), submit = dialog.querySelector('[type="submit"]');
  const current = () => dialog.open && dialog.querySelector('form') === form;
  let busy = false;
  dialog.querySelector('[data-cancel]').onclick = () => { if (!busy && current()) dialog.close(); };
  dialog.oncancel = null;
  dialog.onkeydown = event => { if (event.key === 'Escape') event.stopPropagation(); };
  form.onsubmit = async event => {
    event.preventDefault();
    if (busy || !current()) return;
    busy = true;
    const buttons = [...dialog.querySelectorAll('button')];
    buttons.forEach(button => button.disabled = true);
    submit.textContent = 'Deleting…';
    dialog.oncancel = event => event.preventDefault();
    dialog.querySelector('.folder-error').textContent = '';
    let result;
    try {
      result = await api(`/api/runs/${run.id}`, {method:'DELETE'});
    } catch (error) {
      if (current()) dialog.querySelector('.folder-error').textContent = error.message;
      return;
    } finally {
      busy = false;
      if (current()) {
        dialog.oncancel = null;
        buttons.forEach(button => button.disabled = false);
        submit.textContent = 'Delete session';
      }
    }
    const ownsDialog = current();
    if (ownsDialog) dialog.close();
    state.runsRefresh++;
    state.chatRefresh = (state.chatRefresh || 0) + 1;
    state.sessionEdits = (state.sessionEdits || 0) + 1;
    if (!result.deleted) {
      // The server owns cleanup after acknowledgement, even if this page closes.
      for (const item of [run, ...state.runs, state.chatRun, state.sessionHeaderRun]) {
        if (item?.id === run.id) item.status = 'deleting';
      }
      if (state.chatRun && (state.selected === run.id || state.activeParentId === run.id)) {
        state.chatRun.status = 'deleting';
        updateChatStatus(state.chatRun);
      }
      renderSidebar();
      toast(result.error || 'Deleting session. Its agents and sandboxes will stop automatically.');
      await refreshRuns().catch(showError);
      return;
    }
    state.runs = state.runs.filter(item => item.id !== run.id);
    renderSidebar();
    // A completed request must not navigate away from a different session.
    if (ownsDialog && state.selected && (state.selected === run.id || state.activeParentId === run.id)) {
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
