const assert=require('node:assert/strict');
const {test,before,after}=require('node:test');
const {spawn}=require('node:child_process');
const {chromium}=require('playwright');
let server,browser,base;
const id='a'.repeat(32),count=1000;
before(async()=>{
  server=spawn(process.execPath,['scripts/settings_ui_preview.cjs','--port','0','--root',process.env.CONVERSATION_STATIC_ROOT||'app/static'],{stdio:['ignore','pipe','inherit']});
  base=await new Promise((resolve,reject)=>{
    server.once('error',reject);server.once('exit',code=>reject(Error('Preview exited: '+code)));
    server.stdout.on('data',chunk=>{const match=chunk.toString().match(/http:\/\/127\.0\.0\.1:\d+/);if(match)resolve(match[0]);});
  });
  browser=await chromium.launch({headless:true});
});
after(async()=>{await browser?.close();server?.kill();});
async function open(t,width,size=count){
  const page=await browser.newPage({viewport:{width,height:1000},reducedMotion:'reduce'}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  t.after(async()=>{await page.close();assert.deepEqual(errors,[]);});
  await page.route(url=>url.pathname===`/api/runs/${id}`,async route=>{
    const response=await route.fetch(),run=await response.json();
    run.status='idle';run.active=false;run.active_message_id=null;
    run.messages=Array.from({length:size},(_,i)=>({id:i+1,role:i%2?'assistant':'user',user_id:'user-0',user_name:'Alex',status:'completed',started_at:'2026-10-01T12:00:00Z',
      content:i%2?`Reply ${i+1}\n\n${'A saved response with **formatted text** and useful context. '.repeat(20)}\n\n\`\`\`js\nconst result = ${i+1};\n\`\`\``:`Request ${i+1}`,attachments:[]}));
    run.events=Array.from({length:size},(_,i)=>({id:i+1,kind:'chat',message:i%2?'Response saved':'Response started',data:{message_id:i-i%2+1},created_at:i%2?'2026-10-01T12:00:10Z':'2026-10-01T12:00:00Z'}));
    run.deferred_activity=run.messages.filter(m=>m.role==='user').map(m=>String(m.id));run.loaded_activity=[];run.activity_cursor=size;
    await route.fulfill({response,json:run});
  });
  await page.goto(`${base}/?fixture=feedback#tasks`);await page.locator('#prompt').waitFor();
  const metrics=await page.evaluate(async id=>{
    let markdownCalls=0;const original=renderMarkdown;
    renderMarkdown=(...args)=>{markdownCalls++;return original(...args);};
    const start=performance.now();await openRun(id);await new Promise(requestAnimationFrame);
    return {openMs:Math.round(performance.now()-start),markdownCalls,mounted:document.querySelectorAll('.chat-message').length};
  },id);
  t.diagnostic(JSON.stringify({width,...metrics}));
  if(size===count&&process.env.CONVERSATION_SCREENSHOTS)await page.screenshot({path:`${process.env.CONVERSATION_SCREENSHOTS}-${width}.png`});
  return {page,metrics};
}
for(const width of [1440,768,320])test(`long conversation opens with bounded rendering at ${width}px`,async t=>{
  const {page,metrics}=await open(t,width);
  assert.equal(metrics.mounted,40,'only the latest page should mount when opening a conversation');
  assert.equal(metrics.markdownCalls,20,'older Markdown should not be parsed until requested');
  assert.equal(await page.locator('.chat-message').first().getAttribute('data-region-key'),'message:961');
  assert.equal(await page.locator('.chat-message').last().getAttribute('data-region-key'),'message:1000');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  const composer=await page.locator('#followup').elementHandle();await page.locator('#followup').fill('Keep this unsent draft');
  const room=page.locator('#conversation');await room.evaluate(el=>{el.scrollTop=0;});
  if(process.env.CONVERSATION_SCREENSHOTS)await page.screenshot({path:`${process.env.CONVERSATION_SCREENSHOTS}-history-${width}.png`});
  const anchor=page.locator('[data-region-key="message:961"]'),node=await anchor.elementHandle(),top=await anchor.evaluate(el=>el.getBoundingClientRect().top);
  await page.getByRole('button',{name:'Load earlier messages',exact:true}).click();
  assert.equal(await page.locator('.chat-message').count(),80);
  assert.equal(await page.locator('.chat-message').first().getAttribute('data-region-key'),'message:921');
  assert.equal(await node.evaluate(el=>el.isConnected),true,'existing replies retain their component state');
  assert.ok(Math.abs(top-await anchor.evaluate(el=>el.getBoundingClientRect().top))<2,'prepending history preserves the reading position');
  await page.evaluate(async()=>{
    const run=structuredClone(state.chatRun);run.messages.push({id:1001,role:'user',user_id:'user-0',content:'New follow-up',status:'completed',attachments:[]},{id:1002,role:'assistant',content:'New reply',status:'completed',attachments:[]});
    updateChat(run);await new Promise(requestAnimationFrame);
  });
  assert.equal(await page.locator('.chat-message').count(),82,'refreshes retain the expanded history');
  assert.equal(await composer.evaluate(el=>el.isConnected),true);assert.equal(await page.locator('#followup').evaluate(el=>el.value),'Keep this unsent draft');
  await page.locator('#jump-latest').click();
  assert.equal(await room.evaluate(el=>el.scrollHeight-el.scrollTop-el.clientHeight<2),true);
});

test('refreshes bind history loading once and the oldest page keeps keyboard focus',async t=>{
  const {page}=await open(t,1440,124);
  await page.evaluate(()=>{for(let i=0;i<3;i++){const run=structuredClone(state.chatRun);run.messages.at(-1).content+=' refreshed';updateChat(run);}});
  const earlier=page.getByRole('button',{name:'Load earlier messages',exact:true});
  await earlier.focus();await page.keyboard.press('Enter');assert.equal(await page.locator('.chat-message').count(),80);
  await page.keyboard.press('Enter');assert.equal(await page.locator('.chat-message').count(),120);
  await page.keyboard.press('Enter');assert.equal(await page.locator('.chat-message').count(),124);
  assert.equal(await earlier.count(),0);
  assert.equal(await page.evaluate(()=>document.activeElement.dataset.regionKey),'message:1');
  await page.evaluate(()=>navigate('tasks'));await page.locator('#prompt').waitFor();
  await page.evaluate(id=>openRun(id),id);assert.equal(await page.locator('.chat-message').count(),40,'reopening resets the rendered page');
});
