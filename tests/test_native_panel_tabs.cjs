// Execute the production reducer, not a reimplementation of panel behavior.
// These bounded state regressions do not replace browser or real-provider E2E proof.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const filename = path.join(__dirname, '../native/web/panel-tabs.tsx');
const source = ts.createSourceFile(
  filename, fs.readFileSync(filename, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX,
);
const declarations = source.statements.filter(statement =>
  (ts.isFunctionDeclaration(statement) && statement.name?.text === 'panelReducer') ||
  (ts.isVariableStatement(statement) && statement.declarationList.declarations.some(
    declaration => ts.isIdentifier(declaration.name) && declaration.name.text === 'emptyPanelState',
  )),
);
assert.equal(declarations.length, 2, 'Load both production state exports; never silently substitute fixtures');
const compiled = ts.transpileModule(declarations.map(statement => statement.getFullText(source)).join('\n'), {
  compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022},
}).outputText;
const loaded = import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);

function freeze(value) {
  if (value && typeof value === 'object') {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}
const utility = kind => ({id: kind, kind, title: kind});
const picker = id => ({id: `side-chat:new:${id}`, kind: 'side-chat', title: 'Side chat'});
const sideChat = id => ({id: `side-chat:${id}`, kind: 'side-chat', sessionId: id, title: `Chat ${id}`});
const open = tab => ({type: 'open', tab});

test('opening the same stable resource selects and updates it without duplicating or reordering other tabs', async () => {
  const {panelReducer: reduce, emptyPanelState} = await loaded;
  const pr = {id: 'pr:https://github.com/berriai/moyai/pull/465', kind: 'pull-request',
    url: 'https://github.com/berriai/moyai/pull/465', title: 'Pull request'};
  const state = freeze([open(pr), open(utility('files'))].reduce(reduce, emptyPanelState));
  const result = reduce(state, open({...pr, title: 'Native workspace'}));
  assert.deepEqual(result.tabs.map(tab => tab.id), state.tabs.map(tab => tab.id));
  assert.equal(result.tabs[0].title, 'Native workspace');
  assert.strictEqual(result.tabs[1], state.tabs[1], 'Unrelated mounted content keeps its identity');
  assert.equal(result.activeId, pr.id);
  assert.equal(result.visible, true);
  assert.equal(state.tabs[0].title, 'Pull request', 'Previous state is immutable');
});

test('native colon IDs and identically titled side chats remain distinct', async () => {
  const {panelReducer: reduce, emptyPanelState} = await loaded;
  const agents = ['temporalRun:uuid-a', 'temporalRun:uuid-b'].map(runId => ({
    id: `subagent:${runId}`, kind: 'subagent', runId, name: 'Cicero', title: 'Cicero',
    status: 'running', avatar: 0,
  }));
  const chats = [sideChat('one'), {...sideChat('two'), title: 'Chat one'}];
  const state = [...agents, ...chats].map(open).reduce(reduce, emptyPanelState);
  assert.equal(state.tabs.length, 4);
  assert.deepEqual(state.tabs.slice(0, 2).map(tab => tab.runId), agents.map(tab => tab.runId));
  const closed = reduce(state, {type: 'close', id: chats[0].id});
  assert.equal(closed.tabs.length, 3);
  assert.equal(closed.activeId, chats[1].id);
});

test('selection, hide and show preserve tab identities for mounted drafts and desktop state', async () => {
  const {panelReducer: reduce, emptyPanelState} = await loaded;
  const state = freeze([open(sideChat('draft')), open(utility('computer'))].reduce(reduce, emptyPanelState));
  const selected = reduce(state, {type: 'select', id: 'side-chat:draft'});
  assert.strictEqual(selected.tabs, state.tabs);
  const expanded = reduce(selected, {type: 'expand'});
  assert.equal(expanded.expanded, true);
  const hidden = reduce(expanded, {type: 'hide'});
  assert.strictEqual(hidden.tabs, state.tabs);
  assert.equal(hidden.activeId, 'side-chat:draft');
  assert.equal(hidden.visible, false);
  assert.equal(hidden.expanded, false);
  const shown = reduce(hidden, {type: 'show'});
  assert.strictEqual(shown.tabs, state.tabs);
  assert.equal(shown.activeId, hidden.activeId);
  assert.equal(shown.visible, true);
  assert.equal(shown.expanded, false);
});

for (const [closing, expected] of [['files', 'computer'], ['computer', 'plan'], ['plan', 'computer']]) {
  test(`closing active ${closing} selects its nearest remaining neighbor`, async () => {
    const {panelReducer: reduce, emptyPanelState} = await loaded;
    const state = ['files', 'computer', 'plan'].map(utility).map(open).reduce(reduce, emptyPanelState);
    const selected = reduce(state, {type: 'select', id: closing});
    const result = reduce(freeze(selected), {type: 'close', id: closing});
    assert.equal(result.activeId, expected);
    assert.equal(result.visible, true);
    assert.equal(result.tabs.length, 2);
    assert(!result.tabs.some(tab => tab.id === closing));
  });
}

test('closing a background tab preserves active selection and expanded state', async () => {
  const {panelReducer: reduce, emptyPanelState} = await loaded;
  const state = [open(utility('files')), open(sideChat('draft')), {type: 'expand'}].reduce(reduce, emptyPanelState);
  const result = reduce(freeze(state), {type: 'close', id: 'files'});
  assert.equal(result.activeId, 'side-chat:draft');
  assert.equal(result.visible, true);
  assert.equal(result.expanded, true);
  assert.strictEqual(result.tabs[0], state.tabs[1]);
});

test('closing the final tab clears selection, visibility and expansion', async () => {
  const {panelReducer: reduce, emptyPanelState} = await loaded;
  const state = [open(utility('computer')), {type: 'expand'}].reduce(reduce, emptyPanelState);
  assert.deepEqual(reduce(freeze(state), {type: 'close', id: 'computer'}), emptyPanelState);
});

for (const background of [false, true]) {
  test(`late creation cannot reopen a closed picker${background ? ' or replace the current tab' : ''}`, async () => {
    const {panelReducer: reduce, emptyPanelState} = await loaded;
    const pending = picker('request-a');
    let state = reduce(emptyPanelState, open(pending));
    if (background) state = reduce(state, open(utility('files')));
    state = freeze(reduce(state, {type: 'close', id: pending.id}));
    const result = reduce(state, {type: 'replace', id: pending.id, tab: sideChat('created-a')});
    assert.strictEqual(result, state);
    assert.equal(result.visible, background);
    assert.equal(result.activeId, background ? 'files' : null);
  });
}

test('a closed picker completion cannot consume a newer picker with a different request identity', async () => {
  const {panelReducer: reduce, emptyPanelState} = await loaded;
  const old = picker('old'), current = picker('new');
  const state = [open(old), {type: 'close', id: old.id}, open(current)].reduce(reduce, emptyPanelState);
  const late = reduce(freeze(state), {type: 'replace', id: old.id, tab: sideChat('old-result')});
  assert.strictEqual(late, state);
  assert.equal(late.activeId, current.id);
  assert.deepEqual(late.tabs, [current]);
});

test('late rename/select/close callbacks cannot resurrect a removed tab or reveal a hidden panel', async () => {
  const {panelReducer: reduce, emptyPanelState} = await loaded;
  const state = freeze([open(utility('files')), {type: 'hide'}].reduce(reduce, emptyPanelState));
  for (const action of [
    {type: 'rename', id: 'closed-chat', title: 'Late title'},
    {type: 'select', id: 'closed-chat'}, {type: 'close', id: 'closed-chat'},
  ]) assert.strictEqual(reduce(state, action), state);
  const renamed = reduce(state, {type: 'rename', id: 'files', title: 'Saved files'});
  assert.equal(renamed.tabs[0].title, 'Saved files');
  assert.equal(renamed.visible, false);
  assert.equal(renamed.activeId, 'files');
});

test('resolving an active picker to an already-open side chat selects one existing stable tab', async () => {
  const {panelReducer: reduce, emptyPanelState} = await loaded;
  const chat = sideChat('existing'), pending = picker('choose-existing');
  const state = [open(chat), open(utility('files')), open(pending)].reduce(reduce, emptyPanelState);
  const result = reduce(freeze(state), {type: 'replace', id: pending.id, tab: {...chat, title: 'Renamed chat'}});
  assert.deepEqual(result.tabs.map(tab => tab.id), [chat.id, 'files']);
  assert.equal(result.activeId, chat.id);
  assert.strictEqual(result.tabs[0], state.tabs[0], 'A stale picker must not overwrite the open chat metadata');
  assert.strictEqual(result.tabs[1], state.tabs[1]);
});

test('resolving an active picker to a new side chat opens its completed session', async () => {
  const {panelReducer: reduce, emptyPanelState} = await loaded;
  const pending = picker('new'), chat = sideChat('created');
  const state = [open(utility('computer')), open(pending)].reduce(reduce, emptyPanelState);
  const result = reduce(freeze(state), {type: 'replace', id: pending.id, tab: chat});
  assert.deepEqual(result.tabs.map(tab => tab.id), ['computer', chat.id]);
  assert.equal(result.activeId, chat.id);
  assert.equal(result.visible, true);
});

test('replacing a middle picker preserves its position, neighboring identities and expanded state', async () => {
  const {panelReducer: reduce, emptyPanelState} = await loaded;
  const pending = picker('middle'), chat = sideChat('created-in-place');
  const state = [
    open(utility('files')), open(pending), open(utility('computer')),
    {type: 'select', id: pending.id}, {type: 'expand'},
  ].reduce(reduce, emptyPanelState);
  const result = reduce(freeze(state), {type: 'replace', id: pending.id, tab: chat});
  assert.deepEqual(result.tabs.map(tab => tab.id), ['files', chat.id, 'computer']);
  assert.strictEqual(result.tabs[0], state.tabs[0]);
  assert.strictEqual(result.tabs[2], state.tabs[2]);
  assert.equal(result.activeId, chat.id);
  assert.equal(result.visible, true);
  assert.equal(result.expanded, true);
});

test('creation completing after Hide stores its result without reopening the panel', async () => {
  const {panelReducer: reduce, emptyPanelState} = await loaded;
  const pending = picker('hidden'), chat = sideChat('created-while-hidden');
  const state = [open(pending), {type: 'hide'}].reduce(reduce, emptyPanelState);
  const result = reduce(freeze(state), {type: 'replace', id: pending.id, tab: chat});
  assert.equal(result.visible, false, 'An async completion is not a new request to show the panel');
  assert.equal(result.activeId, chat.id);
  assert.deepEqual(result.tabs, [chat]);
});

test('creation completing after selecting another tab cannot steal its active selection', async () => {
  const {panelReducer: reduce, emptyPanelState} = await loaded;
  const pending = picker('background'), chat = sideChat('created-in-background');
  const state = [open(pending), open(utility('computer'))].reduce(reduce, emptyPanelState);
  const result = reduce(freeze(state), {type: 'replace', id: pending.id, tab: chat});
  assert.equal(result.activeId, 'computer', 'Late creation must not interrupt the selected desktop');
  assert.equal(result.visible, state.visible);
  assert.deepEqual(new Set(result.tabs.map(tab => tab.id)), new Set([chat.id, 'computer']));
});

test('independent conversation instances cannot mutate the shared empty state or one another', async () => {
  const {panelReducer: reduce, emptyPanelState} = await loaded;
  freeze(emptyPanelState);
  const first = reduce(emptyPanelState, open(sideChat('private-a')));
  const second = reduce(emptyPanelState, open(utility('computer')));
  assert.deepEqual(emptyPanelState, {tabs: [], activeId: null, visible: false, expanded: false});
  assert.deepEqual(first.tabs.map(tab => tab.id), ['side-chat:private-a']);
  assert.deepEqual(second.tabs.map(tab => tab.id), ['computer']);
  const reset = reduce(emptyPanelState, {type: 'replace', id: 'old-picker', tab: sideChat('stale')});
  assert.strictEqual(reset, emptyPanelState);
});
