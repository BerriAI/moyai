const assert = require('node:assert/strict');
const {test, before, after} = require('node:test');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');
let server, browser, base;
before(async () => {
  server = spawn(process.execPath, ['scripts/settings_ui_preview.cjs', '--port', '0'], {stdio:['ignore','pipe','inherit']});
  base = await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.once('exit', code => reject(Error(`Preview exited: ${code}`)));
    server.stdout.on('data', data => { const url = data.toString().match(/http:\/\/127\.0\.0\.1:\d+/); if (url) resolve(url[0]); });
  });
  browser = await chromium.launch({headless:true});
});
after(async () => { await browser?.close(); server?.kill(); });

async function open(t, fixture, width = 1440) {
  const page = await browser.newPage({viewport:{width,height:1000},reducedMotion:'reduce'});
  page.setDefaultTimeout(8000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  t.after(async () => { await page.close(); assert.deepEqual(errors, []); });
  await page.goto(`${base}/?fixture=${fixture}#personal-slack`);
  await page.getByRole('heading', {name:'Personal Slack',exact:true}).waitFor();
  return page;
}

for (const width of [1440,768,320]) test(`personal Slack connects, checks, disconnects and starts a private chat at ${width}px`, async t => {
  const page = await open(t, 'personal-disconnected', width);
  await page.getByRole('button', {name:'Connect Slack',exact:true}).click();
  await page.getByRole('button', {name:'Check connection',exact:true}).click();
  await page.getByText('Connection checked.', {exact:true}).waitFor();
  if (process.env.PERSONAL_SLACK_SCREENSHOTS) await page.screenshot({path:`${process.env.PERSONAL_SLACK_SCREENSHOTS}/settings-${width}.png`,fullPage:true});
  assert.ok(await page.locator('#content').evaluate(el => el.scrollWidth <= el.clientWidth + 1));
  await page.getByRole('button', {name:'Disconnect…',exact:true}).click();
  await page.getByRole('button', {name:'Cancel',exact:true}).click();
  await page.getByRole('button', {name:'Check connection',exact:true}).waitFor();
  await page.getByRole('button', {name:'Disconnect…',exact:true}).click();
  await page.getByRole('button', {name:'Disconnect',exact:true}).click();
  await page.getByRole('button', {name:'Connect Slack',exact:true}).waitFor();
  await page.getByRole('button', {name:'Start private web chat',exact:true}).click();
  const choice = page.getByRole('checkbox', {name:'Private session · Only you',exact:true});
  assert.equal(await choice.isChecked(), true);
  assert.equal(await page.getByRole('button', {name:'Swarm',exact:true}).isDisabled(), true);
  await page.getByRole('textbox', {name:'Message Moyai',exact:true}).fill('Read my synthetic Slack context');
  const submitted = page.waitForRequest(r => r.method() === 'POST' && new URL(r.url()).pathname === '/api/runs');
  await page.getByRole('button', {name:'Start session',exact:true}).click();
  assert.equal((await submitted).postDataJSON().private_session, true);
  await page.locator('.session-privacy-notice').waitFor();
  if (process.env.PERSONAL_SLACK_SCREENSHOTS) await page.screenshot({path:`${process.env.PERSONAL_SLACK_SCREENSHOTS}/private-${width}.png`,fullPage:true});
  assert.equal(await page.getByRole('button', {name:/side chat/i}).count(), 0);
  await page.getByRole('button', {name:'Run again',exact:true}).click();
  assert.equal(await choice.isChecked(), true);
  assert.equal(await choice.isDisabled(), true);
});

test('private chat keeps its notice beside the conversation', async t => {
  const page = await open(t, 'personal-chat');
  await page.getByRole('button', {name:'Start private web chat',exact:true}).click();
  await page.getByRole('textbox', {name:'Message Moyai',exact:true}).fill('Synthetic private chat');
  await page.getByRole('button', {name:'Start session',exact:true}).click();
  await page.locator('.chat-panel .session-privacy-notice').waitFor();
  await page.getByRole('textbox', {name:'Message Moyai',exact:true}).waitFor();
});

test('personal Slack errors remain actionable and new sessions start shared', async t => {
  const page = await open(t, 'personal-error');
  await page.getByRole('button', {name:'Check connection',exact:true}).click();
  await page.getByRole('alert').filter({hasText:'Synthetic Slack outage'}).waitFor();
  assert.equal(await page.getByRole('button', {name:'Reconnect Slack',exact:true}).isEnabled(), true);
  await page.goto(`${base}/?fixture=personal-connected#tasks`);
  const choice = page.getByRole('checkbox', {name:'Private session · Only you',exact:true});
  await choice.waitFor();
  assert.equal(await choice.isChecked(), false);
  await choice.focus(); await choice.press('Space');
  assert.equal(await choice.isChecked(), true);
  await page.waitForFunction(() => document.querySelector('[data-new-session-mode=swarm]').disabled);
});
