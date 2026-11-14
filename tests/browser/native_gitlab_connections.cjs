// Actual React settings and generated SDK against synthetic API responses.
// Supplementary coverage; deployed-app/provider/model E2E remains separate.
const assert = require('node:assert/strict');
const { before, after, test } = require('node:test');
const { chromium } = require('playwright');
const { settingsFixture } = require('../fixtures/settings.cjs');

const projects = [
  { id: 7, full_name: 'group/subgroup/' + 'long-project-'.repeat(12) },
  { id: 8, full_name: 'group/another-subgroup/second-project' },
];
const syntheticToken = 'synthetic-gitlab-onboarding-token';
let fixture, browser;
before(async () => { fixture = await settingsFixture(); browser = await chromium.launch(); });
after(async () => { await browser?.close(); await fixture?.close(); });

async function open(t, { width = 1440, theme = 'dark', connected = false } = {}) {
  fixture.reset();
  const page = await browser.newPage({ viewport: { width, height: 1000 }, reducedMotion: 'reduce' });
  page.setDefaultTimeout(8000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  t.after(async () => { await page.close(); assert.deepEqual(errors, []); });
  const state = {
    connected, ids: connected ? [7] : [], revision: 'a'.repeat(64),
    origin: 'https://gitlab.example.test', requests: [], conflict: false, discoveryGate: null,
  };
  await page.addInitScript(theme => localStorage.setItem('moyai-appearance', theme), theme);
  await page.route('**/api/product/connections', route => route.fulfill({ json: [{
    id: 'gitlab', scope: 'organization', connected: state.connected,
    oauth_configured: false, label: state.connected ? 'Selected GitLab projects' : '',
    identity: 'Organization GitLab token · selected projects', tools: [],
    enabled: true, read_only: false, checked_at: null, check_status: null, updated_at: null,
    base_url: state.connected ? state.origin : null, app_registered: null,
    repositories: projects.filter(project => state.ids.includes(project.id)),
  }] }));
  await page.route('**/api/connections/gitlab/**', async route => {
    const request = route.request(), url = new URL(request.url());
    const body = request.method() === 'GET' ? null : request.postDataJSON();
    state.requests.push({ path: url.pathname, method: request.method(), body, query: url.searchParams, headers: request.headers() });
    if (url.pathname.endsWith('/discover') || request.method() === 'GET') {
      if (state.discoveryGate) await state.discoveryGate;
      const query = body?.search ?? url.searchParams.get('search') ?? '';
      const pageNumber = body?.page ?? Number(url.searchParams.get('page') || 1);
      const rows = query === 'missing' ? [] : query === 'second' ? [projects[1]]
        : pageNumber === 1 ? [projects[0]] : [projects[1]];
      return route.fulfill({ json: {
        base_url: body?.base_url || state.origin, repositories: rows, selected_ids: state.ids,
        next_page: !query && pageNumber === 1 ? 2 : null, truncated: false, revision: state.revision,
      } });
    }
    if (state.conflict) return route.fulfill({ status: 409, json: { detail: 'The GitLab connection changed. Discover its projects again.' } });
    state.ids = body.repository_ids; state.connected = true;
    state.origin = body.base_url || state.origin;
    return route.fulfill({ json: { connected: true, label: 'Selected GitLab projects' } });
  });
  await page.goto(fixture.base + '/#settings/connections');
  await page.getByRole('button', { name: 'Manage for GitLab', exact: true }).click();
  await page.getByRole('dialog', { name: 'GitLab · Organization access' }).waitFor();
  return { page, state };
}

async function connectForm(page, origin = 'https://gitlab.example.test') {
  await page.getByRole('button', { name: 'Connect GitLab', exact: true }).click();
  await page.getByLabel('GitLab URL', { exact: true }).fill(origin);
  await page.getByLabel('GitLab access token', { exact: true }).fill(syntheticToken);
}
async function discover(page) {
  await page.getByRole('button', { name: 'Discover projects', exact: true }).click();
  await page.getByRole('group', { name: 'GitLab projects', exact: true }).waitFor();
}

for (const theme of ['dark', 'light']) for (const width of [1440, 768, 320]) {
  test(`GitLab setup wraps long subgroup paths and retains keyboard focus at ${width}px in ${theme}`, async t => {
    const { page } = await open(t, { width, theme });
    await connectForm(page); await discover(page);
    const dialog = page.getByRole('dialog');
    const project = page.getByRole('checkbox', { name: projects[0].full_name + ' #7', exact: true });
    await project.focus(); await page.keyboard.press('Space');
    assert.equal(await project.isChecked(), true);
    const box = await dialog.boundingBox();
    assert(box.x >= 0 && box.x + box.width <= width);
    assert.equal(await dialog.evaluate(el => el.scrollWidth <= el.clientWidth), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    if (width === 320) assert.equal(await page.getByLabel('GitLab access token', { exact: true }).evaluate(el => getComputedStyle(el).fontSize), '16px');
    assert.doesNotMatch(await dialog.innerText(), new RegExp(syntheticToken));
    await page.keyboard.press('Escape'); await dialog.waitFor({ state: 'hidden' });
    await page.waitForFunction(() => document.activeElement?.textContent.includes('Manage'));
    await page.getByRole('button', { name: 'Manage for GitLab', exact: true }).click();
    await page.getByRole('button', { name: 'Connect GitLab', exact: true }).click();
    assert.equal(await page.getByLabel('GitLab access token', { exact: true }).inputValue(), '');
  });
}

test('GitLab connection saves explicit selections across pagination/search using the generated SDK and CSRF', async t => {
  const { page, state } = await open(t);
  await connectForm(page, 'http://127.0.0.1:58990'); await discover(page);
  assert.equal(await page.getByRole('button', { name: 'Save selected projects', exact: true }).isEnabled(), false);
  await page.getByRole('checkbox', { name: projects[0].full_name + ' #7', exact: true }).check();
  await page.getByRole('button', { name: 'Load more projects', exact: true }).click();
  await page.getByRole('checkbox', { name: projects[1].full_name + ' #8', exact: true }).check();
  await page.getByLabel('Search GitLab projects', { exact: true }).fill('missing');
  await page.getByRole('button', { name: 'Search projects', exact: true }).click();
  await page.getByText(/No projects found/).waitFor();
  await page.getByRole('button', { name: 'Clear search', exact: true }).click();
  await page.getByRole('checkbox', { name: projects[0].full_name + ' #7', exact: true }).waitFor();
  await page.getByRole('button', { name: 'Save selected projects', exact: true }).click();
  await page.getByRole('dialog').waitFor({ state: 'hidden' });
  const saved = state.requests.find(request => request.path.endsWith('/connect'));
  assert.deepEqual(saved.body, {
    repository_ids: [7, 8], revision: 'a'.repeat(64), token: syntheticToken, base_url: 'http://127.0.0.1:58990',
  });
  assert.equal(saved.headers['x-moyai-csrf'], 'fixture-csrf');
  assert.equal(state.requests.filter(request => request.path.endsWith('/connect')).length, 1);
  const nextPage = state.requests.find(request => request.body?.page === 2);
  assert.equal(nextPage.body.revision, 'a'.repeat(64));
  assert.equal(await page.evaluate(token => Object.values(localStorage).join(' ').includes(token), syntheticToken), false);
});

test('GitLab project changes reuse server credentials and retain revision-bound selected IDs', async t => {
  const { page, state } = await open(t, { connected: true });
  await page.getByRole('button', { name: 'Choose projects', exact: true }).click();
  const first = page.getByRole('checkbox', { name: projects[0].full_name + ' #7', exact: true });
  await first.waitFor(); assert.equal(await first.isChecked(), true);
  assert.equal(await page.locator('input[type="password"]').count(), 0);
  await page.getByLabel('Search GitLab projects', { exact: true }).fill('second');
  await page.getByRole('button', { name: 'Search projects', exact: true }).click();
  await page.getByRole('checkbox', { name: projects[1].full_name + ' #8', exact: true }).check();
  await page.getByText('Selected projects (2)', { exact: true }).click();
  await page.getByRole('button', { name: 'Remove project 7', exact: true }).click();
  await page.getByRole('button', { name: 'Save selected projects', exact: true }).click();
  await page.getByRole('dialog').waitFor({ state: 'hidden' });
  const saved = state.requests.find(request => request.method === 'PUT');
  assert.deepEqual(saved.body, { repository_ids: [8], revision: 'a'.repeat(64) });
  assert(!JSON.stringify(state.requests).includes(syntheticToken));
});

test('GitLab rejects stale selection saves until projects are rediscovered', async t => {
  const { page, state } = await open(t);
  await connectForm(page); await discover(page);
  await page.getByRole('checkbox', { name: projects[0].full_name + ' #7', exact: true }).check();
  state.conflict = true;
  await page.getByRole('button', { name: 'Save selected projects', exact: true }).click();
  await page.getByRole('alert').filter({ hasText: 'The GitLab connection changed' }).waitFor();
  assert.equal(await page.getByRole('button', { name: 'Save selected projects', exact: true }).count(), 0);
  state.conflict = false; state.revision = 'b'.repeat(64);
  await discover(page);
  assert.equal(await page.getByRole('checkbox', { name: projects[0].full_name + ' #7', exact: true }).isChecked(), false);
  await page.getByRole('checkbox', { name: projects[0].full_name + ' #7', exact: true }).check();
  await page.getByRole('button', { name: 'Save selected projects', exact: true }).click();
  await page.getByRole('dialog').waitFor({ state: 'hidden' });
  assert.equal(state.requests.filter(request => request.path.endsWith('/connect')).at(-1).body.revision, 'b'.repeat(64));
});

test('GitLab clears token and protected discovery results when access is revoked during an await', async t => {
  const { page, state } = await open(t);
  await connectForm(page);
  let release;
  state.discoveryGate = new Promise(resolve => { release = resolve; });
  t.after(() => release());
  const requested = page.waitForRequest('**/api/connections/gitlab/projects/discover');
  await page.getByRole('button', { name: 'Discover projects', exact: true }).click();
  await requested;
  await page.evaluate(() => window.dispatchEvent(new Event('moyai:access-changed')));
  release();
  await page.waitForFunction(() => [...document.querySelectorAll('input[type="password"]')].every(input => input.value === ''));
  assert.equal(await page.getByRole('group', { name: 'GitLab projects', exact: true }).count(), 0);
  assert.equal(state.requests.filter(request => request.path.endsWith('/connect')).length, 0);
});
