const memoryKinds = {preference:'Preference', feedback:'Correction', project:'Project context', reference:'Reference'};

function memoryCard(note) {
  const expired = note.expires_at && new Date(note.expires_at) <= new Date();
  return `<article class="memory-card">
    <div class="memory-card-top"><span class="memory-kind">${memoryKinds[note.kind] || 'Note'}</span><span class="subtext">${expired ? 'Expired' : 'Updated '+relative(note.updated_at)}</span></div>
    <h2>${esc(note.title)}</h2><p class="memory-content">${esc(note.content)}</p>
    <div class="memory-meta">${note.repo_url ? esc(note.repo_url.replace('https://github.com/','')) : note.source?.type==='observation' ? esc(note.source.scope) : 'Across your sessions'}${note.expires_at ? ` · ${expired?'Expired':'Expires'} ${esc(new Date(note.expires_at).toLocaleDateString())}` : ''}</div>
    <div class="memory-card-bottom"><span class="subtext">${note.source?.type==='chat'?'Learned from your message':note.source?.type==='observation'?'Observed during work':'Added by you'}</span><div><button class="quiet" data-memory-edit="${esc(note.id)}">Review & edit</button><button class="quiet danger" data-memory-delete="${esc(note.id)}">Delete</button></div></div>
  </article>`;
}

let memorySearchQuery = '';
function memoryReviewSummary(review, prefs) {
  if (!prefs.enabled || !prefs.auto_save) return '';
  if (!review?.enabled) return 'Automatic saving during sessions is available. Background review is disabled.';
  if (!review.configured) return 'Background review needs a configured model connection.';
  const latest=review.latest;
  const result=latest?.status==='failed' ? 'The last review could not finish. Future completed turns will still be reviewed.' :
    latest?.status==='completed' ? `Last review ${relative(latest.updated_at)}: ${latest.saved_count ? `saved ${latest.saved_count} ${latest.saved_count===1?'memory':'memories'}.` : 'no new lasting context to save.'}` :
    'Finished sessions are reviewed after a short idle period. Recent sessions are checked when background review starts.';
  return `${review.pending ? `${review.pending} finished ${review.pending===1?'turn is':'turns are'} waiting for review. ` : ''}${result}`;
}
async function renderMemory(recent=false) {
  const version=state.pageVersion;
  MoyaiUI.render($('#content'), '<p class="subtext" role="status">Loading your memories…</p>');
  let data;
  try { data=await api('/api/memory',{recent}); }
  catch(error) { if(version===state.pageVersion) { MoyaiUI.render($('#content'), `<section class="memory-page"><h1>Memory</h1><div role="alert"><p class="subtext">${esc(error.message)}</p><p>Check your connection and try again.</p></div><button id="memory-retry">Try again</button></section>`); $('#memory-retry').onclick=()=>renderMemory(); } return; }
  if(version!==state.pageVersion)return;
  const prefs=data.preferences;
  MoyaiUI.render($('#content'), `<section class="memory-page">
    <div class="section-header"><div><h1>Memory</h1><p class="subtext">Preferences and context for future sessions.</p></div><button class="primary" id="memory-add">Add memory</button></div>
    <div class="memory-controls"><div><strong>${prefs.enabled?'Moyai remembers useful context':'Memory is paused'}</strong><p class="subtext">${prefs.enabled?'Preferences, corrections, and lessons learned during work can carry into future sessions.':'Your notes are kept here. Moyai won’t retrieve or save them while paused.'}</p></div><button class="quiet" id="memory-toggle">${prefs.enabled?'Pause memory':'Resume memory'}</button></div>
    <div class="memory-learning"><label for="memory-learning">How new memories are saved</label><select id="memory-learning" ${prefs.enabled?'':'disabled'}><option value="auto" ${prefs.auto_save?'selected':''}>Save useful context automatically</option><option value="manual" ${prefs.auto_save?'':'selected'}>Only save manually</option></select></div>
    ${prefs.enabled&&prefs.auto_save ? `<div class="memory-review"><p class="subtext" role="status">${esc(memoryReviewSummary(data.review,prefs))}</p><button class="quiet" id="memory-refresh">Refresh</button></div>` : ''}
    <p class="memory-privacy">These notes are available only for your requests. Answers in shared sessions may reflect them. Keep keys in <a href="#secrets">Secrets</a> and reusable team workflows in <a href="#skills">Skills</a>.</p>
    <div class="memory-filter"><input type="search" id="memory-search" aria-label="Search your memories" placeholder="Search your memories"><span class="subtext">${data.memories.length} / ${data.limit} notes</span></div>
    <div id="memory-list"></div>
    <p class="subtext memory-footnote">Moyai recalls a few relevant notes at a time. Project context and references expire after 90 days; edit to refresh them. Deleting a note stops future recall, but does not remove existing conversations or backups.</p>
  </section>`);
  $('#memory-search').value=memorySearchQuery;
  const draw=()=>{
    memorySearchQuery=$('#memory-search').value;
    const query=$('#memory-search').value.toLowerCase().trim();
    const rows=data.memories.filter(n=>(n.title+' '+n.content+' '+n.repo_url).toLowerCase().includes(query));
    MoyaiUI.render($('#memory-list'), rows.length?rows.map(memoryCard).join(''):`<div class="memory-empty"><span aria-hidden="true">✧</span><h2>${query?'No matching memories':'Start with what matters to you'}</h2><p class="subtext">${query?'Try another search or clear the search field.':'“Keep PR descriptions short.” “Use staging for benchmark runs.” Moyai can remember useful preferences as you work, or you can add one here.'}</p></div>`);
    document.querySelectorAll('[data-memory-edit]').forEach(b=>b.onclick=()=>openMemoryEditor(data.memories.find(n=>n.id===b.dataset.memoryEdit)));
    document.querySelectorAll('[data-memory-delete]').forEach(b=>b.onclick=()=>deleteMemory(data.memories.find(n=>n.id===b.dataset.memoryDelete)));
  };
  draw();$('#memory-search').oninput=draw;
  $('#memory-add').onclick=()=>openMemoryEditor();
  if(prefs.enabled&&prefs.auto_save)$('#memory-refresh').onclick=async()=>{await renderMemory();if(version===state.pageVersion)$('#memory-refresh')?.focus();};
  const update=async changes=>{
    $('#memory-toggle').disabled=true;$('#memory-learning').disabled=true;
    try { await api('/api/memory/preferences',{method:'PUT',body:JSON.stringify({...prefs,...changes})});if(version===state.pageVersion)await renderMemory(); }
    catch(error) { showError(error);if(version===state.pageVersion)await renderMemory(); }
  };
  $('#memory-toggle').onclick=()=>update({enabled:!prefs.enabled});
  $('#memory-learning').onchange=e=>update({auto_save:e.target.value==='auto'});
}

