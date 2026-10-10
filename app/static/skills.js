function skillToken(skill){return '/'+skill.reference;}
function skillCard(skill){
  return `<article class="skill-card"><div class="skill-info"><div class="skill-title"><span class="skill-identity">${skillIcon(skill)}<strong>${esc(skill.name)}</strong></span><span class="secret-scope">${skill.scope==='personal'?'Personal':'Organization'}</span>${skill.archived?'<span class="secret-scope">Archived</span>':''}<code>${esc(skillToken(skill))}</code></div><p>${esc(skill.description)}</p></div><div class="skill-card-actions">${!skill.archived?`<button class="quiet" data-use-skill="${esc(skill.id)}">Use in chat</button>`:''}<button class="quiet" data-edit-skill="${esc(skill.id)}">${skill.can_manage?'Edit':'View'}</button>${skill.can_manage?`<button class="quiet" data-archive-skill="${esc(skill.id)}">${skill.archived?'Restore':'Archive'}</button>`:''}</div></article>`;
}
const skillLibraryState = {query:'', archived:false};
async function renderSkills(recent=false){
  const version=state.pageVersion,data=await api('/api/skills?archived=true',{recent});if(version!==state.pageVersion)return;
  MoyaiUI.render($('#content'), `<section class="skills-page"><div class="section-header"><div><h1>Skills</h1><p class="subtext">Reusable instructions for your recurring work.</p></div><button class="primary" id="add-skill">Add skill</button></div><details class="settings-hint"><summary>How skills are shared</summary><p>Personal skills are available for your requests. Organization skills are available to every teammate and managed by admins. Session conversations and results keep their existing sharing. You can also ask Moyai to save a skill in chat.</p></details><div id="skill-learning"><p class="subtext" role="status">Loading skill suggestions…</p></div><div class="skills-filters"><input type="search" id="skill-search" aria-label="Search skills" placeholder="Search skills"><label><input type="checkbox" id="skill-show-archived"> Show archived</label><p id="skill-count" class="subtext" role="status"></p></div><div id="skill-library"></div><p class="subtext">Pick a skill in chat or mention its reference. Moyai can also use a matching skill when its description fits your request. Skills follow the same app permissions and approval rules.</p></section>`);
  $('#skill-search').value=skillLibraryState.query;
  $('#skill-show-archived').checked=skillLibraryState.archived;
  const draw=()=>{
    skillLibraryState.query=$('#skill-search').value;
    skillLibraryState.archived=$('#skill-show-archived').checked;
    const search=$('#skill-search').value.trim().toLowerCase(),archived=$('#skill-show-archived').checked;
    const rows=data.skills.filter(s=>(archived||!s.archived)&&(s.name+' '+s.description).toLowerCase().includes(search));
    $('#skill-count').textContent=`${rows.length} skills`;
    MoyaiUI.render($('#skill-library'), rows.length?rows.map(skillCard).join(''):`<div class="settings-empty"><h2>${search?'No matching skills':'No skills yet'}</h2><p>${search?'Try another search or clear your filters.':'Add instructions for reviews, benchmarks, or another recurring task.'}</p>${search?'<button id="clear-skill-search">Clear filters</button>':''}</div>`);
    $('#clear-skill-search')?.addEventListener('click',()=>{$('#skill-search').value='';$('#skill-show-archived').checked=false;draw();$('#skill-search').focus();});
    document.querySelectorAll('[data-edit-skill]').forEach(b=>b.onclick=()=>openSkillEditor(b.dataset.editSkill).catch(showError));
    document.querySelectorAll('[data-use-skill]').forEach(b=>b.onclick=async()=>{try{const skill=rows.find(s=>s.id===b.dataset.useSkill);await navigate('tasks');insertSkill(skill,'prompt');}catch(e){showError(e);}});
    document.querySelectorAll('[data-archive-skill]').forEach(b=>b.onclick=async()=>{
      const skill=rows.find(s=>s.id===b.dataset.archiveSkill);b.disabled=true;
      try{await api('/api/skills/'+skill.id+'/archive',{method:'POST',body:JSON.stringify({archived:!skill.archived,revision:skill.revision})});toast(skill.archived?'Skill restored.':'Skill archived. You can restore it here.');if(version===state.pageVersion)await renderSkills();}catch(e){b.disabled=false;showError(e);}
    });
  };draw();$('#skill-search').oninput=draw;$('#skill-show-archived').onchange=draw;
  $('#add-skill').onclick=()=>openSkillEditor().catch(showError);
  await renderSkillLearning(version);
}
async function openSkillEditor(id='',suggestion=null){
  const version=state.pageVersion,user=state.userId;
  const saved=id?await api('/api/skills/'+id):null;
  if(version!==state.pageVersion||user!==state.userId)return;
  if(suggestion&&saved&&saved.revision!==suggestion.target_revision)throw new Error('This skill changed. Review the current skill before updating it.');
  const skill=suggestion?{...saved,...suggestion,id:saved?.id||'',scope:saved?.scope||'',revision:suggestion.target_revision,can_manage:true}:saved,canEdit=!skill||skill.can_manage,admin=state.role==='admin';
  const dialog=$('#skill-dialog');
  dialog.dataset.dialogScope='settings';
  const references=skill?.files?.length?`<div class="field"><label>Supporting files</label><ul>${skill.files.map(file=>`<li><a href="/api/skills/${encodeURIComponent(skill.id)}/files/${file.path.split('/').map(encodeURIComponent).join('/')}" download>${esc(file.path)}</a> <small>${Math.ceil(file.size/1024)} KB</small></li>`).join('')}</ul><small>These files are preserved when you edit the instructions. Ask Moyai in chat to update or remove a supporting file.</small></div>`:'';
  MoyaiUI.render(dialog, `<form id="skill-form" class="skill-editor"><button type="button" class="dialog-close" aria-label="Close skill editor">×</button><h2>${suggestion?'Review suggested skill':skill?(canEdit?'Edit skill':'View skill'):'Add a skill'}</h2>${suggestion?skillSuggestionEvidence(suggestion):''}<div class="skill-form-row"><div class="field"><label for="skill-name">Name</label><input id="skill-name" required maxlength="64" pattern="[a-z0-9]+(-[a-z0-9]+)*" placeholder="benchmark-review" value="${esc(skill?.name||'')}" ${canEdit?'':'readonly'}><small>Lowercase letters, numbers and hyphens.</small></div><div class="field"><label for="skill-scope">Where should this skill be saved?</label><select id="skill-scope" required ${canEdit?'':'disabled'}><option value="" disabled ${skill?.scope?'':'selected'}>Choose a scope</option><option value="personal" ${skill?.scope==='personal'?'selected':''}>Personal · my requests only</option><option value="organization" ${skill?.scope==='organization'?'selected':''} ${admin||skill?.scope==='organization'?'':'disabled'}>Organization · everyone${admin?'':' (admin only)'}</option></select></div></div>${skillIconField(skill,canEdit)}<div class="field"><label for="skill-description">When should Moyai use this skill?</label><input id="skill-description" required minlength="3" maxlength="320" placeholder="Review benchmark results and report failures and coverage." value="${esc(skill?.description||'')}" ${canEdit?'':'readonly'}></div><div class="field"><div class="skill-instructions-label"><label for="skill-instructions">Markdown instructions</label>${canEdit?'<label class="skill-import">Import .md<input type="file" id="skill-import" accept=".md,.markdown,text/markdown,text/plain" aria-label="Import Markdown skill"></label>':''}</div><textarea id="skill-instructions" required minlength="3" maxlength="32000" rows="12" placeholder="# Benchmark review\n\n1. Check case coverage.\n2. Identify failed cases.\n3. Summarize evidence and limitations." ${canEdit?'':'readonly'}>${esc(skill?.instructions||'')}</textarea><small>Paste or import a SKILL.md. To include reference files, attach them in chat and ask Moyai to save or update this skill. Do not put API keys or other secrets in a skill.</small></div>${references}<p class="subtext">Personal controls who can use the skill. It does not make shared session outputs private. Shared skills are managed by admins.</p><p id="skill-form-error" role="alert"></p><div class="credential-actions">${canEdit?'<button type="submit">Save skill</button>':''}<button type="button" class="quiet" id="cancel-skill">Close</button></div></form>`);
  const close=()=>dialog.close();dialog.onclose=()=>{MoyaiUI.render(dialog, '');};

  const previewIcon=()=>{MoyaiUI.render($('#skill-icon-preview'), skillIcon({name:$('#skill-name').value,icon:$('#skill-icon').value}));};
  $('#skill-icon').onchange=previewIcon;$('#skill-name').oninput=previewIcon;
  dialog.querySelector('[aria-label="Close skill editor"]').onclick=close;$('#cancel-skill').onclick=close;
  if($('#skill-import'))$('#skill-import').onchange=async e=>{
    const input=e.target,file=input.files[0],editor=$('#skill-form');if(!file)return;
    if(file.size>128000){$('#skill-form-error').textContent='Choose a Markdown file with at most 32,000 characters.';return;}
    const text=await file.text();if(!dialog.open||$('#skill-form')!==editor||!$('#skill-instructions'))return;if(text.length>32000||text.includes('\0')){$('#skill-form-error').textContent='Choose a text Markdown file with at most 32,000 characters.';return;}
    $('#skill-instructions').value=text;$('#skill-form-error').textContent='';
  };
  const clientId=crypto.randomUUID();
  const form=$('#skill-form');
  form.onsubmit=async e=>{
    e.preventDefault();if(!canEdit)return;
    if(!$('#skill-scope').value){$('#skill-form-error').textContent='Choose Personal or Organization before saving.';$('#skill-scope').focus();return;}
    const b=e.currentTarget.querySelector('[type="submit"]');b.disabled=true;
    try{await api(suggestion?'/api/skill-learning/suggestions/'+suggestion.id+'/accept':'/api/skills'+(id?'/'+id:''),{method:suggestion?'POST':id?'PUT':'POST',body:JSON.stringify({name:$('#skill-name').value,description:$('#skill-description').value,instructions:$('#skill-instructions').value,scope:$('#skill-scope').value,icon:$('#skill-icon').value,revision:skill?.revision||0,client_id:clientId})});if($('#skill-form')===form)dialog.close();toast('Skill saved. It is available for new requests.');if(version===state.pageVersion&&user===state.userId&&state.view==='skills')await renderSkills();}
    catch(error){if($('#skill-form')===form&&$('#skill-form-error'))$('#skill-form-error').textContent=error.message;b.disabled=false;}
  };
  dialog.showModal();
}
function insertSkill(skill,inputId,input=document.getElementById(inputId)){
  if(!input?.isConnected||document.getElementById(inputId)!==input||input.closest('form')?.inert)return;
  input.setSkillCatalog?.([skill]);
  if(input.maxLength>0&&input.value.length+skillToken(skill).length+1>input.maxLength){toast('Shorten your message before adding this skill.');return;}
  if(!input.value.split(/\s+/).includes(skillToken(skill)))input.value=skillToken(skill)+' '+input.value;
  input.dispatchEvent(new Event('input',{bubbles:true}));autoSize(input);input.focus();
}
async function openSkillPicker(inputId){
  const input=document.getElementById(inputId);if(!input?.isConnected||input.closest('form')?.inert)return;
  const version=state.pageVersion,data=await api('/api/skills');if(version!==state.pageVersion||!input.isConnected||document.getElementById(inputId)!==input||input.closest('form')?.inert)return;
  const dialog=$('#skill-dialog');
  dialog.dataset.dialogScope='workspace';
  MoyaiUI.render(dialog, `<div class="skill-picker"><button type="button" class="dialog-close" aria-label="Close skill picker">×</button><h2>Use a skill</h2><p class="subtext">Choose a workflow for your next message.</p><div class="skill-picker-list">${data.skills.map(s=>`<button class="skill-choice" data-pick-skill="${esc(s.id)}"><strong>${skillIcon(s)}${esc(s.name)} <small>${s.scope==='personal'?'Personal':'Organization'}</small></strong><span>${esc(s.description)}</span></button>`).join('')||'<p>No skills yet. Add one in the Skills library.</p>'}</div><button class="quiet" id="manage-skills">Open Skills library</button></div>`);
  dialog.onclose=()=>{MoyaiUI.render(dialog, '');};dialog.querySelector('[aria-label="Close skill picker"]').onclick=()=>dialog.close();
  dialog.querySelectorAll('[data-pick-skill]').forEach(b=>b.onclick=()=>{const skill=data.skills.find(s=>s.id===b.dataset.pickSkill);dialog.close();insertSkill(skill,inputId,input);});
  $('#manage-skills').onclick=()=>{dialog.close();navigate('skills').catch(showError);};dialog.showModal();
}
document.addEventListener('click',e=>{const button=e.target.closest('[data-skill-picker]');if(button)openSkillPicker(button.dataset.skillPicker).catch(showError);});

