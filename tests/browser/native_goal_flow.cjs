// Real React application against synthetic APIs; backend lifecycle tests cover execution.
const { before, after, test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs/promises');
const { chromium } = require('playwright');
let server, browser, base;
before(async () => {
  const { createServer } = await import('vite');
  server = await createServer({configFile:path.resolve('native/web/vite.config.ts'),server:{host:'127.0.0.1',port:0,hmr:false},logLevel:'error'});
  await server.listen();base=`http://127.0.0.1:${server.httpServer.address().port}`;
  browser=await chromium.launch();
});
after(async()=>{await browser?.close();await server?.close();});
async function scenario(t,width=1440,light=false){
  const page=await browser.newPage({viewport:{width,height:900},reducedMotion:'reduce'}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  t.after(async()=>{await page.close();assert.deepEqual(errors,[]);});
  if(light)await page.addInitScript(()=>localStorage.setItem('moyai-appearance','light'));
  const state={goal:null,messages:[],turns:[],posts:[],reject:false,deny:false,stops:0,
    session:{id:'chat',title:'Goal workflow',status:'idle',active_turn:null,model:'test-model',visibility:'private',surface:'web',owner_id:'viewer'}};
  await page.route(/^https?:\/\/[^/]+\/api\//,async route=>{
    const request=route.request(),p=new URL(request.url()).pathname;
    const json=(data,status=200)=>route.fulfill({json:data,status});
    if(p==='/api/me')return json({user:{id:'viewer',name:'Test viewer',role:'admin',individual:true},csrf:'test',models:['test-model'],default_model:'test-model',product_mode:'standalone',preferences:{connection_ids:[],delivery_mode:'when_idle'}});
    if(p==='/api/auth/config')return json({mode:'standalone',password_enabled:true});
    if(p==='/api/history/sessions')return json({sessions:[],ancestors:[],has_more:false,next_cursor:null});
    if(p==='/api/sessions'&&request.method()==='POST')return json(state.session);
    if(p==='/api/sessions/chat/messages'&&request.method()==='POST'){
      const body=request.postDataJSON();state.posts.push(body);
      if(state.reject)return json({detail:'Goal admission unavailable.'},503);
      if(body.prompt==='/goal clear'){state.goal=null;state.session.active_turn=null;state.session.status='stopped';return json({goal:null});}
      if(body.prompt==='/goal pause'||body.prompt==='/goal resume'){
        state.goal.status=body.prompt.endsWith('pause')?'paused':'active';
        state.session.active_turn=null;state.session.status='idle';return json({goal:state.goal});
      }
      if(body.prompt.startsWith('/goal edit ')){
        state.goal={...state.goal,objective:body.prompt.slice(11).replace(/^--budget (none|[0-9]+) /,''),generation:state.goal.generation+1};
        return json({goal:state.goal});
      }
      const isGoal=body.prompt.startsWith('/goal '),objective=isGoal?body.prompt.slice(6).replace(/^--budget (none|[0-9]+) /,''):body.prompt;
      if(isGoal){
        state.turns.forEach(turn=>{turn.status='stopped';});
        state.goal={objective,status:'active',owner_id:'viewer',generation:(state.goal?.generation||0)+1,tokens_used:0,token_budget:null,turns:0,blocked_count:0,reason:'',created_at:new Date().toISOString(),elapsed_seconds:0};
      }
      const turn={id:`turn-${state.posts.length}`,actor_id:'viewer',session_id:'chat',status:'running',model:'test-model',mode:'when_idle',connections:[],created_at:new Date().toISOString(),usage:{},is_self:true};
      state.turns.unshift(turn);state.session.active_turn=turn.id;state.session.status='running';
      state.messages.push({id:state.messages.length+1,turn_id:turn.id,role:'user',author_id:'viewer',content:objective,attachments:[],sent_as_goal:isGoal});
      return json({...turn,...(isGoal?{goal:state.goal}:{})},202);
    }
    if(p==='/api/sessions/chat/stop'){
      state.stops++;state.goal.status='paused';state.goal.reason='Paused by Stop.';
      state.turns.forEach(turn=>turn.status='stopped');state.session.status='stopped';state.session.active_turn=null;
      return json({stopped:true},202);
    }
    if(p==='/api/sessions/chat')return state.deny?json({detail:'Access revoked.'},403):json({session:state.session,turns:state.turns,messages:state.messages,goal:state.goal,transcript_version:String(state.messages.length),pending:null});
    if(p==='/api/sessions/chat/messages')return json(state.messages);
    if(p.endsWith('/events'))return route.fulfill({contentType:'text/event-stream',body:'event: connected\ndata: {}\n\n'});
    if(p.endsWith('/work')||p.includes('/progress/'))return json({messages:[],tools:[],subagents:[]});
    if(p.endsWith('/workspace'))return json({links:[],files:[],sources:[],subagents:[]});
    if(p==='/api/skills')return json({skills:[],has_more:false,next_offset:null});
    return json([]);
  });
  await page.goto(base+'/#');
  const input=page.getByRole('textbox',{name:'Message Moyai',exact:true});await input.waitFor();
  return {page,input,state};
}
async function selectGoal(page,input){
  await input.fill('/go');await page.getByRole('option',{name:/Goal/}).waitFor();await input.press('Enter');
  await page.getByRole('button',{name:'Remove goal mode'}).waitFor();
  assert.equal((await input.innerText()).trim(),'');
}
async function screenshot(page,name){
  if(!process.env.MOYAI_GOAL_SCREENSHOTS)return;
  await fs.mkdir(process.env.MOYAI_GOAL_SCREENSHOTS,{recursive:true});
  await page.screenshot({path:path.join(process.env.MOYAI_GOAL_SCREENSHOTS,name+'.png')});
}
for(const width of [1440,768,320])for(const light of [false,true])test(`goal composer, Stop, editor, replacement and clear at ${width}px ${light?'light':'dark'}`,async t=>{
  const {page,input,state}=await scenario(t,width,light),label=`${width}-${light?'light':'dark'}`;
  await selectGoal(page,input);
  await input.fill('Fix the failing tests and verify they all pass.');
  await screenshot(page,'chip-'+label);
  await page.getByRole('button',{name:'Remove goal mode'}).hover();
  assert.equal(await page.locator('.goal-chip-remove').evaluate(node=>getComputedStyle(node).opacity),'1');
  await input.press('Enter');
  const bar=page.getByRole('region',{name:'Conversation goal'});
  await bar.waitFor();await page.getByText('Sent as goal',{exact:true}).waitFor();
  assert.equal(state.goal.token_budget,null);
  assert.equal(await page.locator('.message.user').innerText(),'You\nFix the failing tests and verify they all pass.\nSent as goal');
  const goalBox=await bar.boundingBox(),inputBox=await input.boundingBox();
  assert.ok(goalBox.y+goalBox.height<=inputBox.y+1);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await screenshot(page,'active-'+label);
  await page.getByRole('button',{name:'Stop task',exact:true}).click();
  await bar.getByText('Paused goal',{exact:true}).waitFor();assert.equal(state.stops,1);
  await page.getByRole('button',{name:'Resume goal',exact:true}).click();await bar.getByText('Pursuing goal',{exact:true}).waitFor();
  // Stop remains available between automatic turns, with no active_turn yet.
  await page.getByRole('button',{name:'Stop task',exact:true}).click();await bar.getByText('Paused goal',{exact:true}).waitFor();
  await input.fill('Keep this follow-up draft.');
  await page.getByRole('button',{name:'Expand goal',exact:true}).click();
  const editor=page.getByRole('form',{name:'Edit goal'});
  await editor.getByLabel('Objective',{exact:true}).fill('Verify every test and document the fix.');
  await screenshot(page,'editor-'+label);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await editor.getByRole('button',{name:'Save goal'}).click();
  await page.waitForFunction(()=>document.querySelector('.goal-editor button[type=submit]').disabled);
  assert.equal(state.goal.status,'paused');
  await page.getByRole('button',{name:'Close Edit goal tab'}).click();
  assert.equal(await input.innerText(),'Keep this follow-up draft.');
  await selectGoal(page,input);await input.fill('Replace the current goal.');
  await input.press('Enter');await bar.getByText('Replace the current goal.',{exact:true}).waitFor();
  assert.equal(state.goal.status,'active');assert.equal(state.goal.token_budget,null);
  assert.equal(await page.getByText('Sent as goal',{exact:true}).count(),2);
  await input.fill('A normal follow-up.');await input.press('Enter');
  await page.locator('.message.user').filter({hasText:'A normal follow-up.'}).waitFor();
  assert.equal(state.goal.objective,'Replace the current goal.');
  assert.equal(await page.getByText('Sent as goal',{exact:true}).count(),2);
  await page.getByRole('button',{name:'Clear goal',exact:true}).click();await bar.waitFor({state:'hidden'});
  assert.equal(await page.getByText('Sent as goal',{exact:true}).count(),2);
  await page.reload();await input.waitFor();assert.equal(await page.getByText('Sent as goal',{exact:true}).count(),2);
});
test('failed replacement preserves the original goal and the new draft, with an idempotent retry',async t=>{
  const {page,input,state}=await scenario(t);
  await selectGoal(page,input);await input.fill('Original objective.');await input.press('Enter');
  await page.getByRole('region',{name:'Conversation goal'}).waitFor();
  await selectGoal(page,input);await input.fill('Replacement objective.');state.reject=true;
  await input.press('Enter');await page.getByRole('alert').filter({hasText:'Goal admission unavailable'}).waitFor();
  assert.equal(state.goal.objective,'Original objective.');assert.equal(await input.innerText(),'Replacement objective.');
  await page.getByRole('button',{name:'Remove goal mode'}).waitFor();state.reject=false;
  await input.press('Enter');await page.getByRole('region',{name:'Conversation goal'}).getByText('Replacement objective.',{exact:true}).waitFor();
  assert.deepEqual(state.posts[1],state.posts[2]);
});
