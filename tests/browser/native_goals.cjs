// Actual React controls with synthetic state; live-app/model proof is separate.
const { before, after, test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs/promises");
const os = require("node:os");
const { chromium } = require("playwright");
const root = path.resolve(__dirname, "../..");
const moduleUrl = "/tests/__goal_fixture.tsx";
let server, browser, address, cache;
const fixture = `
import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { GoalDialog, GoalPanel } from '../native/web/goal';
import { SkillComposer } from '../native/web/skill-composer';
import { setCsrf } from '../native/web/api';
import '../frontend/theme.css';
import '../native/web/style.css';
setCsrf('synthetic-csrf');
const initial = {objective:'Verify the result and keep working until it is complete.',status:'active',
  generation:1,owner_id:'owner',tokens_used:120,token_budget:10000,turns:2,blocked_count:0,reason:'',created_at:''};
function Fixture() {
  const [goal,setGoal] = useState(initial), [open,setOpen] = useState(false), [draft,setDraft] = useState('');
  window.goalFixture = {setGoal};
  const command = value => { window.goalCommand = value; };
  return <main style={{maxWidth:900,margin:'auto'}}>
    <GoalPanel goal={goal} canControl={true} busy={false} onCommand={command} onEdit={()=>setOpen(true)} />
    <GoalDialog open={open} onOpenChange={setOpen} goal={goal} onSubmit={async value=>command(value)} />
    <SkillComposer value={draft} onChange={setDraft} onSend={command} placeholder="Message Moyai" />
    <output aria-label="Composer value">{draft}</output>
  </main>;
}
createRoot(document.getElementById('root')).render(<Fixture/>);
`;
before(async () => {
  const { createServer } = await import("vite");
  const { default: tailwindcss } = await import("@tailwindcss/vite");
  cache = await fs.mkdtemp(path.join(os.tmpdir(),"moyai-goals-vite-"));
  server = await createServer({
    configFile:false, root, cacheDir:cache, logLevel:"error",
    resolve:{alias:{"@":path.join(root,"frontend")}},
    server:{host:"127.0.0.1",port:0,strictPort:false,hmr:false},
    plugins:[tailwindcss(), {
      name:"goal-regression",
      resolveId(id){if(id===moduleUrl)return path.join(root,"tests/__goal_fixture.tsx");},
      load(id){if(id===path.join(root,"tests/__goal_fixture.tsx"))return fixture;},
      configureServer(vite){vite.middlewares.use((request,response,next)=>{
        if(new URL(request.url,"http://localhost").pathname!=="/")return next();
        void vite.transformIndexHtml("/",`<!doctype html><html lang="en"><meta charset="utf-8"><div id="root"></div><script type="module" src="${moduleUrl}"></script></html>`)
          .then(value=>{response.setHeader("Content-Type","text/html");response.end(value);}).catch(next);
      });},
    }],
  });
  await server.listen();
  address=`http://127.0.0.1:${server.httpServer.address().port}`;
  browser=await chromium.launch();
  await server.transformRequest(moduleUrl);
});
after(async()=>{
  await browser?.close();await server?.close();
  if(cache)await fs.rm(cache,{recursive:true,force:true});
});
async function pageFor(t,width=1440){
  const page=await browser.newPage({viewport:{width,height:900},reducedMotion:"reduce"});
  await page.route("**/api/skills?**",route=>route.fulfill({status:503,json:{detail:"Skills temporarily unavailable"}}));
  t.after(()=>page.close());
  await page.goto(address);
  return page;
}
for(const width of [1440,768,320]){
  test(`goal controls and edit dialog fit ${width}px`,async t=>{
    const page=await pageFor(t,width);
    await page.getByRole("button",{name:"Pause goal",exact:true}).click();
    assert.equal(await page.evaluate(()=>window.goalCommand),"/goal pause");
    await page.getByRole("button",{name:"Edit goal",exact:true}).click();
    await page.getByLabel("Objective",{exact:true}).fill("Verify every output.");
    await page.getByLabel("Token budget (optional)",{exact:true}).fill("20000");
    await page.getByRole("button",{name:"Save goal",exact:true}).click();
    assert.equal(await page.evaluate(()=>window.goalCommand),"/goal edit --budget 20000 Verify every output.");
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.getByRole("button",{name:"Clear goal",exact:true}).click();
    await page.getByRole("dialog").getByRole("button",{name:"Cancel",exact:true}).click();
    assert.notEqual(await page.evaluate(()=>window.goalCommand),"/goal clear");
  });
}
test("native /goal remains available without a skill catalog and inserts plain command text",async t=>{
  const page=await pageFor(t);
  const input=page.getByRole("textbox",{name:"Message Moyai",exact:true});
  await input.fill("/go");
  await page.getByRole("option",{name:/Goal/}).waitFor();
  await input.press("Enter");
  assert.equal(await page.getByLabel("Composer value").textContent(),"/goal ");
  assert.equal(await input.locator(".skill-mention").count(),0);
  await input.fill("/goal Verify every output.");
  await input.press("Enter");
  assert.equal(await page.evaluate(()=>window.goalCommand),"/goal Verify every output.");
});
