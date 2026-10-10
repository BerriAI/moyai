/* Deterministic, local-only UI fixture. Never contacts a gateway or cloud service.
 * node scripts/settings_ui_preview.cjs [--root /path/to/baseline/app/static] [--port 8841] [--delay-ms 300]
 * /?fixture=empty, /?fixture=error, /?fixture=member exercise alternate states.
 * API responses are synthetic. This is visual evidence, not a backend integration test.
 */
const http=require('node:http'),fs=require('node:fs'),path=require('node:path');
const arg=(name,fallback)=>process.argv.includes(name)?process.argv[process.argv.indexOf(name)+1]:fallback;
const root=path.resolve(arg('--root',path.join(__dirname,'../app/static'))),port=Number(arg('--port','8840'));
const delayMs=Math.max(0,Number(arg('--delay-ms','0'))||0);
const stamp='2026-10-07T15:00:00Z';
const agentSession=(number,prompt,status='idle',children=[])=>({id:number.toString(16).padStart(32,'0'),prompt,status,children,updated_at:stamp,created_at:stamp});
const agentSessions=[
 agentSession(1,'Adjust UI alignment across the workspace','idle',[
  agentSession(2,'Explore','idle',[agentSession(3,'Inspect shared components')]),
  agentSession(4,'Reproduction','running'),
 ]),
 agentSession(5,'Calculate 2+2 and reply with the result'),
 agentSession(6,'Review a very long session title that must stay within the sidebar','awaiting_approval'),
 agentSession(7,'Verify integration tests','failed'),
];
const model='openai/gpt-6-astra';
const recipe=(name,repository)=>({name,repository,ref:'main',setup_mode:'detect',clone_access:'github',apt_packages:[],setup:'',startup:'',verify:'',shutdown:'',instructions:''});
let fixture='populated';
let swarmFixtureState='active',swarmFixtureDeadline=new Date(Date.now()+1800000).toISOString(),swarmFixtureMessages=[];
const swarmRoot='a'.repeat(32);
function swarmFixtureRun(id=swarmRoot){
 const children=[
  {id:'b'.repeat(32),agent_label:'Research',harness:'claude-agent-sdk',model:'anthropic/claude-opus-4-6',status:'idle',children:[{id:'e'.repeat(32),agent_label:'Sources',harness:'opencode',model:'openai/gpt-6-astra',status:'idle',children:[]}]},
  {id:'c'.repeat(32),agent_label:'Build',harness:'codex',model:'openai/gpt-6-astra',status:'running',children:[]},
  {id:'d'.repeat(32),agent_label:'Review',harness:'hermes',model:'openai/gpt-6-astra',status:'running',children:[]},
 ];
 const flat=children.flatMap(child=>[child,...child.children]);
 if(swarmFixtureState!=='active')flat.forEach(child=>{if(child.status==='running')child.status='cancelled';});
 const child=flat.find(child=>child.id===id);
 return {id,display_title:child?.agent_label||'Build a better way to work together',prompt:child?'Review the implementation and report findings.':'Research a useful feature, build a small prototype, and review the result.',status:child?.status||(swarmFixtureState==='active'?'waiting_children':swarmFixtureState==='blocked'?'failed':'cancelled'),chat_enabled:true,mode:'modal',harness:child?.harness||'claude-agent-sdk',model:child?.model||'anthropic/claude-opus-4-6',active_model:child?.model||'anthropic/claude-opus-4-6',plugins:[],repo_url:'',parent_run_id:child?swarmRoot:null,snapshot_id:'synthetic',created_at:stamp,updated_at:stamp,approvals:[],credential_requests:[],artifacts:[],pull_requests:[],agents:{groups:child?[]:[{id:'fixture-team',label:'Prototype team',status:'working',children}]},...(child?{}:{swarm:{status:swarmFixtureState,budget_seconds:1800,ends_at:swarmFixtureDeadline,round:2,reason:swarmFixtureState==='paused'?'Paused by you. Resume before the original time limit.':swarmFixtureState==='blocked'?'The model request failed. Your work is saved.':''}}),messages:child?[{id:11,role:'assistant',status:'completed',content:'I checked the implementation and saved the findings. This is a synthetic preview response.'}]:[{id:1,role:'user',user_name:'Alex',status:'completed',content:'Research a useful feature, build a small prototype, and review the result.'},{id:2,role:'assistant',status:'completed',content:'Research is complete. Build and review are working in parallel.'},{id:3,role:'user',source:'swarm',client_id:'swarm:fixture:2',status:'completed',content:'Continue the mission with the next useful step.'},...swarmFixtureMessages],events:[{id:1,kind:'agents',message:'Research saved its findings. Build and Review are working.',created_at:stamp,data:{phase:'agent-result',input_id:3}},{id:2,kind:'tool',message:'Build is checking the prototype.',created_at:stamp,data:{phase:'tool',name:'terminal',input_id:3}}]};
}

