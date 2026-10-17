// Presentation/routing regression. Actual PostgreSQL app proof is separate.
const assert = require('node:assert/strict');
const {test, before, after} = require('node:test');
const path = require('node:path');
const {chromium} = require('playwright');
let server, browser, base, calls = [], invalid = false;

function activity(empty) {
  const metrics = {
    turns: 1, sessions: 1, sessions_active: 1, sessions_created: 1, completed: 1, failed: 0,
    requests: empty ? 0 : 15, input_tokens: empty ? 0 : 150, output_tokens: empty ? 0 : 50,
    total_tokens: empty ? 0 : 200, cost_usd: empty ? null : 3, cost_usd_exact: empty ? null : '3.000000',
    priced_requests: empty ? 0 : 15, unpriced_requests: 0, pricing_status: empty ? 'empty' : 'priced',
    latency_samples: 0, mean_latency_ms: null, p50_latency_ms: null, p95_latency_ms: null,
    prs_created: 0, prs_merged: 0, pr_status_counts: {open: 0, draft: 0, closed: 0, merged: 0, unknown: 0},
  };
  return {
    rows: [], total: 0, limit: 50, offset: 0,
    models: ['historical-root', 'native-root'],
    users: [{id: 'legacy:viewer', display_name: 'Test viewer', username: 'viewer@example.test'}],
    summary: metrics, by_user: [{user_id: 'legacy:viewer', ...metrics}],
    by_model: empty ? [] : [{model: 'historical-model', ...metrics}],
    series: [{date: '2026-10-17', ...metrics, cumulative_cost_usd: metrics.cost_usd,
      cumulative_pricing_status: metrics.pricing_status, models: empty ? [] : [{model: 'historical-model', ...metrics}]}],
    coverage: {legacy_observations: empty ? 0 : 12, native_observations: empty ? 0 : 3,
      legacy_turns: 0, ledger_requests: metrics.requests, priced_requests: metrics.requests,
      unpriced_requests: 0, pr_receipts: 0, pr_status_checked: 0, pr_status_unknown: 0},
    methodology: {cost: 'Recorded historical and native amounts.', model: 'Filters include historical sessions and native turns.'},
    capabilities: {reported_costs: true, unified_billing: true, pr_analytics: true},
  };
}

before(async () => {
  const {createServer} = await import('vite');
  server = await createServer({
    configFile: path.resolve('native/web/vite.config.ts'),
    server: {host: '127.0.0.1', port: 0},
    plugins: [{name: 'canonical-billing-fixture', configureServer(vite) {
      vite.middlewares.use((req, res, next) => {
        const url = new URL(req.url, 'http://local');
        if (!url.pathname.startsWith('/api/')) return next();
        let value = [];
        if (url.pathname === '/api/me') value = {
          user: {id: 'viewer', name: 'Test viewer', role: 'admin'}, csrf: 'fixture',
          models: ['native-root'], product_mode: 'existing',
        };
        if (url.pathname === '/api/auth/config') value = {mode: 'existing', password_enabled: true};
        if (url.pathname === '/api/settings/billing') {
          calls.push(url);
          value = invalid ? {total: {spend: '3.000000'}} :
            {total: {spend: '3.000000'}, activity: activity(url.searchParams.get('turn_status') === 'failed')};
        }
        if (url.pathname === '/api/settings/usage') {
          calls.push(url);
          res.writeHead(410, {'Content-Type': 'application/json'});
          return res.end(JSON.stringify({detail: 'The product UI must use the canonical billing endpoint.'}));
        }
        res.writeHead(200, {'Content-Type': 'application/json'});
        res.end(JSON.stringify(value));
      });
    }}],
  });
  await server.listen();
  base = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({headless: true});
});
after(async () => { await browser?.close(); await server?.close(); });

for (const width of [1440, 768, 320]) test(`canonical billing route, filters and recovery at ${width}px`, async t => {
  calls = []; invalid = false;
  const page = await browser.newPage({viewport: {width, height: 1000}, reducedMotion: 'reduce'});
  t.after(() => page.close());
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(base + '/#settings/usage');
  await page.locator('.analytics-metrics').getByText('$3.00', {exact: true}).waitFor();
  assert.equal(calls.at(-1).pathname, '/api/settings/billing');
  assert.equal(calls.at(-1).searchParams.get('include_activity'), 'true');
  const exportLink = page.getByRole('link', {name: 'Export CSV', exact: true});
  assert.match(await exportLink.getAttribute('href'), /^\/api\/settings\/billing\?/);
  await page.getByRole('combobox', {name: 'Model'}).click();
  await page.getByRole('option', {name: 'historical-root', exact: true}).click();
  await page.waitForFunction(() => document.querySelector('#analytics-report-panel')?.getAttribute('aria-busy') === 'false');
  assert.equal(calls.at(-1).searchParams.get('root_model'), 'historical-root');
  assert.equal(calls.at(-1).searchParams.has('model'), false);
  await page.getByRole('combobox', {name: 'Status'}).click();
  await page.getByRole('option', {name: 'Failed', exact: true}).click();
  await page.locator('.analytics-metrics').getByText('Unavailable', {exact: true}).waitFor();
  assert.equal(calls.at(-1).searchParams.get('turn_status'), 'failed');
  await page.getByRole('button', {name: 'Clear filters', exact: true}).click();
  await page.locator('.analytics-metrics').getByText('$3.00', {exact: true}).waitFor();
  await page.getByRole('tab', {name: 'Users', exact: true}).press('ArrowRight');
  await page.getByRole('heading', {name: 'Usage history', exact: true}).waitFor();
  invalid = true;
  await page.getByRole('button', {name: 'Refresh', exact: true}).click();
  await page.getByRole('alert').getByText('The usage report is unavailable.', {exact: true}).waitFor();
  invalid = false;
  await page.getByRole('button', {name: 'Try again', exact: true}).click();
  await page.getByRole('heading', {name: 'Usage history', exact: true}).waitFor();
  assert.ok(calls.length > 4 && calls.every(url => url.pathname === '/api/settings/billing'));
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  assert.deepEqual(errors, []);
});
