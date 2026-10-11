"""Read-only, current-run diagnostics without host credentials or raw logs."""
import json
import os
import re

from pydantic import BaseModel, ConfigDict

from sandbox.broker_failure import BROKER_ROUTES, safe_id, safe_error
from agent.harnesses.sdk_failure import CODEX_ERRORS, CLAUDE_RESULTS, CLAUDE_TERMINAL_REASONS
from .connectors import TOOLS


class DiagnosticArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')


DIAGNOSTIC_TOOL = {
    'name': 'workspace_diagnostics',
    'description': 'Inspect this session’s tool registration, connection policies and sanitized runtime failure records. Read-only; no arguments. Does not expose credentials, raw server logs, source files or other sessions. Use before claiming tools or repository access are unavailable.',
    'inputSchema': DiagnosticArgs.model_json_schema(),
    'annotations': {'readOnlyHint': True, 'idempotentHint': True},
}


def failure_record(row):
    data = json.loads(row['data'])
    result = {**safe_error(data), 'event_id': row['id'], 'recorded_at': row['created_at'], 'phase': data['phase']}
    for key in ('pending_tools', 'model_calls', 'response_bytes'):
        value = data.get(key)
        if type(value) is int and value >= 0:
            result[key] = value
    for key in ('response_started', 'transient', 'transport_interrupted', 'uncertain_tool', 'boundary_failed'):
        if type(data.get(key)) is bool:
            result[key] = data[key]
    for key, values in {'route': BROKER_ROUTES, 'sdk': {'codex', 'claude-agent-sdk'},
                        'code': CODEX_ERRORS, 'native_status': CLAUDE_RESULTS,
                        'terminal_reason': CLAUDE_TERMINAL_REASONS}.items():
        if isinstance(data.get(key), str) and data[key] in values:
            result[key] = data[key]
    for key in ('request_id', 'broker_request_id'):
        if value := safe_id(data.get(key)):
            result[key] = value
    return result


def inspect_workspace(store, connectors, run, catalog):
    names = sorted(tool['name'] for tool in catalog)
    connections = []
    for provider in sorted({spec[0] for spec in TOOLS.values()}):
        policy = connectors.policy(provider)
        connected = bool(store.rows('SELECT provider FROM connections WHERE provider=?', (provider,)))
        selected = provider in run['plugins']
        connections.append({'provider': provider, 'connected': connected, 'selected_for_session': selected,
            'enabled': policy['enabled'], 'read_only': policy['read_only'],
            'available_tools': [name for name in names if name in TOOLS and TOOLS[name][0] == provider]})
    rows = store.rows("SELECT id,created_at,data FROM events WHERE run_id=? AND kind='error' "
                      "AND json_text(data,'phase') IN ('broker_failure','sdk_failure') "
                      'ORDER BY id DESC LIMIT 20', (run['id'],))
    revision = os.environ.get('RENDER_GIT_COMMIT', '')
    return {
        'run_id': run['id'], 'harness': run['harness'], 'model': run['active_model'] or run['model'],
        'broker_catalog': {'count': len(names), 'names': names},
        'connections': connections, 'recent_failures': [failure_record(row) for row in rows],
        'source': {'repository': 'BerriAI/moyai',
                   'deployed_revision': revision if re.fullmatch(r'[0-9a-f]{40}', revision) else None,
                   'access': 'Use github_repositories, then github_checkout for the authorized repository.'},
        'limitations': 'Broker registration does not prove a tool reached the model or that a provider call succeeds. '
            'The MCP bridge adds its last returned catalog separately. Failure records are bounded to this session; '
            'no recorded failures does not prove no failure occurred. Raw Render/Modal host logs are not exposed.',
    }
