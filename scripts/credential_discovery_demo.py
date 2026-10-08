"""Run the real credential broker and executor with synthetic saved access.

uv run python scripts/credential_discovery_demo.py
uv run python scripts/credential_discovery_demo.py --serve

The optional local page reruns the same demonstration on each click. No live
1Password or provider API is contacted; no real credentials are loaded.
"""
import argparse
import json
from pathlib import Path
import shlex
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security import digest
from sandbox.credential_tools import run as credential_run


def demonstrate() -> str:
    lines = ['MOYAI — CREDENTIAL DISCOVERY',
             'Real broker + credential executor; synthetic credentials and vault result.',
             'No live provider or 1Password requests.\n']
    defaults = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    with TemporaryDirectory(prefix='moyai-credential-demo-') as directory:
        defaults.update(data_dir=Path(directory), public_url='http://127.0.0.1:8798',
                        litellm_api_key='', temporal_enabled=False)
        app = create_app(Settings(_env_file=None, **defaults))
        with TestClient(app, base_url=defaults['public_url'], client=('127.0.0.1', 50000)) as client:
            session = client.get('/api/session').json()
            client.headers.update({'Origin': defaults['public_url'], 'X-CSRF-Token': session['csrf']})
            session = client.get('/api/session').json()
            app.state.settings.temporal_enabled = True
            run = app.state.store.create_run('Credential discovery demo', '', 'modal', [],
                                            chat_enabled=True, user_id=session['user_id'])
            app.state.store.claim_message(run['id'])
            app.state.store.update_run(run['id'], status='running', token_hash=digest('demo-capability'))
            headers = {'Authorization': 'Bearer demo-capability'}
            prefix = '/broker/' + run['id']

            def broker(path, body):
                response = client.post(prefix + path, headers=headers, json=body)
                assert response.status_code == 200, response.text
                return response.json()

            def call(name, arguments):
                return broker('/tools/call', {'name': name, 'arguments': arguments})

            def save(scope, provider, suffix, **extra):
                response = client.post('/api/credentials/secrets', json={
                    'provider': provider, 'scope': scope, 'label': suffix,
                    'value': 'synthetic-demo-provider-key-' + suffix,
                    'client_id': 'demo-save-' + suffix, **extra})
                assert response.status_code == 201, response.text
                return response.json()['id']

            def pending():
                return len(client.get('/api/runs/' + run['id']).json()['credential_requests'])

            personal = save('personal', 'openai', 'personal')
            organization = save('organization', 'openai', 'organization')
            source_id = save('organization', 'generic', 'shared', name='1password-shared',
                lifetime='persistent', value=json.dumps({'OP_SERVICE_ACCOUNT_TOKEN': 'synthetic-demo-vault-token'}))
            args = {'provider': 'openai', 'reason': 'Verify the requested workflow', 'request_key': 'workflow'}
            first = call('credentials_request', args)
            assert first['status'] == 'provided'
            assert app.state.credentials.row(first['request_id'])['secret_id'] == personal
            lines.append('1. Request OpenAI access → saved PERSONAL key reused. Forms: 0.')
            second = call('credentials_report_failure', {
                'request_id': first['request_id'], 'revision': 1, 'failure': 'invalid'})
            assert second['status'] == 'provided' and second['retry_required'] and pending() == 0
            assert app.state.credentials.row(first['request_id'])['secret_id'] == organization
            lines.append('2. Report personal-key rejection → ORGANIZATION key ready. Forms: 0.')
            lookup = call('credentials_report_failure', {
                'request_id': first['request_id'], 'revision': 2, 'failure': 'invalid'})
            assert lookup['status'] == 'lookup_required' and pending() == 0
            lines.append('3. Report organization-key rejection → 1PASSWORD LOOKUP REQUIRED. Forms: 0.')
            retry = call('credentials_http_request', {
                'request_id': first['request_id'], 'method': 'GET', 'path': '/models'})
            assert retry['status_code'] == 401 and retry['response']['status'] == 'lookup_required'
            assert 'form' not in retry['response']['error']['message'].lower() and pending() == 0
            lines.append('   Retry inactive key → ' + retry['response']['error']['message'])
            source = call('credentials_request', {
                'provider': 'generic', 'name': '1password-shared', 'secret_id': source_id,
                'reason': 'Inspect Shared before asking for another key', 'request_key': 'shared',
                'input_fields': [{'name': 'OP_SERVICE_ACCOUNT_TOKEN', 'label': 'Service account token'}]})
            command = shlex.join([sys.executable, '-c',
                "import os; assert os.environ['OP_SERVICE_ACCOUNT_TOKEN']; "
                "print('Synthetic Shared fixture: no matching OpenAI item')"])
            executed = credential_run({'request_ids': [source['request_id']], 'command': command}, broker)
            assert executed['exit_code'] == 0 and pending() == 0
            lines.append('4. Execute source lookup with scoped access → ' + executed['output'].strip() + '.')
            still_lookup = call('credentials_request', args)
            assert still_lookup['status'] == 'lookup_required'
            lines.append('   Loading the source alone does not claim a failed lookup. Forms: 0.')
            final = call('credentials_request', {**args, 'source_checks': [{
                'secret_id': source_id, 'revision': 1, 'outcome': 'not_found'}]})
            assert final['status'] == 'pending' and pending() == 1
            lines.append('5. Report observed missing item → secure credential form allowed. Forms: 1.')
            lines.append('\nPASS — Saved access and source lookup precede the prompt; no operation replayed.')
    return '\n'.join(lines)


PAGE = '''<!doctype html><meta charset="utf-8"><title>Moyai credential discovery demo</title>
<style>body{margin:60px auto;max-width:1050px;padding:0 32px;background:#faf9ff;color:#252033;
font:18px/1.6 system-ui}h1{font-size:32px}button{background:#5b3fd1;color:white;border:0;
border-radius:8px;padding:12px 20px;font:inherit;cursor:pointer}pre{white-space:pre-wrap;
background:white;border:1px solid #ddd7ed;padding:24px;border-radius:12px;font:16px/1.8 monospace}
p{color:#655c78}</style><h1>Check saved access before asking</h1>
<p>Local demonstration of Moyai's actual credential broker and executor.<br>
Synthetic saved credentials and vault result. No live API or 1Password calls.</p>
<button id="run">Run credential discovery</button>
<pre id="result">Ready. Click to execute the backend flow.</pre>
<script>document.querySelector('#run').onclick=async()=>{
 const button=document.querySelector('#run'),result=document.querySelector('#result');
 button.disabled=true;result.textContent='Executing credential requests…';
 try{const response=await fetch('/run',{method:'POST'});if(!response.ok)throw Error('Demo failed');
 result.textContent=(await response.json()).result;}catch(error){result.textContent=String(error);}
 finally{button.disabled=false;}};</script>'''


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--serve', action='store_true')
    args = parser.parse_args()
    if not args.serve:
        print(demonstrate())
        return
    import uvicorn
    demo = FastAPI()

    @demo.get('/', response_class=HTMLResponse)
    def index():
        return PAGE

    @demo.post('/run')
    def execute():
        return {'result': demonstrate()}

    uvicorn.run(demo, host='127.0.0.1', port=8798)


if __name__ == '__main__':
    main()
