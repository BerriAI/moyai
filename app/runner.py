import asyncio
import json
import re
import secrets
import time
from pathlib import Path
from uuid import uuid4

import modal
from fastapi import HTTPException

from .environments import EnvironmentPending
from .security import digest
from .message_queue import MessageQueue
from .modal_clients import ModalClients

TERMINAL = {"completed", "failed", "cancelled", "interrupted", "idle"}
CAPTURE_RELEASE_TIMEOUT = 35
RETAINED_RELEASES = 128
SANDBOX_FILES = Path(__file__).parent.parent / "sandbox"
SAVE_WARNING = ("Your answer is saved, but the latest workspace files could not be saved. "
                "The previous workspace checkpoint is unchanged. Download the recovered files before continuing; "
                "they may be incomplete. Queued follow-ups were stopped, and no actions were replayed.")


def completed_response(run: dict[str, object], result: object) -> bool:
    """Identify a final answer receipt without claiming its workspace is saved."""
    message_id = run.get('active_message_id')
    return (type(message_id) is int and isinstance(result, dict)
            and type(result.get('message_id')) is int and result['message_id'] == message_id
            and result.get('completed') is True
            and not any(result.get(key) for key in ('continuation', 'steer_message_id', 'startup_retry', 'transport_retry', 'wait_group', 'wait_credential'))
            and isinstance(result.get('message'), str) and bool(result['message'].strip()))


def response_status(run: dict[str, object]) -> str:
    """Present a finished answer as saving without settling durable execution."""
    status = str(run.get('status') or '')
    raw = run.get('pending_result')
    if status != 'running' or not isinstance(raw, str) or not raw:
        return status
    try:
        result = json.loads(raw)
    except ValueError:
        return status
    return 'saving' if completed_response(run, result) else status


async def refresh_sandbox_files(sandbox):
    """Refresh our adapter and runtime patches, preserving workspace/history."""
    for path in sorted([*SANDBOX_FILES.glob('*.py'), *SANDBOX_FILES.glob('hermes-*.patch')]):
        await sandbox.filesystem.write_text.aio(path.read_text(), f'/opt/workspace-runner/{path.name}')


def safe_error_detail(exc, secrets_to_hide=()):
    """Keep useful provider diagnostics without logging credentials or URLs."""
    value = str(exc)
    for secret in secrets_to_hide:
        if secret:
            value = value.replace(secret, "[redacted]")
    value = re.sub(r"https?://\S+", "[URL redacted]", value)
    value = re.sub(r"(?i)(bearer\s+|(?:api[_-]?key|token|secret|authorization)[\s\"':=]+)\S+", r"\1[redacted]", value)
    value = re.sub(r"\b(?:sk-|xox[baprs]-|ak-|as-)[A-Za-z0-9_-]+", "[redacted]", value)
    return value[:1500]


