"""Headless IAM Identity Center login; no infrastructure credential chain.

Device authorization is durable and polled in separate tool calls, rather than
holding a credential command open while the person approves it on another device.
"""
import json
import time
from contextlib import closing
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from .db import database
from app.aws_credentials import bundle, cache_paths, profile_fields

AWS_LOGIN_DESCRIPTION = (
    'Start, poll or cancel a personal AWS IAM Identity Center device login. First inspect credentials_list '
    'and reuse an appropriate saved aws-sso profile through credentials_request. For start, supply profile '
    '(lowercase profile name, AWS access portal start_url, sso_region, account_id, role_name and region), '
    'a stable request_key, and lifetime=session or persistent as authorized by the user. Show the returned '
    'verification_uri and user_code to the requester; they must approve on their own device. Do not approve '
    'on their behalf or ask for passwords/tokens. Poll the returned login_id only after interval seconds. '
    'Login is personal and does not grant organization access or change AWS permissions. Completed login '
    'returns metadata only; request name=aws-sso/<profile>, provider=generic, format=aws-sso and the secret_id, '
    'then use credentials_run. Never replay a failed or uncertain AWS write after login.'
)


class SSOProfile(BaseModel):
    model_config = ConfigDict(extra='forbid')
    profile: str = Field(pattern=r'^[a-z0-9_-]{1,64}$')
    start_url: str = Field(max_length=2048)
    sso_region: str = Field(max_length=32)
    account_id: str = Field(pattern=r'^[0-9]{12}$')
    role_name: str = Field(max_length=64)
    region: str = Field(max_length=32)

    @model_validator(mode='after')
    def validate_profile(self):
        profile_fields(self.model_dump())
        return self


