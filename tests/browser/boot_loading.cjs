const assert=require('node:assert/strict');
const {test}=require('node:test');
const {spawn}=require('node:child_process');
const {chromium}=require('playwright');
for(const width of [1440,768,320]) test('initial page announces loading while the session lookup is pending',async()=>{
 const server=spawn(process.execPath,['scripts/settings_ui_preview.cjs','--port','0']);let browser,release;
 try{
 const base=await new Promise(r=>server.stdout.on('data',d=>{const m=d.toString().match(/http:\/\/127\.0\.0\.1:\d+/);if(m)r(m[0]);}));
 browser=await chromium.launch();const page=await browser.newPage({viewport:{width,height:900},reducedMotion:'reduce'});
 const gate=new Promise(r=>release=r);await page.route('**/api/session',async route=>{await gate;await route.continue();});
 await page.goto(base,{waitUntil:'domcontentloaded'});
 assert.match(await page.locator('#content').innerText(),/Loading/);
 assert.equal(await page.locator('#content .settings-loading[role=status]').count(),1);
 release();await page.locator('#prompt').waitFor();assert.equal(await page.locator('#content .settings-loading[role=status]').count(),0);
 }finally{release?.();await browser?.close();server.kill();}
});
