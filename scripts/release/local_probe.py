"""Run the production maintenance observation against an explicit local role.

Only Render environment normalization and the fixed in-container HTTP port are
adapted. Installed source contracts, PostgreSQL observations and exact health
identity validation are the production probe. This module never starts an app.
"""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit


def validate_local(environment, port):
    def loopback(value):
        host = urlsplit(value).hostname
        return host == 'localhost' or (host is not None and ipaddress.ip_address(host).is_loopback)
    try:
        database = urlsplit(environment['MOYAI_DATABASE_URL'])
        valid = (
            type(port) is int and 1024 <= port <= 65535
            and environment.get('MOYAI_PRODUCT_LOCAL_REHEARSAL') == 'true'
            and not any(environment.get(key) for key in (
                'RENDER_SERVICE_ID', 'RENDER_EXTERNAL_URL', 'RENDER_INSTANCE_ID', 'RENDER'))
            and database.scheme in {'postgres', 'postgresql'} and not database.query and not database.fragment
            and bool(database.path.strip('/'))
            and loopback(environment['MOYAI_DATABASE_URL'])
            and loopback('//' + environment['TEMPORAL_ADDRESS'])
            and urlsplit(environment['OBJECT_STORAGE_ENDPOINT']).scheme in {'http', 'https'}
            and loopback(environment['OBJECT_STORAGE_ENDPOINT'])
            and urlsplit(environment['MOYAI_PUBLIC_URL']).scheme in {'http', 'https'}
            and loopback(environment['MOYAI_PUBLIC_URL'])
        )
    except (KeyError, ValueError):
        valid = False
    if not valid:
        raise ValueError('Local probing requires explicit loopback rehearsal endpoints and no Render service markers.')


def probe_source(port):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('Select a local rehearsal health port.')
    from scripts.release.maintenance_probe import piped_source
    return f'''
import json, sys, urllib.request
import render_start
render_start.configure_environment = lambda: None
_urlopen = urllib.request.urlopen
def local_urlopen(request, **kwargs):
    request = urllib.request.Request(request.full_url.replace(":10000/", ":{port}/"),
                                    headers=dict(request.header_items()))
    return _urlopen(request, **kwargs)
urllib.request.urlopen = local_urlopen
_namespace = {{"__name__": "local_release_probe"}}
exec({piped_source()!r}, _namespace)
'''


def observe(service, *, checkout, python, port, environment):
    validate_local(environment, port)
    source = probe_source(port) + '\nprint(json.dumps(_namespace["result"](json.loads(sys.argv[1]))))\n'
    result = subprocess.run([str(python), '-', json.dumps(service.probe_options())],
        cwd=checkout, env=environment, input=source, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ValueError('Local installed-source observation did not complete.')
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError('Local installed-source observation returned an invalid receipt.')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--python', type=Path, default=Path(sys.executable))
    parser.add_argument('--port', type=int, required=True)
    arguments = parser.parse_args()
    try:
        validate_local(os.environ, arguments.port)
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        options = json.loads(raw)
        result = subprocess.run([str(arguments.python), '-', json.dumps(options)],
            cwd=arguments.checkout, env=dict(os.environ),
            input=probe_source(arguments.port) + '\nprint(json.dumps(_namespace["result"](json.loads(sys.argv[1]))))\n',
            capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise ValueError()
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError()
    except Exception:
        parser.exit(1, 'Local release probe failed; no configuration or child diagnostics were printed.\n')
    print(json.dumps(value, sort_keys=True))


if __name__ == '__main__':
    main()
