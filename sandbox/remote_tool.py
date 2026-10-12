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


def run(spec):
    import agent.tools.mcp_bridge as bridge
    os.environ['MOYAI_WORKSPACE'] = spec['workspace']
    os.environ.pop('MOYAI_REMOTE_COMPUTER', None)
    relay = BrokerRelay(spec['broker_url'], os.environ['WORKSPACE_RUN_TOKEN']).start()
    original_broker = bridge.broker
    bridge.broker = scoped_tools(original_broker, spec.get('broker_tool_scope'))
    try:
        os.environ['MOYAI_CREDENTIAL_PROXY_URL'] = relay.url + '/credentials'
        bridge.BROKER = bridge.GIT_BROKER = relay.url
        bridge.TOKEN = os.environ['WORKSPACE_RUN_TOKEN']
        if spec.get('prepare_computer'):
            from sandbox.agent import prepare_workspace, ProjectUnavailable
            try:
                workspace, _ = prepare_workspace(spec, relay)
                result = {'workspace': str(workspace)}
            except ProjectUnavailable as exc:
                result = {'error': str(exc)}
        elif spec.get('finish_computer'):
            from sandbox.artifacts import collect_archive
            from sandbox.computer import request
            request({'action': 'finish'}, start=False)
            artifacts = Path('/artifacts'); artifacts.mkdir(exist_ok=True)
            (artifacts/'result.md').write_text(spec.get('response', ''))
            collect_archive(Path(spec['workspace']), artifacts, os.environ['WORKSPACE_RUN_TOKEN'].encode())
            result = {'saved': True}
        else:
            from sandbox.attachments import prepare_attachments
            from sandbox.project_environment import activate_project
            activate_project(spec)
            os.chdir(spec['workspace'])
            prepare_attachments(spec, os.environ['WORKSPACE_RUN_TOKEN'])
            result = call_tool({'name': spec['name'], 'arguments': spec['arguments']}, None)
        print('WORKSPACE_EVENT '+json.dumps({'kind': 'final', 'message': '', 'tool_result': result}), flush=True)
        return 0
    finally:
        bridge.broker = original_broker
        relay.close()


if __name__ == '__main__':
    raise SystemExit(run(json.loads(Path(sys.argv[1]).read_text())))
