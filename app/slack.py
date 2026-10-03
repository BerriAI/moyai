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
from .slack_chat import SlackChat


class SlackSessions:
    def __init__(self, store, connectors, manager, checkpoints, settings):
        self.store, self.connectors, self.manager = store, connectors, manager
        self.checkpoints, self.settings = checkpoints, settings
        self.jobs = set()
        self.identities = None
        self.automation_events = None
        self.chat = SlackChat(self)
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
        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge", "")}
        if payload.get("type") != "event_callback" or not self.status()["enabled"]:
            return {"ok": True}
        event = payload.get("event", {})
        if (not isinstance(event, dict) or event.get("type") not in {"app_mention", "message", "reaction_added"}
                or payload.get("is_ext_shared_channel") or event.get("is_ext_shared_channel")):
            return {"ok": True}
        bot = self.connectors.slack_installation()
        allowed_users = {x.strip() for x in self.settings.slack_session_users.split(",")}
        if payload.get("team_id") != bot.get("team_id") or ("*" not in allowed_users and event.get("user") not in allowed_users):
            return {"ok": True}
        if event.get("user") == bot.get("user_id"):
            return {"ok": True}
        event_id = payload.get('event_id', '')
        if not isinstance(event_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', event_id):
            raise HTTPException(400, 'Invalid Slack event identifier.')
        if self.automation_events:
            await self.automation_events.slack(payload)
        if (event.get('type') == 'reaction_added' or event.get('bot_id') or event.get('bot_profile')
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
        # A mention can be the subject of a question to somebody else:
        # "@OtherAgent what is @Moyai?" must not wake both agents.
        addressed = re.match(r'^\s*((?:<@[UW][A-Z0-9]{7,30}>[,:]?\s*)+)', text)
        if addressed and mention not in addressed[1]:
            return {'ok': True}
        prompt = text.replace(mention, "").strip()
        if not prompt:
            return {"ok": True}
        if len(prompt) > 16000:
            raise HTTPException(400, "Slack task is too long.")
        direct_message = event.get('type') == 'message' and event.get('channel_type') == 'im' and channel.startswith('D')
        if channel.startswith('D') and (not direct_message or not self.settings.slack_dm_enabled or not self.settings.slack_thread_chat_enabled):
            return {'ok': True}
        await self.channel.handle_validated_event(team=bot['team_id'], event_id=event_id, channel=channel,
            ts=mention_ts, root=thread_ts, user=user, prompt=prompt, mentioned=mention in text,
            direct_message=direct_message, missing_cloud=missing_cloud)
        return {'ok': True}

    async def accept_message(self, *, team, event_id, channel, ts, root, user, prompt,
                             mentioned, direct_message, missing_cloud):
        if self.settings.slack_thread_chat_enabled:
            try:
                run = self.chat.accept(team=team, event_id=event_id, channel=channel, ts=ts,
                                       root=root, user=user, prompt=prompt, mentioned=mentioned,
                                       direct_message=direct_message, missing_cloud=missing_cloud)
            except ValueError as exc:
                raise HTTPException(503, str(exc))
            await self.checkpoints.flush()
            if self.identities:
                self.identities.wake.set()
            if run:
                self.manager.submit(run)
            return
        if not mentioned or len(prompt) < 3:
            return
        if missing_cloud:
            raise HTTPException(503, 'Cloud sessions are not configured.')
        plugins = [x['id'] for x in self.connectors.list() if x['connected'] and x['enabled']]
        try:
            run = self.store.create_slack_run(event_id, prompt, plugins, channel, root, user, ts, team)
        except ValueError:
            raise HTTPException(503, 'The session queue is full.')
        if run:
            await self.checkpoints.flush()
            if self.identities:
                self.identities.wake.set()
            self.manager.submit(run)
            self.submit_reply(run['id'])

    async def prepare(self, run_id):
        """Runs in the session job, never on Slack's acknowledgement path.

        Reads can safely be retried after an interruption, but source messages
        are frozen at the mention timestamp and agent work is never replayed.
        """
        source = self.store.slack_source(run_id)
        if not source or source["context_status"] not in {"pending", "fetching"}:
            return
        self.store.execute("UPDATE slack_events SET context_status='fetching' WHERE run_id=?", (run_id,))
        self.store.event(run_id, "context", "Reading the Slack conversation behind your request")
        context = {"messages": [], "kind": "thread" if source["thread_ts"] != source["mention_ts"] else "channel",
                   "captured_at": now(), "truncated": False, "permalink": ""}
        if source['channel'].startswith('D'):
            # A DM begins with this explicitly addressed request. Subsequent
            # turns use saved session history; do not import earlier DMs through
            # the shared search account, which may belong to another teammate.
            context.update(kind='dm', messages=[{'ts': source['mention_ts'], 'user': source['user_id'],
                           'text': self.store.run(run_id)['prompt']}])
            self.store.execute("UPDATE slack_events SET context_status='ready',context_json=? WHERE run_id=?", (json.dumps(context), run_id))
            self.store.event(run_id, 'context', 'Using the direct message and saved conversation')
            await self.checkpoints.flush()
            return
        try:
            async with asyncio.timeout(25):
                if not self.connectors.allowed("slack_thread"):
                    raise ConnectorError("Slack is unavailable.")
                headers = self.connectors.headers("slack", await self.connectors.credentials("slack"))
                context.update(await self.read_context(source, headers))
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
            context["messages"] = []
            context["warning"] = "Could not read the Slack conversation with the shared connection. Use the request itself, or ask for the missing context; do not guess what the discussion said."
            message = "Slack conversation could not be read; the agent will be told context is missing"
        self.store.execute("UPDATE slack_events SET context_status=?,context_json=? WHERE run_id=?", (status, json.dumps(context), run_id))
        self.store.event(run_id, "context", message)
        await self.checkpoints.flush()

    async def read_context(self, source, headers):
        threaded = source["thread_ts"] != source["mention_ts"]
        endpoint = "conversations.replies" if threaded else "conversations.history"
        params = {"channel": source["channel"], "latest": source["mention_ts"], "inclusive": True,
                  "limit": 50 if threaded else 30}
        if threaded:
            params["ts"] = source["thread_ts"]
        messages, truncated, cursor = {}, False, ""
        for _ in range(3 if threaded else 1):
            if cursor:
                params["cursor"] = cursor
            data = await self.connectors.request("GET", f"https://slack.com/api/{endpoint}", headers=headers, params=params)
            if not isinstance(data.get("messages"), list):
                raise ConnectorError("Slack returned no message list.")
            for item in data["messages"]:
                ts = item.get("ts", "")
                try:
                    valid = bool(re.fullmatch(r"\d{10,16}\.\d{1,9}", ts)) and Decimal(ts) <= Decimal(source["mention_ts"])
                except (TypeError, InvalidOperation):
                    valid = False
                if not valid or item.get("user") == self.connectors.slack_installation().get("user_id"):
                    continue
                if threaded and ts != source["thread_ts"] and item.get("thread_ts", source["thread_ts"]) != source["thread_ts"]:
                    continue
                text = item.get("text") or "[Message has no text]"
                clipped = len(text) > 3000
                messages[ts] = {"ts": ts, "user": item.get("user") or item.get("bot_id") or "unknown",
                                "text": text[:3000], "text_truncated": clipped,
                                "has_attachments": bool(item.get("files") or item.get("attachments"))}
            cursor = data.get("response_metadata", {}).get("next_cursor", "")
            truncated = bool(data.get("has_more") or cursor)
            if not cursor:
                break
        if not messages:
            raise ConnectorError("No Slack messages could be read.")
        ordered = sorted(messages.values(), key=lambda item: Decimal(item["ts"]))
        # Keep the root and closest available messages within a bounded text budget.
        root = messages.get(source["thread_ts"]) if threaded else None
        selected, budget = ([root] if root else []), 24000 - (len(root["text"]) if root else 0)
        for item in reversed(ordered):
            if item is root:
                continue
            if len(selected) >= (50 if threaded else 30) or len(item["text"]) > budget:
                truncated = True
                break
            selected.append(item)
            budget -= len(item["text"])
        selected.sort(key=lambda item: Decimal(item["ts"]))
        truncated = truncated or any(m["text_truncated"] for m in selected)
        warnings = []
        if truncated:
            warnings.append("Only a bounded excerpt is included; more messages or text may exist.")
        if threaded and not root:
            warnings.append("The thread's root message was not returned.")
        if any(m["has_attachments"] for m in selected):
            warnings.append("Attached files and rich attachments were not read.")
        return {"messages": selected, "truncated": truncated, "warning": " ".join(warnings)}

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
            message = f"Created a cloud session for your request. <{url}|Open session>\nResults and approvals stay in the web app. Organization sign-in is required."
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

    async def shutdown(self):
        await self.chat.shutdown()
        jobs = list(self.jobs)
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        await self.agentchat.close()
