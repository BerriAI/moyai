"""Execute one launch-once computer operation inside its workspace sandbox."""
import json
import os
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.tools.mcp_bridge import call_tool
from sandbox.broker_relay import BrokerRelay
from sandbox.broker_transport import scoped_tools
from sandbox.broker_failure import exception_diagnostic


def run(spec):
    observation = {'stage': 'remote_setup'}
    try:
        return execute(spec, observation)
    except Exception as exc:
        # Only this structured record crosses the sandbox boundary. No native
        # traceback, exception message or command is emitted.
        try:
            scope = spec.get('broker_tool_scope') or {}
            diagnostic = exception_diagnostic(exc, stage=observation['stage'], request_id=scope.get('request_id'))
            print('WORKSPACE_EVENT '+json.dumps({'kind': 'error', 'message': 'Computer operation failed.',
                  'data': {'phase': 'supervisor_failure', **diagnostic}}), flush=True)
        except Exception:
            pass  # A broken diagnostic pipe does not change the failed exit.
        return 1


def execute(spec, observation):
    import agent.tools.mcp_bridge as bridge
    os.environ['MOYAI_WORKSPACE'] = spec['workspace']
    os.environ.pop('MOYAI_REMOTE_COMPUTER', None)
    relay = BrokerRelay(spec['broker_url'], os.environ['WORKSPACE_RUN_TOKEN']).start()
    original_broker = bridge.broker
    bridge.broker = scoped_tools(original_broker, spec.get('broker_tool_scope'))
    failed = False
    try:
        os.environ['MOYAI_CREDENTIAL_PROXY_URL'] = relay.url + '/credentials'
        bridge.BROKER = bridge.GIT_BROKER = relay.url
        bridge.TOKEN = os.environ['WORKSPACE_RUN_TOKEN']
        if spec.get('prepare_computer'):
            observation['stage'] = 'remote_prepare'
            from sandbox.agent import prepare_workspace, ProjectUnavailable
            try:
                workspace, _ = prepare_workspace(spec, relay)
                result = {'workspace': str(workspace)}
            except ProjectUnavailable as exc:
                result = {'error': str(exc)}
        elif spec.get('finish_computer'):
            observation['stage'] = 'remote_finish'
            from sandbox.artifacts import collect_archive
            from sandbox.computer import request
            request({'action': 'finish'}, start=False)
            artifacts = Path('/artifacts'); artifacts.mkdir(exist_ok=True)
            (artifacts/'result.md').write_text(spec.get('response', ''))
            collect_archive(Path(spec['workspace']), artifacts, os.environ['WORKSPACE_RUN_TOKEN'].encode())
            result = {'saved': True}
        else:
            observation['stage'] = 'remote_activate'
            from sandbox.attachments import prepare_attachments
            from sandbox.project_environment import activate_project
            activate_project(spec)
            os.chdir(spec['workspace'])
            observation['stage'] = 'remote_attachments'
            prepare_attachments(spec, os.environ['WORKSPACE_RUN_TOKEN'])
            observation['stage'] = 'remote_dispatch'
            result = call_tool({'name': spec['name'], 'arguments': spec['arguments']}, None, relay=relay)
        observation['stage'] = 'remote_result'
        print('WORKSPACE_EVENT '+json.dumps({'kind': 'final', 'message': '', 'tool_result': result}), flush=True)
        return 0
    except BaseException:
        failed = True
        raise
    finally:
        bridge.broker = original_broker
        try:
            relay.close()
        except Exception:
            if not failed:
                observation['stage'] = 'remote_cleanup'
                raise


if __name__ == '__main__':
    raise SystemExit(run(json.loads(Path(sys.argv[1]).read_text())))
