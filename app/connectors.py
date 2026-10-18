"""Native app adapters. Credentials stay in this process, outside agent sandboxes."""
from .personal_slack import PersonalSlack
import asyncio
import html
import json
import re
import time
from urllib.parse import urlencode

import httpx
from pydantic import Field

from .db import Store, database, now
from .connector_errors import ConnectorError
from .github import GitHub, TOOLS as GITHUB_TOOLS
from .github_coding import READS as GITHUB_CODING_READS
from .slack_origin import metadata as slack_metadata
from .connector_schemas import Args, CursorPage, LinearIssueId, LinearUpdateIssue, LinearOptions
from . import notion_tools, slack_tools


class Search(Args):
    query: str = Field(min_length=1)


class CursorSearch(CursorPage, Search):
    pass


class SlackSearch(Search):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=100)


def slack_search_query(query: str) -> str:
    """Use Slack's channel mention syntax for bare channel-ID filters.

    Preserve quoted search text, including an unfinished quote, and leave all
    other filters intact. No channel lookup or broader retry is needed.
    """
    return re.sub(
        r'"(?:\\.|[^"\\])*(?:"|$)|(?<!\S)(-?in:)#?([CG][A-Z0-9]{7,30})(?=\s|$)',
        lambda match: f'{match[1]}<#{match[2]}>' if match[1] else match[0],
        query,
    )


class LinearIssue(Args):
    issue_id: LinearIssueId


class LinearIssueRead(LinearIssue, CursorPage):
    label_cursor: str = Field(default='', description="Independent label cursor; follow labels_next_arguments.")


class LinearComment(LinearIssue):
    body: str = Field(min_length=1)


