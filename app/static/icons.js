/* Small line icons shared by the shell. Decorative only: callers keep accessible labels. */
(function(root){
  const paths={
    cube:'<path d="m12 3 8 4.5v9L12 21l-8-4.5v-9L12 3Zm0 9 8-4.5M12 12 4 7.5M12 12v9M8 5.25l8 4.5"/>',
    code:'<path d="m8 6-6 6 6 6m8-12 6 6-6 6m-3-15-2 18"/>',
    chart:'<path d="M4 3v17h17M8 16v-5m5 5V7m5 9V4"/>',
    design:'<path d="m4 16 11-11 4 4L8 20H4v-4Zm9-9 4 4M16 4l1-1a2 2 0 0 1 3 3l-1 1"/>',
    video:'<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m10 9 5 3-5 3V9Z"/>',
    target:'<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
    pull:'<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="19" r="2"/><path d="M6 7v10M18 17V9a4 4 0 0 0-4-4h-2m3-3-3 3 3 3"/>',
    copy:'<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
    check:'<path d="m5 12 4 4L19 6"/>',
    plus:'<path d="M12 5v14M5 12h14"/>',
    'folder-plus':'<path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/><path d="M12 10v7m-3.5-3.5h7"/>',
    paperclip:'<path d="m21 11-8.5 8.5a6 6 0 0 1-8.5-8.5l9-9a4 4 0 0 1 5.7 5.7l-9 9a2 2 0 0 1-2.8-2.8l8.5-8.5"/>',
    sliders:'<path d="M4 7h8m4 0h4M4 17h2m4 0h10"/><circle cx="14" cy="7" r="2"/><circle cx="8" cy="17" r="2"/>',
    logout:'<path d="M10 4H5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h5M10 12h11m-4-4 4 4-4 4"/>',
    search:'<circle cx="11" cy="11" r="6.5"/><path d="m20 20-4.2-4.2"/>',
    sidebar:'<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M9.5 4.5v15"/>',
    panel:'<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M14.5 4.5v15"/>',
    list:'<path d="M10 7h10M10 12h10M10 17h10"/><rect x="3.5" y="5.5" width="3" height="3" rx=".6"/><rect x="3.5" y="10.5" width="3" height="3" rx=".6"/><rect x="3.5" y="15.5" width="3" height="3" rx=".6"/>',
    monitor:'<rect x="3.5" y="4.5" width="17" height="12" rx="2"/><path d="M8.5 20h7M12 16.5V20"/>',
    terminal:'<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="m7.5 9.5 2.5 2.5-2.5 2.5M12.5 15h4"/>',
    file:'<path d="M14 3.5H7.5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V8z"/><path d="M14 3.5V8h4.5"/>',
    chat:'<path d="M20 12a8 8 0 0 1-11.8 7l-4.2 1 1.1-3.9A8 8 0 1 1 20 12Z"/>',
    gear:'<circle cx="12" cy="12" r="2.8"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5v.2a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1h-.2a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5v-.2a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1h.2a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z"/>',
    clock:'<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    bolt:'<path d="M13 3 5 13.5h6L10 21l8-10.5h-6Z"/>',
    book:'<path d="M3.5 5.5A1.5 1.5 0 0 1 5 4h4.5A2.5 2.5 0 0 1 12 6.5V20a2 2 0 0 0-2-2H3.5Zm17 0A1.5 1.5 0 0 0 19 4h-4.5A2.5 2.5 0 0 0 12 6.5V20a2 2 0 0 1 2-2h6.5Z"/>',
    grid:'<rect x="4" y="4" width="6.5" height="6.5" rx="1.5"/><rect x="13.5" y="4" width="6.5" height="6.5" rx="1.5"/><rect x="4" y="13.5" width="6.5" height="6.5" rx="1.5"/><path d="M16.75 13.5v6.5M13.5 16.75H20"/>',
    expand:'<path d="M14.5 4H20v5.5M20 4l-6.5 6.5M9.5 20H4v-5.5M4 20l6.5-6.5"/>',
    up:'<path d="M12 19V5.5M6 11l6-6 6 6"/>',
    slash:'<rect x="4" y="4" width="16" height="16" rx="3"/><path d="m14 8.5-4 7"/>',
    chevron:'<path d="m6.5 9.5 5.5 5.5 5.5-5.5"/>',
    pin:'<path d="m9 3 6 0-1 6 4 4v2H6v-2l4-4-1-6ZM12 15v6"/>',
    participants:'<circle cx="9" cy="7" r="3"/><path d="M3 21v-2a6 6 0 0 1 12 0v2M16 4a3 3 0 0 1 0 6M17 14a5 5 0 0 1 4 5v2"/>',
    'pull-request':'<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="19" r="2"/><path d="M6 7v10M18 17V9a4 4 0 0 0-4-4h-2m3-3-3 3 3 3"/>',
    'git-merge':'<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="19" r="2"/><path d="M6 7v10M6 7a10 10 0 0 0 10 10"/>',
    'pull-request-closed':'<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="19" r="2"/><path d="M6 7v10M18 17v-4M15 3l6 6m0-6-6 6"/>',
    archive:'<rect x="3" y="3" width="18" height="5" rx="1"/><path d="M5 8v12h14V8M10 12h4"/>',
    restore:'<path d="M5 8v12h14V8M12 15V3m-4 4 4-4 4 4"/>',
    trash:'<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
    more:'<circle cx="5.5" cy="12" r=".9" fill="currentColor"/><circle cx="12" cy="12" r=".9" fill="currentColor"/><circle cx="18.5" cy="12" r=".9" fill="currentColor"/>',
    x:'<path d="m6.5 6.5 11 11M17.5 6.5l-11 11"/>',
  };
  function icon(name,size=16){
    const body=paths[name];if(!body)return '';
    return `<svg class="ui-icon icon-${name}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${body}</svg>`;
  }
  root.MoyaiIcon=icon;
  if(typeof document!=='undefined'){
    // Static shell controls declare data-icon so their markup stays readable.
    const apply=()=>document.querySelectorAll('[data-icon]').forEach(node=>{if(!node.querySelector('.ui-icon'))MoyaiUI.insert(node, 'afterbegin', icon(node.dataset.icon,Number(node.dataset.iconSize)||16));});
    if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',apply);else apply();
  }
})(typeof globalThis!=='undefined'?globalThis:this);
