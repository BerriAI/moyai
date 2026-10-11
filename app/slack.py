"""Signed Slack events start and continue channel-thread and DM sessions."""
import asyncio
import hashlib
import hmac
import json
import re
import time
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException

from .connectors import ConnectorError
from .agentchat_slack import connect_agentchat
from .db import now
from .slack_chat import SlackChat, USER_MENTION
from .slack_notifications import SlackNotifications
from .slack_files import SlackFiles, file_ids as slack_file_ids
from .slack_credentials import SlackCredentials
from .slack_feedback import SlackFeedback
from .slack_references import attachment_reference
from .slack_origin import is_self


# Literal examples are context, not recipients. Preserve them in the prompt.
# re.M anchors quote prefixes; \Z keeps fences open across line endings.
QUOTED_TEXT = re.compile(r'```[\s\S]*?(?:```|\Z)|`[^`\n]*(?:`|$)|^[ \t]*>>>[\s\S]*|^[ \t]*>[^\n]*', re.M)


def routing_text(text: str, mention: str) -> tuple[str, str]:
    parts, visible = [], []
    end = 0
    for match in QUOTED_TEXT.finditer(text):
        plain = text[end:match.start()]
        visible.append(plain)
        parts.extend((plain.replace(mention, ''), match[0]))
        visible.append('\n')
        end = match.end()
    plain = text[end:]
    visible.append(plain)
    parts.append(plain.replace(mention, ''))
    return ''.join(visible), ''.join(parts).strip()


