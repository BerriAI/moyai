const assert = require('node:assert/strict');
const {test, before, after} = require('node:test');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');
let server, browser, base;
before(async () => {
  server = spawn(process.execPath, ['scripts/settings_ui_preview.cjs', '--production-csp', '--port', '0'], {stdio:['ignore','pipe','inherit']});
  base = await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.once('exit', code => reject(new Error(`UI fixture exited before readiness: ${code}`)));
    server.stdout.on('data', data => {const url=data.toString().match(/http:\/\/127\.0\.0\.1:\d+/); if(url)resolve(url[0]);});
  });
  browser = await chromium.launch({headless:true});
});
after(async () => {await browser?.close(); server?.kill();});
const label = '3D Moyai. Drag to rotate, use arrow keys, or press Home to reset.';
const nextPaint = page => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
for (const width of [1440, 768, 320]) test(`sculpture loads and rotates under production CSP at ${width}px`, async t => {
  const page = await browser.newPage({viewport:{width,height:1000}, reducedMotion:'reduce'});
  t.after(() => page.close());
  const errors=[];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    window.cspViolations=[];
    document.addEventListener('securitypolicyviolation', event => window.cspViolations.push({directive:event.effectiveDirective, blocked:event.blockedURI}));
  });
  const response = await page.goto(base);
  assert.match(response.headers()['content-security-policy'], /connect-src 'self';/);
  const canvas=page.getByRole('img', {name:label, exact:true});
  await canvas.waitFor({state:'visible'});
  assert.equal(await page.locator('#home-hub.sculpture-ready canvas').count(), 1);
  await canvas.press('Home');
  await nextPaint(page);
  const initial=await canvas.screenshot({animations:'disabled'});
  await canvas.press('ArrowRight');
  await nextPaint(page);
  assert.notDeepEqual(await canvas.screenshot({animations:'disabled'}), initial, 'ArrowRight must visibly rotate the sculpture');
  await canvas.press('Home');
  await nextPaint(page);
  assert.deepEqual(await canvas.screenshot({animations:'disabled'}), initial, 'Home must restore the original orientation');
  const box=await canvas.boundingBox();
  await page.mouse.move(box.x+box.width/2, box.y+box.height/2);
  await page.mouse.down();
  await page.mouse.move(box.x+box.width/2+40, box.y+box.height/2, {steps:5});
  await page.mouse.up();
  await nextPaint(page);
  assert.notDeepEqual(await canvas.screenshot({animations:'disabled'}), initial, 'Dragging must visibly rotate the sculpture');
  // A route transition disposes the old renderer and returning mounts one fresh canvas.
  await page.goto(`${base}/#skills`);
  await canvas.waitFor({state:'detached'});
  await page.goto(`${base}/#tasks`);
  await canvas.waitFor({state:'visible'});
  assert.equal(await page.locator('#home-hub canvas').count(), 1);
  assert.deepEqual(await page.evaluate(() => window.cspViolations), []);
  assert.deepEqual(errors, []);
});
test('failed model loading retains the usable home screen and logo', async t => {
  const page=await browser.newPage();
  t.after(() => page.close());
  await page.route('**/static/models/*', route => route.abort());
  const failure=page.waitForEvent('requestfailed', request => request.url().includes('/static/models/'));
  await page.goto(base);
  await failure;
  await page.locator('#home-hub > img').waitFor({state:'visible'});
  assert.equal(await page.locator('#home-hub canvas').count(), 0);
  await page.getByRole('textbox', {name:'Message Moyai', exact:true}).fill('The home screen still works.');
  assert.equal(await page.locator('#prompt').textContent(), 'The home screen still works.');
});
