/* Read-only GitHub details. This view has no computer or sandbox lifecycle. */
(function(root,factory){
  const value=factory();if(typeof module==='object'&&module.exports)module.exports=value;else root.MoyaiPullRequest=value;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  function presentation(data){
    const state=data?.merged||data?.state==='merged'?'merged':data?.state==='closed'?'closed':data?.state==='open'?(data.draft?'draft':'open'):'unknown';
    return {state,label:{merged:'Merged',closed:'Closed',draft:'Draft',open:'Open',unknown:'Status unavailable'}[state],
      icon:state==='merged'?'git-merge':state==='closed'?'pull-request-closed':'pull-request'};
  }
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
  function fileDiff(file,esc){
    return (file.patch?`<div class="pr-diff-scroll" role="region" aria-label="Diff for ${esc(file.filename)}" tabindex="0"><table class="pr-diff"><tbody>${diffRows(file.patch).map(row=>`<tr class="pr-${row.kind}"><td class="pr-line">${row.old}</td><td class="pr-line">${row.next}</td><td class="pr-code"><code>${esc(row.text)}</code></td></tr>`).join('')}</tbody></table></div>`:'<p class="pr-notice">GitHub did not provide a text diff for this file. View it on GitHub.</p>')+
      (file.patch_truncated?'<p class="pr-notice">This diff is truncated. Open GitHub to see the complete file.</p>':'');
  }
  function changes(data,esc){
    const files=data.files||[];
    return (data.files_truncated?'<div data-region-key="notice" data-region-leaf><p class="pr-notice">Showing the first 100 files. Open GitHub for all changes.</p></div>':'')+
      (files.length?files.map(file=>`<div data-region-key="file:${esc(file.filename)}" data-region-preserve data-pr-file="${esc(file.filename)}"></div>`).join(''):'<div data-region-key="empty" data-region-leaf><p class="pr-notice">No changed files.</p></div>');
  }
  function mount({element,url,load,markdown,escape:esc,onStatus=()=>{}}){
    let alive=true,active=false,epoch=0,data=null,section='changes',initialized=false;
    let payloadSignature='',headingSignature='',changesSignature='',descriptionSignature=null;
    const expanded=new Set(),files=new Map(),scroll={changes:0,description:0};
    MoyaiUI.render(element, `<div class="native-pr">
      <div class="pr-toolbar"><span data-state class="pr-state">Pull request</span><span class="pr-toolbar-spacer"></span><button type="button" data-refresh aria-label="Refresh pull request">Refresh</button><a href="${esc(url)}" target="_blank" rel="noopener noreferrer">Open in GitHub ↗</a></div>
      <header class="pr-heading" data-heading></header>
      <p class="pr-load-status" data-status role="status" aria-live="polite"></p>
      <div class="pr-sections" role="group" aria-label="Pull request sections"><button type="button" data-section="changes" aria-pressed="true">Changes <span data-count></span></button><button type="button" data-section="description" aria-pressed="false">Description</button></div>
      <div class="pr-content" data-content><div data-pr-section="changes"></div><div data-pr-section="description" hidden></div></div></div>`);
    const q=selector=>element.querySelector(selector),content=q('[data-content]'),status=q('[data-status]'),refresh=q('[data-refresh]');
    const sections={changes:q('[data-pr-section="changes"]'),description:q('[data-pr-section="description"]')};
    function renderDiff(entry){
      if(!entry.detail.open||entry.rendered===entry.signature)return;
      MoyaiUI.render(entry.body,fileDiff(entry.file,esc));entry.rendered=entry.signature;
    }
    function renderChanges(){
      const markup=changes(data,esc);
      if(markup!==changesSignature){MoyaiRegions.sync(sections.changes,markup);changesSignature=markup;}
      const current=new Map((data.files||[]).map(file=>[file.filename,file]));
      for(const name of files.keys())if(!current.has(name)){files.delete(name);expanded.delete(name);}
      sections.changes.querySelectorAll('[data-pr-file]').forEach(host=>{
        const name=host.dataset.prFile,file=current.get(name);let entry=files.get(name);
        if(!entry){
          // The wrapper is native; its disclosure and diff body own independent
          // component roots. Never reconcile the disclosure over a loaded diff.
          MoyaiUI.render(host,`<details class="pr-file" data-file="${esc(name)}" ${expanded.has(name)?'open':''}><summary><span class="pr-filename">${esc(name)}</span><span class="pr-file-status"></span><span class="pr-additions"></span><span class="pr-deletions"></span></summary><div data-diff-body></div></details>`);
          entry={detail:host.querySelector('[data-file]'),body:host.querySelector('[data-diff-body]'),rendered:null};files.set(name,entry);
          entry.detail.ontoggle=()=>{
            if(!alive||!entry.detail.isConnected)return;
            if(entry.detail.open){expanded.add(name);renderDiff(entry);}else expanded.delete(name);
          };
        }
        entry.file=file;entry.signature=JSON.stringify([file.patch||'',!!file.patch_truncated]);
        const setText=(selector,value)=>{const node=entry.detail.querySelector(selector);if(node.textContent!==value)node.textContent=value;};
        setText('.pr-file-status',file.status||'');setText('.pr-additions','+'+(file.additions??0));setText('.pr-deletions','−'+(file.deletions??0));
        if(entry.detail.open)renderDiff(entry);
        else if(entry.rendered!==null&&entry.rendered!==entry.signature){MoyaiUI.render(entry.body,'');entry.rendered=null;}
      });
    }
    function renderDescription(){
      if(!data)return;
      const signature=JSON.stringify([data.body||'',!!data.body_truncated]);
      if(descriptionSignature===signature)return;
      MoyaiUI.render(sections.description,`<div class="pr-description markdown">${data.body?markdown(data.body):'<p>No description provided.</p>'}</div>${data.body_truncated?'<p class="pr-notice">Description truncated. Open GitHub to read it in full.</p>':''}`);
      descriptionSignature=signature;
      sections.description.querySelectorAll('.copy-code').forEach(button=>button.onclick=async()=>{
        try{await navigator.clipboard.writeText(button.closest('.code-block').querySelector('code').textContent);button.textContent='Copied';}
        catch{button.textContent='Select to copy';}
      });
    }
    function clear(){
      data=null;initialized=false;expanded.clear();files.clear();payloadSignature='';headingSignature='';changesSignature='';descriptionSignature=null;
      MoyaiUI.render(q('[data-heading]'),'');MoyaiRegions.sync(sections.changes,'');MoyaiUI.render(sections.description,'');
      scroll.changes=scroll.description=content.scrollTop=0;q('[data-count]').textContent='';
    }
    function render(){
      const signature=JSON.stringify(data);if(signature===payloadSignature)return;
      const {state,label}=presentation(data);
      q('[data-state]').className='pr-state pr-state-'+state;
      q('[data-state]').textContent=label;
      const date=data.created_at?new Date(data.created_at):null;
      const opened=date&&!Number.isNaN(date.getTime())?'Opened '+date.toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'}):'';
      const repository=data.repository||url.split('/').slice(3,5).join('/'),changedFiles=data.changed_files??data.files?.length??0;
      const heading=`<p class="pr-repository">${esc(repository)} · #${esc(data.number)}</p><h2>${esc(data.title)}</h2>
        <div class="pr-branches">${data.author?`<span class="pr-author">${esc(data.author)}</span>`:''}<code>${esc(data.base)}</code><span aria-label="from">←</span><code>${esc(data.head_ref||data.head?.slice(0,7)||'')}</code></div>
        <p class="pr-stats">${opened?`<span>${esc(opened)}</span>`:''}<span>${esc(changedFiles)} files</span><span class="pr-additions">+${esc(data.additions??0)}</span><span class="pr-deletions">−${esc(data.deletions??0)}</span></p>`;
      if(heading!==headingSignature){MoyaiUI.render(q('[data-heading]'),heading);headingSignature=heading;}
      q('[data-count]').textContent=String(changedFiles);
      if(!initialized){if(data.files?.[0])expanded.add(data.files[0].filename);initialized=true;}
      renderChanges();
      if(section==='description')renderDescription();
      else if(descriptionSignature!==null&&descriptionSignature!==JSON.stringify([data.body||'',!!data.body_truncated])){
        MoyaiUI.render(sections.description,'');descriptionSignature=null;
      }
      payloadSignature=signature;
    }
    async function read(){
      const request=++epoch;
      if(!alive||!active)return;
      refresh.disabled=true;status.textContent=data?'Refreshing…':'Loading pull request…';
      try{
        const result=await load();
        if(!alive||!active||request!==epoch)return;
        data=result;render();onStatus(data);status.textContent='';
      }catch(error){
        if(!alive||!active||request!==epoch)return;
        if([401,403,404,409].includes(error.status)){
          clear();q('[data-state]').className='pr-state';q('[data-state]').textContent='Unavailable';onStatus(null);
        }
        status.textContent=(data?'Could not refresh. Showing the previous version. ':'')+error.message+' Use Refresh to try again.';
      }finally{if(alive&&active&&request===epoch)refresh.disabled=false;}
    }
    refresh.onclick=read;
    element.querySelectorAll('[data-section]').forEach(button=>button.onclick=()=>{
      if(!alive||section===button.dataset.section)return;
      scroll[section]=content.scrollTop;section=button.dataset.section;
      element.querySelectorAll('[data-section]').forEach(item=>item.setAttribute('aria-pressed',String(item===button)));
      sections.changes.hidden=section!=='changes';sections.description.hidden=section!=='description';
      if(section==='description')renderDescription();content.scrollTop=scroll[section];
    });
    return {
      activate(){active=true;return read();},
      deactivate(){active=false;epoch++;},
      dispose(){alive=false;active=false;epoch++;clear();}
    };
  }
  return {mount,diffRows,changes,presentation};
});
