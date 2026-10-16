export const automationDefaultSchedule = () => ({frequency:'weekdays',time:'09:00',weekday:1,timezone:Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC'});
export function automationTriggers(d) {
  return d.triggers || [{id:'default',...(d.event?{event:d.event}:{schedule:d.timing||automationDefaultSchedule()})}];
}
export const automationSources = {schedule:'Schedule',session:'Moyai sessions',slack:'Slack',github:'GitHub',gitlab:'GitLab',linear:'Linear',jira:'Jira',pylon:'Pylon',pagerduty:'PagerDuty',webhook:'Generic webhook'};
export const automationEventFields = {
  session:[['session_id','Session ID (optional)','Leave blank for all shared sessions'],['text_contains','Message contains (optional)','error'],['text_starts_with','Message starts with (optional)','bug:']],
  github:[['repository','Repository','BerriAI/litellm',true],['action','Action (optional)','opened, labeled, closed…'],['label','Label (optional)','bug'],['branch','Branch (optional)','main'],['conclusion','CI conclusion (optional)','failure'],['text_contains','Text contains (optional)','investigate']],
  gitlab:[['repository','Project path','group/project',true],['action','Action (optional)','open, update, merge…'],['status','Status (optional)','failed'],['branch','Branch (optional)','main'],['text_contains','Text contains (optional)','investigate']],
  linear:[['team_id','Team ID','Linear team UUID',true],['assignee_id','Assignee ID (optional)','Linear user UUID'],['label_id','Label ID (optional)','Linear label UUID'],['status','Status ID (optional)','Linear state UUID'],['priority','Priority (optional)','0–4']],
  jira:[['project','Project key (optional)','ENG'],['assignee_id','Assignee account ID (optional)','Account ID'],['label','Label (optional)','moyai'],['status','Status (optional)','In Progress'],['epic','Epic / parent key (optional)','ENG-123'],['text_contains','Text contains (optional)','investigate']],
  pylon:[['label','Tag (optional)','bug'],['status','Status (optional)','open']],
  pagerduty:[['service_id','Service ID (optional)','PABC123'],['urgency','Urgency (optional)','high'],['status','Status (optional)','triggered']],
  slack:[['channel_id','Channel ID (optional with another filter)','C0123456789'],['text_contains','Message contains (optional)','investigate'],['text_starts_with','Message starts with (optional)','!moyai'],['reaction','Reaction name (required for reactions)','rotating_light']],
  webhook:[['payload_pattern','Payload matches regex (optional)','"priority":\\s*"high"']]
};
export const automationSetupNotes={
  github:'Add this URL in the repository’s Webhooks settings with application/json. Select the events used by your triggers and enter the generated secret. Signature: X-Hub-Signature-256.',
  gitlab:'Add this URL in the project’s Webhooks settings. Paste its whsec_ signing token below to use Standard Webhooks, or generate a legacy secret token and enter it in GitLab. Select the events used by your triggers.',
  linear:'Create an Issues webhook for your team in Linear → Settings → API → Webhooks. Paste its signing secret below.',
  jira:'Register this URL as a Jira webhook for the selected events and configure its secret. Moyai verifies X-Hub-Signature with HMAC-SHA256.',
  pagerduty:'Create a V3 webhook subscription for the selected incident events and paste its signing secret below. Moyai verifies X-PagerDuty-Signature.',
  pylon:'In Pylon Settings → Webhooks, add this URL and Authorization: Bearer <secret> or X-Webhook-Secret. Then create a Pylon Trigger with the matching kickoff and Send webhook action. Template the body with event_type (issue.created, issue.tag_added, or issue.status_changed) and data: {id, title, description, status, tags, url}. Include event_id or occurred_at in the body so separate changes to the same issue remain distinct; keep it stable on retries.',
  webhook:'Send a JSON object using Authorization: Bearer <secret> or X-Webhook-Secret. Include a unique X-Moyai-Event-Id and reuse it on retries. Without an ID, identical payloads deduplicate. You can also use the timestamped HMAC format documented below.'
};

export function automationTiming(t) {
  if(t.frequency==='once')return 'Once · '+new Date(t.run_at).toLocaleString();
  if(t.frequency==='cron')return t.cron+' · '+t.timezone;
  const days=['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
  return (t.frequency==='hourly'?`Every hour at :${t.time.slice(3)}`:t.frequency==='weekdays'?`Weekdays at ${t.time}`:t.frequency==='weekly'?`${days[t.weekday]} at ${t.time}`:`Every day at ${t.time}`)+` · ${t.timezone}`;
}
export function automationHistoryMerge(first, second) {
  return [...new Map([...first,...second].map(row=>[row.occurrence,row])).values()]
    .sort((a,b)=>b.created_at.localeCompare(a.created_at)||String(b.occurrence).localeCompare(String(a.occurrence)));
}
export const automationRunStatus = r => r.status==='idle'?'Completed':(r.status||r.outcome).replaceAll('_',' ');
