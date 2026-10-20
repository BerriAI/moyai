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
import { GoalChip, GoalEditor, GoalPanel } from '../native/web/goal';
import { SkillComposer } from '../native/web/skill-composer';
import { Home } from '../native/web/home';
import { setCsrf } from '../native/web/api';
import '../frontend/theme.css';
import '../native/web/style.css';
setCsrf('synthetic-csrf');
const initial = {objective:'Verify the result and keep working until it is complete.',status:'active',
  elapsed_seconds:42,generation:1,owner_id:'owner',tokens_used:120,token_budget:10000,turns:2,blocked_count:0,reason:'',created_at:''};
function Fixture() {
  const [goal,setGoal] = useState(initial), [open,setOpen] = useState(false), [draft,setDraft] = useState(''), [goalMode,setGoalMode] = useState(false);
  window.goalFixture = {setGoal};
  const command = value => { window.goalCommand = value; };
  if(location.search.includes('home')) return <Home models={['test-model']} defaultModel="test-model"
    preferences={{delivery_mode:'when_idle',connection_ids:location.search.includes('accounts')?['team']:[]}}
    onStarted={value=>{window.startedConversation=value;}}
    onCreated={async()=>{if(location.search.includes('refreshFailure'))throw new Error('List unavailable.');}} />;
  return <main style={{maxWidth:900,margin:'auto'}}>
    <GoalPanel goal={goal} canControl={true} busy={false} onCommand={command} onEdit={()=>setOpen(true)} />
    {open && <GoalEditor goal={goal} canControl={true} busy={false} onSubmit={async value=>command(value)} />}
    <SkillComposer value={draft} onChange={setDraft} onGoalSelect={()=>{setGoalMode(true);setDraft('');}} onSend={value=>command(goalMode?'/goal --budget none '+value:value)} placeholder="Message Moyai" />
    {goalMode && <GoalChip onRemove={()=>setGoalMode(false)} />}
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
    optimizeDeps:{
      entries:["native/web/main.tsx"],
      include:["three","three/examples/jsm/loaders/GLTFLoader.js"],
    },
    server:{host:"127.0.0.1",port:0,strictPort:false},
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
async function pageFor(t,width=1440,query="",handler){
  const page=await browser.newPage({viewport:{width,height:900},reducedMotion:"reduce"});
  await page.route(/^https?:\/\/[^/]+\/api\//,handler|| (route=>route.fulfill({status:503,json:{detail:"Skills temporarily unavailable"}})));
  t.after(()=>page.close());
  await page.goto(address+query);
  return page;
}
for(const width of [1440,768,320]){
  test(`goal controls and editor fit ${width}px`,async t=>{
    const page=await pageFor(t,width);
    await page.getByRole("button",{name:"Pause goal",exact:true}).click();
    assert.equal(await page.evaluate(()=>window.goalCommand),"/goal pause");
    await page.getByRole("button",{name:"Expand goal",exact:true}).click();
    await page.getByText("Goal details",{exact:true}).click();
    await page.getByLabel("Objective",{exact:true}).fill("Verify every output.");
    await page.getByLabel("Token budget (optional)",{exact:true}).fill("20000");
    await page.getByRole("button",{name:"Save goal",exact:true}).click();
    assert.equal(await page.evaluate(()=>window.goalCommand),"/goal edit --budget 20000 Verify every output.");
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.getByRole("button",{name:"Clear goal",exact:true}).click();
    assert.equal(await page.evaluate(()=>window.goalCommand),"/goal clear");
  });
}
test("native /goal selection activates the removable chip without sending or inserting command text",async t=>{
  const page=await pageFor(t);
  const input=page.getByRole("textbox",{name:"Message Moyai",exact:true});
  await input.fill("/go");
  await page.getByRole("option",{name:/Goal/}).waitFor();
  await input.press("Enter");
  assert.equal(await page.getByLabel("Composer value").textContent(),"");
  assert.equal(await page.evaluate(()=>window.goalCommand),undefined);
  await page.getByRole("button",{name:"Remove goal mode"}).waitFor();
  assert.equal(await input.locator(".skill-mention").count(),0);
  await input.fill("Verify every output.");
  await input.press("Enter");
  assert.equal(await page.evaluate(()=>window.goalCommand),"/goal --budget none Verify every output.");
  await page.getByRole("button",{name:"Remove goal mode"}).click();
  assert.equal(await input.innerText(),"Verify every output.");
  await input.press("Enter");
  assert.equal(await page.evaluate(()=>window.goalCommand),"Verify every output.");
});

async function openHomeGoal(page,objective="Verify the requested result."){
  const input=page.getByRole("textbox",{name:"Message Moyai",exact:true});
  await input.fill("/go");
  await page.getByRole("option",{name:/Goal/}).waitFor();
  await input.press("Enter");
  await page.getByRole("button",{name:"Remove goal mode"}).waitFor();
  await input.fill(objective);
  return input;
}
function homeApi(requests,{fail,connections,upload}={}){
  return async route=>{
    const request=route.request(),url=new URL(request.url());
    if(url.pathname==="/api/skills")return route.fulfill({json:{skills:[],has_more:false,next_offset:null}});
    if(url.pathname==="/api/connections"){
      if(connections)await connections;
      return route.fulfill({json:[{id:"team",name:"Team",enabled:true,shared:true,read_only:true}]});
    }
    if(url.pathname==="/api/uploads"){
      if(upload)await upload;
      return route.fulfill({json:{id:"input-file",name:"input.txt",media_type:"text/plain",size:5}});
    }
    const body=request.postDataJSON();
    requests.push({path:url.pathname,body});
    if(url.pathname==="/api/sessions")return route.fulfill({json:{id:"home-goal",owner_id:"owner",model:"test-model",visibility:"shared",status:"idle"}});
    if(url.pathname.endsWith("/messages")){
      if(fail?.())return route.fulfill({status:503,json:{detail:"Goal admission temporarily unavailable."}});
      return route.fulfill({status:202,json:{id:"goal-turn",actor_id:"owner",status:"queued",model:"test-model",connections:body.connections||[]}});
    }
    return route.fulfill({json:{}});
  };
}
test("Home goal retains its draft and mode after failed admission and retries the same request",async t=>{
  const requests=[];let failed=true;
  const page=await pageFor(t,1440,"?home",homeApi(requests,{fail:()=>failed}));
  const input=await openHomeGoal(page);
  await input.press("Enter");
  await page.getByRole("alert").filter({hasText:"admission temporarily unavailable"}).waitFor();
  assert.equal(await input.innerText(),"Verify the requested result.");
  await page.getByRole("button",{name:"Remove goal mode"}).waitFor();
  assert.equal(await page.evaluate(()=>window.startedConversation),undefined);
  failed=false;
  await input.press("Enter");
  await page.waitForFunction(()=>window.startedConversation);
  const submitted=requests.filter(row=>row.path.endsWith("/messages"));
  assert.equal(submitted.length,2);
  assert.deepEqual(submitted[0],submitted[1]);
  assert.equal(submitted[0].body.prompt,"/goal --budget none Verify the requested result.");
  assert.equal(requests.filter(row=>row.path==="/api/sessions").length,1);
});
test("Home goal preserves its draft while connections load",async t=>{
  const requests=[];let release;
  const connections=new Promise(resolve=>{release=resolve;});
  t.after(()=>release());
  const page=await pageFor(t,768,"?home&accounts",homeApi(requests,{connections}));
  const input=await openHomeGoal(page);
  await input.press("Enter");
  await page.getByRole("alert").filter({hasText:"Connections are still loading"}).waitFor();
  assert.equal(requests.length,0);
  release();
  await page.waitForFunction(()=>!document.querySelector('button[aria-label="Send message"]').disabled);
  await input.press("Enter");
  await page.waitForFunction(()=>window.startedConversation);
  assert.deepEqual(requests.find(row=>row.path.endsWith("/messages")).body.connections,["team"]);
});
for(const pending of [false,true]){
  test(`Home goal carries staged attachments${pending?" after uploading finishes":""}`,async t=>{
    const requests=[];let release=()=>{};
    const upload=pending?new Promise(resolve=>{release=resolve;}):undefined;
    t.after(()=>release());
    const page=await pageFor(t,1440,"?home",homeApi(requests,{upload}));
    await page.locator('input[type="file"]').setInputFiles({name:"input.txt",mimeType:"text/plain",buffer:Buffer.from("input")});
    if(!pending)await page.locator('.image-preview[aria-busy="false"]').waitFor();
    const input=await openHomeGoal(page);
    await input.press("Enter");
    if(pending){
      await page.getByRole("alert").filter({hasText:"files to finish uploading"}).waitFor();
      assert.equal(requests.length,0);
      release();
      await page.waitForFunction(()=>document.querySelector('.image-preview[aria-busy="false"]'));
      await input.press("Enter");
    }
    await page.waitForFunction(()=>window.startedConversation);
    assert.deepEqual(requests.find(row=>row.path.endsWith("/messages")).body.attachments,["input-file"]);
    assert.equal((await page.evaluate(()=>window.startedConversation.attachments))[0].id,"input-file");
  });
}
test("An accepted Home goal is not resubmitted if only the list refresh fails",async t=>{
  const requests=[];
  const page=await pageFor(t,1440,"?home&refreshFailure",homeApi(requests));
  const input=await openHomeGoal(page);
  await input.press("Enter");
  await page.getByRole("alert").filter({hasText:"message was sent"}).waitFor();
  assert.equal(requests.filter(row=>row.path.endsWith("/messages")).length,1);
  assert.equal((await page.evaluate(()=>window.startedConversation.turn)).id,"goal-turn");
});
