const assert=require('node:assert/strict');
const {test,before,after}=require('node:test');
const {spawn}=require('node:child_process');
const {chromium}=require('playwright');
let server,browser,base;
before(async()=>{
  server=spawn(process.execPath,['scripts/settings_ui_preview.cjs','--port','0'],{stdio:['ignore','pipe','inherit']});
  base=await new Promise((resolve,reject)=>{server.once('error',reject);server.stdout.on('data',data=>{const url=data.toString().match(/http:\/\/127\.0\.0\.1:\d+/);if(url)resolve(url[0]);});});
  browser=await chromium.launch({headless:true});
});
after(async()=>{await browser?.close();server?.kill();});
for(const width of [1440,768,320]) test(`durable goal states at ${width}px`,async t=>{
  const page=await browser.newPage({viewport:{width,height:900}});t.after(()=>page.close());
  await page.goto(base+'/?fixture=skill-picker#run='+'a'.repeat(32));
  await page.waitForFunction(()=>globalThis.MoyaiGoal&&globalThis.MoyaiUI);
  for(const [status,label] of [['active','Running'],['wrapping','Reporting'],['budget_exhausted','Budget ended'],['needs_input','Needs your input'],['completed','Completed']]){
    await page.evaluate(({status})=>{
      const node=document.querySelector('#goal-status')||document.querySelector('.goal-status')||document.createElement('div');
      node.id='goal-contract-preview';if(!node.isConnected)document.querySelector('main').prepend(node);
      const goal={id:'synthetic',objective:'Verify the full product and report what remains.',status,elapsed_seconds:10800,budget:{kind:'timebox',seconds:10800},reason:status==='budget_exhausted'?'The work budget ended; some requirements remain.':''};
      MoyaiGoal.render(node,{status:status==='active'?'running':'idle',goal},s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])));
    },{status});
    const panel=page.locator('#goal-contract-preview');
    await panel.getByText(label,{exact:true}).waitFor();
    assert.match(await panel.innerText(),/Timebox:/);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);
    if(process.env.GOAL_SCREENSHOT_DIR&&status==='budget_exhausted')await page.screenshot({path:`${process.env.GOAL_SCREENSHOT_DIR}/goals-${width}.png`});
  }
});
