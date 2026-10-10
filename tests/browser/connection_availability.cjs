const assert=require('node:assert/strict');
const {test}=require('node:test');
const {spawn}=require('node:child_process');
const {chromium}=require('playwright');
for(const width of [1440,768,320]) test('member connection details distinguish installation, policy and allowed actions',async()=>{
 const server=spawn(process.execPath,['scripts/settings_ui_preview.cjs','--port','0']);let browser;
 try{
 const base=await new Promise(r=>server.stdout.on('data',d=>{const m=d.toString().match(/http:\/\/127\.0\.0\.1:\d+/);if(m)r(m[0]);}));
 browser=await chromium.launch();const page=await browser.newPage({viewport:{width,height:900},reducedMotion:'reduce'});await page.goto(base+'/?fixture=member');await page.locator('#prompt').waitFor();
 for(const [connected,enabled,expected] of [[false,true,'Not connected'],[false,false,'Not connected'],[true,false,'Paused for all sessions'],[true,true,'Available to sessions']]){
 await page.evaluate(({connected,enabled})=>{state.role='member';state.connections=[{id:'linear',connected,enabled,read_only:false,label:'Example',identity:'OAuth',tools:[]}];manageConnection('linear');},{connected,enabled});
 const text=await page.locator('#connection-body').innerText();assert.ok(text.includes(expected),text);
 if(!connected||!enabled)assert.ok(!text.includes('Enabled tools run automatically'),text);
 await page.keyboard.press('Escape');
 }
 }finally{await browser?.close();server.kill();}
});
