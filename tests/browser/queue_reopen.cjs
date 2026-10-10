const assert=require('node:assert/strict');
const {test}=require('node:test');
const {spawn}=require('node:child_process');
const {chromium}=require('playwright');
for(const width of [1440,768,320]) test('reopening an active queue editor preserves the visible draft and revision on Save',async()=>{
 const server=spawn(process.execPath,['scripts/settings_ui_preview.cjs','--port','0']);let browser;
 try{
 const base=await new Promise(r=>server.stdout.on('data',d=>{const m=d.toString().match(/http:\/\/127\.0\.0\.1:\d+/);if(m)r(m[0]);}));
 browser=await chromium.launch();const page=await browser.newPage({viewport:{width,height:900},reducedMotion:'reduce'});await page.goto(base);await page.locator('#prompt').waitFor();
 await page.evaluate(()=>{
  const host=document.createElement('section');host.id='queue-test';document.querySelector('#content').append(host);
  const run={messages:[{id:1,role:'user',status:'running'},{id:2,role:'user',user_id:'test',status:'queued',content:'Old scope',revision:4}]};
  window.saved=[];window.q=MoyaiQueue.create({element:host,runId:'test',user:'test',role:'member',api:async(url,o)=>{saved.push(JSON.parse(o.body));return {revision:5};},refresh:async()=>{},toast:()=>{}});q.render(run);
 });
 await page.getByRole('button',{name:'Edit queued message',exact:true}).click();
 await page.locator('#queue-edit-2').fill('New scope');
 await page.getByRole('button',{name:'Edit queued message',exact:true}).click();
 assert.equal(await page.locator('#queue-edit-2').inputValue(),'New scope');
 await page.getByRole('button',{name:'Save edit',exact:true}).click();
 assert.deepEqual(await page.evaluate(()=>saved),[{action:'edit',content:'New scope',revision:4}]);
 }finally{await browser?.close();server.kill();}
});
