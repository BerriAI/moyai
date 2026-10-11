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
for(const width of [1440,768,320]) test(`saved Slack context after unavailable synchronization at ${width}px`,async t=>{
  const page=await browser.newPage({viewport:{width,height:900}});t.after(()=>page.close());
  await page.goto(base+'/?fixture=skill-picker#run='+'a'.repeat(32));
  await page.waitForFunction(()=>typeof renderSlackContext==='function'&&document.querySelector('#slack-context'));
  await page.locator('#toggle-details').click();
  await page.evaluate(()=>renderSlackContext({context_status:'unavailable',kind:'thread',warning:'Synthetic fixture: live source unavailable.',messages:[
    {user:'Moe',author_kind:'human',ts:'1790719000.123456',text:'Please review the proposals.'},
    {user:'QM',author_kind:'bot',ts:'1790719001.123456',text:'Preserve complete context. '+'Evidence '.repeat(1500)+'Final requirement.'},
    {user:'Moyai',author_kind:'self',ts:'1790719002.123456',text:'The sources are saved.'}]}));
  const panel=page.locator('#slack-context');
  await panel.getByText('View observed messages',{exact:true}).click();
  assert.match(await panel.innerText(),/Live synchronization unavailable · 3 saved messages/);
  for(const label of ['Human','Peer bot','Moyai','Final requirement.'])assert.ok((await panel.innerText()).includes(label));
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);
  if(process.env.SLACK_SCREENSHOT_DIR)await page.screenshot({path:`${process.env.SLACK_SCREENSHOT_DIR}/slack-${width}.png`});
});
