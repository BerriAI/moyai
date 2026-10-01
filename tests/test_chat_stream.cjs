const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');

// Run the browser's stream controller with a transport that can permanently
// close on a deployment's HTTP error, as native EventSource does.
function browser() {
  const script = readFileSync('app/static/app.js', 'utf8');
  const sources = [], timers = new Map(), rendered = [], refreshes = [], live = [];
  const notice = {hidden: true};
  const state = {selected: 'chat-a', source: null, drafts: {'chat-a': 'unsent reply'}};
  let nextTimer = 0;
  const context = {
    state,
    EventSource: class {
      constructor(url) { this.url = url; this.handlers = {}; sources.push(this); }
      addEventListener(name, handler) { this.handlers[name] = handler; }
      close() { this.closed = true; }
    },
    $: selector => selector === '#connection-state' ? notice : {
      insertAdjacentHTML: (_, text) => rendered.push(text),
    },
    eventHTML: event => event.id,
    refreshChat: async id => {refreshes.push(id);},
    updateChatStatus: () => {},
    renderLiveWork: (event, disconnected) => { live.push({event, disconnected}); },
    showError: error => {throw error;},
    setTimeout: fn => {timers.set(++nextTimer, fn); return nextTimer;},
    clearTimeout: id => timers.delete(id),
  };
  vm.createContext(context);
  vm.runInContext(
    script.slice(script.indexOf('function stopStream()'), script.indexOf('function sessionTitle(')) +
    script.slice(script.indexOf('function connectChatStream('), script.indexOf('function updateChatStatus(')), context);
  context.connectChatStream({id: 'chat-a', events: [{id: 10}]});
  const retry = () => {const [id, fn] = timers.entries().next().value; timers.delete(id); fn();};
  return {context, state, sources, timers, rendered, refreshes, notice, retry, live};
}

test('recovers after repeated deployment errors, resumes the cursor, and preserves the draft', () => {
  const b = browser();
  b.sources[0].onmessage({data: JSON.stringify({id: 11, kind: 'tool'})});
  b.sources[0].onerror();
  assert.equal(b.sources[0].closed, true);
  assert.equal(b.notice.hidden, false);
  b.retry();
  assert.match(b.sources[1].url, /after=11$/);
  b.sources[1].onerror();
  b.retry();
  b.sources[2].onopen();
  b.sources[2].onmessage({data: JSON.stringify({id: 11, kind: 'tool'})});
  b.sources[2].onmessage({data: JSON.stringify({id: 12, kind: 'tool'})});
  assert.deepEqual(b.rendered, [11, 12]);
  assert.deepEqual(b.live.filter(update=>update.event).map(update=>update.event.id), [11, 12]);
  assert.equal(b.live.filter(update=>update.disconnected).length, 2);
  assert.deepEqual(b.refreshes, ['chat-a']);
  assert.equal(b.notice.hidden, true);
  assert.equal(b.state.drafts['chat-a'], 'unsent reply');
});

test('leaving a chat cancels a pending reconnect', () => {
  const b = browser();
  b.sources[0].onerror();
  b.context.stopStream();
  b.state.selected = 'chat-b';
  assert.equal(b.timers.size, 0);
  b.sources[0].onerror();
  b.sources[0].onopen();
  assert.equal(b.sources.length, 1);
  assert.deepEqual(b.refreshes, []);
});

test('late callbacks from an old stream cannot change the replacement chat', () => {
  const b = browser();
  b.sources[0].onerror();
  b.retry();
  b.sources[1].onopen();
  b.sources[0].onerror();
  b.sources[0].onmessage({data: JSON.stringify({id: 99, kind: 'tool'})});
  assert.equal(b.notice.hidden, true);
  assert.equal(b.timers.size, 0);
  assert.deepEqual(b.rendered, []);
});

test('a steering receipt refreshes its user input before subsequent public updates',()=>{
  const b=browser();
  const receipt={id:11,kind:'status',data:{phase:'steering',message_id:42}};
  b.sources[0].onmessage({data:JSON.stringify(receipt)});
  b.sources[0].onmessage({data:JSON.stringify({id:12,kind:'message',message:'Answer to the correction.'})});
  b.sources[0].onmessage({data:JSON.stringify(receipt)});
  assert.deepEqual(b.refreshes,['chat-a']);
  assert.deepEqual(b.live.filter(update=>update.event).map(update=>update.event.id),[11,12]);
});
