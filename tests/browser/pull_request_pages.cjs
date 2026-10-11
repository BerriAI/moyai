const assert=require('node:assert/strict');
const {test,before,after}=require('node:test');
const {spawn}=require('node:child_process');
const {mkdirSync}=require('node:fs');
const {chromium}=require('playwright');
let server,browser,base;
before(async()=>{
  server=spawn(process.execPath,['scripts/settings_ui_preview.cjs','--port','0'],{stdio:['ignore','pipe','inherit']});
  base=await new Promise((resolve,reject)=>{server.once('error',reject);server.stdout.on('data',data=>{const url=data.toString().match(/http:\/\/127\.0\.0\.1:\d+/);if(url)resolve(url[0]);});});
  browser=await chromium.launch({headless:true});
  if(process.env.PR_SCREENSHOT_DIR)mkdirSync(process.env.PR_SCREENSHOT_DIR,{recursive:true});
});
after(async()=>{await browser?.close();server?.kill();});
for(const width of [1440,768,320])test(`complete PR pages and source changes at ${width}px`,async t=>{
  const page=await browser.newPage({viewport:{width,height:950}});t.after(()=>page.close());
  await page.goto(base+'/');
  await page.evaluate(async()=>{
    const host=document.createElement('div');host.id='pr-fixture';host.style.cssText='position:fixed;inset:0;background:var(--bg,#080d15);z-index:100;width:100%;height:100%;';document.body.append(host);
    const files=Array.from({length:101},(_,i)=>({filename:`source/file-${i}.py`,status:'modified',additions:2,deletions:1,patch:'@@ -1 +1 @@\n-old\n+new\n+'+'Complete evidence '.repeat(i===100?1500:1)}));
    window.prRequests=[];window.prChanged=false;
    window.prView=MoyaiPullRequest.mount({element:host,url:'https://github.com/BerriAI/moyai/pull/123',markdown:esc,escape:esc,
      load:async args=>{
        prRequests.push(args);
        if(prChanged)throw Object.assign(new Error('This pull request changed.'),{status:409});
        const more=args.page===2;
        return {number:123,repository:'BerriAI/moyai',title:'Complete review evidence',state:'open',body:'Complete description '.repeat(1200)+'FINAL REQUIREMENT',head:'a'.repeat(40),base_sha:'b'.repeat(40),base:'main',changed_files:101,
          files:files.slice(more?100:0,more?101:100),files_truncated:!more,
          next_arguments:more?null:{page:2,head_sha:'a'.repeat(40),base_sha:'b'.repeat(40)}};
      }});
    await prView.activate();
  });
  const host=page.locator('#pr-fixture');
  await host.getByRole('button',{name:'Load more files',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('#pr-fixture [data-pr-file]').length===101);
  const last=host.locator('[data-pr-file="source/file-100.py"]');await last.locator('summary').click();
  await page.waitForFunction(()=>document.querySelector('[data-pr-file="source/file-100.py"] .pr-code')?.textContent.includes('@@'));
  assert.equal((await last.locator('.pr-code').last().textContent()).length,1+'Complete evidence '.repeat(1500).length);
  await page.evaluate(async()=>{prView.deactivate();await prView.activate();});
  assert.equal(await host.locator('[data-pr-file]').count(),101);
  assert.equal((await page.evaluate(()=>prRequests[1])).head_sha,'a'.repeat(40));
  await host.getByRole('button',{name:'Description',exact:true}).click();
  assert.equal(await host.getByRole('button',{name:'Description',exact:true}).getAttribute('aria-pressed'),'true');
  assert.ok((await host.locator('.pr-description').textContent()).endsWith('FINAL REQUIREMENT'));
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);
  if(process.env.PR_SCREENSHOT_DIR)await page.screenshot({path:`${process.env.PR_SCREENSHOT_DIR}/complete-pr-${width}.png`});
  await page.evaluate(()=>{prChanged=true;});
  await host.getByRole('button',{name:'Refresh pull request'}).click();
  await host.getByText('Unavailable',{exact:true}).waitFor();
  assert.equal(await host.locator('[data-pr-file]').count(),0);
  assert.equal(await host.locator('.pr-description').count(),0);
});
