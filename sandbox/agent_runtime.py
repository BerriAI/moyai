"""Run the selected agent in an isolated control sandbox, without a workspace.

This is a guest entry point, never imported by the credential-bearing broker to
execute an SDK. The durable controller owns both this sandbox and lazy computer
acquisition. The shared agent core still owns goals, steering and conversation.
"""
from pathlib import Path

from agent.agent import run as run_conversation
from sandbox.agent import hermes_config, on_input


def run(spec, relay, emit, *, control=Path('/control'), session=Path('/session')):
    if spec.get('harness') != 'codex':
        raise ValueError('This harness has not been qualified for an independent agent runtime.')
    control.mkdir(parents=True, exist_ok=True, mode=0o700)
    session.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = hermes_config(spec, relay.url, control)
    environment = config['mcp_servers']['workspace']['env']
    environment.update(MOYAI_REMOTE_COMPUTER='1', MOYAI_HISTORY_REFERENCE_DIR=str(session),
                       MOYAI_WORKSPACE='/workspace')
    # The selected model, effort, prompt, ownership and deadline are unchanged.
    # Attachments already have broker identities and media injection. Download
    # them into the computer only when a tool needs workspace bytes.
    outcome = run_conversation(spec, relay=relay, workspace=control,
        workspace_root=Path('/workspace'), session=session, config=config, emit=emit, on_input=on_input)
    return outcome['exit_code']