let accountLinkPolls=0;
const skills=[
 ['review-pr','Review a pull request for correctness, regressions, and missing tests.','organization'],
 ['benchmark-report','Compare benchmark runs and summarize changes in latency, throughput, and cost.','organization'],
 ['release-notes','Turn merged pull requests into clear, customer-facing release notes.','organization'],
 ['investigate-issue','Reproduce an issue, trace the cause, and propose a focused fix.','personal'],
 ['writing-style','Keep explanations concise and include the evidence behind each recommendation.','personal']
].map(([name,description,scope],i)=>({id:'skill-'+i,name,description,scope,reference:(scope==='personal'?'personal:':'org:')+name,can_manage:true,archived:false,revision:1,instructions:'# '+name+'\n\n1. Gather context.\n2. Review the evidence.\n3. Summarize the result.',files:[]}));
const pickerSkills=[
 ['team','Use a team to reproduce an issue, review the implementation, and verify the fix.','personal'],
 ['team','Coordinate the team workflow with shared engineering guidance and repository checks.','organization'],
 ['team-release-readiness-and-regression-review','Review release readiness with the team, including tests, documentation, and deployment checks.','organization'],
 ['ticket','Create or update a ticket for the issue under discussion with the team.','organization'],
 ['verify','Verify the team’s changes and summarize the result with supporting evidence.','organization'],
].map(([name,description,scope],i)=>({...skills[0],id:'picker-'+i,name,description,scope,reference:(scope==='personal'?'personal:':'org:')+name,icon:'team'}));
let memories=[
 ['preference','Keep PR descriptions focused','Lead with what changed and why. Include the test results and any remaining limitations.'],
 ['feedback','Show the complete interaction','When sharing UI screenshots, include the relevant controls and verify the saved image before sending it.'],
 ['project','Use the project’s test commands','For this repository, run the focused test suite first, then the relevant integration checks.']
].map(([kind,title,content],i)=>({id:'memory-'+i,key:'note-'+i,kind,title,content,repo_url:'',revision:1,updated_at:stamp,source:{type:'manual'}}));
let preferences={enabled:true,auto_save:true,revision:1};
const names=[['Alex Morgan','alex','admin'],['Sam Rivera','sam','admin'],['Jordan Lee','jordan','member'],['Casey Chen','casey','member'],['Taylor Kim','taylor','member'],['Riley Patel','riley','member']];
let users=names.map(([name,id,role])=>({name,email:id+'@example.com',role,has_signed_in:id!=='riley',revision:1}));
const connections=['github','slack','linear','notion'].map(id=>({id,connected:true,enabled:true,read_only:false,label:{github:'example/platform, example/moyai, example/docs',slack:'Example team',linear:'Engineering',notion:'Team knowledge'}[id],identity:id==='github'?'Organization GitHub App':'OAuth connection',checked_at:stamp,check_status:'healthy',tools:[{description:'Read and search connected content.'}],actions:[]}));
let secrets=['GITHUB_TOKEN','STAGING_API_KEY','BENCHMARK_SERVICE_ACCOUNT','OBSERVABILITY_TOKEN','DESIGN_ASSETS_KEY'].map((label,i)=>({id:'secret-'+i,label,name:label.toLowerCase(),provider:'generic',format:'env',scope:i===4?'personal':'organization',lifetime:'persistent',status:i===3?'expired':'active',created_at:stamp,can_manage:true}));
const envs=[
 {id:'env-0',name:'Platform development',repository:'example/platform',active_build:'build-0',enabled:true,is_default:true,refresh_daily:true,revision:3,builds:[{id:'build-0',revision:3,phase:'ready',commit_sha:'a12bc3de'}]},
 {id:'env-1',name:'Moyai development',repository:'example/moyai',active_build:'',activate_on_ready:true,revision:2,builds:[{id:'build-1',revision:2,phase:'failed',error:'Dependency checks failed. Review the build log, update the recipe, and rebuild.'}]},
 {id:'env-2',name:'Documentation',repository:'example/docs',active_build:'',activate_on_ready:true,revision:1,builds:[]},
 {id:'env-3',name:'Gateway benchmarks',repository:'example/benchmarks',active_build:'build-3',enabled:true,refresh_daily:true,revision:1,builds:[{id:'build-3',revision:1,phase:'ready',commit_sha:'b45ef6ab'}]}
].map(e=>({...e,recipe:recipe(e.name,e.repository)}));
let automations=[
 {id:'auto-0',owner:'alex@example.com',paused:false,can_edit:true,revision:1,synced_revision:1,completed_triggers:[],definition:{name:'Weekly engineering digest',prompt:'Review the merged pull requests and completed Linear issues from the past week. Summarize the most important changes, tests, and follow-up work. Link each finding to its source. Keep the digest concise and group related updates together.',model,harness:'claude-agent-sdk',repo_url:'https://github.com/example/platform',environment_id:'auto',mode:'modal',plugins:['github','linear'],max_runs_per_hour:50,triggers:[{id:'weekly',schedule:{frequency:'weekly',time:'09:00',weekday:1,timezone:'America/Los_Angeles'}}]},history:[{created_at:stamp,status:'idle',run_id:'a'.repeat(32)}]},
 {id:'auto-1',owner:'alex@example.com',paused:true,can_edit:true,revision:1,synced_revision:1,completed_triggers:[],definition:{name:'Investigate failed builds',prompt:'Check failed CI runs for the main branch. Read the failing job logs, identify the likely cause, and prepare a fix with focused regression tests.',model,harness:'claude-agent-sdk',repo_url:'https://github.com/example/moyai',environment_id:'auto',mode:'modal',plugins:['github'],max_runs_per_hour:50,triggers:[{id:'event',event:{provider:'github',event:'check_run.completed',repository:'example/moyai',conclusion:'failure'}}]},history:[],trigger:{ready:true,providers:[{provider:'github',ready:true,url:'https://example.com/webhooks/test'}],deliveries:[]}}
];
const providers=[{provider:'modal',name:'Modal',spend:42.75,covered_days:7,missing_days:0,sync_enabled:true,last_synced_at:stamp},{provider:'render',name:'Render',spend:5.70,covered_days:7,missing_days:0,sync_enabled:false},{provider:'temporal',name:'Temporal Cloud',spend:3.42,covered_days:6,missing_days:1,sync_enabled:true,last_synced_at:stamp}];
const spendUsers=users.map((u,i)=>({...u,id:'user-'+i,kind:'google',sessions:[32,24,19,15,11,5][i],requests:[720,510,386,230,164,84][i],total_tokens:[720,510,386,230,164,84][i]*3000,spend:[421.32,304.16,187.54,112.8,98.63,57.11][i],pending_costs:0,missing_costs:0}));
const spend={start:'2026-10-01',end:'2026-10-07',total:{spend:1181.56,requests:2094,total_tokens:6282000,pending_costs:0,missing_costs:12},priced_requests:2082,cost_summary:{total:1233.43,llm:1181.56,infrastructure:51.87,incomplete:true,estimated:5.70},infrastructure:{spend:51.87,pending:false,providers,bills:[{provider:'render',month:'2026-10',amount:'25.25',kind:'estimate',revision:1,note:'Hosting and storage. Reconcile to the final invoice.'}]},users:spendUsers,identities:[],models:[{model,spend:962.30,requests:1670},{model:'anthropic/claude-opus-5-5',spend:219.26,requests:424}],sessions:[{run_id:'a'.repeat(32),title:'Review release readiness',user_id:'user-0',user_name:'Alex Morgan',requests:28,spend:18.22},{run_id:'b'.repeat(32),title:'Investigate gateway latency',user_id:'user-1',user_name:'Sam Rivera',requests:42,spend:24.75}],request_details:[],tracked_since:'2026-09-01'};
// Daily aggregates match the report totals and are independent of request_details.
function spendFixture(params,personal=false,empty=false){
 const own=spendUsers[0],source=personal?{...spend,total:{...own,prompt_tokens:1440000,completion_tokens:720000},users:[own],identities:[own],models:[{model,spend:own.spend,requests:own.requests,total_tokens:own.total_tokens}],sessions:spend.sessions.filter(s=>s.user_id===own.id)}:spend;
 const start=params.get('start')||spend.start,end=params.get('end')||spend.end;
 if(start>end||(new Date(end)-new Date(start))/86400000>92)return null;
 const weights=[.11,.16,.22,.08,.18,.15,.10],amount=(n,i)=>Math.round(Number(n)*weights.slice(0,i+1).reduce((a,b)=>a+b,0)*100)/100-Math.round(Number(n)*weights.slice(0,i).reduce((a,b)=>a+b,0)*100)/100;
 const days=Array.from({length:Math.round((new Date(end)-new Date(start))/86400000)+1},(_,i)=>{
  const date=new Date(new Date(start).getTime()+i*86400000).toISOString().slice(0,10),index=Number(date.slice(-2))-1,has=!empty&&date>=spend.start&&date<=spend.end;
  const models=has?source.models.map((m,mi)=>({...m,spend:amount(m.spend,index).toFixed(2),requests:Math.round(m.requests*weights.slice(0,index+1).reduce((a,b)=>a+b,0))-Math.round(m.requests*weights.slice(0,index).reduce((a,b)=>a+b,0)),total_tokens:Math.round(amount(m.requests*3000,index)),pending_costs:0,missing_costs:!personal&&index===6&&mi===0?12:0})):[];
  return {date,spend:models.reduce((n,m)=>n+Number(m.spend),0).toFixed(2),requests:models.reduce((n,m)=>n+m.requests,0),total_tokens:models.reduce((n,m)=>n+m.total_tokens,0),sessions:has?(personal?[5,8,12,3,11,7,6][index]:[28,39,54,18,42,36,31][index]):0,active_users:has?(personal?1:[4,5,6,3,6,5,4][index]):0,pending_costs:0,missing_costs:has&&!personal&&index===6?12:0,models};
 });
 const ratio=empty?0:days.reduce((n,d)=>n+Number(d.spend),0)/Number(source.total.spend);
 const scale=u=>({...u,spend:(Number(u.spend)*ratio).toFixed(2),requests:Math.round(u.requests*ratio),sessions:Math.round((u.sessions||106)*ratio),total_tokens:Math.round(u.total_tokens*ratio)});
 return {...source,scope:personal?'personal':'organization',start,end,daily:days,total:{...scale(source.total),pending_costs:0,missing_costs:days.reduce((n,d)=>n+d.missing_costs,0)},priced_requests:days.reduce((n,d)=>n+d.requests-d.missing_costs,0),users:empty?[]:source.users.map(scale),models:empty?[]:source.models.map(m=>scale({...m,total_tokens:m.requests*3000})),sessions:empty?[]:source.sessions,infrastructure:personal?undefined:source.infrastructure,cost_summary:personal?undefined:{...source.cost_summary,llm:(Number(source.total.spend)*ratio).toFixed(2),total:(Number(source.total.spend)*ratio+source.infrastructure.spend).toFixed(2)}};
}
const prTitles=['Keep session titles after reconnect','Add streaming retry coverage','Restore saved model preferences','Fix grouped tool results','Show pending request costs','Preserve repository selection','Make Slack links durable','Improve deployment status','Cache model catalog responses','Handle interrupted uploads','Document sandbox lifecycle','Fix request log dates','Retain review history','Add GitHub connection checks','Support workspace memory search','Improve transcript accessibility','Repair deployment retries','Update contributor documentation','Add request-level tracing','Explore model fallback policy','Replace legacy setup wizard','Investigate intermittent timeouts'];
const prActors=[0,0,0,0,0,0,1,1,1,1,2,2,2,3,3,4,5,-1,0,-2,2,3];
const prSessionLedger=new Map();
const pullRequestRows=prTitles.map((title,i)=>{
 const actor=prActors[i],user=actor===-2?{id:'user-pr-only',name:'Robin Shah',email:'robin@example.com'}:actor<0?null:spendUsers[actor],sessionId=(i===1?1:i+1).toString(16).padStart(32,'0');
 if(!prSessionLedger.has(sessionId))prSessionLedger.set(sessionId,{spend:Math.round((9.4+(i%7)*6.13)*100),requests:18+(i%7)*11,pending_costs:i===19?2:0,missing_costs:i===6?1:0});
 const costs=prSessionLedger.get(sessionId),repo=i%4===0?'platform':'moyai',merged=i<18,state=merged?'merged':i===20?'closed':i===21?'unknown':'open';
 return {repository_id:repo==='moyai'?2002:2001,number:210+i,url:`https://github.com/example/${repo}/pull/${210+i}`,title,state,draft:i===19,created_at:i===21?null:i%5===0?'2026-09-28T09:00:00Z':`2026-10-${String(i<18?Math.max(1,i%7):1+i%7).padStart(2,'0')}T09:00:00Z`,tracked_at:i%5===0?'2026-09-29T10:00:00Z':`2026-10-${String(1+i%7).padStart(2,'0')}T10:00:00Z`,merged_at:merged?`2026-10-${String(1+i%7).padStart(2,'0')}T15:00:00Z`:null,user_id:user?.id||'unattributed',user_name:user?.name||'Unattributed',user_email:user?.email||'',sessions:[{id:sessionId,title:i===1?prTitles[0]:title,deleted:i===15}],spend:(costs.spend/100).toFixed(2),requests:costs.requests,pending_costs:costs.pending_costs,missing_costs:costs.missing_costs,stale:i===21};
});
function pullRequestFixture(params,empty=false){
 const start=params.get('start')||spend.start,end=params.get('end')||spend.end,inRange=value=>value&&value.slice(0,10)>=start&&value.slice(0,10)<=end;
 const rows=empty?[]:pullRequestRows,created=rows.filter(row=>inRange(row.created_at)),merged=rows.filter(row=>row.state==='merged'&&inRange(row.merged_at)),people=new Map();
 const personFor=row=>{if(!people.has(row.user_id))people.set(row.user_id,{user_id:row.user_id,name:row.user_name,email:row.user_email,created_prs:0,status_counts:{merged:0,open:0,draft:0,closed:0,unknown:0},merged_prs:0,session_ids:new Set()});return people.get(row.user_id);};
 for(const row of created){const person=personFor(row);person.created_prs++;person.status_counts[row.state==='open'&&row.draft?'draft':row.state]++;}
 for(const row of merged){const person=personFor(row);person.merged_prs++;for(const session of row.sessions)person.session_ids.add(session.id);}
 const leaderboard=[...people.values()].map(({session_ids,...person})=>{const costs=[...session_ids].map(id=>prSessionLedger.get(id)),spend=costs.reduce((sum,c)=>sum+c.spend,0)/100;return {...person,sessions:session_ids.size,spend:spend.toFixed(2),cost_per_merged_pr:person.merged_prs?(spend/person.merged_prs).toFixed(6):null,requests:costs.reduce((sum,c)=>sum+c.requests,0),pending_costs:costs.reduce((sum,c)=>sum+c.pending_costs,0),missing_costs:costs.reduce((sum,c)=>sum+c.missing_costs,0)};}).sort((a,b)=>b.merged_prs-a.merged_prs||a.name.localeCompare(b.name));
 return {start,end,timezone:'UTC',currency:'USD',pull_requests:rows.filter(row=>inRange(row.tracked_at)),created_pull_requests:created,total_created:created.length,merged_pull_requests:merged,leaderboard,total_merged:merged.length,contributors:leaderboard.filter(row=>row.user_id!=='unattributed').length,unknown_status:rows.filter(row=>row.state==='unknown').length,unknown_created_at:rows.filter(row=>!row.created_at).length,stale_status:rows.filter(row=>row.stale).length,pending_refresh:false};
}
const daily=Array.from({length:30},(_,i)=>({date:new Date(Date.UTC(2026,8,8+i)).toISOString().slice(0,10),requests:[4,8,6,5,12,9,11,8,16,12,18,21,16,19,24,28,18,26,31,29,35,27,42,38,45,34,51,48,58,36][i],active_users:Math.min(6,2+Math.floor(i/6)),partial:i===29}));
daily.forEach((d,i)=>d.seven_day_average=Number((daily.slice(Math.max(0,i-6),i+1).reduce((n,d)=>n+d.requests,0)/7).toFixed(1)));
const adoption={start:daily[0].date,end:daily.at(-1).date,total_requests:daily.reduce((n,d)=>n+d.requests,0),active_users:6,daily,weekly:{requests:312,previous_requests:198,percent_change:57.6,delta:114,start:'2026-09-30',end:'2026-10-06',previous_start:'2026-09-23',previous_end:'2026-09-29'}};
let titleModel='openai/gpt-4.1-nano';
let chatPreferences={send_immediately:false,omit_private_tool_payloads:false};
let sandboxConnection={provider:'modal',revision:0,public_key:'c3ludGhldGljLXByZXZpZXctcHVibGljLWtleS1vbmx5',providers:{
 lambda:{configured:false,values:{lambda_region:'us-east-1',lambda_image:'',lambda_image_version:'',lambda_checkpoint_bucket:'',lambda_checkpoint_prefix:'moyai-lambda',lambda_execution_role_arn:'',lambda_egress_connector:'',lambda_profile:''},secrets:{}},
 modal:{configured:true,values:{modal_app_name:'moyai'},secrets:{modal_token_id:true,modal_token_secret:true}},
 substrate:{configured:false,values:{substrate_api_url:'',substrate_router_url:'',substrate_atespace:'moyai',substrate_template:'moyai',substrate_egress_hosts:'*'},secrets:{substrate_api_token:false}}
}};
const json=(res,status,value)=>{res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(value));};
const server=http.createServer(async(req,res)=>{
 const url=new URL(req.url,'http://localhost'),p=url.pathname;
 if(p==='/'){
  fixture=url.searchParams.get('fixture')||'populated';
  accountLinkPolls=0;
  if(fixture.startsWith('swarm')){swarmFixtureState=fixture==='swarm-paused'?'paused':fixture==='swarm-expired'?'expired':fixture==='swarm-error'?'blocked':'active';swarmFixtureDeadline=new Date(Date.now()+(swarmFixtureState==='expired'?-60000:1800000)).toISOString();swarmFixtureMessages=[];}
  let html=fs.readFileSync(path.join(root,'index.html'),'utf8');
  res.writeHead(200,{'Content-Type':'text/html','Cache-Control':'no-store'});return res.end(html);
 }
 if(p.startsWith('/static/')){
  const file=path.resolve(root,p.slice(8));if(!file.startsWith(root+path.sep)||!fs.existsSync(file))return json(res,404,{});
  res.writeHead(200,{'Content-Type':{'.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml','.png':'image/png'}[path.extname(file)]||'application/octet-stream','Cache-Control':'no-store'});return res.end(fs.readFileSync(file));
 }
 const role=fixture==='member'?'member':'admin',empty=fixture==='empty';
 if(delayMs&&req.method==='GET'&&p.startsWith('/api/'))await new Promise(resolve=>setTimeout(resolve,delayMs));
 let body={};if(req.method!=='GET'){const chunks=[];for await(const chunk of req)chunks.push(chunk);try{body=JSON.parse(Buffer.concat(chunks).toString()||'{}');}catch{return json(res,400,{detail:'Invalid JSON'});}}
 const swarmPreview=fixture.startsWith('swarm');
 const supportedWrite = (swarmPreview&&req.method==='POST'&&(p==='/api/runs'||/^\/api\/runs\/[a-f0-9]{32}\/(messages|cancel|swarm\/(pause|resume))$/.test(p))) || (req.method==='PUT' && ['/api/settings/preferences','/api/settings/sandboxes','/api/settings/session-titles','/api/memory/preferences','/api/admin/users/role'].includes(p)) ||
  (req.method==='POST' && p==='/api/memory') ||
  (['PUT','DELETE'].includes(req.method) && /^\/api\/memory\/[^/]+$/.test(p)) ||
  (req.method==='POST' && /^\/api\/automations\/[^/]+\/state$/.test(p));
 if(req.method!=='GET'&&!supportedWrite)return json(res,501,{detail:'This operation is not available in the visual preview.'});
 if(fixture==='error'&&['/api/settings/sandboxes','/api/skills','/api/credentials','/api/memory','/api/admin/environments','/api/admin/spend','/api/spend','/api/admin/adoption','/api/automations'].includes(p))return json(res,503,{detail:'This preview simulates a service outage.'});
 if(p==='/api/settings/sandboxes'){
  if(req.method==='PUT'){
   if(role!=='admin')return json(res,403,{detail:'Administrator access required.'});
   sandboxConnection={...sandboxConnection,provider:body.provider,revision:sandboxConnection.revision+1,message:'Synthetic connection test succeeded.'};
   for(const [key,value] of Object.entries(body.values||{})){
    if(key.includes('token')){if(value)sandboxConnection.providers[body.provider].secrets[key]=true;}
    else sandboxConnection.providers[body.provider].values[key]=value;
   }
  }
  return json(res,200,role==='admin'?sandboxConnection:{provider:sandboxConnection.provider});
 }
 if(swarmPreview&&p==='/api/runs'&&req.method==='POST'){swarmFixtureState='active';swarmFixtureDeadline=new Date(Date.now()+(body.swarm?.budget_seconds||1800)*1000).toISOString();return json(res,200,swarmFixtureRun());}
 if(swarmPreview&&/^\/api\/runs\/[a-f0-9]{32}\/(swarm\/(pause|resume)|cancel)$/.test(p)){swarmFixtureState=p.endsWith('/pause')?'paused':p.endsWith('/resume')?'active':'stopped';const {messages,events,agents,approvals,credential_requests,...summary}=swarmFixtureRun();return json(res,200,summary);}
 if(swarmPreview&&p.endsWith('/messages')&&req.method==='POST'){swarmFixtureMessages.push({id:10+swarmFixtureMessages.length,role:'user',user_name:'Alex',status:'completed',content:body.content});return json(res,200,swarmFixtureRun());}
 if(swarmPreview&&/^\/api\/runs\/[a-f0-9]{32}\/events$/.test(p)){res.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-cache','Connection':'keep-alive'});res.write(': synthetic preview connected\n\n');return;}
 if(swarmPreview&&p.endsWith('/files'))return json(res,200,{files:[]});
 if(swarmPreview&&p.endsWith('/activity'))return json(res,200,{events:swarmFixtureRun().events,has_more:false});
 if(p==='/api/session')return json(res,200,{authenticated:true,local:true,role,user_id:'user-0',preferences:chatPreferences,csrf:'local-fixture',identity:{email:'alex@example.com',name:'Alex Morgan'}});
 if(p==='/api/config')return json(res,200,{missing:[],cloud_ready:true,harness:'claude-agent-sdk',harnesses:[{id:'claude-agent-sdk',name:'Claude Agent SDK',models:[model]},{id:'codex',name:'Codex'},{id:'hermes',name:'Hermes'},{id:'opencode',name:'OpenCode'}],models:[{id:model,name:'GPT-6 Astra'},{id:'anthropic/claude-opus-4-6',name:'Claude Opus'}],model,execution_engine:'Temporal',execution_connected:true,checkpoint_interval_seconds:600,max_concurrent_runs:100,parallel_agents_enabled:true,max_parallel_agents:100,sandbox_idle_seconds:300,run_timeout_seconds:0});
 if(p==='/api/organization')return json(res,200,{name:'Example team',google_signin:true,activity:[],slack_sessions:{enabled:true,audience:'Workspace members',thread_reply_ready:true,direct_message_ready:true}});
 if(p==='/api/runs')return json(res,200,swarmPreview?[swarmFixtureRun()]:fixture==='agent-sidebar'?agentSessions:['Review release readiness','Investigate gateway latency','Update integration tests','Draft the engineering digest'].map((prompt,i)=>({id:String(i+1).repeat(32),prompt,status:'idle',updated_at:stamp,created_at:stamp,children:[]})));
 if(/^\/api\/runs\/[0-9a-f]{32}$/.test(p))return json(res,200,swarmPreview?swarmFixtureRun(p.split('/').at(-1)):{id:p.split('/').at(-1),prompt:'Review release readiness',status:'completed',chat_enabled:['skill-picker','feedback'].includes(fixture),feedback_enabled:fixture==='feedback',mode:'modal',sandbox_provider:fixture==='modal-run'?'modal':'substrate',harness:'claude-agent-sdk',plugins:[],repo_url:'',messages:fixture==='feedback'?[{id:1,role:'user',status:'completed',content:'Review the latest change.'},{id:2,role:'assistant',status:'completed',content:'The change looks good. I checked the API and its regression tests.'}]:[],events:[{id:1,kind:'result',message:'Review complete. No changes needed.',created_at:stamp}],approvals:[],artifacts:[],updated_at:stamp,created_at:stamp});
 if(p==='/api/session-folders')return json(res,200,{folders:[]});
 if(p==='/api/connections')return json(res,200,connections);
 if(p==='/api/settings/preferences'){
  if(req.method==='PUT'){
   if(fixture==='error')return json(res,503,{detail:'This preview simulates a service outage.'});
   for(const key of Object.keys(chatPreferences))if(key in body)chatPreferences[key]=body[key]===true;
  }
  return json(res,200,chatPreferences);
 }
 if(p==='/api/settings/session-titles'){if(req.method==='PUT')titleModel=body.model;return json(res,200,{model:titleModel,enabled:true,gateway_configured:true});}
 if(p==='/api/skills')return json(res,200,{skills:empty?[]:fixture==='skill-picker'?pickerSkills:skills});
 if(p.startsWith('/api/skills/'))return json(res,200,[...skills,...pickerSkills].find(s=>s.id===p.split('/')[3])||{});
 if(p==='/api/memory/preferences'){preferences={...preferences,...body,revision:preferences.revision+1};return json(res,200,preferences);}
 if(p==='/api/memory'&&req.method==='POST'){memories.push({...body,id:'memory-new',updated_at:stamp,source:{type:'manual'}});return json(res,200,{});}
 if(p.startsWith('/api/memory/')&&req.method==='PUT'){const note=memories.find(n=>n.id===p.split('/')[3]);Object.assign(note,body);return json(res,200,note);}
 if(p.startsWith('/api/memory/')&&req.method==='DELETE'){memories=memories.filter(n=>n.id!==p.split('/')[3]);return json(res,200,{});}
 if(p==='/api/memory')return json(res,200,{memories:empty?[]:memories,preferences,limit:200});
 if(p==='/api/credentials')return json(res,200,{secrets:empty?[]:secrets,providers:[{id:'generic',name:'Other service',setup_url:''}],requests:[]});
 if(p==='/api/admin/users')return json(res,200,{users:empty?[]:users,domains:['example.com'],activity:[]});
 if(p==='/api/admin/users/role'){const user=users.find(u=>u.email===body.email);if(user)user.role=body.role;else users.push({name:body.email.split('@')[0],email:body.email,role:body.role,has_signed_in:false,revision:1});return json(res,200,{});}
 if(p==='/api/environments')return json(res,200,empty?[]:envs);
 if(p==='/api/admin/environments')return json(res,200,{environments:empty?[]:envs,templates:[recipe('New environment','example/project')],automatic_setup:true});
 if(p==='/api/automations')return json(res,200,{automations:empty?[]:automations,enabled:true,connected:true,event_choices:{github:[['check_run.completed','Check completed']]},templates:[{name:'My Linear tickets → PR',plugins:['linear','github'],prompt:'Pick a ticket, implement a fix, and prepare a PR.'}]});
 if(p.startsWith('/api/automations/')&&p.endsWith('/state')){const a=automations.find(a=>a.id===p.split('/')[3]);a.paused=body.paused;return json(res,200,{});}
 if(p==='/api/admin/spend')return json(res,200,spend);
 if(p==='/api/admin/pull-requests'){
  if(role!=='admin')return json(res,403,{detail:'Administrator access required.'});
  if(fixture==='pr-error')return json(res,503,{detail:'This preview simulates unavailable PR analytics.'});
  return json(res,200,pullRequestFixture(url.searchParams,empty));
 }
 if(p==='/api/spend'){
  const data=spendFixture(url.searchParams,role!=='admin',empty);
  if(data&&['account-links','account-links-final'].includes(fixture)&&role==='admin'){
   data.identities=[...spendUsers.slice(0,2),{id:'slack:alex',kind:'slack',name:'Alex Morgan',email:'alex@example.com',link_status:'review'}];
   data.total.pending_costs=fixture==='account-links-final'&&++accountLinkPolls>1?0:1;
   data.infrastructure.pending=false;
  }
  return json(res,data?200:422,data||{detail:'Choose a date range of up to 93 days, with start before end.'});
 }
 if(p==='/api/admin/identities/status'){
  const updated=fixture==='account-links-final'&&accountLinkPolls>1;
  // Leave time to open a menu after the cost poll, before its identity response.
  if(updated)await new Promise(resolve=>setTimeout(resolve,2000));
  return json(res,200,{enabled:!updated,ready:!updated,missing_scopes:[]});
 }
 if(p==='/api/admin/adoption'){
  if(role!=='admin')return json(res,403,{detail:'Organization administrator access required.'});
  if(fixture==='activity-error')return json(res,503,{detail:'This preview simulates an activity report outage.'});
  const range=spendFixture(url.searchParams,false,true);
  if(!range)return json(res,422,{detail:'Choose a date range of up to 93 days, with start before end.'});
  const rows=range.daily.map(day=>(!empty&&daily.find(d=>d.date===day.date))||{date:day.date,requests:0,active_users:0,seven_day_average:0,partial:day.date===adoption.end});
  return json(res,200,{...adoption,start:range.start,end:range.end,daily:rows,total_requests:rows.reduce((sum,d)=>sum+d.requests,0),active_users:Math.max(0,...rows.map(d=>d.active_users)),weekly:empty?{...adoption.weekly,requests:0,previous_requests:0,delta:0,percent_change:null}:adoption.weekly});
 }
 return json(res,501,{detail:'This operation is not available in the visual preview.'});
});
server.listen(port,'127.0.0.1',()=>console.log(`Settings UI fixture at http://127.0.0.1:${server.address().port}. Synthetic data; no external services.`));
