// Real component interactions against isolated synthetic API responses.
const assert=require('node:assert/strict');
const {test,before,after}=require('node:test');
const {spawn}=require('node:child_process');
const {chromium}=require('playwright');
let server,browser,base;
before(async()=>{
 server=spawn(process.execPath,['scripts/settings_ui_preview.cjs','--port','0'],{stdio:['ignore','pipe','inherit']});
 base=await new Promise((resolve,reject)=>{server.once('error',reject);server.stdout.on('data',s=>{const m=s.toString().match(/http:\/\/127\.0\.0\.1:\d+/);if(m)resolve(m[0]);});});
 browser=await chromium.launch({headless:true});
});
after(async()=>{await browser?.close();server?.kill();});
async function pageFor(t,width=1440){
 const page=await browser.newPage({viewport:{width,height:900}});
 t.after(()=>page.close());await page.goto(base);await page.locator('#prompt').waitFor();return page;
}
test('tooltip focus and hover preserve controller click handlers',async t=>{
 const p=await pageFor(t);const button=p.getByRole('button',{name:'Settings',exact:true});
 // A tooltip opening rerenders its trigger; the imperative handler must survive.
 await button.focus();await p.waitForTimeout(350);await p.keyboard.press('Enter');
 await p.waitForURL('**/#settings',{timeout:2000});
 await p.evaluate(()=>location.hash='tasks');await p.locator('#prompt').waitFor();
 await button.hover();await p.waitForTimeout(800);await button.click();
 await p.waitForURL('**/#settings',{timeout:2000});
});
