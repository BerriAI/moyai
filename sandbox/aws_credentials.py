"""The bounded AWS SSO bundle shared by the broker and command executor.

Only generated configuration and the two CLI cache files for one profile are
supported. Arbitrary config (credential_process, source_profile, endpoints),
archives, and file paths are deliberately not accepted.
"""
import hashlib
import json
import re
from datetime import datetime

MAX_BUNDLE = 128 * 1024
PROFILE_FIELDS = {'profile', 'start_url', 'sso_region', 'account_id', 'role_name', 'region'}


def profile_fields(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != PROFILE_FIELDS:
        raise ValueError('Supply one complete AWS SSO profile.')
    patterns = {
        'profile': r'[A-Za-z0-9_-]{1,64}',
        'start_url': r'https://[a-zA-Z0-9-]+\.awsapps\.com/start/?',
        'sso_region': r'[a-z]{2}(?:-[a-z]+){1,3}-[0-9]',
        'region': r'[a-z]{2}(?:-[a-z]+){1,3}-[0-9]',
        'account_id': r'[0-9]{12}',
        'role_name': r'[\w+=,.@-]{1,64}',
    }
    if any(not isinstance(value[key], str) or not re.fullmatch(pattern, value[key], flags=re.ASCII)
           for key, pattern in patterns.items()):
        raise ValueError('Invalid AWS SSO profile. Use an AWS access portal URL, regions, account ID and role.')
    return dict(value)


def cache_paths(profile: dict) -> tuple[str, str]:
    session = 'moyai-' + profile['profile']
    token = hashlib.sha1(session.encode()).hexdigest()
    role = hashlib.sha1(json.dumps({
        'sessionName': session, 'roleName': profile['role_name'], 'accountId': profile['account_id'],
    }, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return f'sso/cache/{token}.json', f'cli/cache/{role}.json'


def configuration(profile: dict) -> str:
    return (f"[profile {profile['profile']}]\nsso_session = moyai-{profile['profile']}\n"
            f"sso_account_id = {profile['account_id']}\nsso_role_name = {profile['role_name']}\n"
            f"region = {profile['region']}\noutput = json\n"
            f"\n[sso-session moyai-{profile['profile']}]\nsso_start_url = {profile['start_url']}\n"
            f"sso_region = {profile['sso_region']}\nsso_registration_scopes = sso:account:access\n")


def timestamp(value: object) -> None:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('Invalid AWS cache expiry.')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00').replace('UTC', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
    except ValueError:
        raise ValueError('Invalid AWS cache expiry.') from None


def bundle(value: str) -> dict:
    try:
        if not isinstance(value, str) or len(value.encode()) > MAX_BUNDLE or '\0' in value:
            raise ValueError()
        result = json.loads(value)
        if (not isinstance(result, dict) or set(result) != {'version', 'profile', 'cache'}
                or type(result['version']) is not int or result['version'] != 1):
            raise ValueError()
        profile = profile_fields(result['profile'])
        cache = result['cache']
        token_path, role_path = cache_paths(profile)
        if not isinstance(cache, dict) or not set(cache) <= {token_path, role_path}:
            raise ValueError()
        for path, item in cache.items():
            if not isinstance(item, dict):
                raise ValueError()
            if path == token_path:
                if (not {'accessToken', 'expiresAt', 'startUrl', 'region'} <= set(item)
                        or set(item) - {'accessToken', 'expiresAt', 'startUrl', 'region', 'refreshToken',
                                        'clientId', 'clientSecret', 'registrationExpiresAt'}
                        or item['startUrl'] != profile['start_url'] or item['region'] != profile['sso_region']
                        or any(not isinstance(v, str) or not v or len(v) > 32768 for v in item.values())):
                    raise ValueError()
                timestamp(item['expiresAt'])
                if 'registrationExpiresAt' in item:
                    timestamp(item['registrationExpiresAt'])
            else:
                if set(item) != {'ProviderType', 'Credentials'} or item['ProviderType'] != 'sso':
                    raise ValueError()
                credentials = item['Credentials']
                if (not isinstance(credentials, dict)
                        or not {'AccessKeyId', 'SecretAccessKey', 'SessionToken', 'Expiration'} <= set(credentials)
                        or set(credentials) - {'AccessKeyId', 'SecretAccessKey', 'SessionToken', 'Expiration', 'AccountId'}
                        or any(not isinstance(v, str) or not v or len(v) > 32768 for v in credentials.values())
                        or credentials.get('AccountId', profile['account_id']) != profile['account_id']):
                    raise ValueError()
                timestamp(credentials['Expiration'])
        return result
    except (TypeError, ValueError, UnicodeError, KeyError):
        raise ValueError('Invalid AWS SSO bundle. Supply only the selected profile and its bounded CLI caches.') from None


def secret_values(value: object) -> list[str]:
    if isinstance(value, dict):
        return [text for item in value.values() for text in secret_values(item)]
    return [value] if isinstance(value, str) else []
