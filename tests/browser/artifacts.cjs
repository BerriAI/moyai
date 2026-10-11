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
  if(process.env.ARTIFACT_SCREENSHOT_DIR)mkdirSync(process.env.ARTIFACT_SCREENSHOT_DIR,{recursive:true});
});
after(async()=>{await browser?.close();server?.kill();});
for(const width of [1440,768,320])test(`published files without recovery ZIP and complete preview at ${width}px`,{timeout:60000},async t=>{
  const page=await browser.newPage({viewport:{width,height:900},reducedMotion:'reduce'});t.after(()=>page.close());
  await page.goto(base+'/?fixture=skill-picker#run='+'a'.repeat(32));
  await page.waitForFunction(()=>document.querySelector('#files-button')&&window.MoyaiFiles);
  await page.evaluate(()=>{
    const file={path:'reports/complete.md',workspace_path:'reports/complete.md',name:'complete.md',archive_path:'published:reports/complete.md:r1',url:'/artifact-download',preview_url:'/preview',size:200000};
    window.testFiles=MoyaiFiles.create({
      api:async url=>url.includes('/files')?{files:[file],limited:false}:url.includes('offset=')?{text:'\nFinal exact requirement. 🗿',format:'markdown',truncated:false,next_offset:null}:{text:'# Complete deliverable\n\n'+('Source-backed evidence. '.repeat(1000)),format:'markdown',truncated:true,next_offset:65539},
      markdown:text=>'<div>'+text.replaceAll('&','&amp;').replaceAll('<','&lt;')+'</div>',
      escape:text=>String(text).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),size:bytes=>bytes+' bytes'
    });
    testFiles.sync({id:'a'.repeat(32),has_published_artifacts:true,has_artifact:false,has_captures:false,events:[{kind:'artifact',id:1}]});
  });
  await page.locator('#files-button').click();
  await page.getByRole('button',{name:'Load more',exact:true}).click();
  await page.getByText('Final exact requirement. 🗿',{exact:false}).waitFor();
  assert.equal(await page.locator('.saved-file-more').count(),0);
  assert.equal(await page.getByRole('link',{name:'Download ZIP',exact:true}).count(),0);
  assert.equal(await page.locator('#saved-file-download').getAttribute('href'),'/artifact-download');
  await page.locator('#saved-file-search').fill('missing');
  await page.getByText('No matching files.',{exact:true}).waitFor();
  await page.locator('#saved-file-search').fill('');
  await page.locator('.saved-file-choice').waitFor();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);
  if(process.env.ARTIFACT_SCREENSHOT_DIR)await page.screenshot({path:`${process.env.ARTIFACT_SCREENSHOT_DIR}/artifacts-${width}.png`});
  await page.keyboard.press('Escape');
  await page.locator('[data-dialog-id="saved-files-dialog"]').waitFor({state:'hidden'});
});
