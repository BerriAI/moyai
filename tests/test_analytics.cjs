const assert=require('node:assert/strict');
const {test}=require('node:test');
const {readFileSync}=require('node:fs');
const vm=require('node:vm');
function setup(){
 const c={esc:v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),modelName:v=>v,dollars:v=>'$'+Number(v).toFixed(6),spendCount:v=>String(v),spendState:{user:''}};
 vm.createContext(c);for(const file of ['analytics','spend-analytics'])vm.runInContext(readFileSync('app/static/'+file+'.js','utf8'),c);return c;
}
test('empty, all-zero, and one-day charts have finite geometry and escaped labels',()=>{
 const c=setup();
 for(const rows of [[],[{date:'2026-10-01',spend:0}],[{date:'2026-10-01',spend:0.123456}]]){
  const html=c.analyticsChart(rows,[{key:'spend',label:'<script>alert(1)</script>'}],{title:'Test <chart>',money:true,cumulative:true});
  assert.doesNotMatch(html,/NaN|Infinity|undefined|<script>/);assert.match(html,/role="img"/);assert.match(html,/Exact values/);
 }
});
test('UTC presets handle a year boundary, leap day, and inclusive seven-day ranges',()=>{
 const c=setup();
 assert.equal(JSON.stringify(c.analyticsPreset('last-month',new Date('2026-01-31T23:00:00Z'))),JSON.stringify({start:'2025-12-01',end:'2025-12-31'}));
 assert.equal(c.analyticsPreset('last-month',new Date('2024-03-01T00:00:00Z')).end,'2024-02-29');
 assert.equal(c.analyticsPreset('7',new Date('2026-10-07T00:30:00Z')).start,'2026-10-01');
});
test('CSV export quotes multiline labels and neutralizes spreadsheet formulas',()=>{
 const c=setup();const csv=c.analyticsCSV([['=SUM(A1)','hello,"there"\nfriend',' @malicious','1.23']]);
 assert.equal(csv,'"\'=SUM(A1)","hello,""there""\nfriend","\' @malicious","1.23"');
});
test('model filters and other-model bucketing reconcile the complete daily spend',()=>{
 const c=setup(),models=Array.from({length:6},(_,i)=>({model:'model-'+i,spend:String(i+1),requests:i+2,pending_costs:0,missing_costs:i===0?1:0}));
 const data={models,daily:[{date:'2026-10-01',models}]};
 const all=c.spendHistoryData(data);assert.equal(all.series.length,5);assert.equal(all.total,21);assert.equal(all.rows[0].other,3);
 vm.runInContext("spendAnalyticsState.model='model-0';spendAnalyticsState.tab='history'",c);
 const filtered=c.spendHistoryData(data);assert.equal(filtered.total,1);assert.equal(filtered.rows[0].missing_costs,1);assert.equal(filtered.rows[0].requests,2);
 const exported=c.spendExport(data);assert.equal(exported.length,2);assert.ok(exported[0].includes('model-0 USD'));assert.ok(!exported[0].includes('model-5 USD'));
});
test('user sorting and exports keep the selected identity',()=>{
 const c=setup(),data={users:[{id:'a',email:'a@example.com',spend:'10',sessions:2},{id:'b',email:'b@example.com',spend:'2',sessions:5}]};
 assert.equal(c.spendUserRows(data)[0].id,'a');
 vm.runInContext("spendAnalyticsState.sort='sessions';spendAnalyticsState.tab='users';spendState.user='b'",c);
 assert.equal(c.spendUserRows(data).length,1);assert.equal(c.spendExport(data)[1][0],'b@example.com');
});