class AWSLogin(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['start', 'poll', 'cancel']
    profile: SSOProfile | None = None
    lifetime: Literal['session', 'persistent'] = 'session'
    request_key: str = Field(default='', pattern=r'^[A-Za-z0-9_-]{0,80}$')
    login_id: str = Field(default='', pattern=r'^([0-9a-f]{32})?$')

    @model_validator(mode='after')
    def operation(self):
        if self.action == 'start':
            if not self.profile or not self.request_key or self.login_id:
                raise ValueError('Start requires profile and request_key.')
        elif not self.login_id or self.profile or self.request_key:
            raise ValueError('Poll/cancel requires only login_id.')
        return self


def login_required(*, permission=False):
    return {'status': 'permission_required' if permission else 'login_required',
            'message': ('Ask the AWS administrator about the requested role permission; the login remains valid.'
                        if permission else 'Use credentials_aws_login for personal IAM Identity Center access.'),
            'instruction': 'Verify access with a harmless command after authentication. Never automatically replay a previous command.'}


def oidc(region):
    # A separate botocore session with fixed AWS endpoints, unsigned requests,
    # no ambient profile or credentials, and no automatic token-exchange retry.
    import botocore.session
    session = botocore.session.Session()
    session.get_component('config_store').set_config_variable('profile', None)
    session.set_config_variable('config_file', '/dev/null')
    session.set_config_variable('credentials_file', '/dev/null')
    return closing(session.create_client('sso-oidc', region_name=region,
        aws_access_key_id='', aws_secret_access_key='',
        config=Config(signature_version=UNSIGNED, ignore_configured_endpoint_urls=True,
                      connect_timeout=10, read_timeout=20, retries={'total_max_attempts': 1})))


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def current_in(vault, conn, run):
    current = vault.current_in(conn, run)
    if current['status'] not in {'running', 'reconnecting', 'awaiting_approval'}:
        raise HTTPException(409, 'The requesting turn changed. Start login from the current request.')
    if not current['active_user_id'].startswith(('google:', 'cloudflare:')) and not vault.security.local_preview():
        raise HTTPException(403, 'Sign in with a personal identity before saving an AWS login.')
    return current


def public(vault, row, *, connection=None):
    if row['status'] == 'complete':
        saved = vault.store.rows('SELECT * FROM provider_secrets WHERE id=?', (row['secret_id'],), connection=connection)
        if not saved or vault.status(saved[0]) != 'active':
            return login_required()
        return {'status': 'complete', 'login_id': row['id'], 'secret_id': row['secret_id'],
                'name': saved[0]['name'], 'format': 'aws-sso'}
    result = {'status': row['status'], 'login_id': row['id']}
    if row['status'] == 'pending':
        data = json.loads(vault.security.decrypt(row['encrypted']))
        result.update(verification_uri=data['device']['verificationUri'],
                      user_code=data['device']['userCode'], expires_at=iso(row['expires_at']),
                      interval=row['interval_seconds'])
    elif row['status'] in {'uncertain', 'polling', 'starting'}:
        result['instruction'] = 'Do not retry the token exchange. Cancel this login and start a new device authorization.'
    return result


def start(vault, run, args):
    with vault.store.connect() as conn:
        conn.begin_write()
        current_in(vault, conn, run)
        existing = conn.execute('SELECT * FROM aws_login_requests WHERE run_id=? AND message_id=? AND request_key=?',
                               (run['id'], run['active_message_id'], args.request_key)).fetchone()
        if existing:
            if existing['actor_id'] != run['active_user_id']:
                raise HTTPException(403, 'This login belongs to another requester.')
            if existing['profile_json'] != args.profile.model_dump_json() or existing['lifetime'] != args.lifetime:
                raise ValueError('Use the original AWS login arguments with this request_key.')
            return public(vault, existing, connection=conn)
        # Store an intent before external I/O. An interrupted exchange cannot
        # silently be repeated by a retried tool call or another broker worker.
        identity = uuid4().hex
        conn.execute('''INSERT INTO aws_login_requests
            (id,run_id,message_id,actor_id,request_key,profile_json,lifetime,status,expires_at)
            VALUES(?,?,?,?,?,?,?,'starting',?)''',
            (identity, run['id'], run['active_message_id'], run['active_user_id'], args.request_key,
             args.profile.model_dump_json(), args.lifetime, int(time.time()) + 900))
    try:
        with oidc(args.profile.sso_region) as client:
            registration = client.register_client(clientName='Moyai personal AWS access', clientType='public',
                scopes=['sso:account:access'], grantTypes=['urn:ietf:params:oauth:grant-type:device_code', 'refresh_token'])
            device = client.start_device_authorization(clientId=registration['clientId'],
                clientSecret=registration['clientSecret'], startUrl=args.profile.start_url)
        encrypted = vault.security.encrypt(json.dumps({'registration': registration, 'device': device}))
        interval = max(5, int(device.get('interval', 5)))
        with vault.store.connect() as conn:
            conn.begin_write()
            current_in(vault, conn, run)
            conn.execute("UPDATE aws_login_requests SET status='pending',encrypted=?,expires_at=?,"
                         "interval_seconds=?,next_poll=? WHERE id=? AND status='starting'",
                         (encrypted, int(time.time()) + min(int(device['expiresIn']), 900),
                          interval, int(time.time()) + interval, identity))
    except Exception:
        vault.store.execute("UPDATE aws_login_requests SET status='uncertain',encrypted='' WHERE id=? AND status='starting'", (identity,))
        return {'status': 'uncertain', 'login_id': identity,
                'message': 'AWS device authorization could not be confirmed. Start a new login with a new request_key.'}
    return public(vault, vault.store.rows('SELECT * FROM aws_login_requests WHERE id=?', (identity,))[0])


def poll_or_cancel(vault, run, args):
    with vault.store.connect() as conn:
        conn.begin_write()
        current_in(vault, conn, run)
        row = conn.execute('SELECT * FROM aws_login_requests WHERE id=?', (args.login_id,)).fetchone()
        if not row or row['run_id'] != run['id'] or row['actor_id'] != run['active_user_id']:
            raise HTTPException(404, 'AWS login not found for this requester.')
        if args.action == 'cancel' and row['status'] != 'complete':
            cancelled = conn.execute("UPDATE aws_login_requests SET status='cancelled',encrypted='' "
                                     "WHERE id=? AND status!='complete'", (row['id'],)).rowcount
            if cancelled:
                return {'status': 'cancelled', 'login_id': row['id']}
            live = conn.execute('SELECT * FROM aws_login_requests WHERE id=?', (row['id'],)).fetchone()
            return public(vault, live, connection=conn)
        if row['status'] != 'pending':
            return public(vault, row, connection=conn)
        if row['expires_at'] <= time.time():
            conn.execute("UPDATE aws_login_requests SET status='expired',encrypted='' WHERE id=?", (row['id'],))
            return {'status': 'expired', 'login_id': row['id']}
        if row['next_poll'] > time.time():
            return public(vault, row, connection=conn)
        claimed = conn.execute("UPDATE aws_login_requests SET status='polling' WHERE id=? AND status='pending'", (row['id'],)).rowcount
        if not claimed:
            return {'status': 'polling', 'login_id': row['id']}
    data = json.loads(vault.security.decrypt(row['encrypted']))
    profile = json.loads(row['profile_json'])
    registration, device = data['registration'], data['device']
    try:
        with oidc(profile['sso_region']) as client:
            token = client.create_token(grantType='urn:ietf:params:oauth:grant-type:device_code',
                clientId=registration['clientId'], clientSecret=registration['clientSecret'],
                deviceCode=device['deviceCode'])
    except ClientError as exc:
        code = exc.response['Error']['Code']
        status = 'pending' if code in {'AuthorizationPendingException', 'SlowDownException'} else 'failed'
        interval = row['interval_seconds'] + (5 if code == 'SlowDownException' else 0)
        vault.store.execute("UPDATE aws_login_requests SET status=?,interval_seconds=?,next_poll=?,encrypted=? "
                            "WHERE id=? AND status='polling'",
                            (status, interval, int(time.time()) + interval,
                             row['encrypted'] if status == 'pending' else '', row['id']))
        return public(vault, vault.store.rows('SELECT * FROM aws_login_requests WHERE id=?', (row['id'],))[0])
    except Exception:
        vault.store.execute("UPDATE aws_login_requests SET status='uncertain',encrypted='' WHERE id=? AND status='polling'", (row['id'],))
        return {'status': 'uncertain', 'login_id': row['id'],
                'message': 'AWS token exchange could not be confirmed. Cancel and start a new login; no command was executed.'}
    try:
        cached = {'startUrl': profile['start_url'], 'region': profile['sso_region'],
                  'accessToken': token['accessToken'], 'expiresAt': iso(time.time() + token['expiresIn']),
                  'clientId': registration['clientId'], 'clientSecret': registration['clientSecret'],
                  'registrationExpiresAt': iso(registration['clientSecretExpiresAt'])}
        if token.get('refreshToken'):
            cached['refreshToken'] = token['refreshToken']
        value = json.dumps({'version': 1, 'profile': profile, 'cache': {cache_paths(profile)[0]: cached}})
        bundle(value)
        from .credentials import SaveSecret
        with vault.store.connect() as conn:
            conn.begin_write()
            current_in(vault, conn, run)
            # Cancellation while AWS was responding wins over token storage.
            claimed = conn.execute("UPDATE aws_login_requests SET status='completing' "
                                   "WHERE id=? AND status='polling'", (row['id'],)).rowcount
            if not claimed:
                live = conn.execute("SELECT status FROM aws_login_requests WHERE id=?", (row['id'],)).fetchone()
                return {'status': live['status'], 'login_id': row['id']}
            secret_id = vault.insert_secret(conn, SaveSecret(
                provider='generic', name='aws-sso/' + profile['profile'], format='aws-sso',
                label='AWS SSO ' + profile['profile'], scope='personal', lifetime=row['lifetime'],
                root_id=vault.root(run, connection=conn), client_id='aws-login-' + row['id'], value=SecretStr(value)),
                run['active_user_id'], False)
            conn.execute("UPDATE aws_login_requests SET status='complete',encrypted='',secret_id=? WHERE id=?",
                         (secret_id, row['id']))
    except Exception:
        vault.store.execute("UPDATE aws_login_requests SET status='uncertain',encrypted='' WHERE id=? AND status='polling'", (row['id'],))
        return {'status': 'uncertain', 'login_id': row['id'],
                'message': 'AWS login could not be saved. Check saved access before starting a new login.'}
    return public(vault, vault.store.rows('SELECT * FROM aws_login_requests WHERE id=?', (row['id'],))[0])


async def call(vault, run, args):
    from .private_sinks import deny_export
    await database(deny_export, vault.store, run['id'])
    await database(vault.store.execute,
        "UPDATE aws_login_requests SET status='expired',encrypted='' "
        "WHERE expires_at<=? AND status IN ('starting','pending','polling')", (int(time.time()),))
    return await database(start if args.action == 'start' else poll_or_cancel, vault, run, args)
