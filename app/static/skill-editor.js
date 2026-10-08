// Keep the composer’s string contract at its boundary; atoms only affect display.
function bindSkillEditor(textarea, form) {
  const input = document.createElement('div');
  input.id = textarea.id;
  input.className = 'skill-editor-input';
  input.contentEditable = 'true';
  input.setAttribute('role', 'textbox');
  input.setAttribute('aria-multiline', 'true');
  input.setAttribute('aria-label', textarea.getAttribute('aria-label'));
  input.dataset.placeholder = textarea.placeholder;
  input.maxLength = textarea.maxLength;
  input.minLength = textarea.minLength;
  input.required = textarea.required;
  input.oninput = textarea.oninput;
  const initial = textarea.value;
  const catalog = new Map();
  let composing = false, history = [], historyIndex = -1;

  function text(node) {
    if (node.nodeType === 3) return node.data;
    if (node.dataset?.skillReference) return '/' + node.dataset.skillReference;
    if (node.nodeName === 'BR') return node.dataset.editorTail ? '' : '\n';
    return Array.from(node.childNodes, (child, index) =>
      (index && /^(DIV|P)$/.test(child.nodeName) ? '\n' : '') + text(child)).join('');
  }
  function selection() {
    const s = window.getSelection();
    if (!s.rangeCount || !input.contains(s.anchorNode) || !input.contains(s.focusNode))
      return [input.value.length, input.value.length];
    const r = s.getRangeAt(0), prefix = document.createRange();
    prefix.selectNodeContents(input); prefix.setEnd(r.startContainer, r.startOffset);
    const start = text(prefix.cloneContents()).length;
    prefix.setEnd(r.endContainer, r.endOffset);
    return [start, text(prefix.cloneContents()).length];
  }
  function point(offset) {
    let remaining = offset;
    for (const node of input.childNodes) {
      const length = text(node).length;
      if (remaining <= length) {
        if (node.nodeType === 3) return [node, remaining];
        const index = Array.prototype.indexOf.call(input.childNodes, node);
        return [input, index + (remaining > 0 ? 1 : 0)];
      }
      remaining -= length;
    }
    return [input, input.childNodes.length];
  }
  input.setSelectionRange = (start, end) => {
    const r = document.createRange();
    r.setStart(...point(Math.max(0, start))); r.setEnd(...point(Math.max(start, end)));
    const s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
  };
  function render(value, caret = null) {
    const fragment = document.createDocumentFragment();
    let last = 0;
    for (const match of value.matchAll(/\/(?:personal|org):[a-z0-9]+(?:-[a-z0-9]+)*/g)) {
      const end = match.index + match[0].length;
      const range = slashSkillQuery(value, end);
      const skill = catalog.get(match[0].slice(1));
      if (!skill || !range || range.start !== match.index || range.end !== end) continue;
      fragment.append(document.createTextNode(value.slice(last, match.index)));
      const chip = document.createElement('span');
      chip.className = 'composer-skill'; chip.contentEditable = 'false';
      chip.dataset.skillReference = skill.reference;
      const scope = skill.scope === 'personal' ? 'Personal' : 'Organization';
      chip.title = `${scope} skill: ${skill.name}`;
      chip.setAttribute('aria-label', chip.title);
      const icon = document.createElement('span');
      icon.setAttribute('aria-hidden', 'true'); icon.textContent = '✦';
      chip.append(icon, document.createTextNode(skill.name));
      fragment.append(chip); last = end;
    }
    fragment.append(document.createTextNode(value.slice(last)));
    if (value.endsWith('\n')) {
      const tail = document.createElement('br'); tail.dataset.editorTail = 'true'; fragment.append(tail);
    }
    input.replaceChildren(fragment);
    input.dataset.empty = String(!value);
    if (caret) input.setSelectionRange(...caret);
  }
  function remember() {
    const value = input.value, caret = selection();
    if (history[historyIndex]?.value === value) {history[historyIndex].caret = caret; return;}
    history = history.slice(0, historyIndex + 1);
    history.push({value, caret});
    if (history.length > 100) history.shift();
    historyIndex = history.length - 1;
  }
  function notify() {input.dispatchEvent(new Event('input', {bubbles:true}));}
  function replace(start, end, addition) {
    const value = input.value.slice(0, start) + addition + input.value.slice(end);
    if (input.maxLength > 0 && value.length > input.maxLength) {
      toast('Shorten your message before adding more text.'); return;
    }
    remember();
    render(value, [start + addition.length, start + addition.length]); notify();
  }
  function undo(direction) {
    const next = historyIndex + direction;
    if (!history[next]) return;
    historyIndex = next;
    render(history[next].value, history[next].caret);
    notify();
  }
  Object.defineProperties(input, {
    value: {get: () => text(input), set: value => {render(String(value)); remember();}},
    selectionStart: {get: () => selection()[0]},
    selectionEnd: {get: () => selection()[1]},
    placeholder: {get: () => input.dataset.placeholder, set: value => {input.dataset.placeholder = value;}},
  });
  input.setSkillCatalog = skills => {
    for (const skill of skills) if (!skill.archived) catalog.set(skill.reference, skill);
    const caret = document.activeElement === input ? selection() : null;
    if (!composing) render(input.value, caret);
  };
  input.addEventListener('compositionstart', () => {composing = true;});
  input.addEventListener('compositionend', () => {composing = false; notify();});
  input.addEventListener('input', () => {
    if (composing) return;
    const caret = selection(); render(input.value, caret); remember();
  });
  input.addEventListener('beforeinput', event => {
    if (composing || event.isComposing) return;
    if (event.inputType === 'historyUndo' || event.inputType === 'historyRedo') {
      event.preventDefault(); undo(event.inputType === 'historyUndo' ? -1 : 1); return;
    }
    if (event.inputType === 'insertParagraph' || event.inputType === 'insertLineBreak') {
      event.preventDefault(); replace(...selection(), '\n'); return;
    }
    if (event.data && input.value.length - (input.selectionEnd - input.selectionStart) + event.data.length > input.maxLength)
      event.preventDefault();
  });
  input.addEventListener('keydown', event => {
    if (composing || event.isComposing) return;
    if ((event.ctrlKey || event.metaKey) && !event.altKey && ['z', 'y'].includes(event.key.toLowerCase())) {
      event.preventDefault(); undo(event.shiftKey || event.key.toLowerCase() === 'y' ? 1 : -1); return;
    }
    if (!event.ctrlKey && !event.metaKey && !event.altKey && ['Backspace','Delete'].includes(event.key)) {
      const [start, end] = selection();
      if (start !== end) {event.preventDefault(); replace(start, end, ''); return;}
      let offset = 0;
      for (const node of input.childNodes) {
        const length = text(node).length;
        if (node.dataset?.skillReference && ((event.key === 'Backspace' && start === offset + length) || (event.key === 'Delete' && start === offset))) {
          event.preventDefault(); replace(offset, offset + length, ''); return;
        }
        offset += length;
      }
    }
  });
  for (const type of ['copy', 'cut']) input.addEventListener(type, event => {
    const [start, end] = selection();
    if (start === end) return;
    event.preventDefault(); event.clipboardData.setData('text/plain', input.value.slice(start, end));
    if (type === 'cut') replace(start, end, '');
  });
  input.addEventListener('paste', event => {
    if (event.clipboardData.files.length) return;
    event.preventDefault(); replace(...selection(), event.clipboardData.getData('text/plain').replace(/\r\n?/g, '\n'));
  });
  // Let the attachment handler own file drops, but never import rich HTML.
  input.addEventListener('drop', event => {if (!event.dataTransfer.files.length) event.preventDefault();});
  form.addEventListener('submit', event => {
    const length = input.value.length;
    if ((input.required && !length) || (length && length < input.minLength) || length > input.maxLength) {
      event.preventDefault(); event.stopImmediatePropagation(); input.focus();
      toast(`Enter ${input.minLength > 0 ? input.minLength : 1}–${input.maxLength} characters, or attach a file.`);
    }
  }, true);
  textarea.replaceWith(input); input.value = initial;
  api('/api/skills').then(data => {if (input.isConnected) input.setSkillCatalog(data.skills);}).catch(() => {});
  return input;
}
