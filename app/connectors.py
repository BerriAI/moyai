"""Native app adapters. Credentials stay in this process, outside agent sandboxes."""
import asyncio
import html
import json
import re
import time
from typing import Annotated
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .db import Store, now
from .connector_errors import ConnectorError
from .github import GitHub, TOOLS as GITHUB_TOOLS


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Search(Args):
    query: str = Field(min_length=1, max_length=500)


LinearIssueId = Annotated[str, Field(pattern=r"^(?:[A-Za-z][A-Za-z0-9]*-\d+|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$")]


class LinearIssue(Args):
    issue_id: LinearIssueId


class LinearComment(LinearIssue):
    body: str = Field(min_length=1, max_length=10000)


class LinearCreateIssue(Args):
    team_id: str = Field(pattern=r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
    title: str = Field(min_length=1, max_length=250)
    description: str = Field(min_length=1, max_length=20000)
    parent_id: LinearIssueId | None = Field(default=None, description="Optional parent issue identifier (e.g. LIT-9222) or UUID for a new sub-issue.")


class LinearUpdateIssue(LinearIssue):
    parent_id: LinearIssueId | None = Field(description="Required: parent issue identifier (e.g. LIT-9222) or UUID. Explicit null removes the parent.")


class SlackThread(Args):
    channel: str = Field(pattern=r"^[CDG][A-Z0-9]{7,30}$")
    thread_ts: str = Field(pattern=r"^\d{10,16}\.\d{1,9}$")
    as_bot: bool = Field(default=False, description="Use true to verify a DM sent by slack_send. Reads the bot's conversation instead of the shared search account's conversation.")


class SlackSend(Args):
    channel: str = Field(pattern=r"^[CDGUW][A-Z0-9]{7,30}$", description="Channel/conversation ID, or a recipient's Slack user ID (U…/W…) to open a DM from the Moyai Devin bot. Do not reuse another person's DM ID.")
    text: str = Field(min_length=1, max_length=10000, description="Message body only. The server adds the requesting person's name as 'Name: message'; do not add a sender prefix yourself.")


class NotionPage(Args):
    page_id: str = Field(pattern=r"^(?:[a-fA-F0-9]{32}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})$")


class NotionAppend(NotionPage):
    text: str = Field(min_length=1, max_length=2000)


TOOLS = {
    **GITHUB_TOOLS,
    "linear_my_issues": ("linear", False, Args, "Read up to 50 open Linear tickets assigned to the authenticated person running this session, using their verified work email. Never uses the shared connection owner as the assignee."),
    "linear_teams": ("linear", False, Args, "List up to 50 accessible Linear teams and their IDs. Use before creating an issue; ask if the right team is ambiguous."),
    "linear_search": ("linear", False, Search, "Search Linear issue titles by text. Returns at most 20 issues."),
    "linear_issue": ("linear", False, LinearIssue, "Read a Linear issue's description, status, parent, and recent comments. Use to verify reparenting."),
    "linear_comment": ("linear", True, LinearComment, "Add a comment to a Linear issue directly. No administrator approval step is required."),
    "linear_create_issue": ("linear", True, LinearCreateIssue, "Create a Linear ticket with title and Markdown description directly when the user requests it. Optionally set parent_id to create a sub-issue. To reparent an existing ticket, use linear_update_issue instead; do not create a replacement or substitute cross-links. Include relevant source links. No administrator approval step is required; do not retry an uncertain creation automatically. The connected Linear credential needs Create issues permission."),
    "linear_update_issue": ("linear", True, LinearUpdateIssue, "Update an existing Linear ticket's parent using parent_id; explicit null removes its parent. Accepts issue identifiers or UUIDs. Use for sub-issue reparenting without creating new tickets or substituting cross-links. No administrator approval step is required. The connected credential needs issue-update permission. Verify with linear_issue before retrying an uncertain update."),
    "slack_search": ("slack", False, Search, "Search Slack messages visible to the connected account. Returns at most 20 matches."),
    "slack_thread": ("slack", False, SlackThread, "Read up to 50 messages in a Slack thread; has_more indicates truncation. To verify a bot DM sent with slack_send, use as_bot=true and the returned channel and ts as thread_ts."),
    "slack_send": ("slack", True, SlackSend, "Send a Slack message as the Moyai Devin app, never as the shared connection owner. The server prefixes the body with the current requester's profile name: 'Name: message'. For a DM, pass the recipient's Slack user ID as channel; the server opens the bot's own DM. Requires the installed bot; never falls back to a user token. No administrator approval step is required."),
    "notion_search": ("notion", False, Search, "Search Notion page titles visible to the connected integration (not full-text content)."),
    "notion_page": ("notion", False, NotionPage, "Read a Notion page's first 100 top-level blocks. Nested blocks are indicated, not expanded."),
    "notion_append": ("notion", True, NotionAppend, "Append a paragraph to a Notion page directly. No administrator approval step is required."),
}


class Connectors:
    def __init__(self, store: Store, security, settings):
        self.store, self.security, self.settings = store, security, settings
        self.locks = {provider: asyncio.Lock() for provider in ("linear", "slack", "notion", "github")}
        self.github = GitHub(store, security, settings, self)

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
                identity = 'Moyai Devin bot sends · shared user reads' if credentials.get('bot', {}).get('access_token') else 'Shared user reads · bot required to send'
            if provider == 'github':
                identity = 'Organization GitHub App'
            result.append({"id": provider, "connected": bool(row), "scope": "organization",
                           "oauth_configured": self.configured_oauth(provider),
                           "label": row.get("label", ""), "updated_at": row.get("updated_at"),
                           "identity": identity if row else "Not connected", **self.policy(provider),
                           **({'repositories': self.github.targets(), 'app_registered': bool(self.github.app_config())} if provider == 'github' else {}),
                           "tools": [{"name": name, "write": spec[1], "requires_approval": False, "description": spec[3]}
                                     for name, spec in TOOLS.items() if spec[0] == provider]})
        return result

    def policy(self, provider):
        rows = self.store.rows("SELECT enabled,read_only,checked_at,check_status FROM connection_policies WHERE provider=?", (provider,))
        row = rows[0] if rows else {"enabled": True, "read_only": False, "checked_at": None, "check_status": None}
        return {**row, "enabled": bool(row["enabled"]), "read_only": bool(row["read_only"])}

    def allowed(self, name):
        provider, write, _, _ = TOOLS[name]
        policy = self.policy(provider)
        return policy["enabled"] and (not write or not policy["read_only"]) and bool(
            self.store.rows("SELECT provider FROM connections WHERE provider=?", (provider,)))

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
        self.store.execute("INSERT INTO connections VALUES(?,?,?,?) ON CONFLICT(provider) DO UPDATE SET encrypted=excluded.encrypted,label=excluded.label,updated_at=excluded.updated_at",
                           (provider, self.security.encrypt(json.dumps(credentials)), label, now()))

    async def credentials(self, provider):
        async with self.locks[provider]:
            rows = self.store.rows("SELECT * FROM connections WHERE provider=?", (provider,))
            if not rows:
                raise ConnectorError(f"Connect {provider.title()} in Connections first.")
            credentials = json.loads(self.security.decrypt(rows[0]["encrypted"]))
            if credentials.get("expires_at", 0) and credentials["expires_at"] < time.time() + 90:
                if not credentials.get("refresh_token"):
                    raise ConnectorError(f"The {provider.title()} connection expired. Reconnect it.")
                fresh = await self.exchange(provider, refresh_token=credentials["refresh_token"])
                if provider == "slack" and credentials.get("bot") and not fresh.get("bot"):
                    fresh["bot"] = credentials["bot"]
                self.save(provider, fresh, rows[0]["label"])
                return fresh
            return credentials

    def slack_installation(self):
        rows = self.store.rows("SELECT encrypted FROM connections WHERE provider='slack'")
        if not rows:
            return {}
        credentials = json.loads(self.security.decrypt(rows[0]["encrypted"]))
        bot = credentials.get("bot", {})
        return {"installed": bool(bot.get("access_token")), "team_id": bot.get("team", {}).get("id"),
                "user_id": bot.get("bot_user_id"), "scopes": bot.get("scope", "").split(",")}

    async def slack_bot_token(self):
        async with self.locks["slack"]:
            rows = self.store.rows("SELECT * FROM connections WHERE provider='slack'")
            credentials = json.loads(self.security.decrypt(rows[0]["encrypted"])) if rows else {}
            bot = credentials.get("bot", {})
            if not bot.get("access_token"):
                raise ConnectorError("Reconnect Slack to install the workspace bot.")
            if bot.get("expires_at", 0) and bot["expires_at"] < time.time() + 90:
                if not bot.get("refresh_token"):
                    raise ConnectorError("The Slack bot connection expired. Reconnect Slack.")
                fresh = await self.exchange("slack", refresh_token=bot["refresh_token"])
                credentials["bot"] = bot = {**bot, **fresh}
                self.save("slack", credentials, rows[0]["label"])
            return bot["access_token"]

    async def request(self, method, url, *, allowed_errors=(), **kwargs):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.request(method, url, **kwargs)
                if response.status_code >= 400:
                    raise ConnectorError(f"App request failed ({response.status_code}). Check access, scopes, or rate limits.")
                data = response.json()
                if (data.get("ok") is False and data.get("error") not in allowed_errors) or data.get("errors"):
                    raise ConnectorError("The app rejected this operation. Check the connection and its permissions.")
                return data
        except (httpx.HTTPError, ValueError) as exc:
            raise ConnectorError("Could not reach the app. Please retry.") from exc

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

    async def my_linear_issues(self, run):
        user_id = run.get('active_user_id') or run.get('owner_id')
        rows = self.store.rows("""SELECT COALESCE(linked.email,u.email) AS email,
            COALESCE(linked.kind,u.kind) AS kind FROM users u
            LEFT JOIN users linked ON linked.id=u.linked_user_id WHERE u.id=?""", (user_id,))
        if not rows or rows[0]['kind'] != 'google' or not rows[0]['email']:
            raise ConnectorError('Sign in with Google or link your Slack identity before using My Linear tickets.')
        if rows[0]['email'].rpartition('@')[2] not in self.settings.google_domains():
            raise ConnectorError('Your work email is no longer allowed in this workspace.')
        if not self.allowed('linear_my_issues'):
            raise ConnectorError('The Linear connection is disabled.')
        headers = self.headers('linear', await self.credentials('linear'))
        query = """query($email:String!){issues(first:50,filter:{assignee:{email:{eq:$email}},
            state:{type:{nin:["completed","canceled"]}}}){
            nodes{id identifier title description url priority updatedAt state{name type} team{key name}}
            pageInfo{hasNextPage endCursor}}}"""
        result = await self.request('POST', 'https://api.linear.app/graphql', headers=headers,
                                    json={'query': query, 'variables': {'email': rows[0]['email']}})
        return result['data']

    async def linear_issue_uuid(self, issue_id):
        # Resolve identifiers and UUIDs alike before using relationship inputs.
        data = await self.call("linear_issue", {"issue_id": issue_id})
        issue = data.get("issue") or {}
        if not issue.get("id"):
            raise ConnectorError("Linear issue not found or not accessible.")
        return issue["id"]

    async def call(self, name, arguments, *, run=None):
        if name == 'linear_my_issues':
            raise ConnectorError('My Linear tickets requires an authenticated session owner.')
        provider, _, schema, _ = TOOLS[name]
        args = schema.model_validate(arguments).model_dump()
        if not self.allowed(name):
            raise ConnectorError("This operation is disabled by your organization's connection policy.")
        if name == 'slack_send':
            return await self.slack_send(args, run)
        if name == 'slack_thread' and args['as_bot']:
            headers = {'Authorization': f'Bearer {await self.slack_bot_token()}'}
            if args['channel'].startswith('D') and 'im:history' not in self.slack_installation().get('scopes', []):
                raise ConnectorError('Reconnect Slack with the Moyai Devin bot im:history permission to read its DMs.')
        else:
            headers = self.headers(provider, await self.credentials(provider))
        if not self.allowed(name):
            raise ConnectorError("This operation is disabled by your organization's connection policy.")
        if provider == "linear":
            if name == "linear_teams":
                query = "{teams(first:50){nodes{id name key} pageInfo{hasNextPage endCursor}}}"
                variables = {}
            elif name == "linear_create_issue":
                query = "mutation($input:IssueCreateInput!){issueCreate(input:$input){success issue{id identifier title description url parent{id identifier title url}}}}"
                variables = {"input": {"teamId": args["team_id"], "title": args["title"], "description": args["description"]}}
                if args["parent_id"] is not None:
                    variables["input"]["parentId"] = await self.linear_issue_uuid(args["parent_id"])
            elif name == "linear_update_issue":
                issue_id = await self.linear_issue_uuid(args["issue_id"])
                parent_id = await self.linear_issue_uuid(args["parent_id"]) if args["parent_id"] is not None else None
                if issue_id == parent_id:
                    raise ConnectorError("A Linear issue cannot be its own parent.")
                query = "mutation($id:String!,$input:IssueUpdateInput!){issueUpdate(id:$id,input:$input){success issue{id identifier title url parent{id identifier title url}}}}"
                variables = {"id": issue_id, "input": {"parentId": parent_id}}
            elif name == "linear_search":
                query = "query($q:String!){issues(filter:{title:{containsIgnoreCase:$q}},first:20){nodes{id identifier title url state{name}} pageInfo{hasNextPage endCursor}}}"
                variables = {"q": args["query"]}
            elif name == "linear_issue":
                query = "query($id:String!){issue(id:$id){id identifier title description url state{name} parent{id identifier title url} comments(last:10){nodes{id body}}}}"
                variables = {"id": args["issue_id"]}
            else:
                # commentCreate expects the issue UUID; issue(id:) resolves LIT-123 too.
                issue = await self.call("linear_issue", {"issue_id": args["issue_id"]})
                issue_id = issue.get("issue", {}).get("id")
                if not issue_id:
                    raise ConnectorError("Linear issue not found.")
                query = "mutation($input:CommentCreateInput!){commentCreate(input:$input){success comment{id body url}}}"
                variables = {"input": {"issueId": issue_id, "body": args["body"]}}
            # Relationship resolution can await multiple reads. Recheck the write
            # policy in case the connection changed while those were in flight.
            if not self.allowed(name):
                raise ConnectorError("This operation is disabled by your organization's connection policy.")
            result = await self.request("POST", "https://api.linear.app/graphql", headers=headers, json={"query": query, "variables": variables})
            data = result["data"]
            if name == "linear_comment" and not data.get("commentCreate", {}).get("success"):
                raise ConnectorError("Linear did not confirm the comment was created.")
            if name == "linear_create_issue" and not data.get("issueCreate", {}).get("success"):
                raise ConnectorError("Linear did not confirm the issue was created. Verify it before retrying; the credential needs Create issues permission.")
            if name == "linear_update_issue":
                update = data.get("issueUpdate") or {}
                updated_issue = update.get("issue") or {}
                actual_parent = (updated_issue.get("parent") or {}).get("id")
                if (not update.get("success") or updated_issue.get("id") != issue_id
                        or "parent" not in updated_issue or actual_parent != parent_id):
                    raise ConnectorError("Linear did not confirm the parent update. Read the issue to verify its parent before retrying.")
            return data
        if provider == "slack":
            endpoint, method, payload = {
                "slack_search": ("search.messages", "GET", {"query": args.get("query"), "count": 20, "highlight": False}),
                "slack_thread": ("conversations.replies", "GET", {"channel": args.get("channel"), "ts": args.get("thread_ts"), "limit": 50}),
            }[name]
            return await self.request(method, f"https://slack.com/api/{endpoint}", headers=headers, **({"params": payload} if method == "GET" else {"json": payload}))
        if name == "notion_search":
            return await self.request("POST", "https://api.notion.com/v1/search", headers=headers, json={"query": args["query"], "page_size": 20, "filter": {"value": "page", "property": "object"}})
        url = f"https://api.notion.com/v1/blocks/{args['page_id']}/children"
        if name == "notion_page":
            return await self.request("GET", url, headers=headers, params={"page_size": 100})
        return await self.request("PATCH", url, headers=headers, json={"children": [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": args["text"]}}]}}]})

    def slack_sender_name(self, run):
        # Only the broker supplies run context. Never accept a sender from tool
        # arguments or fall back to the connection owner in a shared chat.
        actor = (run or {}).get('active_user_id')
        if not actor and run and not run.get('chat_enabled'):
            actor = run.get('owner_id')
        rows = self.store.rows('SELECT name,email,kind FROM users WHERE id=?', (actor,)) if actor else []
        if not rows or rows[0]['kind'] not in {'google', 'slack'}:
            raise ConnectorError('Slack sends require an identified requester. Sign in with Google or send from your Slack account.')
        label = ' '.join((rows[0]['name'] or rows[0]['email']).split())[:160]
        if not label:
            raise ConnectorError('The Slack requester has no profile name. Reconnect your sign-in before sending.')
        return html.escape(label, quote=False)

    async def slack_send(self, args, run):
        text = f"{self.slack_sender_name(run)}: {args['text']}"
        # Sending must not load or refresh the shared search user's credential.
        # Slack DMs belong to their participants: resolve the recipient as the bot.
        headers = {'Authorization': f'Bearer {await self.slack_bot_token()}'}
        scopes = set(self.slack_installation().get('scopes', []))
        channel = args['channel']
        if 'chat:write' not in scopes:
            raise ConnectorError('Reconnect Slack with the Moyai Devin bot chat:write permission to send messages.')
        if not self.allowed('slack_send'):
            raise ConnectorError("This operation is disabled by your organization's connection policy.")
        if channel.startswith(('U', 'W')):
            if 'im:write' not in scopes:
                raise ConnectorError('Reconnect Slack with the Moyai Devin bot im:write permission to open DMs.')
            result = await self.request('POST', 'https://slack.com/api/conversations.open',
                                        headers=headers, json={'users': channel})
            channel = (result.get('channel') or {}).get('id')
            if not isinstance(channel, str) or not re.fullmatch(r'D[A-Z0-9]{7,30}', channel):
                raise ConnectorError('Slack did not return a bot DM conversation. No message was sent.')
        if not self.allowed('slack_send'):
            raise ConnectorError("This operation is disabled by your organization's connection policy.")
        return await self.request('POST', 'https://slack.com/api/chat.postMessage', headers=headers,
                                  json={'channel': channel, 'text': text,
                                        'unfurl_links': False, 'unfurl_media': False})

    def authorization_url(self, provider, state):
        client_id = getattr(self.settings, f"{provider}_client_id")
        params = {"client_id": client_id, "redirect_uri": self.redirect_uri(provider), "state": state, "response_type": "code"}
        if provider == "linear":
            params.update(scope="read,write", actor="user")
            base = "https://linear.app/oauth/authorize"
        elif provider == "slack":
            params.update(user_scope="search:read,channels:history,groups:history,im:history,mpim:history")
            if self.settings.slack_bot_enabled:
                params["scope"] = "app_mentions:read,chat:write,im:write,im:history,reactions:write,assistant:write,files:write,files:read"
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
                user["bot"] = {key: result[key] for key in ("access_token", "refresh_token", "expires_in", "bot_user_id", "team", "scope") if key in result}
                if user["bot"].get("expires_in"):
                    user["bot"]["expires_at"] = time.time() + user["bot"]["expires_in"]
            result = user
        if not result.get("access_token"):
            raise ConnectorError("The app did not return an access token. Reconnect with the required user scopes.")
        result["kind"] = "oauth"
        if result.get("expires_in"):
            result["expires_at"] = time.time() + result["expires_in"]
        return result
