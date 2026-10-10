// Uses real session APIs with scripts/session_ui_demo.py running locally.
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const {chromium} = require('playwright');
const base = process.env.SESSION_UI_URL || 'http://127.0.0.1:8830';
for (const width of [1440, 768, 320]) test(`session JSON download at ${width}px`, async t => {
  const browser = await chromium.launch({headless:true, args:['--no-sandbox']});
  t.after(() => browser.close());
  const page = await browser.newPage({viewport:{width,height:900}});
  await page.goto(base + '/demo/login');
  const runs = await (await page.request.get(base + '/api/runs?scope=all')).json();
  const run = (Array.isArray(runs) ? runs : runs.runs)[0];
  assert.ok(run);
  await page.goto(base + '/#run=' + run.id);
  await page.getByRole('button', {name:'Session actions', exact:true}).click();
  const button = page.getByRole('button', {name:'Download session JSON', exact:true});
  const box = await button.boundingBox();
  assert.ok(box.x >= 0 && box.x + box.width <= width);
  const downloadPromise = page.waitForEvent('download');
  await button.click();
  const download = await downloadPromise;
  assert.equal(download.suggestedFilename(), `moyai-session-${run.id}.json`);
  const data = JSON.parse(readFileSync(await download.path(), 'utf8'));
  assert.equal(data.format, 'moyai.session');
  assert.equal(data.session.id, run.id);
  assert.ok(data.messages.length);
  assert.ok(Array.isArray(data.traces));
});
