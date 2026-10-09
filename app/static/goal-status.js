/* Only host lifecycle events confirm a goal. Drafts and queued requests are not active goals. */
(function(root){
  const controls=new Set(['help','status','pause','stop','cancel','resume','clear']);
  function command(text){const match=/^\s*\/goal(?:\s+([\s\S]*))?$/.exec(text);return match?(match[1]||'').trim():null;}
  function draft(text){const arg=command(text);return arg===null?null:!arg?'Goal selected · Add an objective, then send to apply.':controls.has(arg)?`Goal command · Send to ${arg==='status'?'check status':arg} the goal.`:'Goal ready · Send to apply. Not running yet.';}
  function current(run){
    const goal=run.goal;
    if(!goal)return null;
    const ended=['idle','completed','failed','cancelled','interrupted'].includes(run.status)&&!run.active;
    const waiting=['waiting_credential','waiting_children','reconnecting','awaiting_approval','queued','provisioning'].includes(run.status);
    const status=goal.status==='active'?(ended?'paused':waiting?'waiting':'active'):goal.status;
    return {...goal,status,ended};
  }
  function elapsed(goal,run,now=Date.now()){
    let seconds=Math.max(0,Number(goal.elapsed_seconds)||0);
    if(goal.active_since){
      const end=goal.ended?(Date.parse(run.updated_at)||now):now;
      seconds+=Math.max(0,end/1000-Number(goal.active_since));
    }
    return Math.floor(seconds);
  }
  function render(node,run,esc){
    if(!node)return;
    const goal=current(run);
    node.hidden=!goal;if(!goal){MoyaiUI.render(node, '');delete node.dataset.signature;return;}
    const labels={active:'Running',waiting:'Waiting',paused:'Paused',blocked:'Blocked',completed:'Completed'};
    const signature=JSON.stringify([goal.id,goal.objective,goal.status,goal.reason]);
    if(node.dataset.signature!==signature){
      node.dataset.signature=signature;
      MoyaiUI.render(node, `<div class="goal-heading"><strong>Goal</strong><span class="goal-state">${esc(labels[goal.status]||goal.status)}</span><time class="goal-elapsed" aria-label="Goal elapsed time"></time></div><div class="goal-objective">${esc(goal.objective)}</div>${goal.reason&&goal.status!=='active'?`<p class="goal-reason">${esc(goal.reason)}</p>`:''}`);
    }
    node.dataset.status=goal.status;
    node.querySelector('time').textContent=root.MoyaiActivity.duration(0,elapsed(goal,run)*1000);
  }
  const api={command,draft,current,elapsed,render};root.MoyaiGoal=api;
  if(typeof module!=='undefined')module.exports=api;
})(typeof globalThis!=='undefined'?globalThis:window);
