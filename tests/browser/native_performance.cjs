// Presentation regression only. Real gateway/Temporal before-and-after proof is separate.
const assert = require('node:assert/strict');
const {test, before, after} = require('node:test');
const path = require('node:path');
const {chromium} = require('playwright');
let server, browser, base, requests = 0;
const distribution = {samples: 0, mean_ms: null, p50_ms: null, p95_ms: null};
const card = { ...distribution, pending: 0, missing: 1, excluded: 0, total: 1, reasons: {not_instrumented: 1}};
function report(empty) {
  const count = empty ? 0 : 1;
  const current = empty ? {...card, missing: 0, total: 0, reasons: {}} : card;
  return {
    rows: empty ? [] : [{
      id: 'partial-turn', user_id: 'viewer', model: 'test-model', status: 'running', created_at: '2026-10-16T10:00:00Z',
      queue_pickup_ms: 25, api_to_worker_ms: null, api_to_dispatch_ms: null, broker_preparation_ms: null,
      startup_timing_status: 'waiting', queue_timing_status: 'measured', broker_timing_status: 'waiting',
      computer_timing_status: 'no_acquisition', timing_start_source: 'admission', timing_surface: 'slack',
      api_received_at: '2026-10-16T10:00:00Z', worker_picked_up_at: '2026-10-16T10:00:00.025Z',
    }],
    total: count, offset: 0, limit: 50, models: ['test-model'], users: [{id: 'viewer', display_name: 'Test viewer'}],
    summary: {turns: count, latency_samples: 0}, series: [],
    performance: {
      accounted_turns: count, dispatch: distribution,
      breakdown: {samples: 0, api_to_worker_ms: distribution, worker_to_broker_ms: distribution,
        broker_to_dispatch_ms: distribution, api_to_dispatch_ms: distribution},
      computer: {cold: distribution, restore: distribution, reuse: distribution, requested: 0, failed: 0, pending: 0},
      cards: {startup: current, queue: current, broker: current, computer: current},
    },
  };
}
before(async () => {
  const {createServer} = await import('vite');
  server = await createServer({
    configFile: path.resolve('native/web/vite.config.ts'),
    server: {host: '127.0.0.1', port: 0},
    plugins: [{name: 'timing-presentation-fixture', configureServer(vite) {
      vite.middlewares.use((req, res, next) => {
        const url = new URL(req.url, 'http://local');
        if (!url.pathname.startsWith('/api/')) return next();
        let value = [];
        if (url.pathname === '/api/me') value = {
          user: {id: 'viewer', name: 'Test viewer', role: 'admin'}, csrf: 'fixture', models: ['test-model'],
        };
        if (url.pathname === '/api/auth/config') value = {mode: 'standalone', password_enabled: true};
        if (url.pathname === '/api/settings/performance') {
          requests++; value = report(url.searchParams.get('status') === 'failed');
        }
        res.writeHead(200, {'Content-Type': 'application/json'}); res.end(JSON.stringify(value));
      });
    }}],
  });
  await server.listen();
  base = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({headless: true});
});
after(async () => { await browser?.close(); await server?.close(); });

for (const width of [1440, 768, 320]) test(`timing reasons, partial stages and filter recovery at ${width}px`, async t => {
  const page = await browser.newPage({viewport: {width, height: 1000}, reducedMotion: 'reduce'});
  t.after(() => page.close());
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(base + '/#settings/performance');
  const startup = page.getByRole('region', {name: 'Startup to dispatch', exact: true});
  await startup.getByText('Timing coverage · 1 turn', {exact: true}).click();
  await startup.getByText('1 · No instrumentation record', {exact: true}).waitFor();
  const before = requests;
  const refreshed = page.waitForResponse(response => response.url().includes('/api/settings/performance'));
  await page.getByRole('button', {name: 'Refresh', exact: true}).click();
  await refreshed;
  await page.waitForFunction(() => document.querySelector('#analytics-report-panel')?.getAttribute('aria-busy') === 'false');
  assert.ok(requests > before);
  assert.equal(await startup.locator('details').getAttribute('open'), '');
  const row = page.getByRole('row').filter({hasText: 'partial-turn'});
  await row.getByText('Receipt → worker: 25 ms', {exact: true}).waitFor();
  await row.getByText('Timing evidence', {exact: true}).click();
  await row.getByText('Start: Native admission · slack', {exact: true}).waitFor();
  await page.getByRole('combobox', {name: 'Report status'}).click();
  await page.getByRole('option', {name: 'Failed', exact: true}).click();
  await page.getByText('No recorded turns match this range and its filters.', {exact: true}).waitFor();
  await page.getByRole('button', {name: 'Clear filters', exact: true}).click();
  await row.getByText('Receipt → worker: 25 ms', {exact: true}).waitFor();
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  assert.deepEqual(errors, []);
});
