/* Deterministic, local-only UI fixture. Never contacts a gateway or cloud service.
 * node scripts/settings_ui_preview.cjs [--root /path/to/baseline/app/static] [--port 8841]
 * /?fixture=empty, /?fixture=error, /?fixture=member exercise alternate states.
 * API responses are synthetic. This is visual evidence, not a backend integration test.
 */
const http=require('node:http'),fs=require('node:fs'),path=require('node:path');
const arg=(name,fallback)=>process.argv.includes(name)?process.argv[process.argv.indexOf(name)+1]:fallback;
const root=path.resolve(arg('--root',path.join(__dirname,'../app/static'))),port=Number(arg('--port','8840'));
const stamp='2026-10-07T15:00:00Z';
const model='openai/gpt-6-astra';
const recipe=(name,repository)=>({name,repository,ref:'main',setup_mode:'detect',clone_access:'github',apt_packages:[],setup:'',startup:'',verify:'',shutdown:'',instructions:''});
let fixture='populated';
const skills=[
 ['review-pr','Review a pull request for correctness, regressions, and missing tests.','organization'],
 ['benchmark-report','Compare benchmark runs and summarize changes in latency, throughput, and cost.','organization'],
 ['release-notes','Turn merged pull requests into clear, customer-facing release notes.','organization'],
 ['investigate-issue','Reproduce an issue, trace the cause, and propose a focused fix.','personal'],
 ['writing-style','Keep explanations concise and include the evidence behind each recommendation.','personal']
].map(([name,description,scope],i)=>({id:'skill-'+i,name,description,scope,reference:(scope==='personal'?'personal:':'org:')+name,can_manage:true,archived:false,revision:1,instructions:'# '+name+'\n\n1. Gather context.\n2. Review the evidence.\n3. Summarize the result.',files:[]}));
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
const daily=Array.from({length:30},(_,i)=>({date:new Date(Date.UTC(2026,8,8+i)).toISOString().slice(0,10),requests:[4,8,6,5,12,9,11,8,16,12,18,21,16,19,24,28,18,26,31,29,35,27,42,38,45,34,51,48,58,36][i],active_users:Math.min(6,2+Math.floor(i/6)),partial:i===29}));
daily.forEach((d,i)=>d.seven_day_average=Number((daily.slice(Math.max(0,i-6),i+1).reduce((n,d)=>n+d.requests,0)/7).toFixed(1)));
const adoption={start:daily[0].date,end:daily.at(-1).date,total_requests:daily.reduce((n,d)=>n+d.requests,0),active_users:6,daily,weekly:{requests:312,previous_requests:198,percent_change:57.6,delta:114,start:'2026-09-30',end:'2026-10-06',previous_start:'2026-09-23',previous_end:'2026-09-29'}};
let titleModel='openai/gpt-4.1-nano';
let chatPreferences={send_immediately:false};
const json=(res,status,value)=>{res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(value));};
const server=http.createServer(async(req,res)=>{
 const url=new URL(req.url,'http://localhost'),p=url.pathname;
 if(p==='/'){
  fixture=url.searchParams.get('fixture')||'populated';
  let html=fs.readFileSync(path.join(root,'index.html'),'utf8');
  res.writeHead(200,{'Content-Type':'text/html','Cache-Control':'no-store'});return res.end(html);
 }
 if(p.startsWith('/static/')){
  const file=path.resolve(root,p.slice(8));if(!file.startsWith(root+path.sep)||!fs.existsSync(file))return json(res,404,{});
  res.writeHead(200,{'Content-Type':{'.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml','.png':'image/png'}[path.extname(file)]||'application/octet-stream','Cache-Control':'no-store'});return res.end(fs.readFileSync(file));
 }
 const role=fixture==='member'?'member':'admin',empty=fixture==='empty';
 let body={};if(req.method!=='GET'){const chunks=[];for await(const chunk of req)chunks.push(chunk);try{body=JSON.parse(Buffer.concat(chunks).toString()||'{}');}catch{return json(res,400,{detail:'Invalid JSON'});}}
 const supportedWrite = (req.method==='PUT' && ['/api/settings/session-titles','/api/settings/preferences','/api/memory/preferences','/api/admin/users/role'].includes(p)) ||
  (req.method==='POST' && p==='/api/memory') ||
  (['PUT','DELETE'].includes(req.method) && /^\/api\/memory\/[^/]+$/.test(p)) ||
  (req.method==='POST' && /^\/api\/automations\/[^/]+\/state$/.test(p));
 if(req.method!=='GET'&&!supportedWrite)return json(res,501,{detail:'This operation is not available in the visual preview.'});
 if(fixture==='error'&&['/api/skills','/api/credentials','/api/memory','/api/admin/environments','/api/admin/spend','/api/spend','/api/admin/adoption','/api/automations'].includes(p))return json(res,503,{detail:'This preview simulates a service outage.'});
 if(p==='/api/session')return json(res,200,{authenticated:true,local:true,role,user_id:'user-0',preferences:chatPreferences,csrf:'local-fixture',identity:{email:'alex@example.com',name:'Alex Morgan'}});
 if(p==='/api/config')return json(res,200,{missing:[],cloud_ready:true,harness:'claude-agent-sdk',harnesses:[{id:'claude-agent-sdk',name:'Claude Agent SDK',models:[model]}],models:[{id:model,name:'GPT-6 Astra'}],model,execution_engine:'Temporal',execution_connected:true,checkpoint_interval_seconds:600,max_concurrent_runs:100,parallel_agents_enabled:true,max_parallel_agents:100,sandbox_idle_seconds:300,run_timeout_seconds:0});
 if(p==='/api/organization')return json(res,200,{name:'Example team',google_signin:true,activity:[],slack_sessions:{enabled:true,audience:'Workspace members',thread_reply_ready:true,direct_message_ready:true}});
 if(p==='/api/runs')return json(res,200,['Review release readiness','Investigate gateway latency','Update integration tests','Draft the engineering digest'].map((prompt,i)=>({id:String(i+1).repeat(32),prompt,status:'idle',updated_at:stamp,created_at:stamp,children:[]})));
 if(p==='/api/session-folders')return json(res,200,{folders:[]});
 if(p==='/api/connections')return json(res,200,connections);
 if(p==='/api/settings/preferences'){if(req.method==='PUT')chatPreferences={send_immediately:body.send_immediately===true};return json(res,200,chatPreferences);}
 if(p==='/api/settings/session-titles'){if(req.method==='PUT')titleModel=body.model;return json(res,200,{model:titleModel,enabled:true,gateway_configured:true});}
 if(p==='/api/skills')return json(res,200,{skills:empty?[]:skills});
 if(p.startsWith('/api/skills/'))return json(res,200,skills.find(s=>s.id===p.split('/')[3])||{});
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
 if(p==='/api/spend'){
  if(role==='admin')return json(res,200,{...spend,scope:'organization'});
  const own=spendUsers[0],total={...own,prompt_tokens:1440000,completion_tokens:720000};
  return json(res,200,{...spend,scope:'personal',total,priced_requests:own.requests,users:[own],identities:[own],sessions:spend.sessions.filter(s=>s.user_id===own.id),models:[{model,spend:own.spend,requests:own.requests}],infrastructure:undefined,cost_summary:undefined});
 }
 if(p==='/api/admin/identities/status')return json(res,200,{enabled:true,ready:true,missing_scopes:[]});
 if(p==='/api/admin/adoption')return json(res,200,empty?{...adoption,total_requests:0,active_users:0,daily:daily.map(d=>({...d,requests:0,active_users:0,seven_day_average:0})),weekly:{...adoption.weekly,requests:0,previous_requests:0,delta:0,percent_change:null}}:adoption);
 return json(res,501,{detail:'This operation is not available in the visual preview.'});
});
server.listen(port,'127.0.0.1',()=>console.log(`Settings UI fixture at http://127.0.0.1:${port}. Synthetic data; no external services.`));
