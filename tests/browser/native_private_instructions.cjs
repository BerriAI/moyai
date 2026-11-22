const assert = require('node:assert/strict');
const {before, after, test} = require('node:test');
const {chromium} = require('playwright');
const {settingsFixture} = require('../fixtures/settings.cjs');
let fixture, browser;
before(async () => { fixture = await settingsFixture(); browser = await chromium.launch({headless:true}); });
after(async () => { await browser?.close(); await fixture?.close(); });
async function open(t, width=1440, theme='dark') {
  const page = await browser.newPage({viewport:{width,height:1000}, reducedMotion:'reduce'});
  t.after(() => page.close());
  const errors = []; page.on('pageerror', e => errors.push(e.message));
  t.after(() => assert.deepEqual(errors, []));
  await page.addInitScript(theme => localStorage.setItem('moyai-appearance', theme), theme);
  await page.goto(fixture.base + '/#settings/general');
  await page.getByRole('heading', {name:'Personal instructions', exact:true}).waitFor();
  return page;
}
for (const theme of ['dark', 'light']) for (const width of [1440,768,320]) {
  test(`Private instructions save and clear at ${width}px in ${theme}`, async t => {
    fixture.reset();
    const page = await open(t, width, theme);
    const input = page.getByRole('textbox', {name:'Personal instructions', exact:true});
    await input.fill('Use concise sentences and metric units.');
    assert.equal(await input.getAttribute('maxlength'), '4000');
    await input.focus(); await page.keyboard.press('Tab');
    assert.equal(await page.getByRole('button', {name:'Save instructions', exact:true}).evaluate(el => el === document.activeElement), true);
    await page.keyboard.press('Enter');
    await page.getByText('Personal instructions saved.', {exact:true}).waitFor();
    await page.reload();
    assert.equal(await input.inputValue(), 'Use concise sentences and metric units.');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    if (width === 320) assert.equal(await input.evaluate(el => getComputedStyle(el).fontSize), '16px');
    await page.getByRole('button', {name:'Clear instructions', exact:true}).click();
    await page.getByText('Personal instructions cleared.', {exact:true}).waitFor();
    assert.equal(await input.inputValue(), '');
    assert.equal(fixture.state.general.personal_instructions_revision, 2);
  });
}
test('Conflict keeps the draft until explicitly reloading saved instructions', async t => {
  fixture.reset(); const page = await open(t);
  const input = page.getByRole('textbox', {name:'Personal instructions', exact:true});
  await input.fill('My unsaved draft');
  fixture.state.general.personal_instructions = 'Saved in another tab';
  fixture.state.general.personal_instructions_revision = 1;
  await page.getByRole('button', {name:'Save instructions', exact:true}).click();
  await page.getByRole('alert').filter({hasText:'changed elsewhere'}).waitFor();
  assert.equal(await input.inputValue(), 'My unsaved draft');
  await page.getByRole('button', {name:'Reload saved instructions', exact:true}).click();
  await page.getByText('Saved instructions loaded.', {exact:true}).waitFor();
  assert.equal(await input.inputValue(), 'Saved in another tab');
});
test('A late save cannot replace a different settings route', async t => {
  fixture.reset(); const page = await open(t);
  let release; fixture.state.generalGate = new Promise(resolve => { release = resolve; });
  await page.getByRole('textbox', {name:'Personal instructions', exact:true}).fill('Pending draft');
  await page.getByRole('button', {name:'Save instructions', exact:true}).click();
  await page.getByText('Saving…', {exact:true}).waitFor();
  await page.getByRole('link', {name:'Connections', exact:true}).click();
  await page.locator('.ns-content h1').filter({hasText:'Connections'}).waitFor(); release();
  assert.equal(await page.getByRole('textbox', {name:'Personal instructions', exact:true}).count(), 0);
  assert.equal(await page.getByText('Personal instructions saved.', {exact:true}).count(), 0);
});
test('Shared sign-in has no instruction editor', async t => {
  fixture.reset(); fixture.state.individual = false;
  const page = await open(t);
  await page.getByText('Sign in with your individual work account to save personal instructions.').waitFor();
  assert.equal(await page.getByRole('textbox', {name:'Personal instructions', exact:true}).count(), 0);
});
