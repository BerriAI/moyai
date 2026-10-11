// History is scoped to this mounted conversation and never persisted to browser storage.
function bindMessageHistory(input, getMessages) {
  let entries = null, index = 0, drafts = [], replacing = false, historyCaret = null;
  input.addEventListener('input', () => {
    // A successful send clears the composer; the next browse reads fresh history.
    if (!replacing) {historyCaret = null; if (!input.value) entries = null;}
  });
  return event => {
    if (!['ArrowUp', 'ArrowDown'].includes(event.key) || event.defaultPrevented ||
        event.isComposing || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey ||
        input.selectionStart !== input.selectionEnd) return false;
    const up = event.key === 'ArrowUp', value = input.value;
    // Do not interfere with navigation inside multiline (including wrapped) text.
    const browsing = entries && historyCaret === input.selectionStart;
    if (!browsing && (up ? input.selectionStart !== 0 : input.selectionEnd !== value.length)) return false;
    if (!entries) {
      if (!up) return false;
      entries = getMessages().filter(text => typeof text === 'string' && text.trim());
      if (!entries.length) {entries = null; return false;}
      index = entries.length;
      drafts = [...entries, value];
    }
    const next = Math.max(0, Math.min(entries.length, index + (up ? -1 : 1)));
    if (next === index) return false;
    drafts[index] = value;
    index = next;
    replacing = true;
    try {
      input.value = drafts[index];
      // Repeated Up walks older entries; Down walks back to the saved draft.
      const caret = up ? 0 : input.value.length;
      input.setSelectionRange(caret, caret);
      historyCaret = caret;
      input.dispatchEvent(new Event('input', {bubbles:true}));
    } finally {replacing = false;}
    if (index === entries.length) {entries = null; historyCaret = null;}
    event.preventDefault();
    return true;
  };
}
