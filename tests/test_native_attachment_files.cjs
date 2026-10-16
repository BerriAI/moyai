const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const compiled = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../native/web/attachment-files.ts'), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText;
const loaded = import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);

test('mixed drops retain documents, video and images in the original order', async () => {
  const { droppedFiles, attachmentError } = await loaded;
  const files = [
    { name: 'report.md', type: 'text/markdown', size: 500 },
    { name: 'demo.mp4', type: 'video/mp4', size: 12 * 1024 * 1024 },
    { name: 'screen.png', type: 'image/png', size: 2000 },
    { name: 'no-extension', type: '', size: 0 },
  ];
  assert.deepEqual(droppedFiles({ files, items: [] }), files);
  assert.equal(attachmentError(files, 0), undefined);
});

test('file drags are distinct from dragging text, a link, or a page image URL', async () => {
  const { isFileDrag } = await loaded;
  assert.equal(isFileDrag({ types: ['text/plain', 'Files'] }), true);
  for (const types of [[], ['text/plain'], ['text/uri-list', 'text/html']]) {
    assert.equal(isFileDrag({ types }), false);
  }
});

test('folders and unreadable drops fail explicitly instead of silently attaching nothing', async () => {
  const { droppedFiles } = await loaded;
  assert.throws(() => droppedFiles({
    files: [{ name: 'directory' }],
    items: [{ kind: 'file', webkitGetAsEntry: () => ({ isDirectory: true }) }],
  }), /zip it first/);
  assert.throws(() => droppedFiles({ files: [], items: [] }), /No files/);
});

test('batch limits include existing attachments and distinguish image/file size limits', async () => {
  const { attachmentError, MAX_FILE_BYTES, MAX_IMAGE_BYTES } = await loaded;
  const doc = { name: 'readme.txt', type: 'text/plain', size: 12 };
  assert.match(attachmentError([doc, doc], 7), /up to 8 files/);
  assert.equal(attachmentError([{ ...doc, size: MAX_FILE_BYTES }], 7), undefined);
  assert.match(attachmentError([{ ...doc, size: MAX_FILE_BYTES + 1 }], 0), /20 MB/);
  assert.match(attachmentError([{ name: 'clipboard.PNG', type: '', size: MAX_IMAGE_BYTES + 1 }], 0), /8 MB/);
});

test('active SVG and HTML are file cards, never inline image documents', async () => {
  const { isInlineImage } = await loaded;
  assert.equal(isInlineImage('image/png'), true);
  for (const type of ['image/svg+xml', 'text/html', 'application/octet-stream', 'video/mp4']) {
    assert.equal(isInlineImage(type), false);
  }
});

test('drop attaches files once, stops navigation/bubbling and respects a busy composer', async () => {
  const { receiveFileDrop } = await loaded;
  const files = [{ name: 'notes.md', type: 'text/markdown', size: 24 }];
  let attached = [], errors = [], prevented = 0, stopped = 0;
  const event = {
    dataTransfer: { types: ['Files'], files, items: [] },
    preventDefault: () => prevented++,
    stopPropagation: () => stopped++,
  };
  const add = async batch => attached.push(batch);
  assert.equal(receiveFileDrop(event, add, error => errors.push(error), false), true);
  assert.deepEqual(attached, [files]);
  assert.equal(prevented, 1);
  assert.equal(stopped, 1);
  receiveFileDrop(event, add, error => errors.push(error), true);
  assert.equal(attached.length, 1);
  assert.match(errors[0], /Finish the current upload/);
  event.dataTransfer.types = ['text/plain'];
  assert.equal(receiveFileDrop(event, add, error => errors.push(error), false), false);
  assert.equal(prevented, 2);
});
