// Real application and appearance bootstrap against local, synthetic session APIs.
const assert = require('node:assert/strict');
const {before, after, test} = require('node:test');
const path = require('node:path');
const os = require('node:os');
const fs = require('node:fs/promises');
const {chromium} = require('playwright');

const sessionId = 'a'.repeat(32);
const session = {id:sessionId, title:'Appearance fixture', owner_id:'viewer', visibility:'private',
  surface:'web', model:'test-model', status:'idle', active_turn:null, archived:false, pinned:false};
const clients = new Set();
let server, browser, base, cache;

before(async () => {
  const {createServer} = await import('vite');
  cache = await fs.mkdtemp(path.join(os.tmpdir(), 'moyai-appearance-'));
  server = await createServer({configFile:path.resolve('native/web/vite.config.ts'), cacheDir:cache,
    server:{host:'127.0.0.1',port:0,strictPort:false}, plugins:[{
      name:'appearance-synthetic-api', configureServer(vite) {
        vite.middlewares.use((req,res,next) => {
          const url = new URL(req.url,'http://localhost');
          if (!url.pathname.startsWith('/api/')) return next();
          const json = value => {res.writeHead(200,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(value));};
          if (url.pathname === '/api/me') return json({user:{id:'viewer',name:'Test viewer',role:'admin',individual:true},
            csrf:'fixture-csrf',models:['test-model'],default_model:'test-model',product_mode:'standalone'});
          if (url.pathname === '/api/history/sessions') return json({sessions:[{...session,source:'native',url:'/#'+sessionId}],ancestors:[],has_more:false});
          if (['/api/session-folders','/api/connections'].includes(url.pathname)) return json([]);
          if (url.pathname === '/api/skills') return json({skills:[],has_more:false});
          if (url.pathname === '/api/pull-requests/statuses') return json({});
          if (url.pathname === '/api/settings/overview') return json({counts:{},capabilities:{}});
          if (url.pathname === `/api/sessions/${sessionId}`) return json({session,turns:[],messages:[],pending:{calls:[],approvals:[]}});
          if (url.pathname.endsWith('/events')) {
            res.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-store'});
            res.write('event: connected\ndata: {}\n\n');clients.add(res);res.on('close',()=>clients.delete(res));return;
          }
          if (url.pathname.endsWith('/workspace')) return json({links:[],files:[],sources:[],subagents:[],subagents_limited:false});
          if (url.pathname.endsWith('/read')) return json({});
          if (['/messages','/plan','/activity'].some(suffix=>url.pathname.endsWith(suffix))) return json([]);
          res.writeHead(404,{'Content-Type':'application/json'});res.end(JSON.stringify({detail:'Fixture endpoint not implemented: '+url.pathname}));
        });
      },
    }]});
  await server.listen();base = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({headless:true});
});

after(async () => {
  for (const client of clients) client.end();
  await browser?.close();await server?.close();
  if (cache) await fs.rm(cache,{recursive:true,force:true});
});

async function pageFor(t, options = {}, initialize) {
  const page = await browser.newPage({viewport:{width:1440,height:1000},reducedMotion:'reduce',...options});
  t.after(()=>page.close());
  const errors=[];page.on('pageerror',error=>errors.push(error.message));t.after(()=>assert.deepEqual(errors,[]));
  if (initialize) await initialize(page);
  await page.goto(base);return page;
}

async function assertAppearance(page, appearance) {
  await page.waitForFunction(value => document.documentElement.dataset.theme === value,appearance);
  assert.equal(await page.locator('html').evaluate(el=>el.classList.contains('dark')),appearance==='dark');
  assert.equal(await page.locator('html').evaluate(el=>getComputedStyle(el).colorScheme),appearance);
  assert.equal(await page.locator('meta[name="color-scheme"]').getAttribute('content'),appearance);
}