function skillSuggestionEvidence(suggestion){
  return `<p>${esc(suggestion.reason)}</p><details class="settings-hint"><summary>Evidence from completed work</summary><p class="subtext">Drafted from your requests and recorded commands. Review the instructions for accuracy before saving.</p>${suggestion.evidence.map(citation=>{const source=suggestion.sources.find(s=>s.message_id===citation.message_id);return `<blockquote>${esc(citation.quote)}</blockquote>${source?`<a href="#run=${esc(source.run_id)}" data-suggestion-source>Open original session</a>${source.commands.filter(c=>citation.tool_ids.includes(c.id)).map(c=>`<p><code>${esc(c.command)}</code> · exit code 0</p>`).join('')}`:''}`;}).join('')}</details>`;
}
async function renderSkillLearning(version=state.pageVersion){
  const host=$('#skill-learning'),user=state.userId;
  if(!host)return;
  const current=()=>version===state.pageVersion&&user===state.userId&&$('#skill-learning')===host;
  try{
    const data=await api('/api/skill-learning');if(!current())return;
    MoyaiUI.render(host, `<div class="memory-controls"><div><h2>Learn from completed work</h2><p class="subtext">Moyai can suggest reusable workflows after your tasks finish. Suggestions stay private until you review and save them.</p><p class="subtext">Starts with new requests in conversations where only you have asked questions. Turning this off cancels reviews and removes unaccepted drafts.</p></div><label><input id="skill-learning-toggle" type="checkbox" ${data.preferences.enabled?'checked':''} ${!data.configured&&!data.preferences.enabled?'disabled':''}> Suggest skills</label></div>${!data.configured?'<p class="subtext">Background learning needs an enabled model connection.</p>':''}<div class="section-header"><h2>Suggested skills${data.suggestions.length?' · '+data.suggestions.length:''}</h2><button class="quiet" id="skill-learning-refresh">Refresh suggestions</button></div>${data.suggestions.length?data.suggestions.map(s=>`<article class="skill-card"><div class="skill-info"><div class="skill-title"><strong>${esc(s.name)}</strong><span class="secret-scope">${s.target_id?'Suggested update':'Private draft'}</span></div><p>${esc(s.description)}</p><p class="subtext">${esc(s.reason)}</p></div><div class="skill-card-actions"><button data-review-suggestion="${esc(s.id)}">Review</button><button class="quiet" data-dismiss-suggestion="${esc(s.id)}">Dismiss</button></div></article>`).join(''):`<p class="subtext">${data.preferences.enabled?'No suggestions yet. Moyai will look for repeated workflows and useful procedures after new tasks finish.':'Enable suggestions to learn reusable procedures from future work.'}</p>`}<p id="skill-learning-error" role="alert"></p>`);
    $('#skill-learning-refresh').onclick=()=>renderSkillLearning(version);
    $('#skill-learning-toggle').onchange=async e=>{
      const toggle=e.target,enabled=toggle.checked;toggle.disabled=true;
      try{
        if(!enabled&&!await confirmSettingsAction('Turn off skill learning?','Pending reviews will be cancelled and unaccepted drafts removed. Your saved skills stay available.','Turn off')){toggle.checked=true;return;}
        if(!current())return;
        await api('/api/skill-learning/preferences',{method:'PUT',body:JSON.stringify({enabled,revision:data.preferences.revision})});
        if(current())await renderSkillLearning(version);
      }catch(error){if(current()){$('#skill-learning-error').textContent=error.message;toggle.checked=!enabled;}}
      finally{toggle.disabled=false;}
    };
    host.querySelectorAll('[data-review-suggestion]').forEach(b=>b.onclick=async()=>{
      b.disabled=true;
      try{const suggestion=await api('/api/skill-learning/suggestions/'+b.dataset.reviewSuggestion);if(current()){await openSkillEditor(suggestion.target_id,suggestion);document.querySelectorAll('[data-suggestion-source]').forEach(link=>link.onclick=()=>$('#skill-dialog').close());}}
      catch(error){if(current())$('#skill-learning-error').textContent=error.message;}finally{b.disabled=false;}
    });
    host.querySelectorAll('[data-dismiss-suggestion]').forEach(b=>b.onclick=async()=>{
      if(!await confirmSettingsAction('Dismiss this suggestion?','This draft will be removed. The same workflow name or update to this skill revision will not be suggested again for this repository.','Dismiss')||!current())return;
      b.disabled=true;
      try{await api('/api/skill-learning/suggestions/'+b.dataset.dismissSuggestion+'/dismiss',{method:'POST'});if(current())await renderSkillLearning(version);}
      catch(error){if(current())$('#skill-learning-error').textContent=error.message;b.disabled=false;}
    });
  }catch(error){if(current()){MoyaiUI.render(host,`<p role="alert">${esc(error.message)}</p><button id="skill-learning-retry">Retry suggestions</button>`);$('#skill-learning-retry').onclick=()=>renderSkillLearning(version);}}
}
