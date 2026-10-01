const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const {test}=require('node:test');
const vm=require('node:vm');
const flush=()=>new Promise(resolve=>setImmediate(resolve));

function fixture({kind='skill',admin=true,saved=[],skill=null}={}){
  const elements=new Map(),posts=[],submit={disabled:false},closeButton={};
  const element=id=>{
    if(elements.has(id))return elements.get(id);
    let html='';
    const el={id,value:'',textContent:'',disabled:false,required:false,hidden:false,
      focus(){this.focused=true;},reset(){},showModal(){this.open=true;},close(){this.open=false;this.onclose?.();},
      querySelector:selector=>selector==='[type="submit"]'?submit:closeButton,
      get innerHTML(){return html;},
      set innerHTML(value){
        html=value;
        // Minimal form DOM: read native initial values from actual rendered HTML.
        for(const match of value.matchAll(/<(select|textarea)\b([^>]*\bid="([^"]+)"[^>]*)>([\s\S]*?)<\/\1>/g)){
          const [all,tag,attrs,name,body]=match,field=element(name);
          field.required=/\brequired\b/.test(attrs);field.disabled=/\bdisabled\b/.test(attrs);
          if(tag==='select'){
            field.options=Array.from(body.matchAll(/<option\b([^>]*)>([^<]*)<\/option>/g),m=>({
              value:m[1].match(/value="([^"]*)"/)?.[1]||'',selected:/\bselected\b/.test(m[1]),disabled:/\bdisabled\b/.test(m[1]),text:m[2]}));
            field.value=(field.options.find(o=>o.selected)||field.options[0])?.value||'';
          }
        }
        for(const match of value.matchAll(/<button\b[^>]*\bid="([^"]+)"/g))element(match[1]);
        // Inputs are void elements and have no closing tag.
        for(const match of value.matchAll(/<input\b([^>]*\bid="([^"]+)"[^>]*)>/g)){
          const field=element(match[2]);field.value=match[1].match(/\bvalue="([^"]*)"/)?.[1]||'';
          field.required=/\brequired\b/.test(match[1]);field.disabled=/\bdisabled\b/.test(match[1]);
        }
      }};
    elements.set(id,el);return el;
  };
  const ctx={state:{role:admin?'admin':'member',selected:'run-one'},
    $:selector=>selector==='#secret-source'&&!saved.length?null:selector==='#decline-secret'?elements.get('decline-secret'):element(selector.slice(1)),
    document:{addEventListener(){}},crypto:{randomUUID:()=> 'request-unique-id'},
    esc:value=>String(value).replaceAll('<','&lt;').replaceAll('"','&quot;'),
    api:async(path,options)=>{
      if(options){posts.push({path,body:JSON.parse(options.body)});return {};}
      if(path.startsWith('/api/credentials'))return {providers:[{id:'fireworks',name:'Fireworks',setup:'https://example.com'}],secrets:saved};
      return skill;
    },toast(){},refreshChat:async()=>{},showError(){},renderSecrets:async()=>{},
  };
  vm.createContext(ctx);vm.runInContext(readFileSync('app/static/'+(kind==='skill'?'skills.js':'credentials.js'),'utf8'),ctx);
  return {ctx,element,posts,submit};
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
  assert.equal(f.posts.length,1);assert.equal(f.posts[0].body.scope,'personal');assert.equal(value.value,'');
});

test('choosing a saved key disables the unused scope field and keeps its existing scope',async()=>{
  const f=fixture({kind:'key',saved:[{id:'existing-key',provider:'fireworks',label:'Team key',scope:'organization'}]});
  await f.ctx.openCredentialDialog({id:'request-one',provider:'fireworks',reason:'Benchmark',can_personal:true});
  const source=f.element('secret-source'),scope=f.element('secret-scope');
  source.value='existing-key';source.onchange();
  assert.equal(scope.required,false);assert.equal(scope.disabled,true);
  f.element('credential-form').onsubmit({preventDefault(){}});await flush();
  assert.equal(f.posts[0].body.secret_id,'existing-key');assert.equal('scope' in f.posts[0].body,false);
});


test('declining a key request does not require a scope or transmit a draft key',async()=>{
  const f=fixture({kind:'key'});
  await f.ctx.openCredentialDialog({id:'request-one',provider:'fireworks',reason:'Benchmark',can_personal:true});
  assert.equal(f.element('secret-scope').value,'');
  assert.ok(f.element('secret-scope').options.some(option=>option.value==='session'));
  f.element('secret-value').value='do-not-transmit-this-draft';
  f.element('decline-secret').onclick();await flush();
  assert.equal(f.posts[0].body.decision,'decline');assert.equal('scope' in f.posts[0].body,false);
  assert.equal('value' in f.posts[0].body,false);assert.equal(f.element('secret-value').value,'');
});
