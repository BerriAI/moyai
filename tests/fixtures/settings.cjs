// Real React settings with synthetic accounts only. Shared by browser checks and local visual review.
const path = require('node:path');
const os = require('node:os');
const fs = require('node:fs/promises');
const {randomUUID} = require('node:crypto');
const personalPath = '/api/connections/slack/personal';
const mcpConnection = (patch={}) => ({
  id:'12345678-1234-1234-1234-123456789abc',name:'Research gateway',kind:'mcp',owner_id:'viewer',
  shared:false,read_only:true,enabled:true,revision:1,source:null,
  policy:{require_approval:true,allowed_tools:['lookup'],product:null,mcp:{url:'https://gateway.example.com/mcp',transport:'streamable_http'}},
  ...patch,
});
function defaults() {
  return {
    requests: [], individual: true, role:'admin', userId:'viewer', productMode:'existing', connections:[],
    connectionCheck:{tool_count:1,tools:[{name:'lookup',description:'Search the synthetic research catalog.'}]},
    personal: {available:true,connected:true,oauth_configured:true,label:'Example workspace · alex',health:'healthy',effective_source:'personal',limitation:'Personal Slack reads require your private web chat.'},
    memory: {source:'canonical',scope:{kind:'personal',owner_id:'viewer'},preferences:{enabled:true,auto_save:true,revision:1},
      capabilities:{read:true,create:true,update:true,delete:true,preferences:true},
      runtime:{recall:true,auto_save:true,background_review:true,note:'Saved preferences are separate from native pipeline availability.'},
      review:{enabled:true},note:'Canonical encrypted notes and revisions are retained. Unknown team/connection scopes are not remapped.',
      memories:[{id:'a'.repeat(32),key:'style',title:'Keep project updates concise',content:'Lead with the result and include useful next steps.',kind:'preference',revision:1,updated_at:'2026-10-19T10:00:00Z'}]},
  };
}
function report() {
  const metric={samples:24,mean_ms:1200,p50_ms:900,p95_ms:2400,max_ms:3000,pending:0,missing:0,excluded:0,total:24};
  const summary={turns:24,sessions:8,sessions_active:8,sessions_created:5,requests:48,total_tokens:120000,cost_usd:3.2,pricing_status:'priced',latency_samples:24,mean_latency_ms:25000,p50_latency_ms:21000,p95_latency_ms:40000,prs_created:2,prs_merged:1,pr_status_counts:{open:1,draft:0,closed:0,merged:1,unknown:0}};
  return {rows:[],total:0,offset:0,limit:50,models:['test-model'],users:[],summary,series:[{date:'2026-10-19',...summary}],capabilities:{reported_costs:true,unified_billing:true,pr_analytics:true},
    performance:{dispatch:metric,cards:{startup:metric,queue:metric,broker:metric,computer:metric},
      breakdown:{samples:24,api_to_worker_ms:metric,worker_to_broker_ms:metric,broker_to_dispatch_ms:metric,api_to_dispatch_ms:metric},
      computer:{cold:metric,restore:metric,reuse:metric,requested:24,failed:0,pending:0}}};
}
async function settingsFixture() {
  const {createServer} = await import('vite');
  const fixture = {state:defaults(),reset(){this.state=defaults();}};
  const cache = await fs.mkdtemp(path.join(os.tmpdir(),'moyai-settings-'));
  const server = await createServer({configFile:path.resolve('native/web/vite.config.ts'),cacheDir:cache,server:{host:'127.0.0.1',port:0,strictPort:false},plugins:[{
    name:'settings-fixture',configureServer(vite){vite.middlewares.use(async(req,res,next)=>{
      const url=new URL(req.url,'http://localhost');
      if(url.pathname==='/settings-props.html'){
        const html=await vite.transformIndexHtml(url.pathname,`<!doctype html><html><body><div id="settings-root"></div><script type="module">
          import React from "react";
          import {createRoot} from "react-dom/client";
          import {SettingsPage} from "/settings.tsx";
          import "/@fs/${path.resolve('frontend/theme.css')}";
          const root=createRoot(document.getElementById("settings-root"));
          window.renderSettingsUser=user=>root.render(React.createElement(SettingsPage,{
            user,page:"connections",productMode:"existing",sidebarCollapsed:false,
            onNavigate(){},onBack(){},onStartPrivateChat(){},onToggleSidebar(){},onPreferences(){}
          }));
          window.renderSettingsUser({id:"viewer",name:"Alex Example",role:"admin",individual:true});
        </script></body></html>`);
        res.writeHead(200,{'Content-Type':'text/html','Cache-Control':'no-store'});return res.end(html);
      }
      if(!url.pathname.startsWith('/api/'))return next();
      const state=fixture.state;let raw='';for await(const chunk of req)raw+=chunk;
      const body=raw?JSON.parse(raw):null;
      state.requests.push({path:url.pathname,method:req.method,body});
      const json=(value,status=200)=>{res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(value));};
      if(url.pathname==='/api/me')return json({user:{id:state.userId,name:'Alex Example',role:state.role,individual:state.individual},csrf:'fixture-csrf',models:['test-model'],default_model:'test-model',product_mode:state.productMode});
      if(url.pathname==='/api/history/sessions')return json({sessions:[],ancestors:[],has_more:false});
      if(['/api/session-folders','/api/sessions'].includes(url.pathname))return json([]);
      if(url.pathname==='/api/connections'){
        if(state.connectionsError)return json({detail:'Connections could not be loaded.'},503);
        if(req.method==='POST'){
          if(state.connectionSaveError)return json({detail:state.connectionSaveError},422);
          const {credential,clear_credential,...metadata}=body;
          const row=mcpConnection({...metadata,id:randomUUID(),owner_id:state.userId,policy:{allowed_tools:null,require_approval:true,product:null,mcp:null,...body.policy}});
          state.connections.push(row);return json(row);
        }
        return json(state.connections);
      }
      const connectionMatch=url.pathname.match(/^\/api\/connections\/([^/]+)(\/check)?$/);
      if(connectionMatch){
        const row=state.connections.find(item=>item.id===connectionMatch[1]);
        if(!row)return json({detail:'Connection not found.'},404);
        if(connectionMatch[2]){
          if(state.connectionCheckGate)await state.connectionCheckGate;
          if(state.connectionCheckError)return json({detail:state.connectionCheckError},state.connectionCheckStatus||502);
          return json(state.connectionCheck);
        }
        if(req.method==='PUT'){
          if(state.connectionSaveError)return json({detail:state.connectionSaveError},422);
          const {credential,clear_credential,...metadata}=body;
          Object.assign(row,metadata,{revision:row.revision+1});return json(row);
        }
      }
      if(url.pathname==='/api/settings/performance')return json(report());
      if(url.pathname==='/api/settings/billing')return json({activity:report()});
      if(url.pathname==='/api/settings/general')return json({delivery_mode:'when_idle',connection_ids:null});
      if(url.pathname==='/api/pull-requests/statuses')return json({});
      if(url.pathname==='/api/skills')return json({skills:[],has_more:false});
      if(url.pathname===personalPath){
        if(state.personalError)return json({detail:'Slack status is temporarily unavailable.'},503);
        if(req.method==='DELETE')state.personal={...state.personal,connected:false,health:'absent',label:'',effective_source:'organization'};
        return json(state.personal);
      }
      if(url.pathname===personalPath+'/check')return state.checkError?json({detail:'Reconnect your Slack account to restore access.'},409):json(state.personal);
      if(url.pathname===personalPath+'/oauth')return json({url:'/?fixture-oauth=1#settings/personal-slack'});
      if(url.pathname==='/api/product/memory')return json(state.memory);
      if(url.pathname==='/api/product/memory/preferences'){
        state.memory.preferences={...state.memory.preferences,...body,revision:state.memory.preferences.revision+1};
        state.memory.runtime={...state.memory.runtime,recall:body.enabled,auto_save:body.enabled&&body.auto_save};return json(state.memory.preferences);
      }
      if(url.pathname==='/api/product/skills')return json({source:'canonical',capabilities:{read:true},note:'Internal migration note',skills:[],has_more:false,next_offset:null});
      if(url.pathname==='/api/product/connections')return json([{id:'slack',connected:true,enabled:true,read_only:true,label:'Example workspace',identity:'Workspace app',tools:[],check_status:'healthy',checked_at:'2026-10-19T10:00:00Z'}]);
      if(url.pathname==='/api/credentials')return json({secrets:[],providers:[{id:'generic',name:'Other service'}],capabilities:{create:true,update:true,delete:true,native_execution:true,scopes:['personal','organization'],lifetimes:['persistent','session']},note:'Internal migration note'});
      if(url.pathname==='/api/product/automations')return json({source:'canonical_product',automations:[],next_offset:null,capabilities:{create:false},event_choices:{}});
      if(url.pathname==='/api/settings/users')return json({source:'product',users:[],capabilities:{roles:true},note:'Internal migration note'});
      return json({detail:'Fixture route missing: '+url.pathname},404);
    });},
  }]});
  await server.listen();fixture.base=`http://127.0.0.1:${server.httpServer.address().port}`;
  fixture.close=async()=>{await server.close();await fs.rm(cache,{recursive:true,force:true});};return fixture;
}
module.exports={settingsFixture,personalPath,mcpConnection};
if(require.main===module)settingsFixture().then(fixture=>console.log(fixture.base));
