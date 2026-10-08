"""Exercise the real origin middleware using disposable local identities and data.

    uv run python -m scripts.cloudflare_access_demo --output /tmp/moyai-access-demo --delay 2

The recording contains actual request results, never assertions or credentials.
Cloudflare's edge, Google sign-in, and Render are not contacted by this demo.
"""
import argparse
import json
from pathlib import Path
import tempfile
import time

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import jwt

from app.config import Settings
from app.main import create_app
from app.security import digest


def demonstrate(output: Path, delay: float):
    output.mkdir(parents=True, exist_ok=True)
    started, transcript, events = time.monotonic(), [], []

    def log(message):
        print(message, flush=True)
        transcript.append(message)
        events.append([round(time.monotonic() - started, 3), 'o', message + '\r\n'])
        time.sleep(delay)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    def assertion(audience, email='tin@berri.ai', subject='access-tin'):
        return jwt.encode({'iss': 'https://local-demo.cloudflareaccess.com', 'aud': [audience],
                           'iat': int(time.time()), 'exp': int(time.time()) + 300, 'type': 'app',
                           'email': email, 'sub': subject},
                          key, algorithm='RS256', headers={'kid': 'local-demo'})

    with tempfile.TemporaryDirectory(prefix='moyai-access-demo-') as temporary:
        settings = Settings(_env_file=None, data_dir=Path(temporary), public_url='https://moyai.example',
                            password_login_enabled=False, temporal_enabled=False,
                            cloudflare_access_login=True, google_allowed_domains='berri.ai',
                            google_admin_emails='tin@berri.ai',
                            cloudflare_access_team_domain='local-demo.cloudflareaccess.com',
                            cloudflare_access_audience='employee-demo',
                            cloudflare_access_broker_audience='broker-demo')
        app = create_app(settings)
        owner = app.state.store.identity({'method': 'google', 'identity':
            {'sub': 'existing-tin', 'email': 'tin@berri.ai', 'name': 'Tin'}})
        # Pin only the disposable demo key, avoiding external key discovery.
        app.state.cloudflare_access.keys = {'local-demo': key.public_key()}
        app.state.cloudflare_access.expires = time.monotonic() + 300
        with TestClient(app, base_url=settings.public_url) as client:
            client.headers['Origin'] = settings.public_url
            log('MOYAI ACCESS | Actual local ASGI requests | Synthetic identities')
            log('Cloudflare/Render rollout is not exercised by this local demonstration.')
            def check(label, response, expected):
                assert response.status_code == expected, (label, response.status_code)
                log(f'PASS  {label} -> HTTP {response.status_code}')
            check('Anonymous GET /api/credentials', client.get('/api/credentials'), 401)
            check('Forged Access header', client.get('/api/credentials',
                  headers={'Cf-Access-Jwt-Assertion': 'forged'}), 401)
            employee = {'Cf-Access-Jwt-Assertion': assertion('employee-demo')}
            session = client.get('/api/session', headers=employee)
            check('Employee assertion, no second login', session, 200)
            assert session.json()['authenticated'] and session.json()['user_id'] == owner
            assert not app.state.store.rows('SELECT * FROM login_states')
            log('PASS  Existing account ID retained; no Google login transaction')
            check('Employee opens existing personal account', client.get('/api/credentials', headers=employee), 200)
            member = {'Cf-Access-Jwt-Assertion': assertion('employee-demo', 'member@berri.ai', 'access-member')}
            check('Member cannot open administrator controls', client.get('/api/admin/users', headers=member), 403)
            check('Moyai login alone cannot bypass Access', client.get('/api/credentials'), 401)
            run = app.state.store.create_run('Synthetic demo', '', 'modal', [], model='test-model')
            app.state.store.update_run(run['id'], status='running', token_hash=digest('synthetic-run-token'))
            path = '/broker/' + run['id'] + '/v1/models'
            machine = {'Cf-Access-Jwt-Assertion': assertion('broker-demo')}
            check('Broker identity without run bearer', client.get(path, headers=machine), 401)
            machine['Authorization'] = 'Bearer synthetic-run-token'
            check('Broker identity + active run bearer', client.get(path, headers=machine), 200)
            check('Broker identity cannot open dashboard', client.get('/api/credentials', headers=machine), 401)
            check('Unsigned Slack POST remains blocked', client.post('/hooks/slack/events', json={}), 401)
            log('11/11 checks passed. Single sign-in; authorization preserved.')
    header = {'version': 2, 'width': 100, 'height': 22, 'timestamp': int(time.time()),
              'title': 'Moyai Access: real local request recording'}
    (output / 'moyai-access.cast').write_text('\n'.join(json.dumps(row) for row in [header, *events]) + '\n')
    (output / 'verification.txt').write_text('\n'.join(transcript) + '\n')
    return events


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('/tmp/moyai-access-demo'))
    parser.add_argument('--delay', type=float, default=0)
    args = parser.parse_args()
    if not 0 <= args.delay <= 3:
        parser.error('Use a delay between 0 and 3 seconds.')
    demonstrate(args.output, args.delay)
