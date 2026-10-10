// One registry for the React overview, this rail and the command palette.
const settingsGroups = MoyaiConfig.settingsGroups;
// Keep old Usage analytics bookmarks routable without adding a second navigation entry.
const settingsViews = new Set(['settings', 'adoption', ...settingsGroups.flatMap(group => group.items.map(item => item.view))]);

function settingsItemVisible(item, role) {
  return (!item.admin || role === 'admin') && (!item.memberOnly || role !== 'admin');
}

// The same destinations drive the overview and the persistent settings rail.
function settingsNavigation(view, role) {
  const link = (target, title, icon) => `<a href="#${target}"${view === target ? ' aria-current="page"' : ''}>${icon ? settingsIcon(target) : '<span class="settings-overview-icon" aria-hidden="true">⊞</span>'}<span>${title}</span></a>`;
  return `<a class="settings-back" href="#tasks"><span aria-hidden="true">←</span> Back to workspace</a>
    <div class="settings-nav-title">Settings</div>${link('settings', 'Overview')}
    ${settingsGroups.filter(group => !group.admin || role === 'admin').map(group => `<div class="settings-nav-group"><h2>${group.title}</h2>${group.items.filter(item => settingsItemVisible(item, role)).map(item => link(item.view, item.title, true)).join('')}</div>`).join('')}`;
}

function updateSettingsNavigation() {
  const nav = $('#settings-navigation');
  if (nav) MoyaiUI.render(nav, settingsNavigation(state.view, state.role));
}

function settingsLoadError(title, error, retry) {
  MoyaiUI.render($('#content'), `<section class="settings-load-error"><h1>${esc(title)}</h1><div role="alert"><h2>Unable to load this page</h2><p>${esc(error.message)}</p><p>Check your connection and try again.</p></div><button id="settings-retry">Try again</button></section>`);
  $('#settings-retry').onclick = retry;
}

function confirmSettingsAction(title, description, action) {
  return new Promise(resolve => {
    const dialog = MoyaiUI.createDialog();
    dialog.dataset.dialogScope = 'settings';
    dialog.setAttribute('aria-label', title);
    MoyaiUI.render(dialog, `<h2>${esc(title)}</h2><p>${esc(description)}</p><div class="credential-actions"><button class="quiet" data-cancel>Cancel</button><button class="danger" data-confirm>${esc(action)}</button></div>`);
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
  return !!document.querySelector('[data-slot="dialog-content"][data-state="open"]') ||
    !!document.querySelector('[data-slot="select-trigger"][data-state="open"]') ||
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

