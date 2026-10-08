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
test('human activity embeds once and keeps newest breakdown dates first',()=>{
 const {c}=setup();const report={...data,daily:[{...data.daily[0],date:'2026-09-30'},...data.daily]};
 const html=c.adoptionDashboard(report);
 assert.doesNotMatch(html,/<h1|href="#spend"|analytics-tabs|analytics-actions/);
 const rows=html.slice(html.indexOf('<tbody>'));
 assert.ok(rows.indexOf('2026-10-01')<rows.indexOf('2026-09-30'));
 assert.equal(c.adoptionExport(report).length,3);
 assert.equal(c.adoptionExport(report)[0][1],'Human requests');
});
