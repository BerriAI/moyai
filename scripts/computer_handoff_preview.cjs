/* Local UI transport fixture. No model, cloud computer or SSO request is made. */
const fs=require('node:fs');
const id='c'.repeat(32),reason='Sign in with Google SSO in the computer below, then tell me when you’re done.';
const signIn=`<!doctype html><html lang="en"><meta charset="utf-8"><title>Local SSO fixture</title>
<style>body{margin:0;background:#f3f5f8;color:#162033;font:18px system-ui;display:grid;place-items:center;height:100vh}main{width:440px;padding:40px;background:white;border:1px solid #dce2ea;border-radius:16px}h1{font-size:28px}button{font:inherit;border:1px solid #bac7db;border-radius:8px;padding:15px 22px;background:#edf2fb;width:100%;color:#162033}p{line-height:1.5}small{display:block;color:#64748b;margin-top:32px;font-size:14px}</style>
<main><h1>Sign in to your workspace</h1><p>Your browser is ready. Continue with your organization’s account.</p><button>Google · Example SSO</button><small>Local test screen · No real sign-in or credentials</small></main></html>`;
function create(framePath){
 let event=null,waking=false,awake=false,controller='',timer,wakeTimer;
 const json=(res,value)=>{res.writeHead(200,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(value));};
 const stamp=()=>new Date().toISOString();
 function reset(){event=null;waking=false;awake=false;controller='';clearTimeout(timer);clearTimeout(wakeTimer);}
 function run(){return {id,display_title:'Sign-in handoff · local UI demo',prompt:'Open the workspace and show me where to sign in.',
  status:event?'idle':'running',mode:'modal',chat_enabled:true,harness:'codex',model:'openai/gpt-6-astra',plugins:[],
  owner_id:'user-0',active_user_id:'user-0',active_message_id:10,snapshot_id:'local-fixture',created_at:stamp(),updated_at:stamp(),
  messages:[{id:10,role:'user',status:event?'completed':'running',user_id:'user-0',user_name:'Alex',content:'Open the workspace and show me where to sign in.',created_at:stamp()},
   ...(event?[{id:11,role:'assistant',status:'completed',content:'Please finish signing in through Computer, then tell me “done.”',created_at:stamp()}]:[])],
  events:event?[event]:[],approvals:[],credential_requests:[],artifacts:[],pull_requests:[]};}
 function computer(){return {available:awake,has_sandbox:awake,can_wake:!awake&&!waking,waking,wake_supported:true,
  surface:'desktop',actor:'user-0',controller,tab:'',width:1440,height:900,
  ...(awake&&framePath?{frame:fs.readFileSync(framePath).toString('base64')}:{}),captures:[]};}
 async function route(req,res,url){
  const p=url.pathname;
  if(p==='/demo/sign-in'){res.writeHead(200,{'Content-Type':'text/html'});res.end(signIn);return true;}
  if(p==='/api/runs'){json(res,[run()]);return true;}
  if(p===`/api/runs/${id}`){json(res,run());return true;}
  if(p===`/api/runs/${id}/events`){
   res.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-store','Connection':'keep-alive'});res.write(': Local handoff fixture\n\n');
   const send=()=>res.write('data: '+JSON.stringify(event)+'\n\n');
   if(event&&event.id>Number(url.searchParams.get('after')||0))send();
   else if(!event)timer=setTimeout(()=>{event={id:Date.now(),run_id:id,kind:'computer',message:'Computer input requested',created_at:stamp(),data:{phase:'handoff',version:1,turn_id:10,user_id:'user-0',reason}};send();},5000);
   req.on('close',()=>{clearTimeout(timer);res.end();});return true;
  }
  if(p===`/api/runs/${id}/computer`){
   if(req.method==='POST'){
    let raw='';for await(const chunk of req)raw+=chunk;const body=JSON.parse(raw);
    if(body.action==='wake'){waking=true;wakeTimer=setTimeout(()=>{waking=false;awake=true;},1800);}
    if(body.action==='claim')controller='user-0';
    if(body.action==='release')controller='';
   }
   json(res,computer());return true;
  }
  if(p===`/api/runs/${id}/side-chats`){json(res,[]);return true;}
  if(p===`/api/runs/${id}/files`){json(res,{files:[]});return true;}
  return false;
 }
 return {reset,route};
}
module.exports={create};
