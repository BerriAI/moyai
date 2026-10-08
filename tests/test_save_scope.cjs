const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const {test}=require('node:test');
const vm=require('./helpers/ui-vm.cjs');
const flush=()=>new Promise(resolve=>setImmediate(resolve));

function fixture({kind='skill',admin=true,saved=[],skill=null,rootId='root-one',submitError=null,runs=[],userId='google:owner'}={}){
  const elements=new Map(),posts=[],gets=[],submit={disabled:false},closeButton={};
  const decode=value=>value.replaceAll('&quot;','"').replaceAll('&lt;','<').replaceAll('&gt;','>').replaceAll('&amp;','&');
  const parseOptions=body=>Array.from(body.matchAll(/<option\b([^>]*)>([^<]*)<\/option>/g),m=>({
    value:decode(m[1].match(/value="([^"]*)"/)?.[1]||''),selected:/\bselected\b/.test(m[1]),disabled:/\bdisabled\b/.test(m[1]),text:decode(m[2])}));
  const element=id=>{
    if(elements.has(id))return elements.get(id);
    let html='',children=[];
    const el={id,value:'',textContent:'',disabled:false,required:false,hidden:false,dataset:{},
      focus(){this.focused=true;},reset(){},showModal(){this.open=true;},close(){this.open=false;this.onclose?.();},
      removeAttribute(name){delete this[name];},
      querySelector:selector=>selector==='[type="submit"]'?submit:closeButton,
      querySelectorAll:()=>[],
      get innerHTML(){return html;},
      set innerHTML(value){
        const remove=name=>{const child=elements.get(name);if(child)child.innerHTML='';elements.delete(name);};
        children.forEach(remove);html=value;children=[];
        for(const match of value.matchAll(/<([a-z]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)){
          const [,tag,attrs,name]=match,field=element(name);children.push(name);
          field.tagName=tag.toUpperCase();field.required=/\brequired\b/.test(attrs);field.disabled=/\bdisabled\b/.test(attrs);field.hidden=/\bhidden\b/.test(attrs);field.open=/\bopen\b/.test(attrs);
          field.value=decode(attrs.match(/\bvalue="([^"]*)"/)?.[1]||'');field.type=attrs.match(/\btype="([^"]*)"/)?.[1]||'';field.checked=/\bchecked\b/.test(attrs);
        }
        // Derive native initial values from the actual rendered HTML.
        for(const match of value.matchAll(/<(select|textarea)\b([^>]*\bid="([^"]+)"[^>]*)>([\s\S]*?)<\/\1>/g)){
          const [,tag,attrs,name,body]=match,field=element(name);
          if(tag==='select'){
            field.options=parseOptions(body);
            field.value=(field.options.find(o=>o.selected)||field.options[0])?.value||'';
          }else field.value=decode(body);
        }
        if(this.tagName==='SELECT'){this.options=parseOptions(value);this.value=(this.options.find(o=>o.selected)||this.options[0])?.value||'';}
      }};
    elements.set(id,el);return el;
  };
  for(const id of ['skill-dialog','credential-dialog','credential-form','content'])element(id);
  const ctx={state:{role:admin?'admin':'member',selected:'run-one',pageVersion:1,userId},
    $:selector=>elements.get(selector.slice(1))||null,
    document:{addEventListener(){},querySelector:selector=>elements.get(selector.slice(1))||null,querySelectorAll:()=>[]},
    crypto:{randomUUID:()=> 'request-unique-id'},URL,Date,MoyaiIcon:()=>'<svg aria-hidden="true"></svg>',
    esc:value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),relative:()=> 'just now',
    api:async(path,options)=>{
      if(options){posts.push({path,method:options.method,body:options.body?JSON.parse(options.body):null});if(submitError)throw new Error(submitError);return {};}
      gets.push(path);
      if(path.startsWith('/api/runs'))return typeof runs==='function'?runs():runs;
      if(path.startsWith('/api/credentials'))return {providers:[{id:'fireworks',name:'Fireworks',setup_url:'https://example.com'},{id:'generic',name:'Other service',setup_url:''}],secrets:saved,root_id:rootId};
      return skill;
    },toast(){},refreshChat:async()=>{},showError(){},
  };
  const app=readFileSync('app/static/app.js','utf8');
  vm.createContext(ctx);vm.runInContext(readFileSync('app/static/skill-icons.js','utf8'),ctx);
  vm.runInContext(app.slice(app.indexOf('function sessionTitle('),app.indexOf('function modelName('))+readFileSync('app/static/'+(kind==='skill'?'skills.js':'credentials.js'),'utf8'),ctx);
  return {ctx,element,posts,gets,submit,elements};
}

test('request selector maps each exclusive choice to explicit sharing and reuse',async()=>{
  for(const [choice,scope,lifetime] of [['session','personal','session'],['personal','personal','persistent'],['organization','organization','persistent']]){
    const f=fixture({kind:'key'});
    await f.ctx.openCredentialDialog({id:'key',provider:'fireworks',reason:'Benchmark',can_personal:true,can_organization:true});
    assert.equal(f.element('secret-scope').value,'personal');assert.equal(f.element('secret-lifetime').value,'persistent');
    const radio=f.element('secret-use-'+choice);radio.checked=true;radio.onchange();
    f.element('secret-value').value='synthetic-key';
    await f.element('credential-form').onsubmit({preventDefault(){}});
    assert.equal(f.posts[0].body.scope,scope);assert.equal(f.posts[0].body.lifetime,lifetime);
  }
});

test('an admin supplying a teammate request must explicitly choose organization',async()=>{
  const f=fixture({kind:'key'});
  await f.ctx.openCredentialDialog({id:'key',provider:'fireworks',reason:'Benchmark',can_personal:false,can_organization:true});
  assert.equal(f.element('secret-use-personal').disabled,true);assert.equal(f.element('secret-use-session').disabled,true);
  assert.equal(f.element('secret-scope').value,'');
  f.element('secret-value').value='synthetic-key';await f.element('credential-form').onsubmit({preventDefault(){}});
  assert.equal(f.posts.length,0);
});

test('single token input is masked and serialized without asking the user for JSON',async()=>{
  const f=fixture({kind:'key'});
  await f.ctx.openCredentialDialog({id:'token',provider:'generic',name:'service',format:'env',reason:'Read service logs',can_personal:true,input_fields:[{name:'SERVICE_TOKEN',label:'Access token',secret:true,required:true}]});
  assert.equal(f.element('secret-input-0').type,'password');assert.equal(f.elements.has('secret-value'),false);
  f.element('secret-input-0').value='test-token-with-"quotes';
  assert.equal(f.element('secret-scope').value,'personal');assert.equal(f.element('secret-lifetime').value,'persistent');
  f.element('secret-scope').value='personal';f.element('secret-lifetime').value='session';
  await f.element('credential-form').onsubmit({preventDefault(){}});
  assert.deepEqual(JSON.parse(f.posts[0].body.value),{SERVICE_TOKEN:'test-token-with-"quotes'});
  assert.equal(f.elements.has('secret-input-0'),false);
});

test('AWS fields enforce required values and omit an empty optional session token',async()=>{
  const f=fixture({kind:'key'});
  await f.ctx.openCredentialDialog({id:'aws',provider:'generic',name:'aws',format:'env',reason:'Investigate AWS logs',can_personal:true});
  assert.equal(f.elements.has('secret-value'),false);
  assert.equal(f.element('secret-input-1').type,'password');assert.equal(f.element('secret-input-2').required,false);
  f.element('secret-scope').value='personal';f.element('secret-lifetime').value='persistent';
  await f.element('credential-form').onsubmit({preventDefault(){}});assert.equal(f.posts.length,0);
  f.element('secret-input-0').value='test-id';f.element('secret-input-1').value='test-secret';f.element('secret-input-3').value='us-west-2';
  await f.element('credential-form').onsubmit({preventDefault(){}});
  assert.deepEqual(JSON.parse(f.posts[0].body.value),{AWS_ACCESS_KEY_ID:'test-id',AWS_SECRET_ACCESS_KEY:'test-secret',AWS_DEFAULT_REGION:'us-west-2'});
});

test('saved access and decline never transmit labeled input drafts',async()=>{
  const saved={id:'existing',provider:'generic',name:'service',format:'env',label:'Saved',scope:'personal'};
  for(const decline of [true,false]){
    const f=fixture({kind:'key',saved:[saved]});
    await f.ctx.openCredentialDialog({id:'token',provider:'generic',name:'service',format:'env',reason:'Read logs',can_personal:true,input_fields:[{name:'SERVICE_TOKEN',label:'Access token'}]});
    f.element('secret-input-0').value='draft-only';
    if(decline)await f.element('decline-secret').onclick();
    else{f.element('secret-source').value='existing';f.element('secret-source').onchange();assert.equal(f.element('secret-input-0').value,'');assert.equal(f.element('secret-input-0').disabled,true);await f.element('credential-form').onsubmit({preventDefault(){}});}
    assert.equal('value' in f.posts[0].body,false);
  }
});

test('new skill asks for scope and cannot submit until a choice is made',async()=>{
  const f=fixture();await f.ctx.openSkillEditor();
  const scope=f.element('skill-scope'),form=f.element('skill-form');
  assert.equal(scope.value,'');assert.equal(scope.required,true);
  await form.onsubmit({preventDefault(){},currentTarget:form});
  assert.equal(f.posts.length,0);assert.match(f.element('skill-form-error').textContent,/Choose Personal or Organization/);
  scope.value='organization';f.element('skill-name').value='team';f.element('skill-description').value='Team workflow';f.element('skill-instructions').value='Run the workflow.';
  await form.onsubmit({preventDefault(){},currentTarget:form});
  assert.equal(f.posts.length,1);assert.equal(f.posts[0].body.scope,'organization');
});

test('editing preserves the existing choice; members see that organization requires an admin',async()=>{
  const edit=fixture({skill:{id:'skill-one',name:'team',description:'Team workflow',instructions:'Workflow instructions',scope:'organization',revision:2,can_manage:true}});
  await edit.ctx.openSkillEditor('skill-one');assert.equal(edit.element('skill-scope').value,'organization');
  const member=fixture({admin:false});await member.ctx.openSkillEditor();
  assert.equal(member.element('skill-scope').value,'');
  assert.equal(member.element('skill-scope').options.find(o=>o.value==='organization').disabled,true);
});

test('new key requires scope before transmitting or clearing the secret',async()=>{
  const f=fixture({kind:'key'});await f.ctx.openCredentialDialog();
  const scope=f.element('secret-scope'),form=f.element('credential-form'),value=f.element('secret-value');
  assert.equal(scope.value,'');assert.equal(scope.required,true);
  value.value='test-provider-key';form.onsubmit({preventDefault(){}});await flush();
  assert.equal(f.posts.length,0);assert.equal(value.value,'test-provider-key');
  assert.match(f.element('secret-form-error').textContent,/Choose Personal/);
  scope.value='personal';form.onsubmit({preventDefault(){}});await flush();
  assert.equal(f.posts.length,0);assert.equal(value.value,'test-provider-key');
  assert.match(f.element('secret-form-error').textContent,/Choose when/);
  const lifetime=f.element('secret-lifetime');assert.equal(lifetime.required,true);assert.equal(lifetime.value,'');
  assert.equal(lifetime.options.some(o=>o.value==='session'),true);
  lifetime.value='persistent';form.onsubmit({preventDefault(){}});await flush();
  assert.equal(f.posts.length,1);assert.equal(f.posts[0].body.lifetime,'persistent');assert.equal(f.posts[0].body.scope,'personal');assert.equal(value.value,'');
});

test('choosing a saved key disables the unused scope field and keeps its existing scope',async()=>{
  const f=fixture({kind:'key',saved:[{id:'existing-key',provider:'fireworks',label:'Team key',scope:'organization'}]});
  await f.ctx.openCredentialDialog({id:'request-one',provider:'fireworks',reason:'Benchmark',can_personal:true});
  const source=f.element('secret-source'),scope=f.element('secret-scope');
  source.value='existing-key';source.onchange();
  assert.equal(scope.required,false);assert.equal(scope.disabled,true);
  assert.equal(f.element('secret-lifetime').required,false);assert.equal(f.element('secret-lifetime').disabled,true);
  assert.equal(f.elements.has('secret-expiry'),false);assert.equal(f.element('secret-value').disabled,true);
  f.element('credential-form').onsubmit({preventDefault(){}});await flush();
  assert.equal(f.posts[0].body.secret_id,'existing-key');assert.equal('scope' in f.posts[0].body,false);
});


test('declining a key request does not require a scope or transmit a draft key',async()=>{
  const f=fixture({kind:'key'});
  await f.ctx.openCredentialDialog({id:'request-one',provider:'fireworks',reason:'Benchmark',can_personal:true});
  assert.equal(f.element('secret-scope').value,'personal');
  assert.equal(f.element('secret-use-session').value,'session');
  assert.equal(f.element('secret-lifetime').value,'persistent');
  f.element('secret-value').value='do-not-transmit-this-draft';
  f.element('decline-secret').onclick();await flush();
  assert.equal(f.posts[0].body.decision,'decline');assert.equal(f.posts[0].body.generation,0);assert.equal('scope' in f.posts[0].body,false);
  assert.equal('value' in f.posts[0].body,false);assert.equal(f.element('secret-value').value,'');
});

test('generic environment access submits its identity, value, and two explicit choices',async()=>{
  const f=fixture({kind:'key'});await f.ctx.openCredentialDialog();
  const provider=f.element('secret-provider');provider.value='generic';provider.onchange();
  assert.equal(f.element('secret-provider-field').hidden,false);assert.equal(f.element('secret-generic-fields').hidden,false);
  assert.equal(f.element('secret-name').required,true);assert.equal(f.element('secret-name').disabled,false);assert.equal(f.element('secret-env-var').disabled,true);
  assert.equal(f.elements.has('secret-optional-options'),false);
  assert.equal(f.element('secret-value').tagName,'TEXTAREA');
  f.element('secret-name').value='metrics';f.element('secret-value').value='{"SERVICE_TOKEN":"synthetic-token"}';
  f.element('secret-scope').value='organization';f.element('secret-lifetime').value='persistent';
  await f.element('credential-form').onsubmit({preventDefault(){}});
  assert.deepEqual(f.posts[0].body,{provider:'generic',name:'metrics',format:'env',label:'metrics',scope:'organization',lifetime:'persistent',expires_at:'',value:'{"SERVICE_TOKEN":"synthetic-token"}',client_id:'request-unique-id'});
  assert.equal(f.elements.has('secret-value'),false);
});

test('a generic file request can grant organization access for this session only',async()=>{
  const f=fixture({kind:'key'});
  await f.ctx.openCredentialDialog({id:'request-one',provider:'generic',name:'cluster',format:'file',env_var:'KUBECONFIG',reason:'Investigate the cluster',can_personal:true,can_organization:true,generation:2});
  assert.equal(f.element('secret-provider-field').hidden,true);assert.equal(f.element('secret-generic-fields').hidden,true);
  for(const name of ['secret-provider','secret-name','secret-format','secret-env-var']){
    assert.equal(f.element(name).disabled,true);assert.equal(f.element(name).required,false);
  }
  assert.equal(f.element('secret-env-var').value,'KUBECONFIG');
  assert.equal(f.elements.has('secret-optional-options'),false);
  assert.equal(f.elements.has('secret-expiry'),false);assert.equal(f.element('secret-value').required,true);
  assert.equal(f.element('secret-scope').required,true);assert.equal(f.element('secret-lifetime').required,true);
  const file=f.element('secret-file');file.value='synthetic-upload';
  await file.onchange({target:{...file,files:[{size:30,text:async()=> 'apiVersion: v1\nclusters: []\n'}]}});
  assert.equal(f.element('secret-value').value,'apiVersion: v1\nclusters: []\n');
  f.element('secret-scope').value='organization';f.element('secret-lifetime').value='session';
  await f.element('credential-form').onsubmit({preventDefault(){}});
  assert.equal(f.posts[0].path,'/api/credentials/requests/request-one');
  assert.deepEqual(f.posts[0].body,{decision:'provide',generation:2,scope:'organization',lifetime:'session',label:'',expires_at:'',value:'apiVersion: v1\nclusters: []\n'});
});

test('only usable matching saved access from the current root is offered',async()=>{
  const base={provider:'generic',name:'cluster',format:'file',env_var:'KUBECONFIG',label:'Cluster',scope:'organization',lifetime:'persistent',status:'active'};
  const saved=[{...base,id:'valid'},{...base,id:'expired',status:'expired'},{...base,id:'invalid',status:'invalid'},
    {...base,id:'past-date',expires_at:'2020-01-01T00:00:00Z'},{...base,id:'wrong-name',name:'other'},
    {...base,id:'wrong-format',format:'env'},{...base,id:'wrong-variable',env_var:'OTHER_CONFIG'},
    {...base,id:'foreign-session',lifetime:'session',root_id:'other-root'},
    {...base,id:'current-session',lifetime:'session',root_id:'root-one'},
    {...base,id:'legacy-session',scope:'session',lifetime:undefined,root_id:'root-one'}];
  const f=fixture({kind:'key',saved});
  await f.ctx.openCredentialDialog({id:'request-one',provider:'generic',name:'cluster',format:'file',env_var:'KUBECONFIG',reason:'Investigate',can_personal:true});
  assert.deepEqual(Array.from(f.element('secret-source').options,o=>o.value),['','valid','current-session','legacy-session']);
  f.element('secret-value').value='draft-file';
  f.element('secret-source').value='current-session';f.element('secret-source').onchange();
  assert.equal(f.element('secret-value').value,'');assert.equal(f.element('secret-value').required,false);
  await f.element('credential-form').onsubmit({preventDefault(){}});
  assert.deepEqual(f.posts[0].body,{decision:'provide',generation:0,secret_id:'current-session'});
});

test('editing metadata keeps the secret and exact expiry; replacement is explicit',async()=>{
  const saved={id:'saved-one',provider:'fireworks',label:'Saved key',scope:'personal',lifetime:'persistent',revision:4,can_manage:true,expires_at:'2099-01-01T02:03:45+00:00'};
  const f=fixture({kind:'key',saved:[saved]});await f.ctx.openCredentialDialog(null,{...saved,revision:1});
  assert.equal(f.element('secret-value').type,'password');assert.equal(f.element('secret-value').value,'');
  assert.equal(f.element('secret-lifetime').value,'persistent');assert.equal(f.element('secret-lifetime').options.some(o=>o.value==='session'),true);
  f.element('secret-label').value='Renamed key';
  await f.element('credential-form').onsubmit({preventDefault(){}});
  assert.equal(f.posts[0].method,'PATCH');assert.equal(f.posts[0].path,'/api/credentials/secrets/saved-one');
  assert.deepEqual(f.posts[0].body,{revision:4,label:'Renamed key',scope:'personal',lifetime:'persistent',expires_at:saved.expires_at});
  await f.ctx.openCredentialDialog(null,saved);f.element('secret-value').value='replacement-key';f.element('secret-expiry').value='';
  await f.element('credential-form').onsubmit({preventDefault(){}});
  assert.equal(f.posts[1].body.value,'replacement-key');assert.equal(f.posts[1].body.expires_at,'');
});

test('editing a legacy session credential preserves its root and separates personal sharing',async()=>{
  const saved={id:'saved-session',provider:'fireworks',label:'Session key',scope:'session',root_id:'root-original',revision:2,can_manage:true};
  const f=fixture({kind:'key',saved:[saved]});await f.ctx.openCredentialDialog(null,saved);
  assert.equal(f.element('secret-scope').value,'personal');assert.equal(f.element('secret-lifetime').value,'session');
  assert.equal(f.element('secret-root').value,'root-original');assert.ok(f.gets.includes('/api/runs?focus=root-original'));
  assert.deepEqual(Array.from(f.element('secret-root').options,o=>o.value),['','root-original']);
  await f.element('credential-form').onsubmit({preventDefault(){}});
  assert.equal(f.posts[0].body.root_id,'root-original');assert.equal(f.posts[0].body.scope,'personal');assert.equal(f.posts[0].body.lifetime,'session');
});

test('closing or switching a form cannot populate a new form with an earlier file read',async()=>{
  const f=fixture({kind:'key'});await f.ctx.openCredentialDialog();
  f.element('secret-provider').value='generic';f.element('secret-provider').onchange();
  f.element('secret-format').value='file';f.element('secret-format').onchange();
  let finishRead;const result=new Promise(resolve=>{finishRead=resolve;});
  const file=f.element('secret-file'),reading=file.onchange({target:{...file,files:[{size:10,text:()=>result}]}});
  f.element('credential-dialog').close();await f.ctx.openCredentialDialog();
  finishRead('private-old-file');await reading;
  assert.equal(f.element('secret-value').type,'password');assert.equal(f.element('secret-value').value,'');
  assert.equal(file.value,'');
});

test('credential drafts are cleared on failed submission and format change',async()=>{
  const f=fixture({kind:'key',submitError:'The credential needs updating.'});await f.ctx.openCredentialDialog();
  f.element('secret-scope').value='personal';f.element('secret-lifetime').value='persistent';f.element('secret-value').value='private-draft';
  await f.element('credential-form').onsubmit({preventDefault(){}});
  assert.equal(f.element('secret-value').value,'');assert.equal(f.submit.disabled,false);
  assert.equal(f.element('secret-form-error').textContent,'The credential needs updating.');
  f.element('secret-value').value='new-draft';f.element('secret-provider').value='generic';f.element('secret-provider').onchange();
  assert.equal(f.element('secret-value').value,'');
});

test('members cannot choose organization sharing; setup URLs cannot execute scripts',async()=>{
  const f=fixture({kind:'key',admin:false});await f.ctx.openCredentialDialog();
  assert.equal(f.element('secret-scope').options.find(o=>o.value==='organization').disabled,true);
  assert.equal(f.ctx.credentialSetupUrl('javascript:alert(1)'), '');
  assert.equal(f.ctx.credentialSetupUrl('https://example.com/connect'), 'https://example.com/connect');
});

test('compact access card defers setup details to the secure form',async()=>{
  const f=fixture({kind:'key'}),target=f.element('credential-requests');
  const request={id:'aws',provider:'generic',name:'aws dev access',reason:'Investigate the outage. Longer permissions explanation.',format:'env',can_personal:true,
    setup_url:'https://docs.aws.amazon.com/singlesignon/latest/userguide/howtogetcredentials.html',
    setup_instructions:'Open your AWS access portal.\nChoose the permitted account and role. <script>not executable</script>'};
  f.ctx.renderCredentialRequests([request]);
  assert.match(target.innerHTML,/Provide Secret/);assert.match(target.innerHTML,/aria-controls="credential-dialog"/);
  assert.match(target.innerHTML,/Credentials requested:/);assert.doesNotMatch(target.innerHTML,/Longer permissions explanation/);
  assert.doesNotMatch(target.innerHTML,/How to get access|<script>|<a\b|<input|<textarea/);
  assert.equal(f.element('credential-dialog').open,undefined);
  await f.ctx.openCredentialDialog(request);
  assert.equal(f.element('secret-setup').href,request.setup_url);assert.equal(f.element('secret-setup-row').hidden,false);
  assert.match(f.element('credential-form').innerHTML,/Choose the permitted account and role/);
  assert.match(f.element('credential-form').innerHTML,/Longer permissions explanation/);
  assert.match(f.element('credential-form').innerHTML,/&lt;script>/);
});

test('missing or unsafe setup URLs never navigate to Moyai or leave a stale form link',async()=>{
  const f=fixture({kind:'key'}),target=f.element('credential-requests');
  const request={id:'service',provider:'generic',name:'internal service',reason:'Investigate the outage',format:'env',can_personal:true,
    setup_instructions:'Ask the service administrator for access.'};
  for(const url of ['', '/', '#', '/#run=somewhere', 'javascript:alert(1)', 'http://example.com',
    'https://user:password@example.com', 'https://example.com/white space', 'https://example.com\\path']){
    assert.equal(f.ctx.credentialSetupUrl(url),'');
    f.ctx.renderCredentialRequests([{...request,setup_url:url}]);
    assert.doesNotMatch(target.innerHTML,/<a\b/);assert.match(target.innerHTML,/Provide Secret/);
  }
  await f.ctx.openCredentialDialog({...request,setup_url:'https://example.com/setup'});
  assert.equal(f.element('secret-setup').href,'https://example.com/setup');
  await f.ctx.openCredentialDialog(request);
  assert.equal(f.element('secret-setup-row').hidden,true);assert.equal(f.element('secret-setup').href,undefined);
  await f.ctx.openCredentialDialog({id:'inference',provider:'fireworks',reason:'Run a benchmark',can_personal:true});
  assert.equal(f.element('secret-setup').href,'https://example.com/');
});

test('standalone session-only saving requires an explicit eligible root and preserves the draft until chosen',async()=>{
  const runs=[{id:'my-root',prompt:'raw outage prompt',display_title:'Investigate outage',chat_enabled:true,active_user_id:'google:owner'},
    {id:'other-root',prompt:'Other requester',chat_enabled:true,active_user_id:'google:other'},
    {id:'slack-root',prompt:'Slack request',chat_enabled:true,active_user_id:'slack:T:U'},
    {id:'legacy-root',prompt:'Older chat'},
    {id:'child',prompt:'Worker',parent_run_id:'my-root',chat_enabled:true,active_user_id:'google:owner'},
    {id:'not-chat',prompt:'Legacy task',chat_enabled:false,active_user_id:'google:owner'}];
  const f=fixture({kind:'key',runs});f.ctx.state.selected=null;await f.ctx.openCredentialDialog();
  assert.equal(f.gets.some(path=>path.startsWith('/api/runs')),false);
  f.element('secret-scope').value='personal';f.element('secret-lifetime').value='session';
  await f.element('secret-lifetime').onchange();
  const picker=f.element('secret-root');assert.equal(picker.required,true);assert.equal(picker.disabled,false);assert.equal(picker.value,'');
  assert.deepEqual(Array.from(picker.options,o=>o.value),['','my-root','slack-root','legacy-root']);
  assert.equal(picker.options[1].text,'Investigate outage · my-root');
  f.element('secret-value').value='synthetic-provider-key';
  await f.element('credential-form').onsubmit({preventDefault(){}});
  assert.equal(f.posts.length,0);assert.equal(f.element('secret-value').value,'synthetic-provider-key');
  assert.match(f.element('secret-form-error').textContent,/Choose a session/);
  picker.value='my-root';await f.element('credential-form').onsubmit({preventDefault(){}});
  assert.equal(f.posts[0].method,'POST');assert.equal(f.posts[0].body.root_id,'my-root');assert.equal(f.posts[0].body.lifetime,'session');
});

test('manager can restrict persistent org access to any chosen root; changing to personal clears an ineligible draft selection',async()=>{
  const saved={id:'shared-access',provider:'fireworks',label:'Shared access',scope:'organization',lifetime:'persistent',revision:8,can_manage:true};
  const f=fixture({kind:'key',saved:[saved],runs:[{id:'their-root',prompt:'Teammate investigation',chat_enabled:true,active_user_id:'google:other'}]});
  f.ctx.state.selected=null;await f.ctx.openCredentialDialog(null,saved);
  f.element('secret-lifetime').value='session';await f.element('secret-lifetime').onchange();
  assert.deepEqual(Array.from(f.element('secret-root').options,o=>o.value),['','their-root']);
  f.element('secret-root').value='their-root';f.element('secret-scope').value='personal';await f.element('secret-scope').onchange();
  assert.equal(f.element('secret-root').value,'');assert.match(f.element('secret-root-note').textContent,/No matching sessions/);
  f.element('secret-scope').value='organization';await f.element('secret-scope').onchange();
  assert.equal(f.element('secret-root').value,'');f.element('secret-root').value='their-root';
  await f.element('credential-form').onsubmit({preventDefault(){}});
  assert.deepEqual(f.posts[0].body,{revision:8,scope:'organization',lifetime:'session',label:'Shared access',expires_at:'',root_id:'their-root'});
  assert.equal(f.posts[0].method,'PATCH');
});

test('session lookup errors are retryable and persistent reuse disables the root requirement',async()=>{
  let attempts=0;
  const f=fixture({kind:'key',runs:()=>{if(++attempts===1)throw new Error('Unavailable');return [{id:'root-one',prompt:'Chat'}];}});
  await f.ctx.openCredentialDialog();f.element('secret-scope').value='personal';f.element('secret-lifetime').value='session';
  await f.element('secret-lifetime').onchange();assert.match(f.element('secret-root-note').textContent,/Could not load sessions/);
  await f.element('secret-root-reload').onclick();assert.deepEqual(Array.from(f.element('secret-root').options,o=>o.value),['','root-one']);
  f.element('secret-root').value='root-one';f.element('secret-lifetime').value='persistent';await f.element('secret-lifetime').onchange();
  assert.equal(f.element('secret-root-field').hidden,true);assert.equal(f.element('secret-root').required,false);assert.equal(f.element('secret-root').disabled,true);
  f.element('secret-value').value='synthetic-provider-key';await f.element('credential-form').onsubmit({preventDefault(){}});
  assert.equal(f.posts[0].body.lifetime,'persistent');assert.equal('root_id' in f.posts[0].body,false);
});

test('1Password shortcut saves a masked token with explicit organization and reuse choices',async()=>{
  const f=fixture({kind:'key'});
  await f.ctx.openOnePasswordDialog();
  assert.equal(f.element('secret-provider').value,'generic');
  assert.equal(f.element('secret-name').value,'1password-shared');
  assert.equal(f.element('secret-generic-fields').hidden,true);
  assert.equal(f.element('secret-input-0').type,'password');
  assert.equal(f.elements.has('secret-value'),false);
  assert.equal(f.element('secret-scope').value,'');
  assert.equal(f.element('secret-lifetime').value,'');
  f.element('secret-input-0').value='synthetic-op-token';
  await f.element('credential-form').onsubmit({preventDefault(){}});
  assert.equal(f.posts.length,0);
  f.element('secret-scope').value='organization';f.element('secret-lifetime').value='persistent';
  await f.element('credential-form').onsubmit({preventDefault(){}});
  assert.equal(f.posts[0].path,'/api/credentials/secrets');
  assert.equal(f.posts[0].body.name,'1password-shared');
  assert.equal(f.posts[0].body.label,'1Password Shared');
  assert.deepEqual(JSON.parse(f.posts[0].body.value),{OP_SERVICE_ACCOUNT_TOKEN:'synthetic-op-token'});
  assert.equal(f.elements.has('secret-input-0'),false);
});

test('1Password shortcut edits an existing connection and preserves its token when blank',async()=>{
  for(const replacement of ['', 'synthetic-new-token']){
    const saved={id:'shared',provider:'generic',name:'1password-shared',format:'env',scope:'organization',lifetime:'persistent',label:'Shared',revision:4,can_manage:true};
    const f=fixture({kind:'key',saved:[saved]});
    await f.ctx.openOnePasswordDialog();
    assert.equal(f.element('secret-input-0').required,false);
    f.element('secret-input-0').value=replacement;
    await f.element('credential-form').onsubmit({preventDefault(){}});
    assert.equal(f.posts[0].method,'PATCH');
    assert.equal(f.posts[0].path,'/api/credentials/secrets/shared');
    assert.equal(f.posts[0].body.revision,4);
    assert.equal('value' in f.posts[0].body,!!replacement);
    if(replacement)assert.deepEqual(JSON.parse(f.posts[0].body.value),{OP_SERVICE_ACCOUNT_TOKEN:replacement});
  }
});

test('1Password shortcut does not duplicate ambiguous or admin-managed access',async()=>{
  const saved={id:'shared',provider:'generic',name:'1password-shared',format:'env',scope:'organization'};
  const f=fixture({kind:'key',saved:[saved],admin:false});
  await assert.rejects(()=>f.ctx.openOnePasswordDialog(),/managed by an administrator/);
  assert.equal(f.posts.length,0);
  const many=fixture({kind:'key',saved:[{...saved,can_manage:true},{...saved,id:'shared2',can_manage:true}]});
  await assert.rejects(()=>many.ctx.openOnePasswordDialog(),/Multiple Shared connections/);
});

test('1Password pending requests get a masked token field without JSON entry',async()=>{
  const f=fixture({kind:'key',admin:false});
  await f.ctx.openCredentialDialog({id:'shared',provider:'generic',name:'1password-shared',format:'env',can_personal:true,reason:'Check the Shared vault'});
  assert.equal(f.element('secret-input-0').type,'password');
  assert.equal(f.element('secret-use-organization').disabled,true);
  assert.equal(f.elements.has('secret-value'),false);
});

const linkRun='a'.repeat(32),linkRequest='b'.repeat(32);
const accessLink=(generation=2)=>`#run=${linkRun}&credential=${linkRequest}&generation=${generation}`;

test('session links accept only a complete canonical credential target',()=>{
  const {ctx}=fixture({kind:'key'});
  assert.equal(ctx.parseSessionLink('#run='+linkRun).runId,linkRun);
  for(const generation of [0,2,999999999999999]){
    const parsed=ctx.parseSessionLink(accessLink(generation));
    assert.equal(parsed.credentialId,linkRequest);assert.equal(parsed.generation,generation);
  }
  for(const link of ['#tasks',accessLink()+'&token=secret',accessLink().replace('&generation=2',''),accessLink().replace('generation=2','generation=-1'),accessLink().replace('generation=2','generation=02'),accessLink().replace('generation=2','generation=9007199254740993'),accessLink().replace('credential=','credential=%62'),accessLink()+'&credential='+linkRequest,accessLink().replace('#run=','#run=X'),accessLink()+'\n']){
    assert.equal(ctx.parseSessionLink(link),null,link);
  }
});

function linkedFixture(){
  const f=fixture({kind:'key'}),target=f.element('credential-requests'),buttons=new Map(),focuses=[],scrolls=[],paths=[];
  f.ctx.state.selected=linkRun;
  f.ctx.history={replaceState:(_,__,path)=>paths.push(path)};
  target.querySelectorAll=()=>Array.from(target.innerHTML.matchAll(/data-provide-key="([^"]+)"/g),match=>{
    const id=match[1],button={dataset:{provideKey:id},focus:()=>focuses.push(id),closest:()=>({scrollIntoView:()=>scrolls.push(id)})};
    buttons.set(id,button);return button;
  });
  target.querySelector=selector=>buttons.get(selector.match(/data-provide-key="([^"]+)"/)?.[1])||null;
  const setLink=()=>{f.ctx.state.credentialLink={...f.ctx.parseSessionLink(accessLink()),pageVersion:1};};
  setLink();
  const request={id:linkRequest,generation:2,provider:'fireworks',reason:'Deploy',can_personal:true,can_organization:true,preferred_scope:'session'};
  return {...f,target,buttons,focuses,scrolls,paths,request,setLink};
}

test('Slack link highlights and focuses only its exact request, then waits for a click',async()=>{
  const f=linkedFixture(),other={...f.request,id:'c'.repeat(32)};
  f.ctx.renderCredentialRequests([other,f.request]);
  assert.deepEqual(f.focuses,[linkRequest]);assert.deepEqual(f.scrolls,[linkRequest]);
  assert.match(f.target.innerHTML,/credential-request-linked/);assert.match(f.target.innerHTML,/Select Provide Secret below/);
  assert.match(f.target.innerHTML,/Availability: Session only/);
  assert.match(f.target.innerHTML,/aria-describedby="credential-link-instruction"/);
  assert.equal(f.element('credential-dialog').open,undefined);
  assert.equal(f.ctx.state.credentialLink,null);assert.equal(f.paths.at(-1),'#run='+linkRun);
  f.buttons.get(linkRequest).onclick();await flush();
  assert.equal(f.element('credential-dialog').open,true);
  assert.equal(f.element('secret-use-session').checked,true);
  assert.equal(f.element('secret-scope').value,'personal');assert.equal(f.element('secret-lifetime').value,'session');
  assert.doesNotMatch(f.target.innerHTML,/credential-request-linked/);
});

test('missing, replaced, and resolved targets never highlight another request or revive on a poll',()=>{
  for(const first of [[],[{id:linkRequest,generation:3,provider:'fireworks',can_personal:true}],[{id:'c'.repeat(32),generation:2,provider:'fireworks',can_personal:true}]]){
    const f=linkedFixture();f.ctx.renderCredentialRequests(first);
    assert.match(f.target.innerHTML,/access link is no longer current/);
    assert.deepEqual(f.focuses,[]);assert.doesNotMatch(f.target.innerHTML,/credential-request-linked/);
    f.ctx.renderCredentialRequests([f.request]);
    assert.deepEqual(f.focuses,[]);assert.doesNotMatch(f.target.innerHTML,/credential-request-linked/);
    assert.equal(f.ctx.parseSessionLink(f.paths.at(-1)).credentialId,'');
  }
});

test('link permissions are visible without focusing an unavailable action',()=>{
  const f=linkedFixture();f.ctx.renderCredentialRequests([{...f.request,can_personal:false,can_organization:false}]);
  assert.match(f.target.innerHTML,/Only the requester or an organization admin/);
  assert.deepEqual(f.focuses,[]);assert.doesNotMatch(f.target.innerHTML,/data-provide-key|credential-request-linked/);
});

test('credential refresh preserves an open draft and never refocuses the linked action',async()=>{
  const f=linkedFixture();f.ctx.renderCredentialRequests([f.request]);
  f.buttons.get(linkRequest).onclick();await flush();
  f.element('secret-value').value='synthetic-in-progress';
  f.ctx.renderCredentialRequests([{...f.request,preferred_scope:'organization'}]);
  assert.equal(f.element('secret-value').value,'synthetic-in-progress');
  assert.equal(f.element('secret-lifetime').value,'session');assert.deepEqual(f.focuses,[linkRequest]);
});

test('all Slack availability choices carry into the existing editable form and submission',async()=>{
  for(const [preferred_scope,scope,lifetime] of [['session','personal','session'],['personal','personal','persistent'],['organization','organization','persistent']]){
    const f=fixture({kind:'key'});
    await f.ctx.openCredentialDialog({id:'key',generation:4,provider:'fireworks',can_personal:true,can_organization:true,preferred_scope});
    assert.equal(f.element('secret-use-'+preferred_scope).checked,true);
    f.element('secret-value').value='synthetic-key';await f.element('credential-form').onsubmit({preventDefault(){}});
    assert.equal(f.posts[0].body.scope,scope);assert.equal(f.posts[0].body.lifetime,lifetime);assert.equal(f.posts[0].body.generation,4);
  }
});

test('a disallowed Slack choice never silently becomes personal or organization access',async()=>{
  for(const preferred_scope of ['organization','session','personal']){
    const organization=preferred_scope==='organization',f=fixture({kind:'key',admin:!organization});
    await f.ctx.openCredentialDialog({id:'key',provider:'fireworks',can_personal:organization,can_organization:!organization,preferred_scope});
    assert.equal(f.element('secret-use-'+preferred_scope).checked,true);assert.equal(f.element('secret-use-'+preferred_scope).disabled,true);
    assert.equal(f.element('secret-scope').value,'');assert.equal(f.element('secret-lifetime').value,'');
    assert.match(f.element('credential-form').innerHTML,/selected in Slack/);
    f.element('secret-value').value='synthetic-key';await f.element('credential-form').onsubmit({preventDefault(){}});assert.equal(f.posts.length,0);
    const different=f.element(organization?'secret-use-session':'secret-use-organization');different.checked=true;different.onchange();
    await f.element('credential-form').onsubmit({preventDefault(){}});assert.equal(f.posts[0].body.scope,organization?'personal':'organization');
  }
});

test('navigation consumes pending highlights, clears open drafts, and cancels a delayed form response',async()=>{
  const f=linkedFixture();
  let finish;f.ctx.api=()=>new Promise(resolve=>finish=resolve);
  const pending=f.ctx.openCredentialDialog(f.request);
  f.ctx.resetCredentialNavigation();f.ctx.state.pageVersion++;f.ctx.state.selected='elsewhere';
  finish({providers:[{id:'fireworks'}],secrets:[]});await pending;
  assert.equal(f.element('credential-dialog').open,undefined);assert.equal(f.ctx.state.credentialLink,null);
  f.ctx.renderCredentialRequests([f.request]);assert.deepEqual(f.focuses,[]);
  const g=linkedFixture();await g.ctx.openCredentialDialog(g.request);g.element('secret-value').value='synthetic-draft';
  g.ctx.resetCredentialNavigation();assert.equal(g.element('credential-dialog').open,false);assert.equal(g.element('credential-form').innerHTML,'');
});

test('async session opening retains a target across metadata retry and ignores a late old session',async()=>{
  const f=linkedFixture(),app=readFileSync('app/static/app.js','utf8'),renders=[],pending=[];
  Object.assign(f.ctx,{stopStream:()=>f.ctx.resetCredentialNavigation(),setView(){},sessionTitle:()=>'',refreshRuns:async()=>{},
    renderChat:run=>{renders.push(run.id);f.ctx.renderCredentialRequests(run.credential_requests);},
    api:()=>new Promise(resolve=>pending.push(resolve))});
  f.ctx.document.hidden=true;f.ctx.state.runs=[{id:linkRun},{id:'d'.repeat(32)}];f.ctx.state.expandedParents=new Set();
  vm.runInContext(app.slice(app.indexOf('async function openRun('),app.indexOf('function renderChat(')),f.ctx);
  const run={id:linkRun,chat_enabled:true,credential_requests:[f.request]},opening=f.ctx.openRun(linkRun,accessLink());
  f.ctx.state.sessionEdits=1;pending.shift()(run);await flush();
  assert.equal(f.ctx.state.credentialLink.credentialId,linkRequest);
  pending.shift()(run);await opening;assert.deepEqual(f.focuses,[linkRequest]);
  const old=f.ctx.openRun(linkRun,accessLink()),otherId='d'.repeat(32),next=f.ctx.openRun(otherId);
  assert.equal(f.paths.at(-1),'#run='+otherId); // Refresh during loading cannot resurrect the previous target.
  pending[1]({id:otherId,chat_enabled:true,credential_requests:[]});await next;
  pending[0](run);await old;
  assert.equal(f.ctx.state.selected,otherId);assert.equal(f.ctx.state.credentialLink,null);
  assert.equal(f.paths.at(-1),'#run='+otherId);assert.deepEqual(renders,[linkRun,otherId]);assert.deepEqual(f.focuses,[linkRequest]);
});

function routeFixture(authenticated){
  const f=linkedFixture(),app=readFileSync('app/static/app.js','utf8'),opened=[],handlers={},redirects=[];
  for(const id of ['google-signin','signin-error','logout'])f.element(id);
  Object.assign(f.ctx,{location:{hash:accessLink(),search:'',assign:url=>redirects.push(url)},URLSearchParams,
    applyUserSession:session=>f.ctx.state.csrf=session.authenticated?'csrf':'',restoreSessionFolderView(){},refreshRuns:async()=>{},
    registerWebMCP(){},navigate:async view=>opened.push({view}),openRun:async(id,hash)=>opened.push({id,hash}),
    settingsViews:new Set(['secrets','users']),window:{addEventListener:(event,handler)=>handlers[event]=handler},
    api:async(path,options)=>{
      if(path==='/api/session')return {authenticated,google_enabled:true,google_domains:['example.com'],local:true};
      if(path==='/api/auth/google/start'){f.posts.push(JSON.parse(options.body));return {url:'https://accounts.google.com/fixture'};}
      return {};
    }});
  const query=f.ctx.$;f.ctx.$=selector=>selector==='.rail-foot small'?f.element('rail-label'):query(selector);
  f.ctx.document.querySelectorAll=()=>[];
  vm.runInContext(app.slice(app.indexOf("window.addEventListener('hashchange'"),app.indexOf("document.querySelector('.skip-link')"))+
    app.slice(app.indexOf('async function boot('),app.indexOf('function registerWebMCP(')),f.ctx);
  return {...f,opened,handlers,redirects};
}

test('boot and hash changes share the complete session link parser while plain app routes remain intact',async()=>{
  const f=routeFixture(true);await f.ctx.boot();
  assert.deepEqual(f.opened,[{id:linkRun,hash:accessLink()}]);
  f.ctx.location.hash='#run='+linkRun;f.handlers.hashchange();await flush();
  assert.deepEqual(f.opened.at(-1),{id:linkRun,hash:'#run='+linkRun});
  f.ctx.location.hash='#secrets';f.handlers.hashchange();await flush();assert.deepEqual(f.opened.at(-1),{view:'secrets'});
  f.ctx.location.hash=accessLink()+'&key=must-not-parse';f.handlers.hashchange();await flush();assert.deepEqual(f.opened.at(-1),{view:'tasks'});
});

test('signed-out boot sends the complete credential target through Google sign-in',async()=>{
  const f=routeFixture(false);await f.ctx.boot();
  assert.deepEqual(f.opened,[]);
  await f.element('google-signin').onclick();
  assert.deepEqual(f.posts,[{return_to:'/'+accessLink()}]);assert.deepEqual(f.redirects,['https://accounts.google.com/fixture']);
});
