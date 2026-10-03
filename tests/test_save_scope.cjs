const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const {test}=require('node:test');
const vm=require('node:vm');
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
      querySelector:selector=>selector==='[type="submit"]'?submit:closeButton,
      querySelectorAll:()=>[],
      get innerHTML(){return html;},
      set innerHTML(value){
        const remove=name=>{const child=elements.get(name);if(child)child.innerHTML='';elements.delete(name);};
        children.forEach(remove);html=value;children=[];
        for(const match of value.matchAll(/<([a-z]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)){
          const [,tag,attrs,name]=match,field=element(name);children.push(name);
          field.tagName=tag.toUpperCase();field.required=/\brequired\b/.test(attrs);field.disabled=/\bdisabled\b/.test(attrs);field.hidden=/\bhidden\b/.test(attrs);field.open=/\bopen\b/.test(attrs);
          field.value=decode(attrs.match(/\bvalue="([^"]*)"/)?.[1]||'');field.type=attrs.match(/\btype="([^"]*)"/)?.[1]||'';
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
    crypto:{randomUUID:()=> 'request-unique-id'},URL,Date,
    esc:value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),relative:()=> 'just now',
    api:async(path,options)=>{
      if(options){posts.push({path,method:options.method,body:options.body?JSON.parse(options.body):null});if(submitError)throw new Error(submitError);return {};}
      gets.push(path);
      if(path.startsWith('/api/runs'))return typeof runs==='function'?runs():runs;
      if(path.startsWith('/api/credentials'))return {providers:[{id:'fireworks',name:'Fireworks',setup_url:'https://example.com'},{id:'generic',name:'Other service',setup_url:''}],secrets:saved,root_id:rootId};
      return skill;
    },toast(){},refreshChat:async()=>{},showError(){},
  };
  vm.createContext(ctx);vm.runInContext(readFileSync('app/static/'+(kind==='skill'?'skills.js':'credentials.js'),'utf8'),ctx);
  return {ctx,element,posts,gets,submit,elements};
}

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
  assert.equal(f.element('secret-expiry').disabled,true);assert.equal(f.element('secret-value').disabled,true);
  f.element('credential-form').onsubmit({preventDefault(){}});await flush();
  assert.equal(f.posts[0].body.secret_id,'existing-key');assert.equal('scope' in f.posts[0].body,false);
});


test('declining a key request does not require a scope or transmit a draft key',async()=>{
  const f=fixture({kind:'key'});
  await f.ctx.openCredentialDialog({id:'request-one',provider:'fireworks',reason:'Benchmark',can_personal:true});
  assert.equal(f.element('secret-scope').value,'');
  assert.equal(f.element('secret-scope').options.some(option=>option.value==='session'),false);
  assert.ok(f.element('secret-lifetime').options.some(option=>option.value==='session'));
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
  assert.equal(f.element('secret-optional-options').tagName,'DETAILS');assert.equal(f.element('secret-optional-options').open,false);
  assert.equal(f.element('secret-expiry').disabled,false);assert.equal(f.element('secret-value').required,true);
  f.element('secret-expiry').oninvalid();assert.equal(f.element('secret-optional-options').open,true);
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

test('standalone session-only saving requires an explicit eligible root and preserves the draft until chosen',async()=>{
  const runs=[{id:'my-root',prompt:'Investigate outage',chat_enabled:true,active_user_id:'google:owner'},
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
