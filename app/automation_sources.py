"""Provider event vocabularies, scoped filters, and bounded agent context."""
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import SchemaValidator, core_schema

EVENT_CHOICES = {
    'session': [('message.posted','New session message')],
    'slack': [('message.posted','New message'), ('reaction.added','Reaction added')],
    'github': [('issues','Issue'), ('issue_comment','Issue comment'), ('pull_request','Pull request'),
               ('pull_request_review','PR review'), ('pull_request_review_comment','PR review comment'),
               ('check_run','CI check completed'), ('push','Push')],
    'gitlab': [('merge_request','Merge request'), ('note.merge_request','MR comment'), ('issue','Issue'),
               ('note.issue','Issue comment'), ('push','Push'), ('pipeline','Pipeline')],
    'linear': [('issue.created','Issue created'), ('issue.labeled','Label added'), ('issue.status_changed','Status changed'),
               ('issue.priority_changed','Priority changed'), ('issue.assigned','Assigned'), ('issue.moved','Moved to team')],
    'jira': [('issue.created','Issue created'), ('issue.updated','Issue updated'), ('issue.labeled','Label added'),
             ('issue.status_changed','Status changed'), ('issue.assigned','Assigned'),
             ('comment.created','Comment created'), ('comment.updated','Comment edited')],
    'pylon': [('issue.created','Issue created'), ('issue.tag_added','Tag added'), ('issue.status_changed','Status changed')],
    'pagerduty': [('incident.triggered','Incident triggered'), ('incident.acknowledged','Acknowledged'),
                  ('incident.resolved','Resolved'), ('incident.updated','Updated')],
    'webhook': [],
}
IDENTIFIER = r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$'
UUID = r'^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$'


def obj(value):
    return value if isinstance(value, dict) else {}


def text(value, limit=4000):
    return value[:limit] if isinstance(value, str) else ''


def scalar(value):
    return str(value) if isinstance(value, (str, int)) else ''


def items(value):
    return value if isinstance(value, list) else []


