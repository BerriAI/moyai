/* Resolve model-written references only against the authenticated saved archive. */
(function(root,factory){
  const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.MoyaiFiles=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  function reference(value){
    let path=String(value||'').trim();
    try{path=decodeURIComponent(path);}catch{return null;}
    path=path.replace(/^sandbox:(?=\/workspace\/)/,'').replace(/^\/workspace\//,'').replace(/^\.\//,'');
    const suffix=path.match(/(?::([1-9]\d*)(?::([1-9]\d*))?|#L([1-9]\d*)(?:-L([1-9]\d*))?)$/);
    const line=suffix?Number(suffix[1]||suffix[3]):null,endLine=suffix?Number(suffix[4]||line):null;
    if(suffix){
      if(!Number.isSafeInteger(line)||!Number.isSafeInteger(endLine)||endLine<line||(suffix[2]&&!Number.isSafeInteger(Number(suffix[2]))))return null;
      path=path.slice(0,suffix.index);
    }
    if(!path||path.startsWith('/')||/[\\:#?\x00-\x1f\x7f]/.test(path)||path.split('/').some(p=>!p||p==='.'||p==='..'))return null;
    return {path,line,endLine};
  }
  function resolve(value,files){
    const path=reference(value)?.path;if(!path)return null;
    const exact=files.filter(file=>file.workspace_path===path);
    if(exact.length===1)return exact[0];
    // A bare filename may be shorthand for a nested file, but never guess
    // between two repositories or reinterpret an explicit missing path.
    if(path.includes('/'))return null;
    const matches=files.filter(file=>file.workspace_path&&file.name===path);
    return matches.length===1?matches[0]:null;
  }
  function sessionLink(hash){
    if(typeof hash!=='string'||hash.length>1600)return null;
    const match=/^#run=([a-f0-9]{32})&file=([^&#\s]+)$/.exec(hash);
    if(!match||match[0]!==hash)return null;
    let fileRef;try{fileRef=decodeURIComponent(match[2]);}catch{return null;}
    const location=reference(fileRef);
    if(fileRef.length>1024||!location||!/\.(md|markdown)$/i.test(location.path))return null;
    return {runId:match[1],fileRef,hash};
  }
  function decorateReferences(container,files,{runId,onOpen}={}){
    if(!container)return;
    container.querySelectorAll('.markdown [data-file-ref],.markdown strong,.markdown code').forEach(node=>{
      if(node.closest('pre')||(!node.matches('[data-file-ref]')&&(node.closest('a')||node.children.length)))return;
      const ref=node.dataset.fileRef||node.textContent,file=resolve(ref,files);
      if(!file){
        if(node.dataset.savedFile){
          node.removeAttribute('href');delete node.dataset.savedFile;delete node.dataset.fileRun;
          node.classList.remove('saved-file-link','saved-image-link');node.onclick=null;
          if(node.dataset.fileImage)node.textContent=node.dataset.fileLabel;
        }
        if(node.matches('[data-file-ref]')){
          node.title='This file is not available in this session’s saved files.';
          node.setAttribute('aria-disabled','true');
        }
        return;
      }
      let link=node;
      if(node.tagName!=='A'){link=node.ownerDocument.createElement('a');link.textContent=node.textContent;node.replaceChildren(link);}
      link.href=file.url;link.classList.add('saved-file-link');link.dataset.fileRef=ref;link.dataset.savedFile=file.archive_path;
      link.removeAttribute('aria-disabled');
      if(runId)link.dataset.fileRun=runId;else delete link.dataset.fileRun;
      link.removeAttribute('target');link.removeAttribute('rel');link.title='Preview '+file.path;
      if(link.dataset.fileImage){
        const label=link.dataset.fileLabel??=link.textContent||file.name;
        if(file.kind==='image'&&file.inline_url){
          link.classList.add('saved-image-link');
          if(link.querySelector('img')?.getAttribute('src')!==file.inline_url){
            const image=link.ownerDocument.createElement('img');image.src=file.inline_url;image.alt=label;image.loading='lazy';image.decoding='async';
            const caption=link.ownerDocument.createElement('span');caption.className='saved-image-caption';caption.textContent=label;
            const hint=link.ownerDocument.createElement('span');hint.textContent='Open preview ↗';caption.append(hint);
            image.onerror=()=>{image.hidden=true;link.classList.remove('saved-image-link');};
            link.replaceChildren(image,caption);
          }
        }else{link.textContent=label;link.classList.remove('saved-image-link');}
      }
      if(onOpen)link.onclick=event=>{
        if(event.ctrlKey||event.metaKey||event.shiftKey||event.altKey||event.button)return;
        event.preventDefault();onOpen(file,ref);
      };
    });
  }
  function preview(result,ref,{escape:esc,markdown}){
    if(result.text===null)return '<p class="saved-file-empty">Preview isn’t available for this file type. Use Download to open it.</p>';
    const location=reference(ref),notice=result.truncated?'<p class="saved-file-notice">Showing the first 128 KB. Use Download for the complete file.</p>':'';
    if(!location?.line)return notice+(result.format==='markdown'?`<div class="markdown">${markdown(result.text)}</div>`:`<pre class="saved-file-text">${esc(result.text)}</pre>`);
    const lines=result.text.split('\n'),{line,endLine}=location,available=lines.length-(result.truncated?1:0);
    const label=line===endLine?'Line '+line:'Lines '+line+'–'+endLine;
    if(endLine>available)return notice+`<p class="saved-file-notice">${label} is not included in this saved preview.</p><pre class="saved-file-text">${esc(result.text)}</pre>`;
    const before=lines.slice(0,line-1).join('\n'),selected=lines.slice(line-1,endLine).join('\n'),after=lines.slice(endLine).join('\n');
    return notice+`<p class="saved-file-location">${label}</p><pre class="saved-file-text">${esc(before)}${line>1?'\n':''}<mark class="saved-source-selection" data-source-line="${line}" tabindex="-1" aria-label="${label}">${esc(selected)||'\u200b'}</mark>${endLine<lines.length?'\n':''}${esc(after)}</pre>`;
  }
  function reveal(container){
    const target=container.querySelector('[data-source-line]');
    if(target){target.focus({preventScroll:true});target.scrollIntoView({block:'center',inline:'nearest',behavior:'instant'});}
  }
  function create({api,markdown,escape:esc,size,onOpen}){
    let run=null,catalog=null,key='',loading=null,requestId=0,previewId=0,viewId=0,selected=null,dialog=null;
    const doc=document;
    const query=selector=>doc.querySelector(selector);
    function decorate(container){
      if(!container||!catalog||!run)return;
      const following=container.id==='conversation'&&container.scrollHeight-container.scrollTop-container.clientHeight<100;
      decorateReferences(container,catalog.files,{runId:run.id});
      if(following)container.scrollTop=container.scrollHeight;
    }
    function update(){
      const button=query('#files-button');
      if(button){button.hidden=!(run?.has_artifact||run?.has_captures);button.textContent='Files'+(catalog?' · '+catalog.files.length:'');button.onclick=()=>open();}
      const area=query('#artifact-area');
      if(area)MoyaiUI.render(area, (run?.has_artifact||run?.has_captures)?`<button type="button" class="quiet browse-files">Browse saved files${catalog?' · '+catalog.files.length:''}</button>${run.has_artifact?`<a class="session-download" href="/api/runs/${run.id}/artifact">↓ Download workspace ZIP</a>`:''}`:'');
      if(area?.querySelector('.browse-files'))area.querySelector('.browse-files').onclick=()=>open();
      decorate(query('#conversation'));
    }
    async function load(force=false){
      if(!(run?.has_artifact||run?.has_captures))return;
      const next=run.id+':'+(run.events||[]).filter(e=>e.kind==='artifact').at(-1)?.id;
      if(!force&&key===next)return loading;
      key=next;const id=++requestId,rid=run.id;
      loading=(async()=>{
        try{
          const data=await api(`/api/runs/${rid}/files`);
          if(id!==requestId)return;
          catalog=data;update();
        }catch(error){if(id!==requestId)return;catalog={files:[],error:error.message};key='';update();}
      })();
      return loading;
    }
    function sync(value){
      if(run?.id!==value.id){requestId++;previewId++;catalog=null;key='';dialog?.close();}
      run=value;update();load();
    }
    function reset(){run=null;catalog=null;key='';requestId++;previewId++;dialog?.close();}
    function drawList(){
      const filter=query('#saved-file-search').value.toLowerCase();
      const files=(catalog?.files||[]).filter(file=>file.path.toLowerCase().includes(filter));
      MoyaiUI.render(query('#saved-file-list'), files.length?files.map(file=>`<button type="button" class="saved-file-choice ${file.archive_path===selected?.archive_path?'selected':''}" data-file-choice="${esc(file.archive_path)}" ${file.archive_path===selected?.archive_path?'aria-current="true"':''}><span class="saved-file-icon" aria-hidden="true">▤</span><span><strong>${esc(file.name)}</strong><small>${esc(file.path)} · ${size(file.size)}</small></span></button>`).join(''):'<p class="saved-file-empty">No matching files.</p>');
      query('#saved-file-list').querySelectorAll('[data-file-choice]').forEach(button=>button.onclick=()=>select(catalog.files.find(file=>file.archive_path===button.dataset.fileChoice)));
    }
    async function select(file,ref=null){
      if(!file)return;selected=file;const id=++previewId;
      drawList();
      query('#saved-file-title').textContent=file.name;
      const location=reference(ref);
      query('#saved-file-path').textContent=file.path+(location?.line?':'+location.line+(location.endLine!==location.line?'–'+location.endLine:''):'');
      const download=query('#saved-file-download');download.href=file.url;download.hidden=false;download.setAttribute('download',file.name);
      MoyaiUI.render(query('#saved-file-preview'), '<p class="saved-file-empty" role="status">Opening file…</p>');
      if(file.inline_url&&['image','video'].includes(file.kind)){
        MoyaiUI.render(query('#saved-file-preview'), file.kind==='image'?`<img class="saved-capture" src="${esc(file.inline_url)}" alt="${esc(file.name)}">`:`<video class="saved-capture" src="${esc(file.inline_url)}" controls preload="metadata"></video>`);
        return;
      }
      try{
        const result=await api(file.preview_url);if(id!==previewId||!dialog.open)return;
        const target=query('#saved-file-preview');
        MoyaiUI.render(target, preview(result,ref,{escape:esc,markdown}));
        target.scrollTop=0;decorate(target);reveal(target);
        target.querySelectorAll('.copy-code').forEach(button=>button.onclick=async()=>{try{await navigator.clipboard.writeText(button.closest('.code-block').querySelector('code').textContent);button.textContent='Copied';}catch{button.textContent='Select text to copy';}});
      }catch(error){if(id===previewId&&dialog.open)MoyaiUI.render(query('#saved-file-preview'), `<p class="saved-file-empty" role="alert">${esc(error.message)}</p>`);}
    }
    async function open(file=null,ref=null){
      if(onOpen?.(file,ref))return;
      if(!(run?.has_artifact||run?.has_captures))return;
      if(!dialog){dialog=MoyaiUI.createDialog();dialog.id='saved-files-dialog';dialog.setAttribute('aria-labelledby','saved-files-heading');doc.body.append(dialog);dialog.addEventListener('close',()=>{previewId++;viewId++;dialog.querySelectorAll('video').forEach(video=>video.pause());});}
      const rid=run.id,viewing=++viewId;
      MoyaiUI.render(dialog, `<header class="saved-files-heading"><div><h2 id="saved-files-heading">Saved files</h2><p>Latest saved version of this workspace</p></div><button type="button" class="icon-button" aria-label="Close saved files">×</button></header><div class="saved-files-loading" role="status">Loading saved files…</div>`);
      dialog.querySelector('button').onclick=()=>dialog.close();
      if(!dialog.open)dialog.showModal();
      await load(true);if(run?.id!==rid||!dialog.open||viewing!==viewId)return;
      MoyaiUI.replace(dialog.querySelector('.saved-files-loading'), `<div class="saved-files-body"><aside class="saved-files-nav"><input id="saved-file-search" type="search" placeholder="Find a file…" aria-label="Find a saved file"><div id="saved-file-list"></div></aside><section class="saved-file-view"><div class="saved-file-heading"><div><h3 id="saved-file-title">Select a file</h3><p id="saved-file-path"></p></div><a id="saved-file-download" class="small" hidden>↓ Download</a></div><div id="saved-file-preview"><p class="saved-file-empty">Choose a file to preview.</p></div></section></div><footer class="saved-files-footer"><span>${esc(catalog?.error||catalog?.note||'')}${catalog?.limited?' Some entries cannot be previewed.':''}</span>${run.has_artifact?`<a href="/api/runs/${rid}/artifact">Download ZIP</a>`:''}</footer>`);
      selected=null;drawList();query('#saved-file-search').oninput=drawList;
      const choice=file?catalog.files.find(item=>item.archive_path===file.archive_path):catalog.files.find(item=>item.workspace_path)||catalog.files[0];
      if(choice)select(choice,ref);
    }
    doc.addEventListener('click',event=>{
      const link=event.target.closest('[data-saved-file]');
      if(!link||link.dataset.fileRun!==run?.id||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey||event.button)return;
      const file=catalog?.files.find(item=>item.archive_path===link.dataset.savedFile);if(!file)return;
      event.preventDefault();if(dialog?.open){if(query('#saved-file-list'))select(file,link.dataset.fileRef);}else open(file,link.dataset.fileRef);
    });
    return {sync,decorate,reset,open};
  }
  return {reference,resolve,sessionLink,decorate:decorateReferences,preview,reveal,create};
});
