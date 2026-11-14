// Actual React components with synthetic API responses. Supplementary UI checks;
// the lead owns baseline/final deployed-app and real-provider/model E2E proof.
const { before, after, test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs/promises');
const os = require('node:os');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '../..');
const moduleUrl = '/tests/__gitlab_review_fixture.tsx';
const entry = path.join(root, moduleUrl);
const mrUrl = 'https://gitlab.example/team/subgroup/project/-/merge_requests/12';
let server, browser, address, cache;
const fixture = `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { PullRequestPanel } from '../native/web/pull-request';
import { WorkspaceOverview } from '../native/web/workspace';
import { setCsrf } from '../native/web/api';
import '../frontend/theme.css';
import '../native/web/style.css';
setCsrf('synthetic-csrf');
document.documentElement.dataset.theme = new URLSearchParams(location.search).get('theme') || 'dark';
createRoot(document.getElementById('root')).render(location.search.includes('workspace')
  ? <WorkspaceOverview id="review-fixture" pullRequests={[]} onUpdated={() => {}}
      onPullRequest={url => { window.selectedMR = url; }} onSubagent={() => {}} onOutput={() => {}} />
  : <PullRequestPanel sessionId="review-fixture" url="${mrUrl}" onBack={() => {}} />);
`;
const detail = {
  url: mrUrl, repository: 'team/subgroup/project', number: 12, title: 'Synthetic GitLab review',
  state: 'open', draft: false, accessible: true, description: 'Description from GitLab.',
  description_limited: false, author: { login: 'test-author', bot: false },
  head: 'feature', base: 'main', head_sha: 'a'.repeat(40), base_sha: 'b'.repeat(40),
  changed_files: 1, additions: null, deletions: null, commits: null,
  updated_at: '2026-11-13T01:00:00Z', merged_at: '', error: null,
};
const sections = {
  files: { items: [{
    filename: 'src/review.py', previous_filename: '', status: 'modified',
    additions: 1, deletions: 1, patch: '@@ -1 +1 @@\n-old\n+new', patch_status: 'available',
  }], notice: 'GitLab may omit large or binary diffs.' },
  discussion: { items: [{
    id: 'note-55', kind: 'inline', author: { login: 'test-reviewer', bot: false },
    body: 'Please check this change.', body_limited: false, state: 'RESOLVED', path: 'src/review.py',
    line: 1, created_at: '2026-11-13T01:00:00Z', url: mrUrl + '#note_55',
  }] },
  commits: { items: [{
    sha: 'a'.repeat(40), message: 'Verified commit', author: { login: 'test-author', bot: false },
    created_at: '2026-11-13T01:00:00Z', url: null,
  }] },
  checks: { head_sha: 'a'.repeat(40), items: [{
    id: 'pipeline-91', name: 'Pipeline #91', status: 'failure', url: null,
    title: 'feature', summary: '', text: '', annotations: 0, output_limited: false,
  }] },
};

before(async () => {
  const { createServer } = await import('vite');
  const { default: tailwindcss } = await import('@tailwindcss/vite');
  cache = await fs.mkdtemp(path.join(os.tmpdir(), 'moyai-gitlab-review-vite-'));
  server = await createServer({
    configFile: false, root, cacheDir: cache, logLevel: 'error',
    resolve: { alias: { '@': path.join(root, 'frontend') } },
    server: { host: '127.0.0.1', port: 0, strictPort: false, hmr: false },
    plugins: [tailwindcss(), {
      name: 'gitlab-review-regression',
      resolveId(id) { if (id === moduleUrl) return entry; },
      load(id) { if (id === entry) return fixture; },
      configureServer(vite) {
        vite.middlewares.use((request, response, next) => {
          if (new URL(request.url, 'http://localhost').pathname !== '/') return next();
          void vite.transformIndexHtml('/', `<!doctype html><html lang="en"><meta charset="utf-8">
            <meta name="viewport" content="width=device-width,initial-scale=1">
            <title>Synthetic GitLab review fixture</title><div id="root"></div>
            <script type="module" src="${moduleUrl}"></script></html>`)
            .then(html => { response.setHeader('Content-Type', 'text/html'); response.end(html); }).catch(next);
        });
      },
    }],
  });
  await server.listen();
  await server.transformRequest(moduleUrl);
  await server.waitForRequestsIdle();
  address = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch();
});

after(async () => {
  await browser?.close();
  await server?.close();
  if (cache) await fs.rm(cache, { recursive: true, force: true });
});

