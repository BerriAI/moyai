/* Assistant output is untrusted. Render Markdown, then enforce a narrow HTML allowlist. */
function renderMarkdown(content) {
  const renderer = new marked.Renderer();
  const fileReferences = [];
  const defaultLink = renderer.link;
  renderer.link = function(token) {
    // Placeholders survive sanitization; only our code adds the data attribute.
    // No local path is ever used as a browser URL.
    if(MoyaiFiles.reference(token.href)) {
      fileReferences.push({href:token.href});
      return defaultLink.call(this,{...token,href:'#saved-file-'+(fileReferences.length-1)});
    }
    return defaultLink.call(this,token);
  };
  renderer.html = token => esc(token.text);
  renderer.image = token => {
    const label=esc(token.text || 'Image');
    if(MoyaiFiles.reference(token.href)){
      fileReferences.push({href:token.href,image:true});
      return `<a href="#saved-file-${fileReferences.length-1}">${label}</a>`;
    }
    // External images are explicit links, never automatic image requests.
    return `<a href="${esc(token.href)}">${label}</a>`;
  };
  const template = document.createElement('template');
  template.innerHTML = DOMPurify.sanitize(marked.parse(String(content || ''), {renderer, gfm:true, breaks:false}), {
    ALLOWED_TAGS:['p','br','strong','em','del','a','code','pre','h1','h2','h3','h4','h5','h6','ul','ol','li','blockquote','hr','table','thead','tbody','tr','th','td'],
    ALLOWED_ATTR:['href','title','start'], ALLOW_DATA_ATTR:false, ALLOW_ARIA_ATTR:false
  });
  template.content.querySelectorAll('a').forEach(link => {
    const href = link.getAttribute('href') || '';
    const fileIndex = href.match(/^#saved-file-(\d+)$/)?.[1];
    const file=fileIndex!==undefined?fileReferences[fileIndex]:null;
    if(file){
      link.dataset.fileRef=file.href;if(file.image)link.dataset.fileImage='true';
      link.removeAttribute('href');link.setAttribute('aria-disabled','true');
      link.title='Available only when this file is in the session’s saved files.';
    }else if(/^(https?:\/\/|mailto:)/i.test(href)){link.target='_blank';link.rel='noopener noreferrer';}
    else link.replaceWith(...link.childNodes);
  });
  template.content.querySelectorAll('pre').forEach(pre => {
    const wrapper = document.createElement('div'); wrapper.className='code-block';
    const header = document.createElement('div'); header.className='code-header';
    const label = document.createElement('span'); label.textContent='Code';
    const copy = document.createElement('button'); copy.type='button'; copy.className='copy-code quiet'; copy.textContent='Copy'; copy.setAttribute('aria-label','Copy code');
    header.append(label,copy); pre.replaceWith(wrapper); wrapper.append(header,pre);
  });
  template.content.querySelectorAll('table').forEach(table => {
    const wrapper=document.createElement('div'); wrapper.className='table-scroll'; wrapper.tabIndex=0; wrapper.setAttribute('aria-label','Scrollable table'); table.replaceWith(wrapper); wrapper.append(table);
  });
  return template.innerHTML;
}