class EventTrigger(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    provider: Literal['session','slack','github','gitlab','linear','jira','pylon','pagerduty','webhook']
    session_id: str = Field(default='', max_length=32)
    event: str = Field(default='*', min_length=1, max_length=80)
    repository: str = Field(default='', max_length=200)
    team_id: str = Field(default='', max_length=36)
    assignee_id: str = Field(default='', max_length=128)
    label_id: str = Field(default='', max_length=36)
    label: str = Field(default='', max_length=100)
    channel_id: str = Field(default='', max_length=32)
    text_contains: str = Field(default='', max_length=200)
    text_starts_with: str = Field(default='', max_length=200)
    reaction: str = Field(default='', max_length=100)
    include_thread_replies: bool = False
    sender_type: Literal['any','human','bot'] = 'any'
    action: str = Field(default='', max_length=80)
    status: str = Field(default='', max_length=100)
    priority: int | None = Field(default=None, ge=0, le=4)
    conclusion: str = Field(default='', max_length=60)
    branch: str = Field(default='', max_length=200)
    project: str = Field(default='', max_length=100)
    epic: str = Field(default='', max_length=100)
    service_id: str = Field(default='', max_length=100)
    urgency: str = Field(default='', max_length=30)
    payload_pattern: str = Field(default='', max_length=500)

    @model_validator(mode='after')
    def valid_filters(self):
        if self.session_id and (self.provider != 'session' or not re.fullmatch(r'[0-9a-f]{32}', self.session_id)):
            raise ValueError('Session filters require a session source and a 32-character session ID.')
        if self.provider == 'session':
            allowed = {'provider', 'event', 'session_id', 'text_contains', 'text_starts_with', 'sender_type'}
            if self.sender_type == 'bot' or any(
                getattr(self, name) != field.default for name, field in type(self).model_fields.items() if name not in allowed
            ):
                raise ValueError('Session messages support only session ID and text filters, from human senders.')
        supported = dict(EVENT_CHOICES[self.provider])
        legacy_github = self.provider == 'github' and self.event.split('.')[0] in supported
        if self.provider != 'webhook' and self.event not in supported and not legacy_github:
            raise ValueError('Choose a supported event for this integration.')
        if self.provider in {'github','gitlab'} and not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+', self.repository):
            raise ValueError('Choose an exact repository or GitLab project path.')
        if self.provider == 'linear':
            if not re.fullmatch(UUID, self.team_id):
                raise ValueError('A Linear team ID is required.')
            for value in (self.assignee_id, self.label_id):
                if value and not re.fullmatch(UUID, value):
                    raise ValueError('Linear user and label IDs must be UUIDs.')
        if self.provider == 'slack':
            if self.channel_id and not re.fullmatch(r'[CG][A-Z0-9]{7,30}', self.channel_id):
                raise ValueError('Choose a Slack channel ID (C… or G…).')
            if self.event == 'reaction.added' and not self.reaction:
                raise ValueError('Choose a reaction name, such as rotating_light.')
            if self.event == 'reaction.added' and (self.text_contains or self.text_starts_with or self.sender_type == 'bot'):
                raise ValueError('Reaction events support channel and emoji filters, not message text or bot filters.')
            if not self.channel_id and not any((self.text_contains,self.text_starts_with,self.reaction,self.sender_type != 'any')):
                raise ValueError('Across-channel Slack triggers need a message, reaction, or sender filter.')
        if self.payload_pattern:
            try:
                SchemaValidator(core_schema.str_schema(pattern=self.payload_pattern))
            except Exception:
                raise ValueError('Use a valid regular expression without lookarounds or backreferences.') from None
        return self


def normalize(t, payload, event_header=''):
    p = obj(payload)
    event, action, title, body, url, item, branch, status, conclusion = '', '', '', '', '', '', '', '', ''
    extra = {}
    bot = False
    if t.provider == 'session':
        if t.session_id and p.get('session_id') != t.session_id:return None
        event = text(p.get('event'))
        title, body, url = 'Session message', p.get('body'), p.get('url')
        item = scalar(p.get('session_id')) + ':' + scalar(p.get('message_id'))
        extra = {'session_id':text(p.get('session_id'),32), 'message_id':p.get('message_id'),
                 'user_id':text(p.get('user_id'),200),
                 'conversation':[{'role':text(obj(m).get('role'),20), 'content':text(obj(m).get('content'),1000)}
                                 for m in items(p.get('conversation'))[:10]],
                 'context_truncated':bool(p.get('context_truncated'))}
    elif t.provider == 'github':
        if text(obj(p.get('repository')).get('full_name')).lower() != t.repository.lower():
            return None
        action, event = text(p.get('action')), event_header
        bot = obj(p.get('sender')).get('type') == 'Bot'
        issue, pr = obj(p.get('issue')), obj(p.get('pull_request'))
        data = issue if event in {'issues','issue_comment'} else pr
        if event in {'issues','issue_comment'} and not issue or event in {'pull_request','pull_request_review','pull_request_review_comment'} and not pr:
            return None
        detail = obj(p.get('comment') or p.get('review') or p.get('check_run'))
        if event == 'issues' and issue.get('pull_request'):
            return None
        if event in {'issue_comment','pull_request_review','pull_request_review_comment'}:
            expected_action = 'submitted' if event == 'pull_request_review' else 'created'
            if action != (t.action or expected_action):
                return None
        if event == 'check_run':
            if action != 'completed':
                return None
            data = detail
            status, conclusion = text(data.get('status')), text(data.get('conclusion'))
        if t.label:
            labels = [obj(x).get('name') for x in items(data.get('labels'))]
            if action == 'labeled':
                if text(obj(p.get('label')).get('name')) != t.label:return None
            elif t.label not in labels:
                return None
        branch = text(p.get('ref') or obj(pr.get('head')).get('ref') or obj(data.get('check_suite')).get('head_branch')).removeprefix('refs/heads/')
        title = data.get('title') or data.get('name') or ('Push to ' + branch)
        body = detail.get('body') or data.get('body') or obj(p.get('head_commit')).get('message') or obj(data.get('output')).get('summary')
        url = detail.get('html_url') or data.get('html_url') or data.get('details_url') or p.get('compare')
        item = t.repository + ':' + scalar(data.get('number') or data.get('id') or p.get('after'))
        extra = {'repository':t.repository,'sha':text(data.get('head_sha') or p.get('after'),80)}
        if '.' in t.event:
            event += '.' + action
    elif t.provider == 'gitlab':
        project = obj(p.get('project'))
        if text(project.get('path_with_namespace')).lower() != t.repository.lower():
            return None
        data, changes = obj(p.get('object_attributes')), obj(p.get('changes'))
        event, action = text(p.get('object_kind')), text(data.get('action'))
        if event == 'note':
            event = 'note.merge_request' if data.get('noteable_type') == 'MergeRequest' else 'note.issue' if data.get('noteable_type') == 'Issue' else ''
        status = text(data.get('status') or data.get('state'))
        branch = text(p.get('ref') or data.get('ref') or data.get('source_branch')).removeprefix('refs/heads/')
        title = data.get('title') or obj(p.get('merge_request') or p.get('issue')).get('title') or (event + ' · ' + branch)
        body, url = data.get('note') or data.get('description'), data.get('url')
        item = t.repository + ':' + scalar(data.get('iid') or data.get('id') or p.get('after'))
        extra = {'repository':t.repository,'sha':text(data.get('sha') or p.get('after'),80)}
        bot = bool(obj(p.get('user')).get('bot'))
    elif t.provider == 'linear':
        data, previous = obj(p.get('data')), obj(p.get('updatedFrom'))
        if p.get('type') != 'Issue' or (data.get('teamId') or obj(data.get('team')).get('id')) != t.team_id:
            return None
        if t.assignee_id and (data.get('assigneeId') or obj(data.get('assignee')).get('id')) != t.assignee_id:
            return None
        if t.label_id and t.label_id not in items(data.get('labelIds')):
            return None
        if t.priority is not None and data.get('priority') != t.priority:
            return None
        status = text(data.get('stateId') or obj(data.get('state')).get('id'))
        changes = {'issue.status_changed':'stateId','issue.priority_changed':'priority','issue.assigned':'assigneeId','issue.moved':'teamId'}
        if p.get('action') == 'create':
            event = 'issue.created'
        elif p.get('action') == 'update':
            field = changes.get(t.event)
            if field and field in previous and previous[field] != data.get(field) and (field != 'assigneeId' or data.get(field)):
                event = t.event
            if t.event == 'issue.labeled':
                added = set(x for x in items(data.get('labelIds')) if isinstance(x,str)) - set(x for x in items(previous.get('labelIds')) if isinstance(x,str))
                if 'labelIds' in previous and added and (not t.label_id or t.label_id in added):
                    event = t.event
        title, body, url, item = data.get('title'), data.get('description'), p.get('url'), data.get('identifier') or data.get('id')
    elif t.provider == 'jira':
        issue, change = obj(p.get('issue')), obj(p.get('changelog'))
        fields, comment = obj(issue.get('fields')), obj(p.get('comment'))
        if t.project and obj(fields.get('project')).get('key') != t.project:
            return None
        if t.assignee_id and obj(fields.get('assignee')).get('accountId') != t.assignee_id:
            return None
        if t.label and t.label not in items(fields.get('labels')):
            return None
        if t.epic and (obj(fields.get('parent')).get('key') or fields.get('customfield_10014')) != t.epic:
            return None
        status = text(obj(fields.get('status')).get('name'))
        hook = p.get('webhookEvent')
        mapping = {'jira:issue_created':'issue.created','jira:issue_updated':'issue.updated',
                   'comment_created':'comment.created','comment_updated':'comment.updated'}
        event = mapping.get(hook, '')
        for entry in items(change.get('items')):
            entry = obj(entry)
            if hook == 'jira:issue_updated' and entry.get('fromString') != entry.get('toString'):
                field = entry.get('field')
                if t.event == 'issue.labeled' and field == 'labels':
                    added = set(text(entry.get('toString')).split()) - set(text(entry.get('fromString')).split())
                    if added and (not t.label or t.label in added):event=t.event
                elif (t.event,field) in {('issue.status_changed','status'),('issue.assigned','assignee')}:
                    if field != 'assignee' or entry.get('to'):event=t.event
        title, body, url, item = fields.get('summary'), comment.get('body') or fields.get('description'), issue.get('self'), issue.get('key')
        if isinstance(body,dict):body=json.dumps(body,ensure_ascii=False)  # Atlassian document format.
    elif t.provider == 'pylon':
        # Pylon outgoing workflow actions supply the issue and changed fields.
        event = text(p.get('event_type') or p.get('event') or p.get('type'))
        data = obj(p.get('data') or p.get('issue'))
        data = obj(data.get('issue')) or data
        aliases = {'issue_created':'issue.created','issue_tag_added':'issue.tag_added','issue_status_changed':'issue.status_changed'}
        event = aliases.get(event,event)
        status = text(data.get('state') or data.get('status'))
        tags = [text(x) if isinstance(x,str) else text(obj(x).get('name')) for x in items(data.get('tags'))]
        if t.label and t.label not in tags and p.get('tag') != t.label:return None
        title, body, url, item = data.get('title'), data.get('body_html') or data.get('description'), data.get('link') or data.get('url'), data.get('id')
    elif t.provider == 'pagerduty':
        envelope = obj(p.get('event'))
        data = obj(envelope.get('data'))
        event = text(envelope.get('event_type'))
        if t.service_id and obj(data.get('service')).get('id') != t.service_id:return None
        if t.urgency and data.get('urgency') != t.urgency:return None
        title, body, url, item = data.get('title'), data.get('description'), data.get('html_url'), data.get('id')
        status = text(data.get('status'))
    elif t.provider == 'slack':
        data = obj(p.get('event'))
        reaction = data.get('type') == 'reaction_added'
        channel = obj(data.get('item')).get('channel') if reaction else data.get('channel')
        if t.channel_id and channel != t.channel_id:return None
        if not isinstance(channel,str) or not re.fullmatch(r'[CG][A-Z0-9]{7,30}',channel):return None
        if data.get('hidden') or p.get('is_ext_shared_channel') or data.get('is_ext_shared_channel'):return None
        if reaction:
            if obj(data.get('item')).get('type') != 'message' or data.get('reaction') != t.reaction:return None
            event, body = 'reaction.added', 'Reaction :' + t.reaction + ': added to this Slack message.'
            ts = obj(data.get('item')).get('ts')
            if t.text_contains or t.text_starts_with:return None  # Reactions do not carry the message text.
        else:
            if data.get('type') != 'message' or data.get('subtype') not in {None,'bot_message','file_share'}:return None
            if data.get('thread_ts') and not t.include_thread_replies:return None
            event, body, ts = 'message.posted', text(data.get('text')), data.get('ts')
        if not isinstance(ts,str) or not re.fullmatch(r'\d{10,16}\.\d{1,9}',ts):return None
        bot = bool(data.get('bot_id') or data.get('bot_profile') or data.get('subtype') == 'bot_message')
        title, item = 'Slack event in ' + channel, channel + ':' + text(ts,30)
        extra = {'channel':channel,'message_ts':text(ts,30),'user':text(data.get('user'),40)}
    else:
        event = text(p.get('event')) if t.event != '*' else '*'
        title, body, url, item = p.get('title') or 'Webhook event', p.get('body'), p.get('url'), p.get('id')
        if body is None:body=json.dumps(payload,ensure_ascii=False)
        if t.payload_pattern:
            try:SchemaValidator(core_schema.str_schema(pattern=t.payload_pattern)).validate_python(json.dumps(payload,ensure_ascii=False))
            except Exception:return None
    if event != t.event or (t.action and action != t.action) or (t.branch and branch != t.branch) or (t.status and status != t.status) or (t.conclusion and conclusion != t.conclusion):return None
    if t.sender_type != 'any' and (t.sender_type == 'bot') != bot:return None
    content = text(body)
    if t.text_contains and t.text_contains.casefold() not in content.casefold():return None
    if t.text_starts_with and not content.lstrip().startswith(t.text_starts_with):return None
    return {'provider':t.provider,'event':event,'action':action,'item':text(item,200),
            'title':text(title,300),'body':content,'url':text(url,1000),'status':status,'conclusion':conclusion,'branch':branch,**extra}


def example(t):
    if t.provider == 'session':
        return {'event':'message.posted', 'session_id':t.session_id or '0' * 32, 'message_id':123,
                'body':(t.text_starts_with or '') + ' ' + (t.text_contains or 'This still fails; please investigate.'),
                'conversation':[{'role':'assistant','content':'The previous attempt is ready to check.'}],
                'context_truncated':False}
    if t.provider == 'github':
        kind, _, action = t.event.partition('.')
        action = t.action or action or {'issues':'opened', 'issue_comment':'created', 'pull_request_review':'submitted',
            'pull_request_review_comment':'created', 'check_run':'completed'}.get(kind,'opened')
        content = t.text_starts_with or t.text_contains or 'Example context'
        issue = {'number':123,'title':'Example issue','body':content,'labels':[{'name':t.label}]}
        payload = {'action':action,'repository':{'full_name':t.repository},
            'sender':{'type':'Bot' if t.sender_type == 'bot' else 'User'}}
        if kind in {'issues','issue_comment'}:
            payload['issue'] = issue
        elif kind.startswith('pull_request'):
            payload['pull_request'] = issue | {'head':{'ref':t.branch or 'main'}}
        if kind in {'issue_comment','pull_request_review_comment'}:
            payload['comment'] = {'id':321,'body':content}
        elif kind == 'pull_request_review':
            payload['review'] = {'id':321,'body':content}
        elif kind == 'check_run':
            payload['check_run'] = {'id':123,'name':'Tests','status':'completed','conclusion':t.conclusion or 'failure',
                'check_suite':{'head_branch':t.branch or 'main'},'output':{'summary':content}}
        elif kind == 'push':
            payload.update({'ref':'refs/heads/'+(t.branch or 'main'),'after':'example-sha','head_commit':{'message':content}})
            payload.pop('action')
        if action == 'labeled':
            payload['label'] = {'name':t.label}
        return payload
    if t.provider == 'linear':
        return {'type':'Issue','action':'create' if t.event=='issue.created' else 'update',
            'data':{'id':'example-issue','identifier':'TEAM-123','title':'Example issue','teamId':t.team_id,
                'assigneeId':t.assignee_id or 'example-assignee','labelIds':[t.label_id or 'example-label'],'stateId':t.status or 'state-new','priority':t.priority if t.priority is not None else 1},
            'updatedFrom':{'assigneeId':None,'labelIds':[],'teamId':'old-team','priority':0 if t.priority == 4 else 4,'stateId':'state-old'}}
    if t.provider == 'slack':
        event={'type':'message','channel':t.channel_id or 'C12345678','text':(t.text_starts_with or t.text_contains or 'Please investigate')+' example','ts':'1234567890.123456','user':'U12345678'}
        if t.event=='reaction.added':event={'type':'reaction_added','reaction':t.reaction,'item':{'type':'message','channel':event['channel'],'ts':event['ts']},'user':'U12345678'}
        if t.sender_type=='bot':event['bot_id']='B12345678'
        return {'event':event}
    if t.provider == 'gitlab':
        kind=t.event.split('.')[0]
        return {'object_kind':kind,'project':{'path_with_namespace':t.repository},'ref':'refs/heads/'+(t.branch or 'main'),
            'object_attributes':{'id':123,'iid':123,'action':t.action or 'open','status':t.status or 'failed','title':'Example work',
                'noteable_type':'MergeRequest' if t.event=='note.merge_request' else 'Issue','note':t.text_starts_with or t.text_contains or 'Example comment'}}
    if t.provider == 'jira':
        hook='jira:issue_created' if t.event=='issue.created' else 'comment_'+t.event.split('.')[1] if t.event.startswith('comment.') else 'jira:issue_updated'
        return {'webhookEvent':hook,'issue':{'key':'PROJ-123','fields':{'summary':'Example issue','description':'Example context',
            'project':{'key':t.project or 'PROJ'},'status':{'name':t.status or 'In Progress'},'assignee':{'accountId':t.assignee_id or 'user-id'},'labels':[t.label or 'moyai'],'parent':{'key':t.epic}}},
            'comment':{'body':t.text_starts_with or t.text_contains or 'Please investigate'},
            'changelog':{'items':[{'field':'labels','fromString':'','toString':t.label or 'moyai'},
                {'field':'status','fromString':'Todo','toString':t.status or 'In Progress'},
                {'field':'assignee','fromString':None,'toString':'Example user','to':t.assignee_id or 'user-id'}]}}
    if t.provider == 'pylon':
        return {'event_type':t.event,'data':{'id':'issue-123','title':'Customer issue','description':'Example context','status':t.status or 'open','tags':[t.label or 'bug']}}
    if t.provider == 'pagerduty':
        return {'event':{'id':'event-123','event_type':t.event,'data':{'id':'incident-123','title':'Service unavailable',
            'service':{'id':t.service_id or 'PSERVICE'},'urgency':t.urgency or 'high','status':t.status or 'triggered'}}}
    return {'id':'example-123','event':t.event if t.event!='*' else 'build.ready','title':'Example event','body':'Context for the workflow.'}