async function pageFor(t, width, theme, workspace = false) {
  const page = await browser.newPage({ viewport: { width, height: 900 }, reducedMotion: 'reduce' });
  const errors = [], state = { revoked: false, links: [], posts: [] };
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== address) return route.abort();
    if (!url.pathname.startsWith('/api/')) return route.continue();
    if (state.revoked) return route.fulfill({ status: 403, json: { detail: 'Selected GitLab project access was revoked.' } });
    if (url.pathname.endsWith('/pull-request')) {
      const tab = url.searchParams.get('tab') || 'overview';
      return route.fulfill({ json: { ...detail, tab, ...(sections[tab]
        ? { section: { limited: false, error: null, ...sections[tab] } } : {}) } });
    }
    if (url.pathname.endsWith('/workspace')) return route.fulfill({
      json: { links: state.links, files: [], sources: [], subagents: [], subagents_limited: false },
    });
    if (url.pathname.endsWith('/links') && request.method() === 'POST') {
      assert.equal(request.headers()['x-moyai-csrf'], 'synthetic-csrf');
      const body = request.postDataJSON();
      state.posts.push(body);
      state.links.push({ ...body, id: 'mr-link', origin: 'attached', can_remove: true });
      return route.fulfill({ json: state.links[0] });
    }
    return route.fulfill({ status: 404, json: { detail: 'Unexpected synthetic API request' } });
  });
  t.after(async () => { await page.close(); assert.deepEqual(errors, []); });
  await page.goto(`${address}/?theme=${theme}${workspace ? '&workspace' : ''}`);
  return { page, state };
}

async function noOverflow(page) {
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
}

for (const width of [1440, 768, 320]) for (const theme of ['dark', 'light']) {
  test(`GitLab review sections, keyboard and revocation at ${width}px ${theme}`, async t => {
    const { page, state } = await pageFor(t, width, theme);
    await page.getByLabel('Diff for src/review.py').waitFor();
    assert.equal(await page.getByRole('link', { name: 'Open in GitLab' }).getAttribute('href'), mrUrl);
    assert.equal(await page.locator('.pr-summary .pr-added').count(), 0, 'Missing totals are not invented');
    await page.getByRole('button', { name: 'Review details' }).click();
    await page.getByText('team/subgroup/project !12', { exact: true }).waitFor();
    await page.getByRole('tab', { name: 'Changes', exact: true }).focus();
    await page.keyboard.press('ArrowRight');
    await page.getByText('Description from GitLab.', { exact: true }).waitFor();
    await page.getByRole('tab', { name: 'Discussion', exact: true }).click();
    await page.getByText('Please check this change.', { exact: true }).waitFor();
    await page.getByRole('tab', { name: 'Commits', exact: true }).click();
    await page.getByText('Verified commit', { exact: true }).waitFor();
    await page.getByRole('tab', { name: 'Checks', exact: true }).click();
    await page.getByText('Pipeline #91', { exact: true }).waitFor();
    await noOverflow(page);
    if (process.env.MOYAI_GITLAB_REVIEW_EVIDENCE) {
      await page.getByRole('tab', { name: 'Changes', exact: true }).click();
      const directory = process.env.MOYAI_GITLAB_REVIEW_EVIDENCE;
      await fs.mkdir(directory, { recursive: true });
      await page.screenshot({ path: path.join(directory, `synthetic-mr-${width}-${theme}.png`), fullPage: true });
    }
    state.revoked = true;
    await page.getByRole('button', { name: 'Refresh', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: 'Selected GitLab project access was revoked.' }).waitFor();
    assert.equal(await page.getByLabel('Diff for src/review.py').count(), 0);
    assert.equal(await page.getByRole('tablist').count(), 0);
  });
}

for (const width of [1440, 768, 320]) {
  test(`GitLab attachment dialog validates, submits and restores focus at ${width}px`, async t => {
    const { page, state } = await pageFor(t, width, 'dark', true);
    const opener = page.getByRole('button', { name: 'Add pull requests', exact: true });
    await opener.click();
    const dialog = page.getByRole('dialog', { name: 'Attach pull request' });
    await dialog.getByLabel('URL', { exact: true }).fill(mrUrl.replace('https:', 'http:'));
    await dialog.getByRole('button', { name: 'Add link', exact: true }).click();
    await dialog.getByRole('alert').waitFor();
    assert.equal(state.posts.length, 0);
    await dialog.getByLabel('URL', { exact: true }).fill(mrUrl);
    await dialog.getByLabel('Label (optional)', { exact: true }).fill('Synthetic attached MR');
    await page.keyboard.press('Tab');
    assert(await dialog.evaluate(node => node.contains(document.activeElement)));
    await noOverflow(page);
    await dialog.getByRole('button', { name: 'Add link', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.deepEqual(state.posts, [{ kind: 'pull_request', url: mrUrl, title: 'Synthetic attached MR' }]);
    await page.waitForFunction(() => document.activeElement?.getAttribute('aria-label') === 'Add pull requests');
    assert(await opener.evaluate(node => node === document.activeElement));
    await page.getByRole('button', { name: 'Synthetic attached MR', exact: false }).first().click();
    assert.equal(await page.evaluate(() => window.selectedMR), mrUrl);
    await opener.click();
    await dialog.waitFor();
    await page.keyboard.press('Escape');
    await dialog.waitFor({ state: 'hidden' });
    await page.waitForFunction(() => document.activeElement?.getAttribute('aria-label') === 'Add pull requests');
    assert(await opener.evaluate(node => node === document.activeElement));
  });
}
