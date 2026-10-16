// Source/server-component verification only; no browser or shared preview is controlled.
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {spawnSync}=require('node:child_process');
const ts=require('typescript');
const React=require('react');
const {renderToStaticMarkup}=require('react-dom/server');
const root=path.resolve(__dirname,'..'),nativeId='12345678-1234-1234-1234-123456789abc',legacyId='a'.repeat(32);
let server,ui,homeSubmission;
test.before(async()=>{
  const {createServer}=await import('vite');
  server=await createServer({configFile:path.join(root,'native/web/vite.config.ts'),optimizeDeps:{noDiscovery:true,include:[]},server:{middlewareMode:true},appType:'custom'});
  ui=await server.ssrLoadModule('/settings.tsx');
  // Import the actual pure admission helper without evaluating the home canvas bootstrap.
  const source=ts.createSourceFile('home.tsx',fs.readFileSync(path.join(root,'native/web/home.tsx'),'utf8'),ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
  const fn=source.statements.find(node=>ts.isFunctionDeclaration(node)&&node.name?.text==='homeSubmission');assert.ok(fn);
  const code=ts.transpileModule(fn.getText(source),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext}}).outputText;
  ({homeSubmission}=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64')));
});
test.after(async()=>{await server?.close();});
const render=(Component,props)=>renderToStaticMarkup(React.createElement(Component,props));
const note=(patch={})=>({id:legacyId,key:'note-original',title:'Original personal note',content:'Preserved memory <img src=x onerror=bad()>',kind:'project',repo_url:'https://github.com/berriai/moyai',repository_id:42,revision:9,updated_at:'2026-10-15T00:00:00Z',expires_at:'2000-01-01T00:00:00Z',...patch});
const secret=(patch={})=>({id:legacyId,provider:'generic',label:'Stored access',name:'service-access',format:'env',env_var:'',scope:'personal',lifetime:'persistent',revision:6,status:'active',can_manage:true,...patch});
const draft=(patch={})=>({provider:'generic',label:'Service access',name:'service-access',format:'env',env_var:'',scope:'personal',lifetime:'persistent',root_id:'',expires_at:'',value:'{"SERVICE_TOKEN":"synthetic-value"}',...patch});
const caps={create:true,update:true,delete:true,scopes:['personal','organization'],lifetimes:['persistent','session'],native_execution:false};

test('canonical memory body preserves identity/revision and drops stale repository IDs on scope change',()=>{
  const original=note(),values={title:'Changed title',content:'Updated note body',kind:'project',repo_url:original.repo_url,owner_id:'other',namespace:'other/private'};
  const body=ui.productMemoryWrite(original,values,'request-0001');
  assert.equal(body.key,'note-original');assert.equal(body.revision,9);assert.equal(body.repository_id,42);assert.equal(body.request_id,'request-0001');
  assert.equal(body.owner_id,undefined);assert.equal(body.namespace,undefined);
  assert.equal(ui.productMemoryWrite(original,{...values,repo_url:'https://github.com/berriai/litellm'},'request-0001').repository_id,undefined);
  assert.equal(ui.productMemoryWrite(original,{...values,repo_url:''},'request-0001').repository_id,undefined);
  assert.equal(ui.productMemoryWrite(undefined,values,'request-0001').key,'note-request-0001');
});

test('memory and skill provenance links are source-qualified, revision-bound and reject traversal',()=>{
  assert.equal(ui.productMemorySourceLink({engine:'legacy',session_id:legacyId,url:'/#run='+legacyId}),'#run='+legacyId);
  assert.equal(ui.productMemorySourceLink({engine:'native',session_id:nativeId,url:'/#'+nativeId}),'#'+nativeId);
  assert.equal(ui.productMemorySourceLink({run_id:legacyId}),undefined,'Do not guess raw stored provenance');
  assert.equal(ui.productMemorySourceLink({engine:'legacy',session_id:legacyId,url:'https://evil.test/#run='+legacyId}),undefined);
  assert.equal(ui.productSkillFileUrl(legacyId,'references/東京 notes.md',7),`/api/product/skills/${legacyId}/files/references/%E6%9D%B1%E4%BA%AC%20notes.md?revision=7`);
  for(const file of ['../other','/absolute','a/../../b','a\\b','a\u0000b'])assert.equal(ui.productSkillFileUrl(legacyId,file,7),undefined);
  assert.equal(ui.productSkillFileUrl('..','a.md',7),undefined);assert.equal(ui.productSkillFileUrl(legacyId,'a.md',0),undefined);
});

test('canonical note view retains expiry/content and does not expose editing for unavailable capabilities or truncated text',()=>{
  const html=render(ui.ProductMemoryNotes,{notes:[note({truncated:true})],capabilities:{update:true,delete:false},onEdit(){},onRemove(){}});
  assert.match(html,/Expired/);assert.match(html,/Original personal note/);assert.match(html,/&lt;img/);assert.doesNotMatch(html,/<img|Review &amp; edit|Delete/);
});

test('retained skill library displays original references and archive state without runtime-use/mutation claims',()=>{
  const html=render(ui.ProductSkillRows,{skills:[{id:legacyId,name:'review',description:'Saved instructions',scope:'organization',revision:7,archived:true,reference:'org:review',can_manage:false}],onView(){}});
  assert.match(html,/Organization/);assert.match(html,/Archived/);assert.match(html,/org:review/);assert.match(html,/View instructions/);
  assert.doesNotMatch(html,/Use in chat|Save skill|Delete skill|Restore/);
});

