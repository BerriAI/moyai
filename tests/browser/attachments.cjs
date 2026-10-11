const assert=require('node:assert/strict');
const {test,before,after}=require('node:test');
const {spawn}=require('node:child_process');
const {mkdirSync}=require('node:fs');
const {chromium}=require('playwright');
let server,browser,base=process.env.SESSION_UI_URL;
before(async()=>{
 if(!base){
  server=spawn('uv',['run','--frozen','python','scripts/session_ui_demo.py','--port','0'],{stdio:['ignore','pipe','pipe']});
  base=await new Promise((resolve,reject)=>{
   server.once('error',reject);server.once('exit',code=>reject(Error('UI server exited: '+code)));
   server.stderr.on('data',data=>{const match=data.toString().match(/Uvicorn running on (http:\/\/127\.0\.0\.1:\d+)/);if(match)resolve(match[1]);});
  });
 }
 browser=await chromium.launch({headless:true});
});
after(async()=>{await browser?.close();server?.kill();});
for(const width of [1440,768,320])test(`complete attachments with retry and original download at ${width}px`,async t=>{
 const page=await browser.newPage({viewport:{width,height:1000}});t.after(()=>page.close());
 const errors=[];page.on('pageerror',err=>errors.push(err.message));
 const baseline=process.env.ATTACHMENT_BASELINE_ROOT;
 if(baseline)for(const file of ['attachments.js','attachment-transport.js','audio-recorder.js','app.js'])
  await page.route(url=>url.pathname.endsWith('/'+file),route=>route.fulfill({path:baseline+'/'+file,contentType:'application/javascript'}));
 await page.goto(base+'/demo/login');await page.locator('#prompt').waitFor();
 await page.locator('#prompt').fill('Inspect all complete input files. Local API and demo execution only.');
 const files=Array.from({length:12},(_,i)=>({name:`reference-${i}.txt`,mimeType:'text/plain',
   buffer:i<2?Buffer.alloc(11*1024*1024,i+65):Buffer.from(`Complete source ${i}: 日志🔎`)}));
 let lost=false;
 if(!baseline)await page.route('**/api/attachments/*/parts/1?*',async route=>{
   if(lost)return route.continue();lost=true;await route.fetch();await route.abort('failed');
 });
 await page.locator('input[type=file][aria-label="Choose attachments"]').setInputFiles(files);
 if(!baseline){
  await page.locator('.attachment-retry').first().waitFor();
  await page.locator('.attachment-retry').first().click();
 }
 const expected=baseline?8:12;
 await page.waitForFunction(expected=>document.querySelectorAll('.draft-attachment').length===expected&&
   !document.querySelector('.upload-error')&&!Array.from(document.querySelectorAll('.draft-attachment small')).some(el=>el.textContent.includes('Uploading')),expected);
 assert.equal(await page.locator('.draft-attachment').count(),expected);
 await page.locator('.attachment-open').last().click();
 await page.getByRole('dialog').waitFor();
 await page.getByRole('button',{name:'Close preview',exact:true}).click();
 if(!baseline)await page.waitForFunction(()=>document.querySelector('#toast').hidden);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 const output=process.env.ATTACHMENT_SCREENSHOTS||'/tmp/moyai-attachment-ui';mkdirSync(output,{recursive:true});
 await page.screenshot({path:`${output}/attachments-${width}.png`,fullPage:true});
 if(baseline)return;
 const submitted=page.waitForResponse(r=>r.url()===base+'/api/runs'&&r.request().method()==='POST');
 await page.getByRole('button',{name:'Start session',exact:true}).click();
 const response=await submitted;assert.equal(response.status(),201);const run=await response.json();
 await page.locator('#followup').waitFor();
 const saved=await page.evaluate(id=>api('/api/runs/'+id),run.id);
 const attachments=saved.messages.find(m=>m.role==='user').attachments;assert.equal(attachments.length,12);
 const downloaded=await page.request.get(base+attachments.find(f=>f.name==='reference-0.txt').url);
 assert.deepEqual(await downloaded.body(),files[0].buffer);
 await page.reload();await page.locator('#followup').waitFor();
 assert.equal(await page.locator('.chat-message.user .sent-attachment').count(),12);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 assert.deepEqual(errors,[]);
});