class RunManager:
    """Single-process durable admission; interrupted work is never silently replayed."""
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        store.sandbox_provider = lambda: settings.sandbox_provider
        self.jobs = {}
        self.sandboxes = {}
        self.releases = {}
        self.slots = asyncio.Semaphore(settings.max_concurrent_runs)
        self.closing = False
        self.prepare_context = None
        self.environments = None
        self.coordinator = None
        self.credentials = None
        self.message_queue = MessageQueue(store)
        self.modal_clients = ModalClients()

    async def persist(self):
        """Replaced by the cloud checkpoint callback when hosted on Modal."""

    def receive_result(self, run_id: str, result: dict[str, object]) -> None:
        """Persist the answer and wake browser readers without settling its turn."""
        serialized = json.dumps(result)
        run = self.store.run(run_id)
        if run['pending_result'] == serialized:
            return
        self.store.update_run(run_id, summary=str(result.get('message', '')), pending_result=serialized)
        self.store.event(run_id, 'chat', 'Response received',
                         {'message_id': result.get('message_id'), 'response_complete': completed_response(run, result)})

    def preserve_answer(self, run_id, reason=SAVE_WARNING):
        """Called on failure/restart; never turn an unsaved workspace into success."""
        row = self.store.run(run_id)
        pending = json.loads(row.get("pending_result") or "null")
        if not pending or not pending.get("message") or pending.get("checkpoint_saved"):
            return False
        pending["save_failed"] = True
        self.store.update_run(run_id, summary=pending["message"] + "\n\n---\n**Workspace save warning:** " + reason,
                              checkpoint_error=reason, pending_result=json.dumps(pending))
        return True

    async def terminate(self, sandbox, run_id):
        identity = (run_id, sandbox.object_id)
        task = self.releases.get(identity)
        if not task or (task.done() and (task.cancelled() or task.exception())):
            task = asyncio.create_task(self.release_sandbox(sandbox, run_id))
            # A disconnected waiter must not abandon cleanup or leave a failed
            # background task unobserved. A later caller retries failed releases.
            task.add_done_callback(self.release_done)
        self.releases.pop(identity, None)
        self.releases[identity] = task
        await asyncio.shield(task)

    def release_done(self, task):
        if not task.cancelled():
            task.exception()
        completed = [key for key, value in self.releases.items() if value.done()]
        # Keep recent deduplication without retaining every rotated sandbox.
        # Very late callers can repeat idempotent finish/copy/provider cleanup.
        for key in completed[:-RETAINED_RELEASES]:
            self.releases.pop(key, None)

    async def release_sandbox(self, sandbox, run_id):
        if getattr(self, 'computer', None):
            try:
                # The runtime serializes finish, and capture_locks serialize
                # immutable copies. A busy UI request must not block shutdown.
                async with asyncio.timeout(CAPTURE_RELEASE_TIMEOUT):
                    await self.computer.save_captures(sandbox, run_id, releasing=True)
            except Exception:
                self.store.event(run_id, 'error', 'Some browser captures could not be saved. Continuing workspace shutdown.')
        async with asyncio.timeout(30):
            await sandbox.terminate.aio()
            # terminate() acknowledges the request before the machine exits.
            await sandbox.wait.aio(raise_on_termination=False)

    async def recover(self):
        if self.store.rows("SELECT 1 FROM sqlite_master WHERE type='table' AND name='durable_sessions'"):
            if any(json.loads(row['state']).get('phase', 'idle') != 'idle'
                   for row in self.store.rows('SELECT state FROM durable_sessions')):
                raise RuntimeError('Drain Temporal sessions before disabling Temporal; unfinished work was preserved')
        rows = self.store.rows("SELECT * FROM runs WHERE status NOT IN ('completed','failed','cancelled','interrupted','idle') OR EXISTS(SELECT 1 FROM messages WHERE messages.run_id=runs.id AND messages.status IN ('running','queued'))")
        for row in rows:
            pending = json.loads(row.get("pending_result") or "null")
            if pending and pending.get("message"):
                saved = pending.get("checkpoint_saved") is True
                if not saved:
                    self.preserve_answer(row["id"])
                response = self.store.run(row["id"])["summary"]
                status = ("completed" if pending.get("completed") and pending.get("exit_code") == 0 else "failed") if saved else "save_failed"
                if pending.get("message_id"):
                    self.store.finish_message(row["id"], pending["message_id"], response, status)
                self.store.update_run(row["id"], status="idle" if status == "completed" else "failed", token_hash="",
                                      error="" if status == "completed" else "The workspace restarted after the answer was received.")
            else:
                self.store.update_run(row["id"], status="interrupted", token_hash="", error="The workspace restarted. This task was not replayed.")
            self.store.execute("UPDATE approvals SET status='expired' WHERE run_id=? AND status IN ('pending','approved')", (row["id"],))
            self.store.execute("UPDATE approvals SET status='uncertain' WHERE run_id=? AND status='executing'", (row["id"],))
            self.store.execute("UPDATE messages SET status='interrupted' WHERE run_id=? AND status IN ('running','queued','injected')", (row["id"],))
            self.store.event(row["id"], "error", "Workspace restarted. Received answers were preserved; unfinished messages were interrupted and not replayed.")
            if row["sandbox_id"] and not self.settings.missing_sandbox(row.get("sandbox_provider")):
                try:
                    sandbox = await self.provider(row).get(row["sandbox_id"])
                    await self.terminate(sandbox, row['id'])
                except Exception:
                    self.store.event(row["id"], "error", "Could not confirm sandbox cleanup. Check the sandbox provider; its configured timeout still applies.")

    def provider(self, run=None, *, identity='', name=None):
        from .sandboxes import provider, provider_for_id
        selected = name or (provider_for_id(identity) if identity else (run or {}).get('sandbox_provider'))
        backend = provider(self.settings, selected, modal_clients=self.modal_clients)
        if backend.name == 'modal':
            backend.client, backend.image = self.client, self.image
        return backend

    async def client(self):
        return await self.modal_clients.get(self.settings)

    def submit(self, run):
        if self.closing or run.get('deleted_at') or run["id"] in self.jobs:
            return
        task = asyncio.create_task(self.chat(run) if run.get("chat_enabled") else self.execute(run))
        self.jobs[run["id"]] = task
        def done(completed):
            if self.jobs.get(run["id"]) is completed:
                self.jobs.pop(run["id"], None)
            if not completed.cancelled() and completed.exception():
                self.store.update_run(run["id"], status="failed", token_hash="", error="Session processing stopped unexpectedly. No unfinished messages were replayed.")
                self.store.execute("UPDATE messages SET status='interrupted' WHERE run_id=? AND status IN ('running','queued','injected')", (run["id"],))
            # A message may arrive while the last checkpoint is being saved.
            if not self.closing and run.get("chat_enabled") and self.store.has_queued_messages(run["id"]):
                self.submit(self.store.run(run["id"]))
        task.add_done_callback(done)

    async def chat(self, run):
        run_id = run["id"]
        while not self.closing:
            message = self.store.claim_message(run_id)
            if not message:
                return
            try:
                if self.prepare_context:
                    await self.prepare_context(run_id)
                    message = self.store.rows('SELECT * FROM messages WHERE id=?', (message['id'],))[0]
                if self.stopped(run_id):
                    self.store.finish_message(run_id, message['id'], 'Stopped before starting the response.', 'cancelled')
                    if self.store.run(run_id)['status'] == 'stopping':
                        self.store.update_run(run_id, status='cancelled')
                    return
                turn = {**self.store.run(run_id), "prompt": message["content"], "message_id": message["id"]}
                await self.execute(turn)
            except asyncio.CancelledError:
                row = self.store.run(run_id)
                pending = json.loads(row.get("pending_result") or "{}")
                self.store.finish_message(run_id, message["id"], row["summary"] if pending.get("message") else "This response was interrupted. Send a new message to continue from the last saved workspace; external actions were not replayed.", "save_failed" if pending.get("save_failed") else "interrupted")
                self.store.execute("UPDATE messages SET status='interrupted' WHERE run_id=? AND status='queued'", (run_id,))
                raise
            row = self.store.run(run_id)
            if row["status"] in {"completed", "steered"}:
                self.store.finish_message(run_id, message["id"], row["summary"], row['status'])
                self.store.update_run(run_id, status="queued" if self.store.has_queued_messages(run_id) else "idle")
                await self.persist()
            else:
                explanation = row["summary"] or row["error"] or "This response was stopped. The last completed workspace checkpoint is preserved; recent unfinished changes may not be saved."
                preserved = json.loads(row.get("pending_result") or "{}").get("save_failed")
                self.store.finish_message(run_id, message["id"], explanation, "save_failed" if preserved else row["status"])
                self.store.execute("UPDATE messages SET status='cancelled' WHERE run_id=? AND status='queued'", (run_id,))
                await self.persist()
                return

    async def shutdown(self):
        self.closing = True
        tasks = list(self.jobs.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def stopped(self, run_id):
        return self.store.run(run_id)["status"] in TERMINAL | {"stopping"}

    async def cancel(self, run_id):
        run = self.store.run(run_id)
        if run["status"] in TERMINAL and run_id not in self.jobs:
            if self.coordinator:
                await self.coordinator.cancel_children(run_id)
            return
        # Revoke capabilities immediately, even while provisioning is still in flight.
        self.store.update_run(run_id, status="stopping", token_hash="")
        self.store.execute("UPDATE messages SET status='cancelled' WHERE run_id=? AND status='queued'", (run_id,))
        self.store.execute("UPDATE approvals SET status='expired' WHERE run_id=? AND status IN ('pending','approved')", (run_id,))
        self.store.event(run_id, "status", "Stop requested. Saving recordings before shutting down the workspace.")
        sandbox = self.sandboxes.get(run_id)
        if not sandbox and (run["status"] == "queued" or (run.get("chat_enabled") and not self.store.rows(
                "SELECT 1 FROM messages WHERE run_id=? AND status='running'", (run_id,)))):
            # Slack may already have atomically reserved the stop while the
            # background job is still fetching context, before claiming a turn.
            self.store.update_run(run_id, status="cancelled")
        pending = ([self.terminate(sandbox, run_id)] if sandbox else [])
        if self.coordinator:
            pending.append(self.coordinator.cancel_children(run_id))
        for result in await asyncio.gather(*pending, return_exceptions=True):
            if isinstance(result, BaseException):
                raise result

    async def execute(self, run):
        run_id = run["id"]
        try:
            async with self.slots:
                if self.stopped(run_id):
                    return
                deadline = self.settings.run_timeout_seconds + self.settings.snapshot_timeout_seconds + 180 if self.settings.run_timeout_seconds else None
                async with asyncio.timeout(deadline):
                    if run["mode"] == "demo":
                        await self.demo(run)
                    else:
                        turn = run
                        while not self.stopped(run_id):
                            rotate = await self.cloud(turn)
                            if not rotate or self.stopped(run_id):
                                break
                            # Only a successful filesystem/history checkpoint may
                            # move the same user turn to another machine.
                            sandbox = self.sandboxes.get(run_id)
                            if sandbox:
                                await self.terminate(sandbox, run_id)
                                self.sandboxes.pop(run_id, None)
                            self.store.event(run_id, "status", "Workspace saved. Continuing on a fresh cloud machine.")
                            turn = {**self.store.run(run_id), "prompt": run["prompt"],
                                    "message_id": run.get("message_id"), "continuation": True}
        except asyncio.CancelledError:
            self.preserve_answer(run_id)
            self.store.update_run(run_id, status="interrupted", token_hash="", error="Workspace shut down while the task was active.")
            self.store.event(run_id, "error", "Workspace shut down. This run was interrupted.")
            raise
        except Exception as exc:
            self.preserve_answer(run_id)
            if self.stopped(run_id):
                self.store.update_run(run_id, status="cancelled", token_hash="")
            else:
                message = str(exc.detail) if isinstance(exc, HTTPException) else ("Task exceeded its time limit." if isinstance(exc, TimeoutError) else f"Cloud run failed ({type(exc).__name__}). Check runtime configuration and sandbox provider logs.")
                self.store.update_run(run_id, status="failed", token_hash="", error=message)
                self.store.event(run_id, "error", message)
        finally:
            row = self.store.run(run_id)
            pending = json.loads(row.get("pending_result") or "{}")
            if row.get("chat_enabled") and pending and not pending.get("checkpoint_saved") and row["status"] in {"stopping", "cancelled", "interrupted", "failed"}:
                self.preserve_answer(run_id)
            sandbox = self.sandboxes.pop(run_id, None)
            if sandbox:
                try:
                    await self.terminate(sandbox, run_id)
                    self.store.event(run_id, "status", "Sandbox terminated")
                except Exception:
                    self.store.event(run_id, "error", "Sandbox cleanup was not confirmed. Check the sandbox provider; the sandbox timeout still applies.")
            row = self.store.run(run_id)
            if row["status"] == "stopping":
                self.store.update_run(run_id, status="cancelled")
            self.store.update_run(run_id, token_hash="")
            self.store.execute("UPDATE approvals SET status='expired' WHERE run_id=? AND status IN ('pending','approved')", (run_id,))
            if not run.get('chat_enabled') and self.store.tracing:
                row = self.store.run(run_id)
                self.store.tracing.finish_turn(run_id, None, row['summary'] or row['error'], row['status'])
            await self.persist()

    async def demo(self, run):
        run_id = run["id"]
        self.store.update_run(run_id, status="running")
        steps = [
            ("status", "Demo started · no cloud machine or model is being used"),
            ("plan", "Preview the task workflow, stream activity, and save a result."),
            ("tool", "Simulated sandbox ready", {"command": "Sandbox creation will use your selected provider in cloud mode."}),
            ("tool", "Simulated workspace inspection", {"command": "No repository files are read or changed in demo mode."}),
            ("result", "Demo complete. Your task and activity are saved. Configure a sandbox provider and your model gateway in Runtime to execute this task."),
        ]
        for step in steps:
            await asyncio.sleep(self.settings.demo_step_seconds)
            if self.stopped(run_id):
                return
            if run.get('chat_enabled') and self.message_queue.accept_steer(run_id, self.store.run(run_id)['active_message_id']):
                self.store.update_run(run_id, status='steered', summary='')
                return
            self.store.event(run_id, *step)
        self.store.update_run(run_id, status="completed", summary=steps[-1][1])

    def image(self):
        from .workspace_image import workspace_image
        return workspace_image(self.settings)

    def is_active(self, run_id):
        return run_id in self.jobs

    def spec(self, run):
        from .attachments import attachment_context
        from sandbox.harness_registry import resolve
        run_id = run["id"]
        fresh_child = bool(run.get('parent_run_id')) and not run.get('continuation') and not self.store.rows(
            "SELECT 1 FROM messages WHERE run_id=? AND role='assistant' LIMIT 1", (run_id,))
        context_checkpoint = bool(resolve(run.get('harness', 'hermes')).durable_context
            and run.get('chat_enabled') and run.get('snapshot_id') and not run.get('checkpoint_error') and not fresh_child)
        uploads = self.store.attachments.for_run(run_id, run.get('message_id') or 0)
        by_message = {}
        for upload in uploads:
            by_message.setdefault(upload['message_id'], []).append(upload)
        from .progress import active_input
        with self.store.connect() as conn:
            activity_input_id = active_input(conn, run_id, run.get('message_id') or 0)
        spec = {"run_id": run_id, "prompt": run["prompt"], "repo_url": run["repo_url"],
                "attachments": uploads, "harness": run.get('harness', 'hermes'),
                "attachment_context": attachment_context(by_message.get(run.get('message_id'), [])),
                "github_repository_id": run.get("github_repository_id"),
                "github_enabled": "github" in run["plugins"],
                "broker_url": f"{self.settings.public_url.rstrip('/')}/broker/{run_id}",
                "model": self.settings.resolve_model(fallback=run.get('active_model') or run.get('model') or ''), "max_iterations": self.settings.max_agent_iterations,
                "timeout": self.settings.run_timeout_seconds - 90 if self.settings.run_timeout_seconds else None,
                "transport_recovery_seconds": self.settings.transport_recovery_seconds,
                "rotation_seconds": self.settings.sandbox_rotation_seconds if not self.settings.run_timeout_seconds and run.get("chat_enabled") else 0,
                "continuation": bool(run.get("continuation")),
                "activity_input_id": activity_input_id,
                "tracing_enabled": bool(self.store.tracing and self.store.tracing.enabled),
                "omit_private_tool_payloads": bool(self.store.tracing and
                    self.store.tracing.preferences.for_run(run)['omit_private_tool_payloads']),
                "is_child_agent": bool(run.get('parent_run_id')),
                "fresh_child": fresh_child,
                "context_checkpoint": context_checkpoint,
                "chat_enabled": bool(run.get("chat_enabled")),
                "workspace_warning": run.get("checkpoint_error", ""),
                "side_chat_context": run.get('side_chat_context', ''),
                "slack_source": self.store.slack_source(run_id),
                "slack_thread_chat": bool(self.store.rows("SELECT 1 FROM slack_threads WHERE run_id=?", (run_id,))) if self.settings.slack_thread_chat_enabled else False,
                "history_fallback": [] if context_checkpoint else [{"role": m["role"], "content": (f"[Prior {m['status']} message; context only, do not replay] " if m["role"] == "user" and m["status"] != "completed" else "") + m["content"] + attachment_context(by_message.get(m['id'], []))} for m in self.store.messages(run_id)
                                     if m["id"] != run.get("message_id", 0) and m["status"] not in {"queued", "running", "deleted"}]}
        if self.environments:
            spec["project_environment"] = self.environments.context(run)
            if spec['project_environment']:
                spec['repo_url'] = 'https://github.com/' + spec['project_environment']['repository']
        return spec

    async def cloud(self, run):
        run_id = run["id"]
        self.store.update_run(run_id, status="provisioning")
        self.store.event(run_id, "status", "Provisioning an isolated sandbox")
        waiting = ''
        while self.environments and not self.stopped(run_id):
            try:
                await self.environments.prepare(run_id)
                break
            except EnvironmentPending as pending:
                if waiting != pending.build_id:
                    self.store.event(run_id, 'status', str(pending), {'activity_version': 1, 'phase': 'environment'})
                    waiting = pending.build_id
                await asyncio.sleep(5)
        if self.stopped(run_id):
            return
        project = self.environments.context(self.store.run(run_id)) if self.environments else {}
        fresh = self.store.run(run_id)
        run = {**run, 'repo_url': fresh['repo_url'], 'environment_build_id': fresh['environment_build_id']}
        backend = self.provider(run)
        if self.stopped(run_id):
            return
        token = secrets.token_urlsafe(48)
        self.store.update_run(run_id, token_hash=digest(token))
        snapshot_id = run.get("snapshot_id") or project.get("snapshot_id")
        provision = asyncio.create_task(backend.create(
            name='moyai-' + run_id + '-' + uuid4().hex[:8], snapshot_id=snapshot_id or '', token=token,
            timeout=self.settings.sandbox_lifetime_seconds()))
        try:
            sandbox = await asyncio.shield(provision)
        except asyncio.CancelledError:
            sandbox = await provision
            self.sandboxes[run_id] = sandbox
            self.store.update_run(run_id, sandbox_id=sandbox.object_id)
            raise
        self.sandboxes[run_id] = sandbox
        self.store.update_run(run_id, sandbox_id=sandbox.object_id)
        await self.persist()
        if self.stopped(run_id):
            return
        spec = self.spec(run)
        # Restored snapshots can contain an older adapter; refresh only our own
        # runner files, preserving all user workspace files and agent history.
        if snapshot_id:
            await refresh_sandbox_files(sandbox)
        if getattr(self, 'computer', None):
            await self.computer.restore(sandbox, run_id, required=False)
        await sandbox.filesystem.write_text.aio(json.dumps(spec), "/tmp/task.json")
        self.store.update_run(run_id, status="running")
        from .harnesses import resolve
        self.store.event(run_id, "status", f"Sandbox ready. Starting {resolve(run['harness']).name}.")
        # Modal streams arbitrary chunks by default. Protocol events are JSON
        # lines and must be framed before decoding, including parallel tools.
        process = await sandbox.exec.aio("/opt/hermes-env/bin/python", "/opt/workspace-runner/agent.py", "/tmp/task.json",
                                         timeout=self.settings.run_timeout_seconds or None, bufsize=1)
        result = None
        secrets_to_hide = [token, self.settings.litellm_api_key, self.settings.modal_token_secret]

        def scrub(value):
            for secret_value in secrets_to_hide:
                if secret_value:
                    value = value.replace(secret_value, "[redacted]")
            return value

        async def stderr():
            # Drain both streams to prevent subprocess deadlocks. Raw third-party diagnostics
            # stay in Modal, since they may contain credentials or provider request bodies.
            async for _ in process.stderr:
                pass

        drain = asyncio.create_task(stderr())
        try:
            async for line in process.stdout:
                if self.stopped(run_id):
                    return
                if line.startswith("WORKSPACE_EVENT "):
                    try:
                        event = json.loads(scrub(line[len("WORKSPACE_EVENT "):]))
                        if event.get("kind") == "final":
                            result = event
                            self.message_queue.acknowledge(run_id, run.get('message_id'), event.get('steering_applied', []))
                            # Store the answer independently of artifacts and the Modal
                            # checkpoint. Recovery can finish this exact turn after a crash.
                            result["message_id"] = run.get("message_id")
                            self.receive_result(run_id, result)
                            await self.persist()
                        elif event.get('kind') == 'trace' and self.store.tracing:
                            self.store.tracing.tool(run_id, event.get('data'))
                        elif event.get("kind") in {"tool", "status", "error", "message"}:
                            self.store.event(run_id, event["kind"], str(event.get("message", "")), event.get("data", {}))
                    except (ValueError, TypeError):
                        self.store.event(run_id, "error", "An agent progress event could not be decoded.")
            code = await process.wait.aio()
            if result:
                result["exit_code"] = code
                self.store.update_run(run_id, pending_result=json.dumps(result))
            if self.stopped(run_id):
                return
            await self.save_artifact(sandbox, run_id)
            if self.stopped(run_id):
                return
            if run.get("chat_enabled"):
                self.store.update_run(run_id, status="saving", token_hash="")
                self.store.event(run_id, "status", "Saving conversation and workspace for your next message")
                started = time.monotonic()
                try:
                    snapshot = await sandbox.snapshot_filesystem.aio(timeout=self.settings.snapshot_timeout_seconds, ttl=None)
                except Exception as exc:
                    preserved = self.preserve_answer(run_id)
                    warning = SAVE_WARNING if preserved else "The workspace save failed and no final answer was received. The previous checkpoint is unchanged; queued follow-ups were stopped."
                    self.store.update_run(run_id, status="failed", error=warning, checkpoint_error=warning)
                    self.store.event(run_id, "error", "Workspace save failed; the received answer was preserved." if preserved else "Workspace save failed before a final answer was received.",
                                     {"detail": {"stage": "snapshot_filesystem", "error_type": type(exc).__name__,
                                                 "elapsed_seconds": round(time.monotonic() - started, 2),
                                                 "timeout_seconds": self.settings.snapshot_timeout_seconds,
                                                 "reason": safe_error_detail(exc, secrets_to_hide)}})
                    return
                if result:
                    result["checkpoint_saved"] = True
                self.store.update_run(run_id, snapshot_id=snapshot.object_id, checkpoint_error="", pending_result=json.dumps(result) if result else "")
                await self.persist()
            if self.stopped(run_id):
                return
            if (result and result.get('steer_message_id') and code == 0 and result.get('checkpoint_saved')
                    and self.message_queue.accepted(run_id, result['steer_message_id'])):
                self.store.update_run(run_id, status='steered', summary='')
                return
            if result and result.get("continuation") and result.get("checkpoint_saved") and code == 0:
                return True
            if not result or code != 0 or not result.get("completed"):
                self.store.update_run(run_id, status="failed", error="Hermes did not complete the task.", summary=str((result or {}).get("message", "")))
                self.store.event(run_id, "error", str((result or {}).get("message") or "Hermes exited before completing. Review sandbox logs for startup or provider errors."))
            else:
                self.store.update_run(run_id, status="completed", summary=result["message"])
                self.store.event(run_id, "result", result["message"])
        finally:
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)

    async def save_artifact(self, sandbox, run_id):
        if getattr(self, 'computer', None):
            await self.computer.save_captures(sandbox, run_id, releasing=False)
        try:
            info = await sandbox.filesystem.stat.aio("/artifacts/result.zip")
            if info.size > 20 * 1024 * 1024:
                self.store.event(run_id, "error", "Artifact archive exceeds the 20 MB limit and was not downloaded.")
                return
            data = await sandbox.filesystem.read_bytes.aio("/artifacts/result.zip")
            if len(data) > 20 * 1024 * 1024:
                return
            await asyncio.to_thread(self.store.artifacts.save, f"{run_id}.zip", data)
            self.store.event(run_id, "artifact", "Result archive saved", {"download": f"/api/runs/{run_id}/artifact"})
        except Exception:
            self.store.event(run_id, "error", "No result archive was recovered from this run.")
