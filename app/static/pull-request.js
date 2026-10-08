/* Read-only GitHub details. This view has no computer or sandbox lifecycle. */
(function(root,factory){
  const value=factory();if(typeof module==='object'&&module.exports)module.exports=value;else root.MoyaiPullRequest=value;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  function diffRows(patch){
    let oldLine=0,newLine=0;
    return patch.split('\n').map(text=>{
      const hunk=/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(text);
      if(hunk){oldLine=Number(hunk[1]);newLine=Number(hunk[2]);return {text,kind:'hunk',old:'',next:''};}
      if(text.startsWith('+'))return {text,kind:'addition',old:'',next:newLine++};
      if(text.startsWith('-'))return {text,kind:'deletion',old:oldLine++,next:''};
      if(text.startsWith(' '))return {text,kind:'context',old:oldLine++,next:newLine++};
      return {text,kind:'note',old:'',next:''};
    });
  }
  function changes(data,esc,expanded){
    const files=data.files||[];
    return (data.files_truncated?'<p class="pr-notice">Showing the first 100 files. Open GitHub for all changes.</p>':'')+
      (files.length?files.map((file,index)=>`<details class="pr-file" data-file="${index}" ${expanded.has(file.filename)?'open':''}>
        <summary><span class="pr-filename">${esc(file.filename)}</span><span class="pr-file-status">${esc(file.status)}</span><span class="pr-additions">+${esc(file.additions??0)}</span><span class="pr-deletions">−${esc(file.deletions??0)}</span></summary>
        ${file.patch?`<div class="pr-diff-scroll" role="region" aria-label="Diff for ${esc(file.filename)}" tabindex="0"><table class="pr-diff"><tbody>${diffRows(file.patch).map(row=>`<tr class="pr-${row.kind}"><td class="pr-line">${row.old}</td><td class="pr-line">${row.next}</td><td class="pr-code"><code>${esc(row.text)}</code></td></tr>`).join('')}</tbody></table></div>`:'<p class="pr-notice">GitHub did not provide a text diff for this file. View it on GitHub.</p>'}
        ${file.patch_truncated?'<p class="pr-notice">This diff is truncated. Open GitHub to see the complete file.</p>':''}
      </details>`).join(''):'<p class="pr-notice">No changed files.</p>');
  }
  function mount({element,url,load,markdown,escape:esc}){
    let alive=true,active=false,epoch=0,data=null,section='changes',initialized=false;
    const expanded=new Set();
    element.innerHTML=`<div class="native-pr">
      <div class="pr-toolbar"><span data-state class="pr-state">Pull request</span><span class="pr-toolbar-spacer"></span><button type="button" data-refresh aria-label="Refresh pull request">Refresh</button><a href="${esc(url)}" target="_blank" rel="noopener noreferrer">Open in GitHub ↗</a></div>
      <header class="pr-heading" data-heading></header>
      <p class="pr-load-status" data-status role="status" aria-live="polite"></p>
      <div class="pr-sections" role="group" aria-label="Pull request sections"><button type="button" data-section="changes" aria-pressed="true">Changes <span data-count></span></button><button type="button" data-section="description" aria-pressed="false">Description</button></div>
      <div class="pr-content" data-content></div></div>`;
    const q=selector=>element.querySelector(selector),content=q('[data-content]'),status=q('[data-status]'),refresh=q('[data-refresh]');
    function renderContent(){
      if(!data){content.innerHTML='';return;}
      if(section==='description'){
        content.innerHTML=`<div class="pr-description markdown">${data.body?markdown(data.body):'<p>No description provided.</p>'}</div>${data.body_truncated?'<p class="pr-notice">Description truncated. Open GitHub to read it in full.</p>':''}`;
        content.querySelectorAll('.copy-code').forEach(button=>button.onclick=async()=>{
          try{await navigator.clipboard.writeText(button.closest('.code-block').querySelector('code').textContent);button.textContent='Copied';}
          catch{button.textContent='Select to copy';}
        });
      }else{
        content.innerHTML=changes(data,esc,expanded);
        content.querySelectorAll('[data-file]').forEach(detail=>detail.ontoggle=()=>{
          if(!detail.isConnected)return;
          const name=data.files[Number(detail.dataset.file)]?.filename;
          if(detail.open)expanded.add(name);else expanded.delete(name);
        });
      }
    }
    function render(){
      const state=data.merged?'merged':data.state==='closed'?'closed':data.draft?'draft':data.state==='open'?'open':'unknown';
      q('[data-state]').className='pr-state pr-state-'+state;
      q('[data-state]').textContent={merged:'Merged',closed:'Closed',draft:'Draft',open:'Open',unknown:'Status unavailable'}[state];
      const date=data.created_at?new Date(data.created_at):null;
      const opened=date&&!Number.isNaN(date.getTime())?'Opened '+date.toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'}):'';
      const repository=data.repository||url.split('/').slice(3,5).join('/');
      q('[data-heading]').innerHTML=`<p class="pr-repository">${esc(repository)} · #${esc(data.number)}</p><h2>${esc(data.title)}</h2>
        <div class="pr-branches">${data.author?`<span class="pr-author">${esc(data.author)}</span>`:''}<code>${esc(data.base)}</code><span aria-label="from">←</span><code>${esc(data.head_ref||data.head?.slice(0,7)||'')}</code></div>
        <p class="pr-stats">${opened?`<span>${esc(opened)}</span>`:''}<span>${esc(data.changed_files??data.files.length)} files</span><span class="pr-additions">+${esc(data.additions??0)}</span><span class="pr-deletions">−${esc(data.deletions??0)}</span></p>`;
      q('[data-count]').textContent=String(data.changed_files??data.files.length);
      if(!initialized){if(data.files[0])expanded.add(data.files[0].filename);initialized=true;}
      renderContent();
    }
    async function read(){
      const request=++epoch;
      if(!alive||!active)return;
      refresh.disabled=true;status.textContent=data?'Refreshing…':'Loading pull request…';
      try{
        const result=await load();
        if(!alive||!active||request!==epoch)return;
        data=result;render();status.textContent='';
      }catch(error){
        if(!alive||!active||request!==epoch)return;
        if([401,403,404,409].includes(error.status)){
          data=null;initialized=false;expanded.clear();q('[data-heading]').innerHTML='';q('[data-count]').textContent='';
          q('[data-state]').className='pr-state';q('[data-state]').textContent='Unavailable';renderContent();
        }
        status.textContent=(data?'Could not refresh. Showing the previous version. ':'')+error.message+' Use Refresh to try again.';
      }finally{if(alive&&active&&request===epoch)refresh.disabled=false;}
    }
    refresh.onclick=read;
    element.querySelectorAll('[data-section]').forEach(button=>button.onclick=()=>{
      section=button.dataset.section;
      element.querySelectorAll('[data-section]').forEach(item=>item.setAttribute('aria-pressed',String(item===button)));
      renderContent();content.scrollTop=0;
    });
    return {
      activate(){active=true;return read();},
      deactivate(){active=false;epoch++;},
      dispose(){alive=false;active=false;epoch++;}
    };
  }
  return {mount,diffRows,changes};
});
