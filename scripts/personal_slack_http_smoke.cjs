/* Real local backend contract probe, for use after backend integration.
 * Start an isolated app with Alice and Bob test sessions and synthetic upstreams.
 * DEMO_BASE_URL=http://127.0.0.1:8000 DEMO_ALICE_COOKIE=... DEMO_BOB_COOKIE=... node scripts/personal_slack_http_smoke.cjs
 * Cookies come from the isolated fixture, never production. They are never logged.
 * Creates one private DEMO session; no model, OAuth exchange, or Slack upstream request.
 * This script is NOT proof of credential precedence. The backend fixture must test that.
 */
const assert=require('node:assert/strict');
const {randomUUID}=require('node:crypto');
(async()=>{
 const base=new URL(process.env.DEMO_BASE_URL||'http://127.0.0.1:8000');
 assert.ok(['127.0.0.1','localhost','[::1]'].includes(base.hostname),'Only an isolated local backend is supported.');
 const alice=process.env.DEMO_ALICE_COOKIE,bob=process.env.DEMO_BOB_COOKIE;
 assert.ok(alice&&bob,'Provide Alice and Bob isolated-fixture cookies through environment variables.');
 async function request(path,cookie,options={}) {
  return fetch(new URL(path,base),{...options,redirect:'error',headers:{Cookie:cookie,Origin:base.origin,'Content-Type':'application/json',...options.headers}});
 }
 const sessionResponse=await request('/api/session',alice);assert.equal(sessionResponse.status,200);
 const session=await sessionResponse.json();assert.ok(session.authenticated&&session.csrf);
 const personalResponse=await request('/api/connections/slack/personal',alice);assert.equal(personalResponse.status,200);
 const personal=await personalResponse.json();
 for(const key of ['connected','available','oauth_configured','label','effective_source','health'])assert.ok(key in personal,`Missing ${key}`);
 assert.ok(personal.available,'Alice must be an individual fixture identity.');
 const response=await request('/api/runs',alice,{method:'POST',headers:{'X-CSRF-Token':session.csrf},body:JSON.stringify({prompt:'Isolated HTTP private-session demo. No provider calls.',mode:'demo',private_session:true,plugins:[],client_id:randomUUID()})});
 assert.equal(response.status,201,'Real backend must accept private_session:true.');
 const run=await response.json();assert.ok(run.id);
 const ownerResponse=await request(`/api/runs/${run.id}`,alice);assert.equal(ownerResponse.status,200);
 const owned=await ownerResponse.json();assert.ok(owned.private_owner_id,'Response must retain immutable private owner.');
 const denied=await request(`/api/runs/${run.id}`,bob);assert.ok([403,404].includes(denied.status),'Bob must not read Alice private run.');
 console.log(JSON.stringify({evidence:'real local HTTP; demo execution; no Slack or OAuth claim',personal_source:personal.effective_source,create_status:response.status,owner_read_status:ownerResponse.status,bob_read_status:denied.status,browser_url:new URL(`/#run=${run.id}`,base).href},null,2));
})().catch(error=>{console.error(error.message);process.exitCode=1;});
