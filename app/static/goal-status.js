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
    const status=goal.status==='active'?(ended?'interrupted':waiting?'waiting':'active'):goal.status;
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
  function reporting(goal){
    const schedule=goal.reporting||{},lines=[],duration=seconds=>root.MoyaiActivity.duration(0,seconds*1000);
    if(schedule.at_seconds?.length)lines.push(`Report checkpoints at ${schedule.at_seconds.map(duration).join(', ')} of active work.`);
    if(schedule.interval_seconds)lines.push(`Report every ${duration(schedule.interval_seconds)} of active work.`);
    if(lines.length)lines.push('Checkpoints do not end the goal.');
    if(goal.checkpoint)lines.push(`Last checkpoint saved at ${duration(goal.checkpoint.elapsed_seconds)}.`);
    return lines.join(' ');
  }
  function render(node,run,esc){
    if(!node)return;
    const goal=current(run);
    node.hidden=!goal;if(!goal){MoyaiUI.render(node, '');delete node.dataset.signature;return;}
    const labels={active:'Running',waiting:'Waiting',paused:'Paused',blocked:'Blocked',completed:'Completed',wrapping:'Reporting',budget_exhausted:'Budget ended',needs_input:'Needs your input',interrupted:'Interrupted',cancelled:'Cancelled'};
    const budget=goal.budget||{};
    const budgetText=budget.seconds?`${({timebox:'Timebox',floor:'Minimum work',cap:'Work cap'})[budget.kind]||'Work budget'}: ${root.MoyaiActivity.duration(0,budget.seconds*1000)}`:budget.deadline?`Deadline: ${new Date(budget.deadline*1000).toLocaleString()}`:'';
    const reportText=reporting(goal);
    const signature=JSON.stringify([goal.id,goal.objective,goal.status,goal.reason,budgetText,reportText]);
    if(node.dataset.signature!==signature){
      node.dataset.signature=signature;
      MoyaiUI.render(node, `<div class="goal-heading"><strong>Goal</strong><span class="goal-state">${esc(labels[goal.status]||goal.status)}</span><time class="goal-elapsed" aria-label="Goal elapsed time"></time></div><div class="goal-objective">${esc(goal.objective)}</div>${budgetText?`<p class="goal-budget">${esc(budgetText)}</p>`:''}${reportText?`<p class="goal-reporting">${esc(reportText)}</p>`:''}${goal.reason&&goal.status!=='active'?`<p class="goal-reason">${esc(goal.reason)}</p>`:''}`);
    }
    node.dataset.status=goal.status;
    node.querySelector('time').textContent=root.MoyaiActivity.duration(0,elapsed(goal,run)*1000);
  }
  const api={command,draft,current,elapsed,reporting,render};root.MoyaiGoal=api;
  if(typeof module!=='undefined')module.exports=api;
})(typeof globalThis!=='undefined'?globalThis:window);