function openMemoryEditor(note=null) {
  const dialog=$('#memory-dialog'), requestId=crypto.randomUUID();
  MoyaiUI.render(dialog, `<form id="memory-form" class="memory-editor"><button type="button" class="dialog-close" aria-label="Close memory editor">×</button><h2>${note?'Edit memory':'Add a memory'}</h2><p class="subtext">Keep one useful idea per note. It stays in your personal library.</p>
    <div class="field"><label for="memory-title">Title</label><input id="memory-title" required minlength="3" maxlength="120" value="${esc(note?.title||'')}" placeholder="How I like PR descriptions"></div>
    <div class="field"><label for="memory-kind">Type</label><select id="memory-kind">${Object.entries(memoryKinds).map(([key,label])=>`<option value="${key}" ${note?.kind===key?'selected':''}>${label}</option>`).join('')}</select></div>
    <div class="field"><label for="memory-content">What should Moyai remember?</label><textarea id="memory-content" required minlength="3" maxlength="1200" rows="5" placeholder="Keep PR descriptions short and include the test results.">${esc(note?.content||'')}</textarea></div>
    <div class="field"><label for="memory-repo">Repository (optional)</label><input type="url" id="memory-repo" value="${esc(note?.repo_url||'')}" placeholder="https://github.com/BerriAI/litellm"><small>Leave blank to use across your sessions.</small></div>
    ${['chat','observation'].includes(note?.source?.type)?`<details class="memory-source"><summary>Why Moyai saved this</summary>${note.source.type==='observation'?`<p>Observed during work in ${esc(note.source.scope)}. Recheck if the environment changes.</p><blockquote>${esc(note.source.evidence)}</blockquote>`:`<blockquote>${esc(note.source.quote)}</blockquote>`}<a href="#run=${esc(note.source.run_id)}" id="memory-source-link">Open original session</a></details>`:''}
    <p id="memory-error" role="alert"></p><div class="credential-actions"><button type="submit">Save memory</button><button type="button" class="quiet" id="memory-cancel">Cancel</button></div></form>`);
  const close=()=>dialog.close();dialog.onclose=()=>{MoyaiUI.render(dialog, '');};
  dialog.querySelector('[aria-label="Close memory editor"]').onclick=close;$('#memory-cancel').onclick=close;
  if($('#memory-source-link'))$('#memory-source-link').onclick=close;
  const form=$('#memory-form');
  form.onsubmit=async e=>{
    e.preventDefault();const button=e.currentTarget.querySelector('[type="submit"]');button.disabled=true;
    try {
      await api('/api/memory'+(note?'/'+note.id:''),{method:note?'PUT':'POST',body:JSON.stringify({
        key:note?.key||'note-'+requestId,title:$('#memory-title').value,content:$('#memory-content').value,
        kind:$('#memory-kind').value,repo_url:$('#memory-repo').value,revision:note?.revision||0,request_id:requestId})});
      if($('#memory-form')===form)dialog.close();toast('Memory saved.');if(state.view==='memory')await renderMemory();
    } catch(error) { if($('#memory-form')===form && $('#memory-error'))$('#memory-error').textContent=error.message;button.disabled=false; }
  };
  dialog.showModal();$('#memory-title').focus();
}

function deleteMemory(note) {
  const dialog=$('#memory-dialog');
  MoyaiUI.render(dialog, `<div class="memory-editor"><h2>Delete this memory?</h2><p>${esc(note.title)}</p><p class="subtext">Moyai will stop recalling this note. Existing conversations and backups are not changed.</p><p id="memory-error" role="alert"></p><div class="credential-actions"><button class="danger" id="memory-confirm-delete">Delete memory</button><button class="quiet" id="memory-cancel">Cancel</button></div></div>`);
  dialog.onclose=()=>{MoyaiUI.render(dialog, '');};$('#memory-cancel').onclick=()=>dialog.close();
  $('#memory-confirm-delete').onclick=async e=>{
    e.currentTarget.disabled=true;
    try { await api('/api/memory/'+note.id,{method:'DELETE',body:JSON.stringify({revision:note.revision})});dialog.close();toast('Memory deleted.');if(state.view==='memory')await renderMemory(); }
    catch(error) { if($('#memory-error'))$('#memory-error').textContent=error.message;if($('#memory-confirm-delete'))$('#memory-confirm-delete').disabled=false; }
  };
  dialog.showModal();$('#memory-cancel').focus();
}