class SlackSessions:
    def __init__(self, store, connectors, manager, checkpoints, settings, lens_feedback=None):
        self.store, self.connectors, self.manager = store, connectors, manager
        self.checkpoints, self.settings = checkpoints, settings
        self.lens_feedback = lens_feedback
        self.jobs = set()
        self.identities = None
        self.automation_events = None
        self.session_titles = None
        self.files = SlackFiles(self)
        from .slack_sources import SlackSources
        self.sources = SlackSources(self.store, owner=self)
        self.store.slack_sources = self.sources
        self.chat = SlackChat(self)
        self.notifications = SlackNotifications(self)
        self.store.public_delivery = self.chat.settle_public_in
        self.access = SlackCredentials(self)
        self.feedback = SlackFeedback(self, lens_feedback)
        self.agentchat, self.channel = connect_agentchat(self)

    def status(self):
        bot = self.connectors.slack_installation()
        enabled = bool(self.settings.slack_bot_enabled and self.settings.slack_signing_secret
                       and self.settings.slack_session_users and bot.get("installed")
                       and self.connectors.policy("slack")["enabled"])
        latest = self.store.rows("SELECT created_at,reply_status FROM slack_events ORDER BY created_at DESC LIMIT 1")
        return {"enabled": enabled, "bot_installed": bot.get("installed", False),
                "thread_chat_enabled": self.settings.slack_thread_chat_enabled,
                "messaging_adapter": "agentchat",
                "reaction_ready": "reactions:write" in set(bot.get("scopes", [])),
                "direct_message_ready": self.settings.slack_thread_chat_enabled and self.settings.slack_dm_enabled and "im:history" in set(bot.get("scopes", [])),
                "thread_reply_ready": self.settings.slack_thread_chat_enabled and {"channels:history", "groups:history"} <= set(bot.get("scopes", [])),
                "bot_user_id": bot.get("user_id"), "team_id": bot.get("team_id"),
                "audience": "Workspace members" if self.settings.slack_session_users == "*" else "Selected Slack users",
                "last_session": latest[0] if latest else None}

    def verify_signature(self, body, headers):
        secret = self.settings.slack_signing_secret
        timestamp = headers.get("x-slack-request-timestamp", "")
        try:
            valid_time = abs(time.time() - int(timestamp)) <= 300
        except ValueError:
            valid_time = False
        if not secret or not valid_time:
            raise HTTPException(401, "Invalid Slack request.")
        expected = "v0=" + hmac.new(secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, headers.get("x-slack-signature", "")):
            raise HTTPException(401, "Invalid Slack request.")

    async def receive(self, request, missing_cloud):
        body = await request.body()
        self.verify_signature(body, request.headers)
        try:
            payload = json.loads(body)
        except ValueError:
            raise HTTPException(400, "Invalid Slack event.")
        if not isinstance(payload, dict):
            raise HTTPException(400, "Invalid Slack event.")
        return await self.receive_payload(payload, missing_cloud)

    async def receive_payload(self, payload, missing_cloud):
        """Process a signature-verified event (direct request or durable inbox)."""
        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge", "")}
        if payload.get("type") != "event_callback" or not self.status()["enabled"]:
            return {"ok": True}
        event = payload.get("event", {})
        if (not isinstance(event, dict) or event.get("type") not in {"app_mention", "message", "reaction_added", "reaction_removed"}
                or payload.get("is_ext_shared_channel") or event.get("is_ext_shared_channel")):
            return {"ok": True}
        bot = self.connectors.slack_installation()
        allowed_users = {x.strip() for x in self.settings.slack_session_users.split(",")}
        if payload.get("team_id") != bot.get("team_id"):
            return {"ok": True}
        event_id = payload.get('event_id', '')
        if not isinstance(event_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', event_id):
            raise HTTPException(400, 'Invalid Slack event identifier.')
        observed_channel = event.get('channel') or (event.get('item') or {}).get('channel', '')
        if observed_channel.startswith('D') and not self.settings.slack_dm_enabled:
            return {'ok': True}
        authored = event.get('text', '')
        if not isinstance(authored, str):
            raise HTTPException(400, 'Invalid Slack message text.')
        visible_source, authored_prompt = routing_text(authored, f"<@{bot.get('user_id')}>")
        from .slack_chat import explicit_control
        control = explicit_control(authored_prompt)
        aside = bool(re.match(r'^\s*(?:!aside|\(aside\))(?:\s|$)', authored_prompt, re.I))
        direct = (control or event.get('type') == 'app_mention' or f"<@{bot.get('user_id')}>" in visible_source or
                  (event.get('channel_type') == 'im' and event.get('subtype') not in {'message_changed', 'message_deleted'}))
        direct = direct and event.get('subtype') in {None, 'file_share'} and event.get('type') in {'message', 'app_mention'} and not (event.get('bot_id') or event.get('bot_profile')) and ('*' in allowed_users or event.get('user') in allowed_users)
        source_run = self.sources.observe(bot['team_id'], event_id, event, bot.get('user_id'),
            wake=self.settings.slack_thread_chat_enabled and not direct and not aside)
        await self.checkpoints.flush()
        if source_run:
            self.manager.submit(self.store.run(source_run))
        if is_self(self.connectors, event, observed_channel):
            return {'ok': True}
        if '*' not in allowed_users and event.get('user') not in allowed_users:
            return {'ok': True}
        if self.automation_events:
            await self.automation_events.slack(payload)
        if (event.get('type') in {'reaction_added', 'reaction_removed'} or event.get('bot_id') or event.get('bot_profile')
                or event.get('subtype') not in {None, 'file_share'}):
            return {'ok': True}
        event_id, channel, user = payload.get("event_id", ""), event.get("channel", ""), event.get("user", "")
        mention_ts = event.get("ts", "")
        thread_ts = event.get("thread_ts") or mention_ts
        text = event.get("text", "")
        if not all(isinstance(v, str) for v in (event_id, channel, user, thread_ts, mention_ts, text)):
            raise HTTPException(400, "Invalid Slack event fields.")
        if not (re.fullmatch(r"[A-Za-z0-9_-]{1,100}", event_id)
                and re.fullmatch(r"[CGD][A-Z0-9]{7,30}", channel)
                and re.fullmatch(r"[UW][A-Z0-9]{7,30}", user)
                and re.fullmatch(r"\d{10,16}\.\d{1,9}", thread_ts)
                and re.fullmatch(r"\d{10,16}\.\d{1,9}", mention_ts)
                and Decimal(thread_ts) <= Decimal(mention_ts)):
            raise HTTPException(400, "Invalid Slack event fields.")
        mention = f"<@{bot.get('user_id')}>"
        visible, prompt = routing_text(text, mention)
        bound_thread = (not channel.startswith('D') and self.settings.slack_thread_chat_enabled
                        and self.store.rows('SELECT 1 FROM slack_threads WHERE team_id=? AND channel=? AND thread_ts=?',
                                            (bot['team_id'], channel, thread_ts)))
        if bound_thread and not direct:
            return {'ok': True}  # Stored observation, coalesced wake; no invented user request.
        file_ids = slack_file_ids(event.get('files'))
        reference, _ = attachment_reference(event)
        if not prompt and reference:
            prompt = 'Please respond to the quoted Slack attachment.'
        if not prompt and not file_ids:
            return {"ok": True}
        if file_ids and not prompt:
            prompt = 'Please respond to the attached files.'
        direct_message = event.get('type') == 'message' and event.get('channel_type') == 'im' and channel.startswith('D')
        if channel.startswith('D') and (not direct_message or not self.settings.slack_dm_enabled or not self.settings.slack_thread_chat_enabled):
            return {'ok': True}
        await self.channel.handle_validated_event(team=bot['team_id'], event_id=event_id, channel=channel,
            ts=mention_ts, root=thread_ts, user=user, prompt=prompt, mentioned=mention in visible,
            direct_message=direct_message, missing_cloud=missing_cloud, file_ids=file_ids, reference=reference)
        return {'ok': True}

    async def accept_message(self, *, team, event_id, channel, ts, root, user, prompt,
                             mentioned, direct_message, missing_cloud, file_ids=(), reference=''):
        if self.settings.slack_thread_chat_enabled:
            try:
                run = self.chat.accept(team=team, event_id=event_id, channel=channel, ts=ts,
                                       root=root, user=user, prompt=prompt, mentioned=mentioned,
                                       direct_message=direct_message, missing_cloud=missing_cloud, file_ids=file_ids, reference=reference)
            except ValueError as exc:
                raise HTTPException(503, str(exc))
            await self.checkpoints.flush()
            if self.identities:
                self.identities.wake.set()
            if run:
                self.manager.submit(run)
                if self.session_titles:
                    await self.session_titles.request(run['id'])
            return
        if not mentioned or len(prompt) < 3:
            return
        if missing_cloud:
            raise HTTPException(503, 'Cloud sessions are not configured.')
        plugins = [x['id'] for x in self.connectors.list() if x['connected'] and x['enabled']]
        try:
            run = self.store.create_slack_run(event_id, prompt + reference, plugins, channel, root, user, ts, team, file_ids=file_ids,
                                            harness=self.settings.default_harness(), model=self.settings.resolve_model())
        except ValueError:
            raise HTTPException(503, 'The session queue is full.')
        if run:
            await self.checkpoints.flush()
            if self.identities:
                self.identities.wake.set()
            self.manager.submit(run)
            self.submit_reply(run['id'])
            if self.session_titles:
                await self.session_titles.request(run['id'])

    async def prepare(self, run_id):
        await self.prepare_source(run_id)
        self.files.include_context(run_id)
        await self.files.prepare(run_id)

    async def prepare_source(self, run_id):
        """Runs in the session job, never on Slack's acknowledgement path.

        Reads synchronize live source versions; retrying never repeats agent work.
        """
        source = self.store.slack_source(run_id)
        if not source:
            return
        self.store.execute("UPDATE slack_events SET context_status='fetching' WHERE run_id=?", (run_id,))
        self.store.event(run_id, "context", "Reading the Slack conversation behind your request")
        context = {"messages": [], "kind": "thread",
                   "captured_at": now(), "truncated": False, "permalink": ""}
        if source['channel'].startswith('D'):
            # A DM begins with this explicitly addressed request. Subsequent
            # turns use saved session history; do not import earlier DMs through
            # the shared search account, which may belong to another teammate.
            context.update(kind='dm', messages=[{'ts': source['mention_ts'], 'user': source['user_id'],
                           'text': self.store.run(run_id)['prompt']}])
            context.update(self.sources.snapshot(run_id))
            self.store.execute("UPDATE slack_events SET context_status='ready',context_json=? WHERE run_id=?", (json.dumps(context), run_id))
            self.store.event(run_id, 'context', 'Using the direct message and saved conversation')
            await self.checkpoints.flush()
            return
        try:
            if not self.connectors.allowed("slack_thread"):
                raise ConnectorError("Slack is unavailable.")
            headers = self.connectors.headers("slack", await self.connectors.credentials("slack"))
            context.update(await self.read_context(source, headers))
            context.update(self.sources.snapshot(run_id))
            if not self.connectors.allowed("slack_thread"):
                raise ConnectorError("Slack access was paused.")
            # A permalink is helpful but must not discard successfully read context.
            try:
                async with asyncio.timeout(3):
                    link = await self.connectors.request("GET", "https://slack.com/api/chat.getPermalink", headers=headers,
                                                         params={"channel": source["channel"], "message_ts": source["mention_ts"]})
                    url = link.get("permalink", "")
                    if re.fullmatch(r"https://[A-Za-z0-9-]+\.slack\.com/archives/[A-Z0-9]+/p\d+(?:\?[^\s]*)?", url):
                        context["permalink"] = url
            except (ConnectorError, TimeoutError):
                pass
            status = "ready"
            message = f"Read {len(context['messages'])} Slack messages from the {context['kind']}"
            if context["truncated"]:
                message += " (limited context; see source details)"
        except asyncio.CancelledError:
            self.store.execute("UPDATE slack_events SET context_status='pending' WHERE run_id=?", (run_id,))
            raise
        except Exception:
            status = "unavailable"
            installation = self.connectors.slack_installation()
            self.store.execute("UPDATE slack_source_coverage SET status='incomplete',updated_at=? WHERE team_id=? AND channel=? AND thread_ts=?",
                (now(), installation.get('team_id'), source['channel'], source['thread_ts']))
            context.update(self.sources.snapshot(run_id))
            context["warning"] = "Live Slack synchronization is unavailable. Saved observations are retained; use slack_context for their versions and coverage. Do not assume the source is complete."
            message = "Slack conversation could not be read; the agent will be told context is missing"
        self.store.execute("UPDATE slack_events SET context_status=?,context_json=? WHERE run_id=?", (status, json.dumps(context), run_id))
        self.store.event(run_id, "context", message)
        await self.checkpoints.flush()

    async def read_context(self, source, headers):
        # Every channel mention belongs to one thread, including a new root.
        # Channel history would import unrelated neighboring tasks.
        params = {"channel": source["channel"], "ts": source["thread_ts"], "inclusive": True, "limit": 100, "include_all_metadata": True}
        messages, cursor, seen = {}, '', set()
        installation = self.connectors.slack_installation()
        scope = (installation['team_id'], source['channel'], source['thread_ts'])
        while True:
            if cursor:
                params['cursor'] = cursor
            data = await self.connectors.request('GET', 'https://slack.com/api/conversations.replies', headers=headers, params=params)
            if self.connectors.slack_installation().get('team_id') != installation.get('team_id') or not self.status()['enabled']:
                raise ConnectorError('Slack source access changed during synchronization.')
            if not isinstance(data.get('messages'), list):
                raise ConnectorError('Slack returned no message list.')
            for item in data['messages']:
                if not isinstance(item, dict) or not isinstance(item.get('ts'), str):
                    continue
                ts = item['ts']
                if not re.fullmatch(r'\d{10,16}\.\d{1,9}', ts) or Decimal(ts) < Decimal(source['thread_ts']):
                    continue
                if ts != source['thread_ts'] and item.get('thread_ts') != source['thread_ts']:
                    continue
                version = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
                self.sources.observe(scope[0], 'backfill:' + source['channel'] + ':' + ts + ':' + version,
                                     {**item, 'type': 'message', 'channel': source['channel']}, installation.get('user_id'))
                reference, _ = attachment_reference(item)
                messages[ts] = {'ts': ts, 'user': item.get('user') or item.get('bot_id') or 'unknown',
                    'author_kind': 'self' if item.get('user') == installation.get('user_id') else 'bot' if item.get('bot_id') else 'human',
                    'text': (item.get('text') or '') + reference, 'text_truncated': False,
                    'has_attachments': bool(item.get('files') or item.get('attachments')), 'file_ids': slack_file_ids(item.get('files'))}
            cursor = data.get('response_metadata', {}).get('next_cursor', '')
            incomplete = bool(data.get('has_more')) and not cursor
            if cursor in seen:
                incomplete, cursor = True, ''
            self.store.execute("""INSERT INTO slack_source_coverage(team_id,channel,thread_ts,status,cursor,updated_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(team_id,channel,thread_ts) DO UPDATE SET status=excluded.status,cursor=excluded.cursor,updated_at=excluded.updated_at""",
                (*scope, 'fetching' if cursor else 'incomplete' if incomplete else 'synchronized', cursor, now()))
            if not cursor:
                break
            seen.add(cursor)
        ordered = sorted(messages.values(), key=lambda item: Decimal(item['ts']))
        warning = 'Slack did not supply a usable continuation cursor; coverage is incomplete.' if incomplete else ''
        if source['thread_ts'] not in messages:
            warning += ' The thread root was not returned.'
        return {'messages': ordered, 'truncated': incomplete, 'warning': warning,
                'coverage': 'incomplete' if incomplete else 'synchronized',
                'retrieval_tool': 'slack_context', 'captured_at': now()}

    def submit_reply(self, run_id):
        job = asyncio.create_task(self.reply(run_id))
        self.jobs.add(job)
        job.add_done_callback(self.jobs.discard)

    async def reply(self, run_id):
        # Persist a claim before the external write. A lost response is never
        # automatically retried, including after process restart.
        claimed = self.store.execute("UPDATE slack_events SET reply_status='sending' WHERE run_id=? AND reply_status='pending'", (run_id,))
        if not claimed:
            return
        row = self.store.rows("SELECT channel,thread_ts FROM slack_events WHERE run_id=?", (run_id,))[0]
        try:
            await self.checkpoints.flush()
            if not self.status()["enabled"]:
                self.store.execute("UPDATE slack_events SET reply_status='skipped' WHERE run_id=?", (run_id,))
                return
            token = await self.connectors.slack_bot_token()
            url = f"{self.settings.public_url.rstrip('/')}/#run={run_id}"
            message = f"Created a cloud session for your request. <{url}|Open session>\nResults stay in the web app. Organization sign-in is required."
            await self.connectors.request("POST", "https://slack.com/api/chat.postMessage",
                                          headers={"Authorization": f"Bearer {token}"},
                                          json={"channel": row["channel"], "thread_ts": row["thread_ts"], "text": message,
                                                "unfurl_links": False, "unfurl_media": False})
            self.store.execute("UPDATE slack_events SET reply_status='sent' WHERE run_id=?", (run_id,))
        except asyncio.CancelledError:
            self.store.execute("UPDATE slack_events SET reply_status='uncertain' WHERE run_id=?", (run_id,))
            raise
        except Exception:
            self.store.execute("UPDATE slack_events SET reply_status='uncertain' WHERE run_id=?", (run_id,))
            self.store.event(run_id, "status", "Slack session-link reply could not be confirmed; it will not be retried automatically.")
        finally:
            await self.checkpoints.flush()

    def recover(self):
        self.store.execute("UPDATE slack_events SET reply_status='uncertain' WHERE reply_status='sending'")
        for row in self.store.rows("SELECT run_id FROM slack_events WHERE reply_status='pending' AND run_id NOT IN (SELECT run_id FROM slack_threads)"):
            self.submit_reply(row["run_id"])
        self.chat.recover()
        self.notifications.recover()

    async def shutdown(self, *, graceful=False):
        await self.notifications.shutdown(graceful=graceful)
        await self.chat.shutdown(graceful=graceful)
        jobs = list(self.jobs)
        if not graceful:
            for job in jobs:
                job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        await self.agentchat.close()
