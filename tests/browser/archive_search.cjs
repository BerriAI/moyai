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
test('search discovers archived sessions while recent inventory stays active-only',async t=>{
 const p=await pageFor(t);const reads=[];
 await p.route('**/api/runs?*',async route=>{
   const u=new URL(route.request().url());reads.push(u);
   const results=u.searchParams.get('include_archived')==='true' ? [{id:'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',prompt:'Maple archived checklist',display_title:'Maple archived checklist',archived:true,status:'idle',children:[],search_query:'maple',search_match:true,search_snippet:'Saved rollback owner',created_at:new Date().toISOString(),updated_at:new Date().toISOString()}] : [];
   await route.fulfill({json:results});
 });
 await p.locator('#search-sessions').click();
 await p.locator('#command-search').fill('Maple');
 await p.getByRole('option').filter({hasText:'Maple archived checklist'}).waitFor();
 assert.match(await p.locator('#command-palette').innerText(),/Archived/);
 assert(reads.some(u=>u.searchParams.get('search')==='maple'&&u.searchParams.get('include_archived')==='true'));
 await p.locator('#command-search').fill('');
 await p.waitForTimeout(250);
 assert.equal(await p.getByRole('option').filter({hasText:'Maple archived checklist'}).count(),0);
 assert(!reads.at(-1).searchParams.has('include_archived'));
});
