/* Search results belong to the palette, never to the polling sidebar inventory. */
function createCommandPalette() {
  const dialog = MoyaiUI.createDialog();
  dialog.id = 'command-palette';
  dialog.setAttribute('aria-label', 'Commands and session search');
  const icon = name => globalThis.MoyaiIcon?.(name, 20) || '';
  const modifier = /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘' : 'Ctrl';
  let mode = 'commands', query = '', rows = [], active = 0, generation = 0, timer;
  let runs = [], loading = false, failed = false;
  const scopeKey = () => JSON.stringify([state.userId, state.role, sessionListScope(), state.authenticated, state.pageVersion]);
  const close = () => dialog.close();
  const commands = () => {
    const items = [
      {id:'new', label:'Start session with a prompt…', icon:'plus', shortcut:modifier+' ⇧ O', action:() => $('#new-task').click()},
      {id:'search', label:'Search sessions…', icon:'search', keepOpen:true, action:() => setMode('sessions')},
    ];
    const run = state.sessionHeaderRun?.id === state.selected ? state.sessionHeaderRun : null;
    if (run && run.status !== 'deleting') {
      items.push({id:'copy', label:'Copy session link', icon:'copy', action:async() => {
        await navigator.clipboard.writeText(new URL('/#run='+encodeURIComponent(run.id), location.href).href);
        toast('Session link copied.');
      }});
      if (!run.parent_run_id) items.push(
        {id:'rename', label:'Rename session…', icon:'design', action:() => renameSession(run)},
        {id:'move', label:'Move to folder…', icon:'folder-plus', action:() => moveSessionToFolder(run)},
        ...(!state.sessionMutation ? [{id:'pin', label:run.pinned?'Unpin session':'Pin session', icon:'pin', action:() => changeSessionPin(run)}] : []),
      );
    }
    items.push({id:'folder', label:'New folder…', icon:'folder-plus', action:() => editSessionFolder()},
      {id:'sidebar', label:'Toggle sidebar', icon:'sidebar', action:() => {
        const open = matchMedia('(max-width:850px)').matches ? document.body.classList.contains('sidebar-open') : !document.body.classList.contains('rail-collapsed');
        $(open?'#close-sidebar':'#open-sidebar').click();
      }},
      {id:'settings', label:'Settings', icon:'gear', action:() => navigate('settings')});
    for (const group of (query ? settingsGroups : []).filter(group => settingsItemVisible(group, state.role))) {
      for (const item of group.items.filter(item => settingsItemVisible(item, state.role))) {
        items.push({id:item.view, label:item.title, description:item.description, icon:'grid', action:() => navigate(item.view)});
      }
    }
    return items;
  };
  function sessionOptions() {
    return (query ? runs : runs.slice(0, 6)).flatMap(root => {
      const children = sessionRows(root.children || []);
      const matches = query ? children.filter(run => sessionMatches(run, query)) : [];
      // A folder-only hit has no row-level match. Keep its root as a destination.
      const visible = !query || sessionMatches(root, query) || !matches.length ? [root, ...matches] : matches;
      return visible.map(run => ({id:'run:'+run.id, label:sessionTitle(run), icon:'chat',
        description:[run.parent_run_id?sessionTitle(root):'', run.archived?'Archived':'', sessionStatus(run)].filter(Boolean).join(' · '),
        snippet:query && run.search_query === query ? run.search_snippet : '',
        action:() => openRun(run.id)}));
    });
  }
  function select(index, scroll = false) {
    active = rows.length ? (index + rows.length) % rows.length : -1;
    dialog.querySelectorAll('[data-command-index]').forEach((node, i) => node.setAttribute('aria-selected', String(i === active)));
    const input = dialog.querySelector('#command-search');
    if (active < 0) input.removeAttribute('aria-activedescendant');
    else input.setAttribute('aria-activedescendant', 'command-option-'+active);
    if (scroll) dialog.querySelector('[aria-selected="true"]')?.scrollIntoView({block:'nearest'});
  }
  function render() {
    const previous = rows[active]?.id;
    const actions = mode === 'commands' ? commands().filter(item => !query || (item.label+' '+(item.description||'')).toLowerCase().includes(query)) : [];
    const sessions = sessionOptions();
    rows = [...actions, ...sessions];
    let index = 0;
    const group = (title, items) => items.length ? `<div role="group" aria-label="${esc(title)}"><p class="command-group-title" aria-hidden="true">${esc(title)}</p>${items.map(item => {
      const i = index++;
      return `<div class="command-option" role="option" id="command-option-${i}" data-command-index="${i}" aria-selected="false"><span class="command-icon" aria-hidden="true">${icon(item.icon)}</span><span class="command-copy"><span class="command-label">${esc(item.label)}</span>${item.description?`<span class="command-description">${esc(item.description)}</span>`:''}${item.snippet?`<span class="command-snippet">${esc(item.snippet)}</span>`:''}</span>${item.shortcut?`<kbd>${esc(item.shortcut)}</kbd>`:''}</div>`;
    }).join('')}</div>` : '';
    MoyaiUI.render(dialog.querySelector('#command-results'), group('Actions', actions)+group(query?'Matching sessions':'Recent sessions', sessions));
    dialog.querySelector('#command-results').setAttribute('aria-busy', String(loading));
    const status = loading ? 'Searching titles and messages…' : failed ? 'Sessions could not be loaded.' : query && !rows.length ? 'No matching sessions or commands.' : !rows.length ? 'No sessions yet.' : '';
    MoyaiUI.render(dialog.querySelector('#command-status'), `${esc(status)}${failed?' <button type="button" data-command-retry>Retry search</button>':query&&!loading&&!rows.length?' <button type="button" data-command-clear>Clear search</button>':''}`);
    select(Math.max(0, rows.findIndex(row => row.id === previous)));
  }
  function search() {
    clearTimeout(timer);
    const current = ++generation, scope = scopeKey();
    query = dialog.querySelector('#command-search').value.trim().toLowerCase();
    runs = []; loading = true; failed = false; active = 0; rows = [];
    render();
    const valid = () => dialog.open && generation === current && scopeKey() === scope;
    timer = setTimeout(async() => {
      try {
        const params = new URLSearchParams({scope:sessionListScope()});
        if (query) { params.set('search', query); params.set('include_archived', 'true'); }
        const result = await api('/api/runs?'+params);
        if (!valid()) return;
        runs = result;
      } catch {
        if (!valid()) return;
        failed = true;
      }
      if (!valid()) return;
      loading = false; render();
    }, query ? 200 : 0);
  }
  function setMode(next) {
    mode = next;
    const input = dialog.querySelector('#command-search');
    input.placeholder = next === 'sessions' ? 'Search titles and messages…' : 'Type a command or search…';
    dialog.querySelector('[data-command-back]').hidden = next !== 'sessions';
    input.focus(); search();
  }
  function open(next = 'commands') {
    if (!state.authenticated || !state.csrf) return;
    if (dialog.open) { setMode(next); return; }
    if (document.querySelector('[data-slot="dialog-content"]')) return;
    const menu = $('#session-actions');
    if (MoyaiUI.isOpen(menu)) menu.hidePopover();
    mode = next; query = ''; rows = []; runs = []; active = 0;
    MoyaiUI.render(dialog, `<div class="command-search-bar"><button type="button" class="icon-button" data-command-back aria-label="Back to commands" hidden><span aria-hidden="true">←</span></button><span class="command-search-icon" aria-hidden="true">${icon('search')}</span><input id="command-search" role="combobox" aria-label="Search commands and sessions" aria-autocomplete="list" aria-expanded="true" aria-controls="command-results" aria-describedby="command-scope" autocomplete="off" spellcheck="false" maxlength="200"><button type="button" class="command-close" data-command-close aria-label="Close command palette"><kbd>esc</kbd></button></div><div class="command-body"><p id="command-scope">${sessionListScope()==='all'?'All sessions':'My sessions'} · Search titles and messages</p><div id="command-results" role="listbox" aria-label="Commands and sessions"></div><div id="command-status" role="status" aria-live="polite"></div></div><div class="command-footer"><span><kbd>↑</kbd> <kbd>↓</kbd> Navigate</span><span><kbd>↵</kbd> Select</span><span><kbd>esc</kbd> Close</span></div>`);
    dialog.showModal(); setMode(next);
    $('#search-sessions').setAttribute('aria-expanded', 'true');
    $('#command-menu').setAttribute('aria-expanded', 'true');
  }
  async function activate(index) {
    const row = rows[index];
    if (!row) return;
    if (!row.keepOpen) close();
    try { await row.action(); } catch (error) { showError(error); }
  }
  // The host owns listeners, independent of component callbacks and tooltip updates.
  dialog.addEventListener('input', event => { if (event.target.id === 'command-search' && !event.isComposing) search(); });
  dialog.addEventListener('compositionend', event => { if (event.target.id === 'command-search') search(); });
  dialog.addEventListener('click', event => {
    const target = event.target.closest('button,[data-command-index]');
    if (!target) return;
    if (target.hasAttribute('data-command-close')) close();
    else if (target.hasAttribute('data-command-back')) setMode('commands');
    else if (target.hasAttribute('data-command-retry')) search();
    else if (target.hasAttribute('data-command-clear')) { dialog.querySelector('#command-search').value = ''; search(); dialog.querySelector('#command-search').focus(); }
    else if (target.hasAttribute('data-command-index')) activate(Number(target.dataset.commandIndex));
  });
  dialog.addEventListener('pointermove', event => {
    const row = event.target.closest('[data-command-index]');
    if (row) select(Number(row.dataset.commandIndex));
  });
  dialog.addEventListener('keydown', event => {
    if (event.key === 'Escape') event.stopPropagation();
    if (event.target.id !== 'command-search' || event.isComposing) return;
    if (['ArrowDown', 'ArrowUp'].includes(event.key)) { event.preventDefault(); select(active+(event.key==='ArrowDown'?1:-1), true); }
    if (event.key === 'Enter') { event.preventDefault(); activate(active); }
  });
  dialog.addEventListener('close', () => {
    clearTimeout(timer); ++generation; runs = []; rows = [];
    $('#search-sessions').setAttribute('aria-expanded', 'false');
    $('#command-menu').setAttribute('aria-expanded', 'false');
  });
  return {open, close, toggle:() => dialog.open ? close() : open(), modifier};
}