test('appearance defaults to dark and changes across home, chat, settings and reload without replacing drafts', async t => {
  const page = await pageFor(t,{colorScheme:'light'});
  const light = () => page.getByRole('button',{name:'Switch to light mode',exact:true});
  const dark = () => page.getByRole('button',{name:'Switch to dark mode',exact:true});
  await light().waitFor();await assertAppearance(page,'dark');
  const homeDraft = page.getByRole('textbox',{name:'Message Moyai',exact:true});
  await homeDraft.fill('Keep the home draft while changing appearance.');
  await light().press('Enter');await dark().waitFor();await assertAppearance(page,'light');
  assert.equal(await homeDraft.innerText(),'Keep the home draft while changing appearance.');
  assert.equal(await page.evaluate(()=>localStorage.getItem('moyai-appearance')),'light');
  await page.locator(`[data-session-key="native:${sessionId}"]`).getByRole('link').click();
  await page.getByRole('heading',{name:'Appearance fixture',exact:true}).waitFor();
  const chatDraft = page.getByRole('textbox',{name:'Message Moyai',exact:true});
  await chatDraft.fill('Keep the chat draft too.');
  await dark().click();await assertAppearance(page,'dark');
  assert.equal(await chatDraft.innerText(),'Keep the chat draft too.');
  await page.getByRole('button',{name:'Settings',exact:true}).click();
  await page.getByRole('heading',{name:'Settings',exact:true}).waitFor();
  await light().press('Space');await assertAppearance(page,'light');
  await page.reload();await dark().waitFor();await assertAppearance(page,'light');
});

test('saved light appearance is applied before the application module loads', async t => {
  const page = await pageFor(t,{},async page => {
    await page.addInitScript(()=>localStorage.setItem('moyai-appearance','light'));
    await page.route('**/main.tsx',route=>route.abort());
  });
  await assertAppearance(page,'light');
  assert.equal(await page.locator('.app').count(),0);
});

test('blocked appearance storage keeps switching usable for the current page', async t => {
  const page = await pageFor(t,{},async page=>page.addInitScript(()=>{
    Storage.prototype.getItem=()=>{throw new Error('Storage unavailable');};
    Storage.prototype.setItem=()=>{throw new Error('Storage unavailable');};
  }));
  await page.getByRole('button',{name:'Switch to light mode',exact:true}).click();
  await assertAppearance(page,'light');
  await page.getByRole('button',{name:'Settings',exact:true}).click();
  await page.getByRole('button',{name:'Switch to dark mode',exact:true}).click();
  await assertAppearance(page,'dark');
});

test('appearance choices synchronize between open tabs', async t => {
  const context = await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
  t.after(()=>context.close());
  const first=await context.newPage(), second=await context.newPage();
  await first.goto(base);await second.goto(base+'/#settings/overview');
  await second.getByRole('button',{name:'Switch to light mode',exact:true}).waitFor();
  await first.getByRole('button',{name:'Switch to light mode',exact:true}).click();
  await second.getByRole('button',{name:'Switch to dark mode',exact:true}).waitFor();
  await assertAppearance(first,'light');await assertAppearance(second,'light');
  await first.evaluate(()=>window.dispatchEvent(new StorageEvent('storage',{
    key:'moyai-appearance',newValue:'dark',storageArea:sessionStorage,
  })));
  await assertAppearance(first,'light');
  await second.getByRole('button',{name:'Switch to dark mode',exact:true}).click();
  await first.getByRole('button',{name:'Switch to light mode',exact:true}).waitFor();
  await assertAppearance(first,'dark');
});

test('appearance controls remain reachable in narrow workspace and Settings navigation', async t => {
  const page = await pageFor(t,{viewport:{width:320,height:1000},isMobile:true,hasTouch:true});
  await page.getByRole('button',{name:'Toggle conversations',exact:true}).tap();
  const light=page.getByRole('button',{name:'Switch to light mode',exact:true});
  const bounds=await light.boundingBox();
  assert.ok(bounds&&bounds.width>=44&&bounds.height>=44);
  await light.tap();await assertAppearance(page,'light');
  await page.getByRole('button',{name:'Settings',exact:true}).tap();
  await page.getByRole('button',{name:'Switch to dark mode',exact:true}).tap();
  await assertAppearance(page,'dark');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
});
