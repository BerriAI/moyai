import asyncio
import json
import re
import secrets
import time
from pathlib import Path

import modal

from .security import digest

TERMINAL = {"completed", "failed", "cancelled", "interrupted", "idle"}
SANDBOX_FILES = Path(__file__).parent.parent / "sandbox"
SAVE_WARNING = ("Your answer is saved, but the latest workspace files could not be saved. "
                "The previous workspace checkpoint is unchanged. Download the recovered files before continuing; "
                "they may be incomplete. Queued follow-ups were stopped, and no actions were replayed.")


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
        self.jobs = {}
        self.sandboxes = {}
        self.slots = asyncio.Semaphore(settings.max_concurrent_runs)
        self.closing = False
        self.prepare_context = None
        self.coordinator = None
        self.credentials = None

    async def persist(self):
        """Replaced by the cloud checkpoint callback when hosted on Modal."""

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

    async def terminate(self, sandbox):
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
            self.store.execute("UPDATE messages SET status='interrupted' WHERE run_id=? AND status IN ('running','queued')", (row["id"],))
            self.store.event(row["id"], "error", "Workspace restarted. Received answers were preserved; unfinished messages were interrupted and not replayed.")
            if row["sandbox_id"] and self.settings.modal_token_id and self.settings.modal_token_secret:
                try:
                    sandbox = await modal.Sandbox.from_id.aio(row["sandbox_id"], client=await self.client())
                    await self.terminate(sandbox)
                except Exception:
                    self.store.event(row["id"], "error", "Could not confirm sandbox cleanup. Check Modal; its configured timeout still applies.")

    async def client(self):
        return await modal.Client.from_credentials.aio(self.settings.modal_token_id, self.settings.modal_token_secret)

    def submit(self, run):
        if self.closing or run["id"] in self.jobs:
            return
        task = asyncio.create_task(self.chat(run) if run.get("chat_enabled") else self.execute(run))
        self.jobs[run["id"]] = task
        def done(completed):
            if self.jobs.get(run["id"]) is completed:
                self.jobs.pop(run["id"], None)
            if not completed.cancelled() and completed.exception():
                self.store.update_run(run["id"], status="failed", token_hash="", error="Session processing stopped unexpectedly. No unfinished messages were replayed.")
                self.store.execute("UPDATE messages SET status='interrupted' WHERE run_id=? AND status IN ('running','queued')", (run["id"],))
            # A message may arrive while the last checkpoint is being saved.
            if not self.closing and run.get("chat_enabled") and self.store.has_queued_messages(run["id"]):
                self.submit(self.store.run(run["id"]))
        task.add_done_callback(done)

    async def chat(self, run):
        run_id = run["id"]
        if self.prepare_context:
            await self.prepare_context(run_id)
        while not self.closing:
            message = self.store.claim_message(run_id)
            if not message:
                return
            turn = {**self.store.run(run_id), "prompt": message["content"], "message_id": message["id"]}
            try:
                await self.execute(turn)
            except asyncio.CancelledError:
                row = self.store.run(run_id)
                pending = json.loads(row.get("pending_result") or "{}")
                self.store.finish_message(run_id, message["id"], row["summary"] if pending.get("message") else "This response was interrupted. Send a new message to continue from the last saved workspace; external actions were not replayed.", "save_failed" if pending.get("save_failed") else "interrupted")
                self.store.execute("UPDATE messages SET status='interrupted' WHERE run_id=? AND status='queued'", (run_id,))
                raise
            row = self.store.run(run_id)
            if row["status"] == "completed":
                self.store.finish_message(run_id, message["id"], row["summary"])
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
        if self.coordinator:
            await self.coordinator.cancel_children(run_id)
        run = self.store.run(run_id)
        if run["status"] in TERMINAL and run_id not in self.jobs:
            return
        # Revoke capabilities immediately, even while provisioning is still in flight.
        self.store.update_run(run_id, status="stopping", token_hash="")
        self.store.execute("UPDATE messages SET status='cancelled' WHERE run_id=? AND status='queued'", (run_id,))
        self.store.execute("UPDATE approvals SET status='expired' WHERE run_id=? AND status IN ('pending','approved')", (run_id,))
        self.store.event(run_id, "status", "Stop requested. Finishing sandbox cleanup.")
        sandbox = self.sandboxes.get(run_id)
        if sandbox:
            await sandbox.terminate.aio()
        elif run["status"] == "queued" or (run.get("chat_enabled") and not self.store.rows(
                "SELECT 1 FROM messages WHERE run_id=? AND status='running'", (run_id,))):
            # Slack may already have atomically reserved the stop while the
            # background job is still fetching context, before claiming a turn.
            self.store.update_run(run_id, status="cancelled")

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
                                await self.terminate(sandbox)
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
                message = "Task exceeded its time limit." if isinstance(exc, TimeoutError) else f"Cloud run failed ({type(exc).__name__}). Check runtime configuration and Modal logs."
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
                    await self.terminate(sandbox)
                    self.store.event(run_id, "status", "Sandbox terminated")
                except Exception:
                    self.store.event(run_id, "error", "Sandbox cleanup was not confirmed. Check Modal; the sandbox timeout still applies.")
            row = self.store.run(run_id)
            if row["status"] == "stopping":
                self.store.update_run(run_id, status="cancelled")
            self.store.update_run(run_id, token_hash="")
            self.store.execute("UPDATE approvals SET status='expired' WHERE run_id=? AND status IN ('pending','approved')", (run_id,))
            await self.persist()

    async def demo(self, run):
        run_id = run["id"]
        self.store.update_run(run_id, status="running")
        steps = [
            ("status", "Demo started · no cloud machine or model is being used"),
            ("plan", "Preview the task workflow, stream activity, and save a result."),
            ("tool", "Simulated sandbox ready", {"command": "Sandbox creation will happen on Modal in cloud mode."}),
            ("tool", "Simulated workspace inspection", {"command": "No repository files are read or changed in demo mode."}),
            ("result", "Demo complete. Your task and activity are saved. Configure Modal and your model gateway in Runtime to execute this task with Hermes."),
        ]
        for step in steps:
            await asyncio.sleep(self.settings.demo_step_seconds)
            if self.stopped(run_id):
                return
            self.store.event(run_id, *step)
        self.store.update_run(run_id, status="completed", summary=steps[-1][1])

    def image(self):
        revision = self.settings.hermes_revision
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("HERMES_REVISION must be a full commit SHA")
        return (modal.Image.debian_slim(python_version="3.14")
                .apt_install("git", "chromium", "ca-certificates", "build-essential", "libffi-dev", "ripgrep", "nodejs", "npm")
                .pip_install("playwright==1.58.0")
                .env({"HERMES_RUNTIME_DIR": "/opt/hermes-tools", "PYTHONPATH": "/opt/hermes"})
                .run_commands(f"git init /opt/hermes && cd /opt/hermes && git remote add origin https://github.com/NousResearch/hermes-agent.git && git fetch --depth 1 origin {revision} && git checkout --detach FETCH_HEAD",
                              "cd /opt/hermes && python -m pm.build_env --source /opt/hermes --out /opt/hermes-env --no-install-project --extra mcp",
                              "cd /opt/hermes && /opt/hermes-env/bin/python -c 'from run_agent import AIAgent; import mcp; from cryptography.fernet import Fernet'")
                .add_local_dir(SANDBOX_FILES, remote_path="/opt/workspace-runner", copy=True)
                .env({"PYTHONUNBUFFERED": "1", "PYTHONPATH": "/opt/hermes", "HERMES_PYTHON": "/opt/hermes-env/bin/python", "HERMES_HOME": "/tmp/hermes-home", "GIT_TERMINAL_PROMPT": "0"}))

    def is_active(self, run_id):
        return run_id in self.jobs

    def spec(self, run):
        run_id = run["id"]
        spec = {"run_id": run_id, "prompt": run["prompt"], "repo_url": run["repo_url"],
                "github_repository": self.settings.allowed_github_repositories()[0] if 'github' in run['plugins'] else '',
                "github_repositories": self.settings.allowed_github_repositories() if 'github' in run['plugins'] else [],
                "broker_url": f"{self.settings.public_url.rstrip('/')}/broker/{run_id}",
                "model": self.settings.resolve_model(fallback=run.get('active_model') or run.get('model') or ''), "max_iterations": self.settings.max_agent_iterations,
                "timeout": self.settings.run_timeout_seconds - 90 if self.settings.run_timeout_seconds else None,
                "rotation_seconds": self.settings.sandbox_rotation_seconds if not self.settings.run_timeout_seconds and run.get("chat_enabled") else 0,
                "continuation": bool(run.get("continuation")),
                "is_child_agent": bool(run.get('parent_run_id')),
                "fresh_child": bool(run.get('parent_run_id')) and not run.get('continuation') and not any(m['role'] == 'assistant' for m in self.store.messages(run_id)),
                "chat_enabled": bool(run.get("chat_enabled")),
                "workspace_warning": run.get("checkpoint_error", ""),
                "slack_source": self.store.slack_source(run_id),
                "slack_thread_chat": bool(self.store.rows("SELECT 1 FROM slack_threads WHERE run_id=?", (run_id,))) if self.settings.slack_thread_chat_enabled else False,
                "history_fallback": [{"role": m["role"], "content": (f"[Prior {m['status']} message; context only, do not replay] " if m["role"] == "user" and m["status"] != "completed" else "") + m["content"]} for m in self.store.messages(run_id)
                                     if m["id"] < run.get("message_id", 0) and m["status"] not in {"queued", "running"}]}
        return spec

    async def cloud(self, run):
        run_id = run["id"]
        self.store.update_run(run_id, status="provisioning")
        self.store.event(run_id, "status", "Provisioning an isolated Modal sandbox")
        client = await self.client()
        app = await modal.App.lookup.aio(self.settings.modal_app_name, create_if_missing=True, client=client)
        if self.stopped(run_id):
            return
        token = secrets.token_urlsafe(48)
        self.store.update_run(run_id, token_hash=digest(token))
        secret = modal.Secret.from_dict({"WORKSPACE_RUN_TOKEN": token})
        # Shield provisioning so cancellation cannot discard a successfully-created sandbox ID.
        image = modal.Image.from_id(run["snapshot_id"], client=client) if run.get("chat_enabled") and run.get("snapshot_id") else self.image()
        provision = asyncio.create_task(modal.Sandbox.create.aio(
            app=app, client=client, image=image, secrets=[secret],
            env={"PYTHONUNBUFFERED": "1", "PYTHONPATH": "/opt/hermes", "HERMES_HOME": "/tmp/hermes-home",
                 "HERMES_RUNTIME_DIR": "/opt/hermes-tools", "HERMES_PYTHON": "/opt/hermes-env/bin/python", "GIT_TERMINAL_PROMPT": "0"},
            timeout=self.settings.sandbox_lifetime_seconds(), cpu=2, memory=4096,
            experimental_options={"vm_runtime": True} if self.settings.modal_vm_runtime else {},
        ))
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
        if run.get("snapshot_id"):
            for name in ("agent.py", "artifacts.py", "continuation.py", "mcp_bridge.py", "broker_relay.py", "broker_transport.py", "github_tools.py"):
                await sandbox.filesystem.write_text.aio((SANDBOX_FILES / name).read_text(), f"/opt/workspace-runner/{name}")
        await sandbox.filesystem.write_text.aio(json.dumps(spec), "/tmp/task.json")
        self.store.update_run(run_id, status="running")
        self.store.event(run_id, "status", "Sandbox ready. Starting Hermes Agent.")
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
                            # Store the answer independently of artifacts and the Modal
                            # checkpoint. Recovery can finish this exact turn after a crash.
                            result["message_id"] = run.get("message_id")
                            self.store.update_run(run_id, summary=str(result.get("message", "")), pending_result=json.dumps(result))
                            await self.persist()
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
            if result and result.get("continuation") and result.get("checkpoint_saved") and code == 0:
                return True
            if not result or code != 0 or not result.get("completed"):
                self.store.update_run(run_id, status="failed", error="Hermes did not complete the task.", summary=str((result or {}).get("message", "")))
                self.store.event(run_id, "error", str((result or {}).get("message") or "Hermes exited before completing. Review Modal logs for startup or provider errors."))
            else:
                self.store.update_run(run_id, status="completed", summary=result["message"])
                self.store.event(run_id, "result", result["message"])
        finally:
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)

    async def save_artifact(self, sandbox, run_id):
        try:
            info = await sandbox.filesystem.stat.aio("/artifacts/result.zip")
            if info.size > 20 * 1024 * 1024:
                self.store.event(run_id, "error", "Artifact archive exceeds the 20 MB limit and was not downloaded.")
                return
            data = await sandbox.filesystem.read_bytes.aio("/artifacts/result.zip")
            if len(data) > 20 * 1024 * 1024:
                return
            directory = self.settings.data_dir / "artifacts"
            directory.mkdir(exist_ok=True, mode=0o700)
            path = directory / f"{run_id}.zip"
            staged = path.with_suffix('.next')
            staged.write_bytes(data)
            staged.chmod(0o600)
            staged.replace(path)
            self.store.event(run_id, "artifact", "Result archive saved", {"download": f"/api/runs/{run_id}/artifact"})
        except Exception:
            self.store.event(run_id, "error", "No result archive was recovered from this run.")
