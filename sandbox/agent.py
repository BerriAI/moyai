"""Prepare an isolated workspace and launch Moyai. Never execute agent work on the host."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import urllib.request
import uuid

# Direct execution is also used when restoring older sandbox checkpoints.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.agent import run as run_conversation
from agent.context_store import ContextUnavailable
from agent.harnesses.native_session import native_storage
from agent.tools.github_tools import checkout as github_checkout
from sandbox.broker_relay import BrokerRelay
from sandbox.artifacts import collect_archive
from sandbox.computer import request as computer_request
from sandbox.attachments import prepare_attachments
from sandbox.startup import StartupUnavailable, REPOSITORY_METADATA_BUDGET
from sandbox.project_environment import prepare_project
from sandbox.hermes_compat import apply_hermes_patches
from sandbox.codex_runtime import RuntimeLease, discard_orphan
LOCK = threading.Lock()
ACTIVITY_INPUT_ID = None


def emit(kind, message, data=None, **extra):
    with LOCK:
        print("WORKSPACE_EVENT " + json.dumps({"kind": kind, "message": str(message),
              "data": {**(data or {}), "activity_id": uuid.uuid4().hex, "input_id": ACTIVITY_INPUT_ID}, **extra}), flush=True)


def run(spec):
    global ACTIVITY_INPUT_ID
    ACTIVITY_INPUT_ID = spec.get('activity_input_id')
    relay = BrokerRelay(spec['broker_url'], os.environ['WORKSPACE_RUN_TOKEN'], notify=reconnecting,
        report_error=lambda failure: emit('error', 'Cloud request failed; transport diagnostics saved.',
            {'activity_version': 1, 'phase': 'broker_failure', **failure})).start()
    os.environ['MOYAI_CREDENTIAL_PROXY_URL'] = relay.url + '/credentials'
    try:
        discard_orphan()
        if spec.get('harness') == 'codex' and spec.get('codex_runtime_idle_seconds'):
            relay.codex_runtime = RuntimeLease(spec['codex_runtime_scope'], spec['codex_runtime_idle_seconds'],
                                              model=spec['model'])
        return run_agent(spec, relay)
    except StartupUnavailable as exc:
        # This typed result is produced only before journal/model work. Never
        # infer replay safety from a generic process failure or missing answer.
        emit('final', str(exc), completed=False,
             startup_retry={'version': 1, 'stage': exc.stage, 'reason': exc.reason})
        return 75
    except ContextUnavailable as exc:
        emit('final', str(exc), completed=False)
        return 1
    finally:
        try:
            if getattr(relay, 'codex_runtime', None):
                relay.codex_runtime.close()
        except Exception:
            pass  # Optional runtime teardown must not change the emitted outcome.
        finally:
            relay.close()


def reconnecting(message):
    emit('status', message, {'activity_version': 1, 'phase': 'reconnecting'})


def hermes_config(spec, broker_url, workspace):
    return {
        "model": {"default": spec["model"], "provider": "custom", "base_url": broker_url + "/v1"},
        "terminal": {"backend": "local", "cwd": str(workspace)},
        "security": {"allow_lazy_installs": False},
        # Keep core terminal/file tools direct. Hermes discovers the authorized
        # MCP catalog locally, but sends only its search bridge and a bounded
        # listing to the model until a schema is requested.
        "tools": {"tool_search": {"enabled": "on", "defer": [],
                                   "listing": "auto", "listing_max_tokens": 600}},
        "mcp_servers": {"workspace": {"command": "/usr/local/bin/python", "args": ["/opt/workspace-runner/agent/tools/mcp_bridge.py"],
                                      # SDKs may sanitize env or serialize it to argv.
                                      # Keep edge secrets in the relay process only.
                                      "env": {"WORKSPACE_BROKER_URL": broker_url, "WORKSPACE_GIT_BROKER_URL": broker_url,
                                              "WORKSPACE_RUN_TOKEN": os.environ["WORKSPACE_RUN_TOKEN"],
                                              "WORKSPACE_ACCESS_ORIGIN": "", "WORKSPACE_ACCESS_CLIENT_ID": "",
                                              "WORKSPACE_ACCESS_CLIENT_SECRET": ""}, "timeout": 930}},
    }


def run_agent(spec, relay):
    with native_storage(Path('/session/.native-sdk'), Path('/root'), Path('/tmp')):
        return _run_agent(spec, relay)


def _run_agent(spec, relay):
    harness = spec.get('harness', 'hermes')
    if harness == 'hermes':
        apply_hermes_patches()
    workspace = Path("/workspace")
    workspace.mkdir(exist_ok=True)
    prepare_attachments(spec, os.environ['WORKSPACE_RUN_TOKEN'], notify=reconnecting)
    artifacts = Path("/artifacts")
    artifacts.mkdir(exist_ok=True)
    if spec["repo_url"]:
        requested_repo = spec['repo_url'].removeprefix('https://github.com/').removesuffix('.git').lower()
        if spec.get('github_enabled'):
            def broker(path, body):
                request = urllib.request.Request(relay.url + path, data=json.dumps(body).encode(),
                    headers={'Authorization': 'Bearer ' + os.environ['WORKSPACE_RUN_TOKEN'], 'Content-Type': 'application/json'})
                try:
                    with urllib.request.urlopen(request, timeout=REPOSITORY_METADATA_BUDGET + 10) as response:
                        return json.load(response)
                except urllib.error.HTTPError:
                    if relay.startup_failure and relay.startup_failure.stage == 'repository_metadata':
                        raise relay.startup_failure from None
                    raise
            emit('tool', 'Preparing the shared GitHub repository')
            relay.repository_startup = True
            try:
                checked_out = github_checkout(broker, spec['broker_url'], os.environ['WORKSPACE_RUN_TOKEN'], **({'repository_id': spec['github_repository_id']} if spec.get('github_repository_id') else {'repository': requested_repo}))
            finally:
                relay.repository_startup = False
            if checked_out.get('error'):
                raise RuntimeError('The shared GitHub checkout was not confirmed')
        elif not (workspace / "repo").exists():
            emit("tool", "Cloning the repository", {"command": f"git clone --depth 1 {spec['repo_url']}"})
            subprocess.run(["git", "-c", "http.followRedirects=false", "clone", "--depth", "1", "--", spec["repo_url"], str(workspace / "repo")], check=True, timeout=120)
        workspace /= "repo"
    try:
        prepare_project(spec, emit)
    except Exception:
        emit('final', 'The prepared project services did not start successfully. Your workspace is preserved. '
             'Ask an administrator to check the environment startup command before continuing.', completed=False)
        return 1
    os.chdir(workspace)
    config = hermes_config(spec, relay.url, workspace)
    if harness == 'hermes':
        home = Path(os.environ["HERMES_HOME"])
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        (home / "config.yaml").write_text(json.dumps(config))  # JSON is valid YAML.
        os.environ["OPENAI_API_KEY"] = os.environ["WORKSPACE_RUN_TOKEN"]
        os.environ["OPENAI_BASE_URL"] = relay.url + "/v1"
    def on_input(input_id):
        global ACTIVITY_INPUT_ID
        ACTIVITY_INPUT_ID = input_id

    def report(kind, message, data=None, **extra):
        emit(kind, message, data, **extra)
        if kind == 'final':
            (artifacts / 'result.md').write_text(message)

    try:
        outcome = run_conversation(
            spec, relay=relay, workspace=workspace, workspace_root=Path('/workspace'),
            session=Path('/session'), config=config, emit=report,
            prepare_input=lambda item: prepare_attachments(
                {**spec, 'attachments': item.get('attachments', [])}, os.environ['WORKSPACE_RUN_TOKEN']),
            on_input=on_input,
        )
    finally:
        try:
            capture = computer_request({'action': 'finish'}, start=False)
            if capture.get('error'):
                emit('error', capture['error'])
        except Exception:
            emit('error', 'Browser recording could not be finalized; any partial file remains in the workspace.')
    collect_archive(workspace, artifacts, os.environ['WORKSPACE_RUN_TOKEN'].encode())
    return outcome['exit_code']


if __name__ == "__main__":
    try:
        sys.exit(run(json.loads(Path(sys.argv[1]).read_text())))
    except Exception as exc:
        import traceback
        traceback.print_exc(file=sys.stderr)
        emit("error", f"Agent startup or execution failed ({type(exc).__name__}).")
        sys.exit(1)
