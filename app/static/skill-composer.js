// Slash completion only reads the signed-in user's skill catalog. Authorization
// and instruction loading remain on the server for the submitted turn.
function slashSkillQuery(value, start, end = start) {
  if (start !== end) return null;
  const before = value.slice(0, start);
  const outsideFences = before.replace(/```[\s\S]*?```/g, '');
  if (outsideFences.includes('```') || (outsideFences.split('\n').at(-1).match(/`/g) || []).length % 2) return null;
  const match = /(^|\s)\/(?:(?:skills?)[ \t]+)?([a-z0-9:-]*)$/i.exec(before);
  if (!match) return null;
  const suffix = /^[a-z0-9:-]*/i.exec(value.slice(start))[0];
  const tokenEnd = start + suffix.length;
  if (tokenEnd < value.length && !/\s/.test(value[tokenEnd])) return null;
  let query = match[2].toLowerCase();
  if (query === 'skill' || query === 'skills') query = '';
  return {start: match.index + match[1].length, end: tokenEnd, query};
}

function matchingSkills(skills, query) {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  return skills.filter(skill => !skill.archived && terms.every(term =>
    `${skill.name} ${skill.reference} ${skill.description} ${skill.scope}`.toLowerCase().includes(term)
  )).sort((a, b) => Number(b.name.startsWith(query)) - Number(a.name.startsWith(query)) ||
    a.name.localeCompare(b.name) || Number(b.scope === 'personal') - Number(a.scope === 'personal'));
}

function skillCompletion(value, range, skill) {
  return value.slice(0, range.start) + completionToken(skill) + ' ' + value.slice(range.end).replace(/^ /, '');
}

function completionToken(item) {return item.builtin ? '/goal' : skillToken(item);}

function goalCompletions(range, value) {
  return !value.slice(0, range.start).trim() && 'goal'.startsWith(range.query) ? [{
    name:'goal', reference:'goal', scope:'builtin', builtin:true,
    description:'Keep working until verified completion. Set an objective, or use status, pause, resume, clear.',
  }] : [];
}

function bindInlineSkillPicker(input, form) {
  const popup = document.createElement('div');
  popup.className = 'skill-inline';
  popup.hidden = true;
  form.append(popup);
  const goalHint = document.createElement('div');
  goalHint.className = 'goal-draft';
  goalHint.setAttribute('role', 'status');
  form.prepend(goalHint);
  function updateGoalHint() {
    const text = globalThis.MoyaiGoal?.draft(input.value);
    goalHint.hidden = !text;
    goalHint.textContent = text || '';
  }
  updateGoalHint();
  const listId = input.id + '-skill-options';
  input.setAttribute('aria-autocomplete', 'list');
  input.setAttribute('aria-controls', listId);
  input.setAttribute('aria-haspopup', 'listbox');
  let skills = null, loadedAt = 0, loading = false, error = '', selected = 0, destroyed = false;
  let matches = [], range = null, lastQuery = '', dismissed = '';
  const signature = () => input.value + ':' + input.selectionStart;
  const close = () => {popup.hidden = true; input.removeAttribute('aria-activedescendant');};
  const dismiss = () => {dismissed = signature(); close();};

  function position() {
    if (popup.hidden || !input.isConnected) return;
    const rect = form.getBoundingClientRect();
    const viewport = window.visualViewport;
    const viewportTop = viewport?.offsetTop || 0;
    const top = Math.max(viewportTop, document.getElementById('content')?.getBoundingClientRect().top || 0);
    const bottom = viewportTop + (viewport?.height || window.innerHeight);
    const above = rect.top - top - 12, below = bottom - rect.bottom - 12;
    const up = input.id === 'followup' ? above >= 160 || above > below : below < 160 && above > below;
    popup.classList.toggle('above', up);
    popup.style.maxHeight = Math.max(80, Math.min(340, up ? above : below)) + 'px';
  }

  function draw() {
    if (destroyed || !input.isConnected || document.activeElement !== input) {close(); return;}
    range = slashSkillQuery(input.value, input.selectionStart, input.selectionEnd);
    if (!range || dismissed === signature()) {close(); return;}
    if (range.query !== lastQuery) selected = 0;
    lastQuery = range.query;
    matches = [...goalCompletions(range, input.value), ...matchingSkills(skills || [], range.query)];
    selected = Math.min(selected, Math.max(0, matches.length - 1));
    const empty = loading ? 'Loading your skills…' : error || (skills?.length ? 'No matching skills. Try another name.' : 'No skills yet. Add one in your Skills library.');
    MoyaiUI.render(popup, `<div class="skill-inline-heading"><span>✦ Commands & skills</span><small>Built-in · Personal · Organization</small></div>
      <div id="${listId}" class="skill-inline-list" role="listbox" aria-label="Available commands and skills">${matches.map((skill, index) =>
        `<button type="button" role="option" tabindex="-1" id="${listId}-${index}" aria-selected="${index === selected}" data-skill-index="${index}" class="skill-inline-option"><span class="skill-inline-icon" aria-hidden="true">${skillIcon(skill)}</span><span class="skill-inline-copy"><span class="skill-inline-name">/${esc(skill.name)}<small>${skill.builtin ? 'Built-in' : skill.scope === 'personal' ? 'Personal' : 'Organization'}</small></span><span class="skill-inline-description">${esc(skill.description)}</span></span></button>`
      ).join('')}</div>${!matches.length || error ? `<p class="skill-inline-empty" role="status">${esc(empty)}</p>` : ''}
      <div class="skill-inline-footer"><span>${matches.length ? '↑ ↓ navigate · Enter select · Esc close' : 'Type / followed by a skill name'}</span><button type="button" class="quiet" data-skill-library>${error ? 'Retry' : 'Skills library ↗'}</button></div>`);
    popup.hidden = false;
    if (matches.length) input.setAttribute('aria-activedescendant', listId + '-' + selected);
    else input.removeAttribute('aria-activedescendant');
    position();
    const list = popup.querySelector('.skill-inline-list'), option = popup.querySelector('[aria-selected="true"]');
    if (list && option) {
      const top = option.offsetTop - list.offsetTop;
      if (top < list.scrollTop) list.scrollTop = top;
      else if (top + option.offsetHeight > list.scrollTop + list.clientHeight)
        list.scrollTop = top + option.offsetHeight - list.clientHeight;
    }
  }

  function refresh() {
    if (destroyed) return;
    updateGoalHint();
    const query = slashSkillQuery(input.value, input.selectionStart, input.selectionEnd);
    if (query && dismissed !== signature() && !loading && !error && (!skills || Date.now() - loadedAt > 30000)) {
      loading = true;
      api('/api/skills').then(data => {skills = data.skills; loadedAt = Date.now();})
        .catch(() => {error = 'Couldn’t load skills. Retry to see your library.'; skills = null;})
        .finally(() => {loading = false; draw();});
    }
    draw();
  }

  function choose(index) {
    const current = slashSkillQuery(input.value, input.selectionStart, input.selectionEnd);
    const skill = matches[index];
    if (!current || !skill || (loading && !skill.builtin)) return;
    const value = skillCompletion(input.value, current, skill);
    if (input.maxLength > 0 && value.length > input.maxLength) {toast('Shorten your message before adding this skill.'); return;}
    const caret = current.start + completionToken(skill).length + 1;
    input.setSkillCatalog?.([skill]);
    input.value = value;
    input.focus();
    input.setSelectionRange(caret, caret);
    close();
    input.dispatchEvent(new Event('input', {bubbles: true}));
  }

  popup.addEventListener('pointerdown', event => event.preventDefault());
  popup.addEventListener('click', event => {
    const option = event.target.closest('[data-skill-index]');
    if (option) choose(Number(option.dataset.skillIndex));
    else if (event.target.closest('[data-skill-library]')) {
      if (error) {error = ''; refresh();}
      else {dismiss(); navigate('skills').catch(showError);}
    }
  });
  input.addEventListener('input', refresh);
  input.addEventListener('click', refresh);
  input.addEventListener('focus', refresh);
  input.addEventListener('blur', close);
  input.addEventListener('keyup', event => {if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) refresh();});
  form.addEventListener('submit', dismiss);
  window.addEventListener('resize', position);
  window.visualViewport?.addEventListener('resize', position);
  window.visualViewport?.addEventListener('scroll', position);
  return {
    destroy() {
      destroyed = true;
      close(); popup.remove(); goalHint.remove();
      window.removeEventListener('resize', position);
      window.visualViewport?.removeEventListener('resize', position);
      window.visualViewport?.removeEventListener('scroll', position);
    },
    keydown(event) {
      if (popup.hidden || event.isComposing || event.ctrlKey || event.metaKey || event.altKey) return false;
      if (event.key === 'Escape') {event.preventDefault(); dismiss(); return true;}
      if (event.shiftKey) {close(); return false;}
      if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
        event.preventDefault();
        if (matches.length) selected = (selected + (event.key === 'ArrowDown' ? 1 : -1) + matches.length) % matches.length;
        draw(); return true;
      }
      if (event.key === 'Enter' || (event.key === 'Tab' && matches.length)) {
        event.preventDefault(); choose(selected); return true;
      }
      return false;
    },
  };
}
