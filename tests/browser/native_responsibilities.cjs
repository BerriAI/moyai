// Real React app with synthetic inbox data; actual-model evidence is recorded separately.
const assert = require('node:assert/strict');
const {before,after,test} = require('node:test');
const {chromium} = require('playwright');
const {settingsFixture} = require('../fixtures/settings.cjs');
let fixture,browser;
before(async()=>{fixture=await settingsFixture();browser=await chromium.launch({headless:true});});
after(async()=>{await browser?.close();await fixture?.close();});
const responsibility={id:'responsibility',source_session_id:'source',objective:'Report launch blockers',model:'test-model',status:'waiting',generation:1,watermark:0,interval_seconds:30,max_evaluations:10,evaluations:1,active_turn_id:null,reason:'',next_check_at:'2026-11-24T06:00:00Z',created_at:'2026-11-24T05:00:00Z'};
const finding={turn_id:'worker',responsibility_id:'responsibility',source_session_id:'source',worker_session_id:'review',objective:'Report launch blockers',summary:'The review reports a release blocker.',evidence:[{message_id:5,quote:'QA found a release blocker.'}],created_at:'2026-11-24T05:01:00Z',read_at:null};
async function open(t,width){
 fixture.reset();const page=await browser.newPage({viewport:{width,height:1000},reducedMotion:'reduce'});t.after(()=>page.close());
 const errors=[];page.on('pageerror',e=>errors.push(e.message));t.after(()=>assert.deepEqual(errors,[]));
 let row={...responsibility},item={...finding};const writes=[];
 await page.route(/\/api\/responsibilit/,async route=>{
  const request=route.request(),url=new URL(request.url());
  if(request.method()==='POST'){
   const body=request.postDataJSON();writes.push({path:url.pathname,body,csrf:request.headers()['x-moyai-csrf']});
   if(url.pathname.endsWith('/control')){row={...row,status:body.action==='pause'?'paused':'waiting',generation:row.generation+1};return route.fulfill({json:row});}
   if(url.pathname.endsWith('/read')){item={...item,read_at:'2026-11-24T05:02:00Z'};return route.fulfill({json:{read:true}});}
   return route.fulfill({status:503,json:{detail:'Please retry the saved draft.'}});
  }
  if(url.pathname.endsWith('/reviews'))return route.fulfill({json:{items:[{turn_id:'failed-review',generation:1,through_sequence:1,status:'failed',reconciled:true,accepted:false,created_at:'2026-11-24T05:01:00Z',finished_at:'2026-11-24T05:02:00Z',error:'The gateway could not finish this review.',decision:null}]}});
  return route.fulfill({json:{items:url.pathname.includes('inbox')?[item]:[row]}});
 });
 await page.route('**/api/sessions',route=>route.fulfill({json:[{id:'source',owner_id:'viewer',title:'Launch readiness',visibility:'private',surface:'web'}]}));
 await page.goto(fixture.base+'/#settings/responsibilities');await page.getByText(finding.summary,{exact:true}).waitFor();return {page,writes};
}
for(const width of [1440,768,320])test(`Dot inbox, evidence and controls fit ${width}px`,async t=>{
 const {page,writes}=await open(t,width);
 await page.getByText('Source evidence',{exact:true}).click();await page.getByText('QA found a release blocker.',{exact:true}).waitFor();
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 await page.getByRole('button',{name:'Pause',exact:true}).click();await page.getByRole('button',{name:'Resume',exact:true}).waitFor();
 assert.deepEqual(writes[0].body,{generation:1,action:'pause'});assert.equal(writes[0].csrf,'fixture-csrf');
 await page.getByRole('button',{name:'Mark reviewed',exact:true}).click();await page.getByText('Reviewed',{exact:true}).waitFor();
 await page.getByRole('button',{name:'Review activity',exact:true}).last().click();
 await page.getByRole('dialog').getByText('The gateway could not finish this review.',{exact:true}).waitFor();
 await page.keyboard.press('Escape');await page.getByRole('dialog').waitFor({state:'hidden'});
 await page.getByRole('button',{name:'Add responsibility'}).click();const dialog=page.getByRole('dialog');
 const box=await dialog.boundingBox();assert.ok(box.x>=0&&box.x+box.width<=width);
 await page.getByLabel('What should Moyai pay attention to?').fill('Retain this draft');
 await page.keyboard.press('Escape');await dialog.waitFor({state:'hidden'});
 await page.getByRole('button',{name:'Add responsibility'}).click();assert.equal(await page.getByLabel('What should Moyai pay attention to?').inputValue(),'Retain this draft');
});
test('Failed creation preserves the draft and retries the same request identity',async t=>{
 const {page,writes}=await open(t,1440);await page.getByRole('button',{name:'Add responsibility'}).click();
 await page.getByLabel('Private source chat').click();await page.getByRole('option',{name:'Launch readiness'}).click();
 await page.getByLabel('What should Moyai pay attention to?').fill('Watch launch readiness');
 await page.getByRole('button',{name:'Start following'}).click();await page.getByRole('dialog').getByRole('alert').waitFor();
 assert.equal(await page.getByLabel('What should Moyai pay attention to?').inputValue(),'Watch launch readiness');
 await page.getByRole('button',{name:'Start following'}).click();await page.getByRole('dialog').getByRole('alert').waitFor();
 assert.equal(writes.length,2);assert.equal(writes[0].body.request_key,writes[1].body.request_key);
});
test('Review activity suppresses pending and rejected evidence',async t=>{
 const {page}=await open(t,1440);
 await page.route('**/api/responsibilities/responsibility/reviews',route=>route.fulfill({json:{items:['pending','invalid'].map(validation=>({
  turn_id:validation,generation:1,through_sequence:1,status:'completed',reconciled:validation==='invalid',accepted:true,created_at:'2026-11-24T05:01:00Z',finished_at:'2026-11-24T05:02:00Z',error:null,usage:{},validation,
  decision:{action:'notify',summary:'Unvalidated summary',evidence:[{message_id:999,quote:'Forged quotation'}]},
 }))}}));
 await page.getByRole('button',{name:'Review activity',exact:true}).last().click();
 const dialog=page.getByRole('dialog');
 await dialog.getByText('Validation failed',{exact:true}).waitFor();
 await dialog.getByText('Awaiting validation',{exact:true}).waitFor();
 assert.equal(await dialog.getByText('Forged quotation',{exact:true}).count(),0);
 assert.equal(await dialog.getByText('Unvalidated summary',{exact:true}).count(),0);
});