class LinearCreateIssue(Args):
    team_id: str = Field(pattern=r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
    title: str = Field(min_length=1, max_length=250)
    description: str = Field(min_length=1)
    parent_id: LinearIssueId | None = Field(default=None, description="Optional parent issue identifier (e.g. LIT-9222) or UUID for a new sub-issue.")


class SlackChannel(CursorPage):
    channel: str = Field(pattern=r"^[CDG][A-Z0-9]{7,30}$")
    as_bot: bool = Field(default=False, description="Use true to verify a DM sent by slack_send. Reads the bot's conversation instead of the shared search account's conversation.")


class SlackThread(SlackChannel):
    thread_ts: str = Field(pattern=r"^\d{10,16}\.\d{1,9}$")


class SlackSend(Args):
    channel: str = Field(pattern=r"^(?:me|[CDGUW][A-Z0-9]{7,30})$", description="Use 'me' to DM the authenticated requester (the owner in an automation run), resolved through their verified Slack/Google account. Otherwise use a channel/conversation or Slack user ID. Do not reuse another person's DM ID.")
    text: str = Field(min_length=1, description="Message body only. The server adds the requesting person's name as 'Name: message'. Long bodies continue in replies at Slack's per-message limit; receipts identify each part.")


class NotionPage(Args):
    page_id: str = Field(pattern=r"^(?:[a-fA-F0-9]{32}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})$")


class NotionRead(NotionPage, CursorPage):
    pass


class NotionAppend(NotionPage):
    text: str = Field(min_length=1)


TOOLS = {
    **GITHUB_TOOLS,
    **notion_tools.TOOLS,
    **slack_tools.TOOLS,
    "linear_my_issues": ("linear", False, CursorPage, "Read open Linear tickets assigned to the authenticated requester, using their verified work email. Follow next_arguments for every page. Never uses the shared connection owner as the assignee."),
    "linear_teams": ("linear", False, CursorPage, "List accessible Linear teams and their IDs. Follow next_arguments for every page. Use before creating an issue; ask if the right team is ambiguous."),
    "linear_search": ("linear", False, CursorSearch, "Search Linear issue titles by text. Follow next_arguments to exhaust results."),
    "linear_issue": ("linear", False, LinearIssueRead, "Read a Linear issue's editable fields, relationship IDs and comments to reconcile updates. Follow next_arguments for comments and labels_next_arguments for labels; the two collections have independent cursors."),
    "linear_comment": ("linear", True, LinearComment, "Add a comment to a Linear issue directly. No administrator approval step is required."),
    "linear_create_issue": ("linear", True, LinearCreateIssue, "Create a Linear ticket with title and Markdown description directly when the user requests it. Optionally set parent_id to create a sub-issue. To reparent an existing ticket, use linear_update_issue instead; do not create a replacement or substitute cross-links. Include relevant source links. No administrator approval step is required; do not retry an uncertain creation automatically. The connected Linear credential needs Create issues permission."),
    "linear_options": ("linear", False, LinearOptions, "Discover valid team states (status IDs), members (assignees), labels, projects or cycles. Use linear_teams or linear_issue for team_id, select resource, and follow next_arguments. Names may be ambiguous; update using returned IDs."),
    "linear_update_issue": ("linear", True, LinearUpdateIssue, "Update supplied fields on an existing issue: title, description, state/status ID, assignee, priority, labels, project, cycle, due date or parent. Omitted fields stay unchanged. Null clears nullable fields; [] clears labels and 0 clears priority. Use linear_options for valid IDs. Parent-only calls remain supported. No administrator approval step is required. Verify with linear_issue before retrying an uncertain update."),
    "slack_search": ("slack", False, SlackSearch, "Search Slack messages visible to the connected account. Use in:channel-name or in:<#CHANNEL_ID>; bare in:CHANNEL_ID is normalized. Follow next_arguments through all available pages; Slack's own search coverage applies."),
    "slack_channel": ("slack", False, SlackChannel, "Read accessible channel history with cursor pagination. Replies require slack_thread. Use as_bot=true for the installed bot's own DM history. Follow next_arguments; coverage describes this collection, not a complete workspace mirror."),
    "slack_thread": ("slack", False, SlackThread, "Read a Slack thread with full cursor pagination. Follow next_arguments. To verify a bot DM sent with slack_send, use as_bot=true and the returned channel and ts as thread_ts."),
    "slack_me": ("slack", False, Args, "Resolve the authenticated requester's verified Slack user ID, workspace, work email and name. Use when asked who the current user is or for their Slack recipient ID, including the owner of an automation run. No email or user ID argument is needed. Never infer the requester from the shared connection owner or a triggering message."),
    "slack_send": ("slack", True, SlackSend, "Send a Slack message as the Moyai app, prefixed with the current requester's name. For 'DM me' use channel='me'; the server resolves the requester's verified account and opens the bot's DM. Save channel='me' in automations that notify their owner. For an explicitly named other recipient, pass their Slack user ID. Requires the installed bot; never falls back to a user token. No administrator approval step is required."),
    "notion_search": ("notion", False, CursorSearch, "Search Notion page titles visible to the integration (not full-text content). Follow next_arguments through all pages."),
    "notion_page": ("notion", False, NotionRead, "Read children of a Notion page or block. Follow next_arguments through all pages. For every has_children block, call again with its ID as page_id; descendant reads are explicit and have independent cursors."),
    "notion_append": ("notion", True, NotionAppend, "Append a paragraph to a Notion page directly. No administrator approval step is required."),
}

# Explicit broker-request replay contract, separate from connection write policy.
# Metadata/identity caches may refresh; no action or context selection is replayed.
# github_checkout here only resolves metadata, before the sandbox changes files.
RETRY_SAFE_READS = frozenset({
    *notion_tools.READS, *slack_tools.READS, 'linear_options',
    *GITHUB_CODING_READS,
    'github_rulesets', 'github_ruleset', 'github_repositories', 'github_repository',
    'github_checkout', 'github_pull_request', 'github_pull_request_comments',
    'github_ci_checks', 'github_workflow_runs', 'github_workflow_jobs', 'github_job_logs',
    'linear_my_issues', 'linear_teams', 'linear_search', 'linear_issue',
    'slack_search', 'slack_thread', 'slack_channel', 'slack_me', 'notion_search', 'notion_page',
})


def paged(data, arguments, *, scope, has_more, cursor=None, page=None):
    """Preserve provider payloads and make collection coverage actionable."""
    next_arguments = None
    if has_more and cursor and cursor != arguments.get('cursor'):
        next_arguments = {**arguments, 'cursor': cursor}
    elif has_more and page is not None and page > arguments.get('page', 0):
        next_arguments = {**arguments, 'page': page}
    warning = ''
    if has_more is None:
        warning = 'The provider did not report pagination coverage.'
    elif has_more and not next_arguments:
        warning = 'The provider reported more items without a usable continuation. Coverage is incomplete.'
    return {**data, 'next_arguments': next_arguments,
            'coverage': {'scope': scope, 'has_more': has_more, 'warning': warning,
                         'consistency': 'live provider pages; concurrent changes may affect enumeration'}}


def linear_page(data, args, collection, scope):
    info = (collection or {}).get('pageInfo', {})
    return paged(data, args, scope=scope, has_more=info.get('hasNextPage'), cursor=info.get('endCursor'))


class Connectors:
    def __init__(self, store: Store, security, settings):
        self.store, self.security, self.settings = store, security, settings
        self.locks = {provider: asyncio.Lock() for provider in ("linear", "slack", "notion", "github")}
        self.github = GitHub(store, security, settings, self)
        self.slack_identities = None
        self.personal_slack = PersonalSlack(store, security, settings, self)

    def check_execution(self, run):
        if not run:
            return
        fresh = self.store.run(run['id'])
        if (not fresh or fresh['deleted_at'] or fresh['deletion_requested_at']
                or fresh['status'] not in {'running', 'reconnecting', 'awaiting_approval'}
                or any(fresh[key] != run[key] for key in ('token_hash', 'active_user_id', 'active_message_id'))):
            raise ConnectorError('The executing session changed. No further action was sent.',
                                 code='execution_changed', dispatched=False)

    def connection_revision(self, provider):
        rows = self.store.rows('SELECT encrypted FROM connections WHERE provider=?', (provider,))
        return rows[0]['encrypted'] if rows else None

    def guard(self, name, run, revision):
        """Recheck after preflight awaits and immediately before external actions."""
        self.check_execution(run)
        provider = TOOLS[name][0]
        fresh = self.store.run(run['id']) if run else None
        if (not self.allowed(name) or self.connection_revision(provider) != revision
                or (fresh and provider not in fresh['plugins'])):
            raise ConnectorError('The connection or session selection changed. No further action was sent.',
                                 code='access_changed', dispatched=False)

    def configured_oauth(self, provider):
        if provider == 'github':
            return True
        return bool(getattr(self.settings, f"{provider}_client_id") and getattr(self.settings, f"{provider}_client_secret"))

    def list(self):
        connected = {row["provider"]: row for row in self.store.rows("SELECT * FROM connections")}
        result = []
        for provider in ("linear", "slack", "notion", "github"):
            row = connected.get(provider, {})
            credentials = json.loads(self.security.decrypt(row["encrypted"])) if row else {}
            identity = ("Shared user OAuth" if provider == "slack" else "OAuth connection") if credentials.get("kind") == "oauth" else ("Personal API key" if provider == "linear" else "Integration token")
            if provider == 'slack':
                identity = 'Moyai bot sends · shared user reads' if credentials.get('bot', {}).get('access_token') else 'Shared user reads · bot required to send'
            if provider == 'github':
                identity = 'Organization GitHub App'
            result.append({"id": provider, "connected": bool(row), "scope": "organization",
                           "oauth_configured": self.configured_oauth(provider),
                           "label": row.get("label", ""), "updated_at": row.get("updated_at"),
                           "identity": identity if row else "Not connected", **self.policy(provider),
                           **({'repositories': self.github.repository_options(), 'app_registered': bool(self.github.app_config())} if provider == 'github' else {}),
                           "tools": [{"name": name, "write": spec[1], "requires_approval": False, "description": spec[3]}
                                     for name, spec in TOOLS.items() if spec[0] == provider]})
        return result

    def policy(self, provider, *, connection=None):
        rows = self.store.rows("SELECT enabled,read_only,checked_at,check_status FROM connection_policies WHERE provider=?", (provider,), connection=connection)
        row = rows[0] if rows else {"enabled": True, "read_only": False, "checked_at": None, "check_status": None}
        return {**row, "enabled": bool(row["enabled"]), "read_only": bool(row["read_only"])}

    def allowed(self, name, run=None, *, as_bot=False):
        provider, write, _, _ = TOOLS[name]
        policy = self.policy(provider)
        if run is not None and not as_bot and name in {'slack_search', 'slack_thread', 'slack_channel'}:
            try:
                owner = self.personal_slack.selected_owner(run)
                if self.personal_slack.configured(owner):
                    self.personal_slack.eligible(run, owner)
                    return policy['enabled'] and ('plugins' not in run or 'slack' in run['plugins'])
            except ConnectorError:
                return False
        return self.organization_allowed(name, policy=policy)

    def organization_allowed(self, name, *, connection=None, policy=None):
        """Organization catalog policy, without personal-account selection."""
        provider, write, _, _ = TOOLS[name]
        policy = policy if policy is not None else self.policy(provider, connection=connection)
        return policy["enabled"] and (not write or not policy["read_only"]) and bool(
            self.store.rows("SELECT provider FROM connections WHERE provider=?", (provider,), connection=connection))

    def enabled_providers(self, *, connection=None):
        """Admission needs provider state, not decrypted UI metadata."""
        return {row['provider'] for row in self.store.rows(
            """SELECT c.provider FROM connections c LEFT JOIN connection_policies p ON p.provider=c.provider
                WHERE c.provider IN ('linear','slack','notion','github') AND coalesce(p.enabled,1)=1""",
            connection=connection)}

    def audit(self, provider, action):
        self.store.execute("INSERT INTO connection_audit(provider,action,actor,created_at) VALUES(?,?,?,?)",
                           (provider, action, "Organization admin", now()))

    def expire_approvals(self, provider):
        names = [name for name, spec in TOOLS.items() if spec[0] == provider and spec[1]]
        with self.store.connect() as conn:
            for name in names:
                conn.execute("UPDATE approvals SET status='expired' WHERE tool=? AND status IN ('pending','approved')", (name,))
            conn.execute("UPDATE runs SET status='running' WHERE status='awaiting_approval' AND NOT EXISTS(SELECT 1 FROM approvals WHERE run_id=runs.id AND status='pending')")

    def record_check(self, provider, status):
        self.store.execute("INSERT INTO connection_policies(provider,checked_at,check_status) VALUES(?,?,?) ON CONFLICT(provider) DO UPDATE SET checked_at=excluded.checked_at,check_status=excluded.check_status",
                           (provider, now(), status))

    def save(self, provider, credentials, label):
        from .connection_authority import new_credentials
        self.store.execute("INSERT INTO connections VALUES(?,?,?,?) ON CONFLICT(provider) DO UPDATE SET encrypted=excluded.encrypted,label=excluded.label,updated_at=excluded.updated_at",
                           (provider, self.security.encrypt(json.dumps(new_credentials(credentials))), label, now()))

    async def credentials(self, provider):
        from .connection_authority import refresh_connection
        async with self.locks[provider]:
            rows = await database(self.store.rows, "SELECT * FROM connections WHERE provider=?", (provider,))
            if not rows:
                raise ConnectorError(f"Connect {provider.title()} in Connections first.", dispatched=False)
            credentials = json.loads(self.security.decrypt(rows[0]["encrypted"]))
            if credentials.get("expires_at", 0) and credentials["expires_at"] < time.time() + 90:
                if not credentials.get("refresh_token"):
                    raise ConnectorError(f"The {provider.title()} connection expired. Reconnect it.", dispatched=False)
                fresh = await self.exchange(provider, refresh_token=credentials["refresh_token"])
                if provider == "slack" and credentials.get("bot") and not fresh.get("bot"):
                    fresh["bot"] = credentials["bot"]
                return await database(
                    refresh_connection, self.store, self.security, provider, rows[0], fresh)
            return credentials

    def slack_installation(self, *, connection=None):
        rows = self.store.rows("SELECT encrypted FROM connections WHERE provider='slack'", connection=connection)
        if not rows:
            return {}
        credentials = json.loads(self.security.decrypt(rows[0]["encrypted"]))
        bot = credentials.get("bot", {})
        return {"installed": bool(bot.get("access_token")), "team_id": bot.get("team", {}).get("id"),
                "user_id": bot.get("bot_user_id"), "bot_id": bot.get("bot_id"), "app_id": bot.get("app_id"), "scopes": bot.get("scope", "").split(",")}

    async def slack_bot_token(self):
        from .connection_authority import refresh_connection
        async with self.locks["slack"]:
            rows = await database(self.store.rows, "SELECT * FROM connections WHERE provider='slack'")
            credentials = json.loads(self.security.decrypt(rows[0]["encrypted"])) if rows else {}
            bot = credentials.get("bot", {})
            if not bot.get("access_token"):
                raise ConnectorError("Reconnect Slack to install the workspace bot.", dispatched=False)
            if bot.get("expires_at", 0) and bot["expires_at"] < time.time() + 90:
                if not bot.get("refresh_token"):
                    raise ConnectorError("The Slack bot connection expired. Reconnect Slack.", dispatched=False)
                fresh = await self.exchange("slack", refresh_token=bot["refresh_token"])
                credentials["bot"] = bot = {**bot, **fresh}
                await database(
                    refresh_connection, self.store, self.security, "slack", rows[0], credentials)
            return bot["access_token"]

    async def request(self, method, url, *, allowed_errors=(), **kwargs):
        dispatched = False
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                dispatched = True
                response = await client.request(method, url, **kwargs)
                if response.status_code >= 400:
                    retry_after = response.headers.get('retry-after', '')
                    raise ConnectorError(f"App request failed ({response.status_code}). Check access, scopes, or rate limits.",
                        code='rate_limited' if response.status_code == 429 else 'access_denied' if response.status_code in {401, 403} else 'provider_http_error',
                        dispatched=True, rejected=400 <= response.status_code < 500,
                        status_code=response.status_code, retryable=response.status_code == 429 or response.status_code >= 500,
                        retry_after=float(retry_after) if retry_after.replace('.', '', 1).isdigit() else None)
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError('Expected a provider object.')
                if data.get("ok") is False and data.get("error") not in allowed_errors:
                    # Slack error codes are identifiers, never arbitrary body text.
                    code = data.get('error', '')
                    code = code if isinstance(code, str) and re.fullmatch(r'[a-z_]+', code) else 'provider_rejected'
                    raise ConnectorError("The app rejected this operation. Check the connection and its permissions.", code=code, dispatched=True, rejected=True)
                if data.get('errors'):
                    raise ConnectorError('The app returned GraphQL errors. Read the destination to verify any mutation.',
                                         code='graphql_errors', dispatched=True)
                return data
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise ConnectorError('Could not connect to the app. No request was sent.',
                                 code='connection_unavailable', dispatched=False, retryable=True) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ConnectorError('The app response was not received or could not be decoded.',
                                 code='response_unavailable', dispatched=dispatched, retryable=True) from exc

    async def verify(self, provider, credentials):
        if provider == 'github':
            return await self.github.verify(credentials)
        token = credentials["access_token"]
        headers = self.headers(provider, credentials)
        if provider == "linear":
            result = await self.request("POST", "https://api.linear.app/graphql", headers=headers, json={"query": "{ viewer { id name organization { name } } }"})
            return result["data"]["viewer"]["organization"]["name"]
        if provider == "slack":
            result = await self.request("POST", "https://slack.com/api/auth.test", headers={"Authorization": f"Bearer {token}"})
            return result.get("team", "Slack workspace")
        result = await self.request("GET", "https://api.notion.com/v1/users/me", headers=headers)
        return result.get("bot", {}).get("workspace_name") or result.get("name") or "Notion workspace"

    @staticmethod
    def headers(provider, credentials):
        token = credentials["access_token"]
        # Linear personal API keys use the raw token; OAuth uses Bearer.
        authorization = token if provider == "linear" and credentials.get("kind") == "personal" else f"Bearer {token}"
        headers = {"Authorization": authorization}
        if provider == "notion":
            headers["Notion-Version"] = "2025-09-03"
        return headers

    async def my_linear_issues(self, run, arguments=None):
        args = CursorPage.model_validate(arguments or {}).model_dump()
        user_id = run.get('active_user_id') or run.get('owner_id')
        rows = self.store.rows("""SELECT COALESCE(linked.email,u.email) AS email,
            COALESCE(linked.kind,u.kind) AS kind FROM users u
            LEFT JOIN users linked ON linked.id=u.linked_user_id WHERE u.id=?""", (user_id,))
        if not rows or rows[0]['kind'] not in {'google', 'cloudflare'} or not rows[0]['email']:
            raise ConnectorError('Sign in with Google or link your Slack identity before using My Linear tickets.', dispatched=False)
        if rows[0]['email'].rpartition('@')[2] not in self.settings.google_domains():
            raise ConnectorError('Your work email is no longer allowed in this workspace.', dispatched=False)
        if not self.allowed('linear_my_issues'):
            raise ConnectorError('The Linear connection is disabled.', dispatched=False)
        headers = self.headers('linear', await self.credentials('linear'))
        query = """query($email:String!,$first:Int!,$after:String){issues(first:$first,after:$after,filter:{assignee:{email:{eq:$email}},
            state:{type:{nin:["completed","canceled"]}}}){
            nodes{id identifier title description url priority updatedAt state{name type} team{key name}}
            pageInfo{hasNextPage endCursor}}}"""
        result = await self.request('POST', 'https://api.linear.app/graphql', headers=headers,
                                    json={'query': query, 'variables': {'email': rows[0]['email'], 'first': args['page_size'], 'after': args['cursor'] or None}})
        data = result['data']
        return linear_page(data, args, data.get('issues'), 'requester open issues')

    async def linear_issue_uuid(self, issue_id, run=None):
        # Resolve identifiers and UUIDs alike before using relationship inputs.
        try:
            data = await self.call("linear_issue", {"issue_id": issue_id}, run=run)
        except ConnectorError as exc:
            raise ConnectorError('Could not resolve the Linear issue. No mutation was sent.',
                                 code=exc.code, dispatched=False, retryable=exc.retryable,
                                 status_code=exc.status_code, retry_after=exc.retry_after) from exc
        issue = data.get("issue") or {}
        if not isinstance(issue, dict) or not issue.get("id"):
            raise ConnectorError("Linear issue not found or not accessible.", dispatched=False)
        return issue["id"]

    async def call(self, name, arguments, *, run=None):
        if name == 'linear_my_issues':
            raise ConnectorError('My Linear tickets requires an authenticated session owner.', dispatched=False)
        provider, _, schema, _ = TOOLS[name]
        args = schema.model_validate(arguments).model_dump()
        selection = None
        if name in {'slack_search', 'slack_thread', 'slack_channel'} and not args.get('as_bot'):
            selection = await self.personal_slack.select(run, name)
        if not self.allowed(name, run=run if selection else None):
            raise ConnectorError("This operation is disabled by your organization's connection policy.", dispatched=False)
        if name == 'slack_send':
            return await self.slack_send(args, run)
        if name in slack_tools.TOOLS:
            return await slack_tools.call(self, name, args, run)
        if name == 'slack_me':
            return await self.slack_identities.requester(run)
        if name in {'slack_thread', 'slack_channel'} and args['as_bot']:
            headers = {'Authorization': f'Bearer {await self.slack_bot_token()}'}
            if args['channel'].startswith('D') and 'im:history' not in self.slack_installation().get('scopes', []):
                raise ConnectorError('Reconnect Slack with the Moyai bot im:history permission to read its DMs.', dispatched=False)
        else:
            headers = self.headers(provider, selection.credentials if selection else await self.credentials(provider))
        if not self.allowed(name, run=run if selection else None):
            raise ConnectorError("This operation is disabled by your organization's connection policy.", dispatched=False)
        if name in notion_tools.TOOLS:
            return await notion_tools.call(self, name, args, headers, run)
        if provider == "linear":
            revision = self.connection_revision(provider)
            issue_fields = """id identifier title description url priority dueDate updatedAt
                team{id key name} state{id name type} assignee{id name email}
                project{id name} cycle{id number name} parent{id identifier title url}
                labels(first:100){nodes{id name} pageInfo{hasNextPage endCursor}}"""
            if name == "linear_teams":
                query = "query($first:Int!,$after:String){teams(first:$first,after:$after){nodes{id name key} pageInfo{hasNextPage endCursor}}}"
                variables = {'first': args['page_size'], 'after': args['cursor'] or None}
            elif name == "linear_options":
                fields = {'states': 'id name type', 'members': 'id name email active',
                          'labels': 'id name', 'projects': 'id name url',
                          'cycles': 'id number name startsAt endsAt'}[args['resource']]
                # Resource/fields are selected from finite server-owned maps.
                query = ("query($team:String!,$first:Int!,$after:String){team(id:$team){id "
                         + args['resource'] + "(first:$first,after:$after){nodes{" + fields
                         + "} pageInfo{hasNextPage endCursor}}}}")
                variables = {'team': args['team_id'], 'first': args['page_size'], 'after': args['cursor'] or None}
            elif name == "linear_create_issue":
                query = "mutation($input:IssueCreateInput!){issueCreate(input:$input){success issue{id identifier title description url parent{id identifier title url}}}}"
                variables = {"input": {"teamId": args["team_id"], "title": args["title"], "description": args["description"]}}
                if args["parent_id"] is not None:
                    variables["input"]["parentId"] = await self.linear_issue_uuid(args["parent_id"], run)
            elif name == "linear_update_issue":
                issue_id = await self.linear_issue_uuid(args["issue_id"], run)
                patch = {}
                mapping = {'title': 'title', 'description': 'description', 'state_id': 'stateId',
                           'assignee_id': 'assigneeId', 'priority': 'priority', 'label_ids': 'labelIds',
                           'project_id': 'projectId', 'cycle_id': 'cycleId', 'due_date': 'dueDate'}
                for key, field in mapping.items():
                    if key in args:
                        patch[field] = args[key]
                if 'parent_id' in args:
                    parent_id = await self.linear_issue_uuid(args["parent_id"], run) if args["parent_id"] is not None else None
                    if issue_id == parent_id:
                        raise ConnectorError("A Linear issue cannot be its own parent.", dispatched=False)
                    patch['parentId'] = parent_id
                query = "mutation($id:String!,$input:IssueUpdateInput!){issueUpdate(id:$id,input:$input){success issue{" + issue_fields + "}}}"
                variables = {"id": issue_id, "input": patch}
            elif name == "linear_search":
                query = "query($q:String!,$first:Int!,$after:String){issues(filter:{title:{containsIgnoreCase:$q}},first:$first,after:$after){nodes{id identifier title url state{name}} pageInfo{hasNextPage endCursor}}}"
                variables = {"q": args["query"], 'first': args['page_size'], 'after': args['cursor'] or None}
            elif name == "linear_issue":
                read_fields = issue_fields.replace('labels(first:100)', 'labels(first:$first,after:$labelAfter)')
                query = "query($id:String!,$first:Int!,$after:String,$labelAfter:String){issue(id:$id){" + read_fields + " comments(first:$first,after:$after){nodes{id body url createdAt user{id name}} pageInfo{hasNextPage endCursor}}}}"
                variables = {"id": args["issue_id"], 'first': args['page_size'], 'after': args['cursor'] or None,
                             'labelAfter': args['label_cursor'] or None}
            else:
                # commentCreate expects the issue UUID; issue(id:) resolves LIT-123 too.
                issue_id = await self.linear_issue_uuid(args["issue_id"], run)
                query = "mutation($input:CommentCreateInput!){commentCreate(input:$input){success comment{id body url}}}"
                variables = {"input": {"issueId": issue_id, "body": args["body"]}}
            # Relationship resolution can await multiple reads. Recheck the write
            # policy in case the connection changed while those were in flight.
            self.guard(name, run, revision)
            result = await self.request("POST", "https://api.linear.app/graphql", headers=headers, json={"query": query, "variables": variables})
            data = result["data"]
            if name == "linear_comment" and not data.get("commentCreate", {}).get("success"):
                raise ConnectorError("Linear did not confirm the comment was created.", dispatched=True)
            if name == "linear_create_issue" and not data.get("issueCreate", {}).get("success"):
                raise ConnectorError("Linear did not confirm the issue was created. Verify it before retrying; the credential needs Create issues permission.", dispatched=True)
            if name == "linear_update_issue":
                update = data.get("issueUpdate") or {}
                updated_issue = update.get("issue") or {}
                confirmed = update.get('success') and updated_issue.get('id') == issue_id
                for field, value in patch.items():
                    if field == 'labelIds':
                        labels = updated_issue.get('labels') or {}
                        confirmed = (confirmed and 'labels' in updated_issue
                                     and not labels.get('pageInfo', {}).get('hasNextPage', True)
                                     and {item['id'] for item in labels.get('nodes', [])} == set(value))
                    elif field.endswith('Id'):
                        relation = field[:-2]
                        confirmed = confirmed and relation in updated_issue and (updated_issue.get(relation) or {}).get('id') == value
                    elif field == 'description':
                        confirmed = confirmed and field in updated_issue and (updated_issue[field] or '') == (value or '')
                    else:
                        confirmed = confirmed and field in updated_issue and updated_issue[field] == value
                if not confirmed:
                    raise ConnectorError("Linear did not confirm the requested update. Read the issue to verify supplied fields before retrying.", dispatched=True)
            if name == 'linear_options':
                self.guard(name, run, revision)
                team = data.get('team') or {}
                if not team.get('id'):
                    raise ConnectorError('Linear team not found or inaccessible.', dispatched=False)
                return linear_page(data, args, team.get(args['resource']), f"team {args['resource']}")
            if name in {'linear_teams', 'linear_search', 'linear_issue'}:
                self.guard(name, run, revision)
                collection = (data.get('issue') or {}).get('comments') if name == 'linear_issue' else data.get('teams' if name == 'linear_teams' else 'issues')
                result = linear_page(data, args, collection, 'issue comments' if name == 'linear_issue' else name)
                if name == 'linear_issue':
                    labels = linear_page({}, {**args, 'cursor': args['label_cursor']},
                                         (data.get('issue') or {}).get('labels'), 'issue labels')
                    continuation = labels['next_arguments']
                    result.update(labels_next_arguments={**args, 'label_cursor': continuation['cursor']} if continuation else None,
                                  labels_coverage=labels['coverage'])
                return result
            return data
        if provider == "slack":
            endpoint, method, payload = {
                "slack_search": ("search.messages", "GET", {"query": slack_search_query(args.get("query", "")), "count": args.get('page_size'), 'page': args.get('page'), "highlight": False}),
                "slack_thread": ("conversations.replies", "GET", {"channel": args.get("channel"), "ts": args.get("thread_ts"), "limit": args.get('page_size')}),
                "slack_channel": ("conversations.history", "GET", {"channel": args.get("channel"), "limit": args.get('page_size')}),
            }[name]
            if args.get('cursor'):
                payload['cursor'] = args['cursor']
            try:
                data = await self.request(method, f"https://slack.com/api/{endpoint}", headers=headers, params=payload)
            except Exception:
                if selection and self.personal_slack.configured(selection.owner):
                    self.personal_slack.record_error(selection.owner, selection.revision)
                    raise ConnectorError('Personal Slack read failed. Retry or reconnect personal Slack in Settings > Personal Slack.') from None
                raise
            if selection:
                self.personal_slack.recheck(selection)
            elif not self.allowed(name):
                raise ConnectorError('The Slack connection is disabled.')
            if name == 'slack_search':
                messages = data.get('messages', {})
                paging = messages.get('paging', {})
                pages = paging.get('pages', messages.get('pagination', {}).get('page_count'))
                return paged(data, args, scope='search matches', has_more=args['page'] < pages if isinstance(pages, int) else None,
                             page=args['page'] + 1)
            return paged(data, args, scope='thread' if name == 'slack_thread' else 'channel roots',
                         has_more=data.get('has_more'), cursor=data.get('response_metadata', {}).get('next_cursor'))
        if name == "notion_search":
            payload = {'query': args['query'], 'page_size': args['page_size'], 'filter': {'value': 'page', 'property': 'object'}}
            if args['cursor']:
                payload['start_cursor'] = args['cursor']
            data = await self.request("POST", "https://api.notion.com/v1/search", headers=headers, json=payload)
            return paged(data, args, scope='page titles', has_more=data.get('has_more'), cursor=data.get('next_cursor'))
        url = f"https://api.notion.com/v1/blocks/{args['page_id']}/children"
        if name == "notion_page":
            params = {'page_size': args['page_size']}
            if args['cursor']:
                params['start_cursor'] = args['cursor']
            data = await self.request("GET", url, headers=headers, params=params)
            return {**paged(data, args, scope='direct block children', has_more=data.get('has_more'), cursor=data.get('next_cursor')),
                    'child_collections': [{'page_id': block['id']} for block in data.get('results', []) if block.get('has_children')]}
        # Notion's API limits each rich-text value to 2,000 characters, arrays
        # to 100 blocks, and payloads to 500 KB. These are transport boundaries;
        # the full requested text remains represented across acknowledged parts.
        results, rich_text, completed = [], [], 0
        async def append_batch(batch):
            self.check_execution(run)
            if not self.allowed(name):
                raise ConnectorError('The organization connection changed.', code='access_changed', dispatched=False)
            return await self.request('PATCH', url, headers=headers, json={'children': batch})
        batches = []
        for offset in range(0, len(args['text']), 2000):
            chunk = {'type': 'text', 'text': {'content': args['text'][offset:offset+2000]}}
            block = {'object': 'block', 'type': 'paragraph', 'paragraph': {'rich_text': [*rich_text, chunk]}}
            if rich_text and (len(rich_text) == 100 or len(json.dumps({'children': [block]}, ensure_ascii=False).encode()) > 500000):
                batches.append(rich_text)
                rich_text = []
            rich_text.append(chunk)
        if rich_text:
            batches.append(rich_text)
        for rich_text in batches:
            children = [{'object': 'block', 'type': 'paragraph', 'paragraph': {'rich_text': rich_text}}]
            try:
                result = await append_batch(children)
            except ConnectorError as exc:
                return {**exc.receipt(write=True), 'acknowledged_results': results, 'completed_characters': completed,
                        'instruction': 'Earlier parts are acknowledged. Verify the failed part at the destination before continuing; do not append the entire original text again.'}
            results.extend(result.get('results', []))
            completed += sum(len(item['text']['content']) for item in rich_text)
        return {**result, 'results': results, 'completed_characters': completed, 'complete': True}

    def slack_sender_name(self, run):
        # Only the broker supplies run context. Never accept a sender from tool
        # arguments or fall back to the connection owner in a shared chat.
        actor = (run or {}).get('active_user_id')
        if not actor and run and not run.get('chat_enabled'):
            actor = run.get('owner_id')
        rows = self.store.rows('SELECT name,email,kind FROM users WHERE id=?', (actor,)) if actor else []
        if not rows or rows[0]['kind'] not in {'google', 'cloudflare', 'slack'}:
            raise ConnectorError('Slack sends require an identified requester. Sign in with Google or send from your Slack account.', dispatched=False)
        label = ' '.join((rows[0]['name'] or rows[0]['email']).split())
        if not label:
            raise ConnectorError('The Slack requester has no profile name. Reconnect your sign-in before sending.', dispatched=False)
        return html.escape(label, quote=False)

    async def slack_send(self, args, run, *, thread_ts=None, operation='slack_send', revision=None):
        text = f"{self.slack_sender_name(run)}: {args['text']}"
        installation = self.slack_installation()
        identity = await self.slack_identities.requester(run) if args['channel'] == 'me' else None
        # Sending must not load or refresh the shared search user's credential.
        # Slack DMs belong to their participants: resolve the recipient as the bot.
        headers = {'Authorization': f'Bearer {await self.slack_bot_token()}'}
        if revision is None:
            revision = self.connection_revision('slack')
        scopes = set(self.slack_installation().get('scopes', []))
        channel = identity['user_id'] if identity else args['channel']
        if identity:
            self.slack_identities.check_requester(run, installation)
        if 'chat:write' not in scopes:
            raise ConnectorError('Reconnect Slack with the Moyai bot chat:write permission to send messages.', dispatched=False)
        if not self.allowed('slack_send'):
            raise ConnectorError("This operation is disabled by your organization's connection policy.", dispatched=False)
        if channel.startswith(('U', 'W')):
            if 'im:write' not in scopes:
                raise ConnectorError('Reconnect Slack with the Moyai bot im:write permission to open DMs.', dispatched=False)
            result = await self.request('POST', 'https://slack.com/api/conversations.open',
                                        headers=headers, json={'users': channel})
            channel = (result.get('channel') or {}).get('id')
            if not isinstance(channel, str) or not re.fullmatch(r'D[A-Z0-9]{7,30}', channel):
                raise ConnectorError('Slack did not return a bot DM conversation. No message was sent.', dispatched=False)
        if identity:
            self.slack_identities.check_requester(run, installation)
            if await self.slack_identities.requester(run) != identity:
                raise ConnectorError('Your Slack recipient changed. Resolve your identity again before sending.', dispatched=False)
            self.slack_identities.check_requester(run, installation)
        if not self.allowed('slack_send'):
            raise ConnectorError("This operation is disabled by your organization's connection policy.", dispatched=False)
        parts = []
        for offset in range(0, len(text), 40000):  # Slack's text field limit.
            part = text[offset:offset+40000]
            payload = {'channel': channel, 'text': part, 'metadata': slack_metadata(self, channel, part),
                       'unfurl_links': False, 'unfurl_media': False}
            if thread_ts or parts:
                payload['thread_ts'] = thread_ts or parts[0]['ts']
            try:
                self.guard(operation, run, revision)
                if identity:
                    self.slack_identities.check_requester(run, installation)
                result = await self.request('POST', 'https://slack.com/api/chat.postMessage', headers=headers, json=payload)
                if not result.get('ts') or (thread_ts and (result.get('ok') is not True or result.get('channel') != channel)):
                    raise ConnectorError('Slack did not return the message receipt.', code='missing_receipt', dispatched=True)
            except ConnectorError as exc:
                return {**exc.receipt(write=True), 'acknowledged_messages': parts, 'completed_formatted_characters': offset,
                        'instruction': 'Earlier parts are acknowledged. Read the destination to verify the failed part before continuing; do not resend the full message.'}
            parts.append({'channel': result.get('channel', channel), 'ts': result['ts']})
        return {**result, 'channel': parts[0]['channel'], 'ts': parts[0]['ts'], 'parts': parts, 'complete': True}

    def authorization_url(self, provider, state):
        client_id = getattr(self.settings, f"{provider}_client_id")
        params = {"client_id": client_id, "redirect_uri": self.redirect_uri(provider), "state": state, "response_type": "code"}
        if provider == "linear":
            params.update(scope="read,write", actor="user")
            base = "https://linear.app/oauth/authorize"
        elif provider == "slack":
            params.update(user_scope="search:read,channels:history,groups:history,im:history,mpim:history")
            if self.settings.slack_bot_enabled:
                params["scope"] = "app_mentions:read,chat:write,im:write,im:history,im:read,mpim:read,channels:read,groups:read,reactions:read,reactions:write,assistant:write,files:write,files:read"
                if self.settings.slack_identity_linking_enabled:
                    params["scope"] += ",users:read,users:read.email"
                if self.settings.slack_thread_chat_enabled:
                    params["scope"] += ",channels:history,groups:history"
            base = "https://slack.com/oauth/v2/authorize"
        else:
            params["owner"] = "user"
            base = "https://api.notion.com/v1/oauth/authorize"
        return base + "?" + urlencode(params)

    def redirect_uri(self, provider):
        return f"{self.settings.public_url.rstrip('/')}/oauth/{provider}/callback"

    async def exchange(self, provider, *, code=None, refresh_token=None):
        client_id = getattr(self.settings, f"{provider}_client_id")
        client_secret = getattr(self.settings, f"{provider}_client_secret")
        payload = {"grant_type": "refresh_token" if refresh_token else "authorization_code"}
        if refresh_token:
            payload["refresh_token"] = refresh_token
        else:
            payload.update(code=code, redirect_uri=self.redirect_uri(provider))
        if provider == "notion":
            result = await self.request("POST", "https://api.notion.com/v1/oauth/token", auth=(client_id, client_secret), json=payload)
        else:
            payload.update(client_id=client_id, client_secret=client_secret)
            result = await self.request("POST", "https://api.linear.app/oauth/token" if provider == "linear" else "https://slack.com/api/oauth.v2.access", data=payload)
        if provider == "slack" and result.get("authed_user"):
            # Preserve the workspace bot independently from the user token used
            # by Slack search. User-token refresh must not erase the bot grant.
            user = result["authed_user"]
            if result.get("access_token"):
                user["bot"] = {key: result[key] for key in ("access_token", "refresh_token", "expires_in", "bot_user_id", "app_id", "bot_id", "team", "scope") if key in result}
                if user["bot"].get("expires_in"):
                    user["bot"]["expires_at"] = time.time() + user["bot"]["expires_in"]
            result = user
        if not result.get("access_token"):
            raise ConnectorError("The app did not return an access token. Reconnect with the required user scopes.", dispatched=False)
        result["kind"] = "oauth"
        if result.get("expires_in"):
            result["expires_at"] = time.time() + result["expires_in"]
        return result