test('account metadata preserves canonical provider identity and member-only view actions',()=>{
  const html=render(ui.ProductConnectionRows,{admin:false,connections:[{id:'github',connected:true,scope:'organization',oauth_configured:true,label:'BerriAI/moyai',identity:'Organization GitHub App',enabled:false,read_only:true,tools:[]}],onManage(){}});
  assert.match(html,/Organization GitHub App/);assert.match(html,/Paused/);assert.match(html,/Read-only policy/);assert.match(html,/View access/);assert.doesNotMatch(html,/>Manage</);
});

test('OAuth/setup destinations allow authenticated local handoff and HTTPS, rejecting script/credential URLs',()=>{
  assert.equal(ui.productRedirect('/auth/github/register?state=synthetic','http://127.0.0.1:8807'),'http://127.0.0.1:8807/auth/github/register?state=synthetic');
  assert.equal(ui.productRedirect('https://github.com/apps/example/installations/new','https://moyai.test'),'https://github.com/apps/example/installations/new');
  for(const value of ['javascript:alert(1)','data:text/html,bad','https://user:password@example.test/','http://external.test/','https://example.test/ bad',''])assert.throws(()=>ui.productRedirect(value,'https://moyai.test'));
});

test('original credential metadata remains masked, and AWS/login or other-owner entries cannot be edited',()=>{
  const rows=[secret({value:'NEVER_RENDER_THIS'}),secret({id:'b'.repeat(32),format:'aws-sso',name:'aws-sso/work'}),secret({id:'c'.repeat(32),label:'Other owner',can_manage:false})];
  const html=render(ui.ProductSecretRows,{secrets:rows,providers:[],capabilities:caps,onEdit(){},onRevoke(){}});
  assert.doesNotMatch(html,/NEVER_RENDER_THIS/);assert.equal((html.match(/aria-label="Value hidden"/g)||[]).length,3);
  assert.equal((html.match(/>Edit</g)||[]).length,1);assert.equal((html.match(/>Revoke access</g)||[]).length,2);
  assert.match(html,/AWS SSO access is renewed/);
});

test('credential mutations preserve source revision, explicit sharing/lifetime, and blank replacement semantics',()=>{
  const body=ui.productCredentialBody(draft({value:''}),secret(),'ignored');
  assert.equal(body.revision,6);assert.equal(body.value,undefined);assert.equal(body.provider,undefined);assert.equal(body.client_id,undefined);
  const created=ui.productCredentialBody(draft(),undefined,'request-0001');assert.equal(created.client_id,'request-0001');assert.equal(created.scope,'personal');assert.equal(created.lifetime,'persistent');
  assert.throws(()=>ui.productCredentialBody(draft({scope:''}),undefined,'request-0001'),/who can use/);
  assert.throws(()=>ui.productCredentialBody(draft({lifetime:''}),undefined,'request-0001'),/future sessions/);
  assert.throws(()=>ui.productCredentialBody(draft(),secret({format:'aws-sso'}),'request-0001'),/AWS SSO/);
});

test('credential session roots never alias original history and native IDs',()=>{
  assert.equal(ui.productCredentialRoot({source:'legacy',id:legacyId}),legacyId);
  assert.equal(ui.productCredentialRoot({source:'native',id:nativeId}),'native:'+nativeId);
  assert.throws(()=>ui.productCredentialRoot({source:'native',id:legacyId}));
  assert.throws(()=>ui.productCredentialBody(draft({lifetime:'session',root_id:nativeId}),undefined,'request-0001'),/Choose a session/);
  assert.equal(ui.productCredentialBody(draft({lifetime:'session',root_id:'native:'+nativeId}),undefined,'request-0001').root_id,'native:'+nativeId);
});

test('Home first message includes only eligible selected account IDs and stable retry identity',()=>{
  const accounts=[{id:'organization',name:'Organization',enabled:true,shared:true,read_only:false,credential:'DO_NOT_SEND'}, {id:'personal',name:'Personal Slack',enabled:true,shared:false,read_only:true}];
  const result=homeSubmission('Task','model',true,['image'],['personal','organization'],accounts);
  assert.deepEqual(result.body,{prompt:'Task',model:'model',attachments:['image'],connections:['organization','personal']});assert.equal(result.visibility,'private');
  assert.equal(result.signature,homeSubmission('Task','model',true,['image'],['organization','personal','personal'],accounts).signature);
  assert.doesNotMatch(JSON.stringify(result),/DO_NOT_SEND/);
  assert.throws(()=>homeSubmission('Task','model',false,[],['personal'],accounts),/Account access changed/);
  assert.throws(()=>homeSubmission('Task','model',true,[],['stale-grant'],accounts),/Account access changed/);
  assert.equal(homeSubmission('Task','model',false,[],['organization'],accounts).visibility,'shared');
});

const python=path.join(root,'.venv/bin/python');
test('UI note/credential payloads validate against canonical and native-root Python schemas',{skip:!fs.existsSync(python)&&'Native Python environment not installed'},()=>{
  const payload={note:ui.productMemoryWrite(note(),{title:'Saved title',content:'Saved note content',kind:'preference',repo_url:''},'request-0001'),create:ui.productCredentialBody(draft({root_id:'native:'+nativeId,lifetime:'session'}),undefined,'request-0001'),update:ui.productCredentialBody(draft({value:''}),secret(),'ignored')};
  const result=spawnSync(python,['-c','import json,sys; from app.memory import Note; from moyai.product_secrets import ProductSaveSecret,ProductUpdateSecret; v=json.load(sys.stdin); Note.model_validate(v["note"]); ProductSaveSecret.model_validate(v["create"]); ProductUpdateSecret.model_validate(v["update"]); print("validated")'],{cwd:root,input:JSON.stringify(payload),encoding:'utf8'});
  assert.equal(result.status,0,result.stderr);assert.equal(result.stdout.trim(),'validated');
});
