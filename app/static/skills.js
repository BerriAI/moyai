function skillToken(skill){return '/'+skill.reference;}
function skillCard(skill){
  return `<article class="skill-card"><div><div class="skill-title"><strong>${esc(skill.name)}</strong><span class="secret-scope">${skill.scope==='personal'?'Personal':'Organization'}</span>${skill.archived?'<span class="secret-scope">Archived</span>':''}</div><p>${esc(skill.description)}</p><code>${esc(skillToken(skill))}</code></div><div class="skill-card-actions">${!skill.archived?`<button class="quiet" data-use-skill="${esc(skill.id)}">Use in chat</button>`:''}<button class="quiet" data-edit-skill="${esc(skill.id)}">${skill.can_manage?'Edit':'View'}</button>${skill.can_manage?`<button class="quiet" data-archive-skill="${esc(skill.id)}">${skill.archived?'Restore':'Archive'}</button>`:''}</div></article>`;
}
async function renderSkills(){
  const version=state.pageVersion,data=await api('/api/skills?archived=true');if(version!==state.pageVersion)return;
  $('#content').innerHTML=`<section class="skills-page"><div class="section-header"><div><h1>Skills</h1><p class="subtext">Reusable instructions for how Moyai works. You can also ask Moyai to save a skill directly in chat.</p></div><button id="add-skill">Add skill</button></div><p class="secret-sharing">Personal skills are available for your requests. Organization skills are available to every signed-in teammate and managed by admins. Session conversations and results keep their existing sharing.</p><div class="skills-filters"><input type="search" id="skill-search" aria-label="Search skills" placeholder="Search skills"><label><input type="checkbox" id="skill-show-archived"> Show archived</label></div><div id="skill-library"></div><p class="subtext">Pick a skill in chat or mention its reference. Moyai can also use a matching skill when its description fits your request. Skills follow the same app permissions and approval rules.</p></section>`;
  const draw=()=>{
    const search=$('#skill-search').value.trim().toLowerCase(),archived=$('#skill-show-archived').checked;
    const rows=data.skills.filter(s=>(archived||!s.archived)&&(s.name+' '+s.description).toLowerCase().includes(search));
    $('#skill-library').innerHTML=rows.length?rows.map(skillCard).join(''):'<div class="card"><p>No skills to show.</p><p class="subtext">Add a Markdown workflow for benchmarks, reviews, or another repeatable task.</p></div>';
    document.querySelectorAll('[data-edit-skill]').forEach(b=>b.onclick=()=>openSkillEditor(b.dataset.editSkill).catch(showError));
    document.querySelectorAll('[data-use-skill]').forEach(b=>b.onclick=async()=>{try{const skill=rows.find(s=>s.id===b.dataset.useSkill);await navigate('tasks');insertSkill(skill,'prompt');}catch(e){showError(e);}});
    document.querySelectorAll('[data-archive-skill]').forEach(b=>b.onclick=async()=>{
      const skill=rows.find(s=>s.id===b.dataset.archiveSkill);b.disabled=true;
      try{await api('/api/skills/'+skill.id+'/archive',{method:'POST',body:JSON.stringify({archived:!skill.archived,revision:skill.revision})});toast(skill.archived?'Skill restored.':'Skill archived. You can restore it here.');await renderSkills();}catch(e){b.disabled=false;showError(e);}
    });
  };draw();$('#skill-search').oninput=draw;$('#skill-show-archived').onchange=draw;
  $('#add-skill').onclick=()=>openSkillEditor().catch(showError);
}
async function openSkillEditor(id=''){
  const skill=id?await api('/api/skills/'+id):null,canEdit=!skill||skill.can_manage,admin=state.role==='admin';
  const dialog=$('#skill-dialog');
  const references=skill?.files?.length?`<div class="field"><label>Supporting files</label><ul>${skill.files.map(file=>`<li><a href="/api/skills/${encodeURIComponent(skill.id)}/files/${file.path.split('/').map(encodeURIComponent).join('/')}" download>${esc(file.path)}</a> <small>${Math.ceil(file.size/1024)} KB</small></li>`).join('')}</ul><small>These files are preserved when you edit the instructions. Ask Moyai in chat to update or remove a supporting file.</small></div>`:'';
  dialog.innerHTML=`<form id="skill-form" class="skill-editor"><button type="button" class="dialog-close" aria-label="Close skill editor">×</button><h2>${skill?(canEdit?'Edit skill':'View skill'):'Add a skill'}</h2><div class="skill-form-row"><div class="field"><label for="skill-name">Name</label><input id="skill-name" required maxlength="64" pattern="[a-z0-9]+(-[a-z0-9]+)*" placeholder="benchmark-review" value="${esc(skill?.name||'')}" ${canEdit?'':'readonly'}><small>Lowercase letters, numbers and hyphens.</small></div><div class="field"><label for="skill-scope">Who can use it?</label><select id="skill-scope" ${canEdit?'':'disabled'}><option value="personal" ${skill?.scope==='organization'?'':'selected'}>Personal · my requests only</option>${admin||skill?.scope==='organization'?`<option value="organization" ${skill?.scope==='organization'?'selected':''}>Organization · everyone</option>`:''}</select></div></div><div class="field"><label for="skill-description">When should Moyai use this skill?</label><input id="skill-description" required minlength="3" maxlength="320" placeholder="Review benchmark results and report failures and coverage." value="${esc(skill?.description||'')}" ${canEdit?'':'readonly'}></div><div class="field"><div class="skill-instructions-label"><label for="skill-instructions">Markdown instructions</label>${canEdit?'<label class="skill-import">Import .md<input type="file" id="skill-import" accept=".md,.markdown,text/markdown,text/plain" aria-label="Import Markdown skill"></label>':''}</div><textarea id="skill-instructions" required minlength="3" maxlength="32000" rows="12" placeholder="# Benchmark review\n\n1. Check case coverage.\n2. Identify failed cases.\n3. Summarize evidence and limitations." ${canEdit?'':'readonly'}>${esc(skill?.instructions||'')}</textarea><small>Paste or import a SKILL.md. To include reference files, attach them in chat and ask Moyai to save or update this skill. Do not put API keys or other secrets in a skill.</small></div>${references}<p class="subtext">Personal controls who can use the skill. It does not make shared session outputs private. Shared skills are managed by admins.</p><p id="skill-form-error" role="alert"></p><div class="credential-actions">${canEdit?'<button type="submit">Save skill</button>':''}<button type="button" class="quiet" id="cancel-skill">Close</button></div></form>`;
  const close=()=>dialog.close();dialog.onclose=()=>{dialog.innerHTML='';};
  dialog.querySelector('[aria-label="Close skill editor"]').onclick=close;$('#cancel-skill').onclick=close;
  if($('#skill-import'))$('#skill-import').onchange=async e=>{
    const file=e.target.files[0];if(!file)return;
    if(file.size>128000){$('#skill-form-error').textContent='Choose a Markdown file with at most 32,000 characters.';return;}
    const text=await file.text();if(!dialog.open||!$('#skill-instructions'))return;if(text.length>32000||text.includes('\0')){$('#skill-form-error').textContent='Choose a text Markdown file with at most 32,000 characters.';return;}
    $('#skill-instructions').value=text;$('#skill-form-error').textContent='';
  };
  const clientId=crypto.randomUUID();
  $('#skill-form').onsubmit=async e=>{
    e.preventDefault();if(!canEdit)return;const b=e.currentTarget.querySelector('[type="submit"]');b.disabled=true;
    try{await api('/api/skills'+(id?'/'+id:''),{method:id?'PUT':'POST',body:JSON.stringify({name:$('#skill-name').value,description:$('#skill-description').value,instructions:$('#skill-instructions').value,scope:$('#skill-scope').value,revision:skill?.revision||0,client_id:clientId})});dialog.close();toast('Skill saved. It is available for new requests.');if(state.view==='skills')await renderSkills();}
    catch(error){if($('#skill-form-error'))$('#skill-form-error').textContent=error.message;else showError(error);b.disabled=false;}
  };
  dialog.showModal();
}
function insertSkill(skill,inputId){
  const input=document.getElementById(inputId);if(!input)return;
  if(!input.value.split(/\s+/).includes(skillToken(skill)))input.value=skillToken(skill)+' '+input.value;
  input.dispatchEvent(new Event('input',{bubbles:true}));autoSize(input);input.focus();
}
async function openSkillPicker(inputId){
  const version=state.pageVersion,data=await api('/api/skills');if(version!==state.pageVersion)return;
  const dialog=$('#skill-dialog');
  dialog.innerHTML=`<div class="skill-picker"><button type="button" class="dialog-close" aria-label="Close skill picker">×</button><h2>Use a skill</h2><p class="subtext">Choose a workflow for your next message.</p><div class="skill-picker-list">${data.skills.map(s=>`<button class="skill-choice" data-pick-skill="${esc(s.id)}"><strong>${esc(s.name)} <small>${s.scope==='personal'?'Personal':'Organization'}</small></strong><span>${esc(s.description)}</span></button>`).join('')||'<p>No skills yet. Add one in the Skills library.</p>'}</div><button class="quiet" id="manage-skills">Open Skills library</button></div>`;
  dialog.onclose=()=>{dialog.innerHTML='';};dialog.querySelector('[aria-label="Close skill picker"]').onclick=()=>dialog.close();
  dialog.querySelectorAll('[data-pick-skill]').forEach(b=>b.onclick=()=>{const skill=data.skills.find(s=>s.id===b.dataset.pickSkill);dialog.close();insertSkill(skill,inputId);});
  $('#manage-skills').onclick=()=>{dialog.close();navigate('skills').catch(showError);};dialog.showModal();
}
document.addEventListener('click',e=>{const button=e.target.closest('[data-skill-picker]');if(button)openSkillPicker(button.dataset.skillPicker).catch(showError);});
