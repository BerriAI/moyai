const assert=require('node:assert/strict');
const {test}=require('node:test');
const {readFileSync}=require('node:fs');
const vm=require('node:vm');
function setup(){
  const elements=new Map();
  const c={state:{role:'admin',pageVersion:1},esc:v=>String(v).replaceAll('<','&lt;'),spendCount:v=>String(v),URLSearchParams,document:{querySelector:()=>null},$:k=>{if(!elements.has(k))elements.set(k,{});return elements.get(k);},showError:()=>{}};
  vm.createContext(c);for(const name of ['analytics','adoption'])vm.runInContext(readFileSync('app/static/'+name+'.js','utf8'),c);
  return {c,elements};
}
const data={start:'2026-10-01',end:'2026-10-01',total_requests:0,active_users:0,daily:[{date:'2026-10-01',requests:0,seven_day_average:0,active_users:0,partial:true}],weekly:{start:'2026-09-24',end:'2026-09-30',previous_start:'2026-09-17',previous_end:'2026-09-23',requests:0,previous_requests:0,delta:0,percent_change:null}};
test('empty and single-day charts are finite, accessible, and honest about missing baseline',()=>{
 const {c}=setup();const html=c.adoptionDashboard(data);
 assert.doesNotMatch(html,/NaN|Infinity|undefined/);assert.match(html,/No recorded human requests/);assert.match(html,/No requests in either week/);assert.match(html,/role="img"/);assert.match(html,/Daily breakdown/);assert.match(html,/partial/);
 const growth=c.adoptionDashboard({...data,weekly:{...data.weekly,requests:5}});
 assert.match(growth,/No prior-week baseline/);assert.doesNotMatch(growth,/Infinity/);
});
test('members cannot fetch the admin report',async()=>{
 const {c,elements}=setup();c.state.role='member';c.api=()=>{throw Error('must not fetch');};await c.renderAdoption();assert.match(elements.get('#content').innerHTML,/administrators/);
});
test('filters fetch real endpoint and stale navigation cannot overwrite content',async()=>{
 const {c,elements}=setup();const calls=[];c.api=async url=>{calls.push(url);return data;};await c.renderAdoption();assert.equal(calls[0],'/api/admin/adoption?');
 elements.get('#adoption-filter-form').onsubmit({preventDefault(){},currentTarget:{elements:{start:{value:'2026-09-01'},end:{value:'2026-09-30'}}}});
 await Promise.resolve();assert.equal(calls[1],'/api/admin/adoption?start=2026-09-01&end=2026-09-30');
 c.api=async()=>{c.state.pageVersion++;elements.get('#content').innerHTML='next page';return data;};await c.renderAdoption();assert.equal(elements.get('#content').innerHTML,'next page');
});
