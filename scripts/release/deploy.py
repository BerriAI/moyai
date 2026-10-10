"""Coordinate the verified Render topology; never deploy on import.

Run via .github/workflows/deploy-production.yml. Requests that change state are
never retried automatically: a lost response may still mean Render accepted it.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from typing import Callable
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
REPOSITORY = 'BerriAI/moyai'
COORDINATOR = 'srv-db41eiqj9qps73fpuan0'
WORKER = 'srv-db4s0b142hec73epgpt0'
BROKER = 'srv-db4tlsajnfac738gigb0'
SERVICE_NAMES = {'coordinator': 'moyai-private', 'worker': 'moyai-worker', 'broker': 'moyai-broker'}
MUTABLE = {'MOYAI_BUILD_SHA', 'MAINTENANCE_DRAIN', 'RENDER_MIGRATION_STAGE'}
ACTIVE_DEPLOYS = {'created', 'queued', 'build_in_progress', 'pre_deploy_in_progress', 'update_in_progress'}
FAILED_DEPLOYS = {'build_failed', 'pre_deploy_failed', 'update_failed', 'canceled', 'deactivated'}
SHARED_KEYS = ('MOYAI_DATABASE_URL', 'MOYAI_DATABASE_SCHEMA', 'SESSION_SECRET', 'ENCRYPTION_KEY',
               'OBJECT_STORAGE_BUCKET', 'OBJECT_STORAGE_ENDPOINT', 'OBJECT_STORAGE_PREFIX',
               'MOYAI_PUBLIC_URL', 'TEMPORAL_ADDRESS', 'TEMPORAL_NAMESPACE', 'TEMPORAL_TASK_QUEUE',
               'MAX_CONCURRENT_RUNS', 'MAX_PENDING_RUNS', 'MAX_CONCURRENT_MODEL_REQUESTS')


class ReleaseError(RuntimeError):
    """Only fixed, credential-free messages may be used here."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, base: str, token: str):
        if not token:
            raise ReleaseError('A required deployment credential is missing; no changes made.')
        self.base, self.token = base, token
        self.opener = urllib.request.build_opener(NoRedirect())

    def call(self, method: str, path: str, body=None):
        request = urllib.request.Request(self.base + path,
            data=None if body is None else json.dumps(body).encode(), method=method,
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json',
                     'Accept': 'application/json', 'User-Agent': 'moyai-production-release'})
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise ReleaseError('Deployment API response exceeded its size limit.')
                return json.loads(raw) if raw else None
        except (urllib.error.URLError, ValueError, TimeoutError):
            # Never expose HTTP bodies, URLs, or headers; env responses contain secrets.
            raise ReleaseError('Deployment API request failed. A write may have been accepted; inspect the receipt before retrying.') from None

    def pages(self, path: str, key: str):
        result, cursor = [], ''
        for _ in range(20):
            query = urllib.parse.urlencode({'limit': 100, **({'cursor': cursor} if cursor else {})})
            page = self.call('GET', path + ('&' if '?' in path else '?') + query)
            if not isinstance(page, list):
                raise ReleaseError('Unexpected Render list response.')
            result.extend(item[key] for item in page)
            if len(page) < 100:
                return result
            next_cursor = page[-1].get('cursor', '')
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        raise ReleaseError('Render pagination did not finish; refusing an incomplete inventory.')


class GitHub:
    def __init__(self, api: API, run_id: str, dispatch_sha: str):
        self.api, self.run_id, self.dispatch_sha = api, run_id, dispatch_sha
        self.prefix = '/repos/' + REPOSITORY

    def candidate(self) -> str:
        # checkout and workflow_dispatch both refer to this immutable commit.
        # Resolving main again could deploy code newer than the release tooling.
        sha = self.dispatch_sha
        if not re.fullmatch('[0-9a-f]{40}', sha):
            raise ReleaseError('The main workflow dispatch did not provide a full commit SHA.')
        return sha

    def forward_from(self, previous: str, selected: str):
        comparison = self.api.call('GET', self.prefix + f'/compare/{previous}...{selected}')
        if comparison['status'] not in {'ahead', 'identical'}:
            raise ReleaseError('The selected commit is older than or diverges from production. Start a new main workflow; automatic rollback is not supported.')

    def ready(self, sha: str) -> bool:
        # Docker startup runs on every push, including changes outside path filters.
        runs = self.api.call('GET', self.prefix + '/actions/workflows/docker.yml/runs?' +
                             urllib.parse.urlencode({'head_sha': sha, 'event': 'push', 'per_page': 100}))['workflow_runs']
        docker = max(runs, key=lambda run: run['id'], default=None)
        if docker and docker['status'] == 'completed' and docker['conclusion'] != 'success':
            raise ReleaseError('The required Docker startup workflow failed for the selected commit.')
        checks = []
        for page in range(1, 21):
            batch = self.api.call('GET', self.prefix + f'/commits/{sha}/check-runs?per_page=100&page={page}&filter=latest')
            checks.extend(batch['check_runs'])
            # total_count can include historical reruns excluded by filter=latest.
            # Stop at the end of the returned pages, not that unfiltered count.
            if len(batch['check_runs']) < 100:
                break
        else:
            raise ReleaseError('GitHub checks inventory was incomplete.')
        ready = bool(docker and docker['conclusion'] == 'success')
        for check in checks:
            if f'/actions/runs/{self.run_id}/' in check.get('details_url', ''):
                continue  # This deployment job is itself a check on main.
            if check['status'] != 'completed':
                ready = False
            elif check['conclusion'] not in {'success', 'neutral', 'skipped'}:
                raise ReleaseError('A check failed for the selected commit; production was not changed.')
        statuses = self.api.call('GET', self.prefix + f'/commits/{sha}/status')
        if statuses['total_count']:
            if statuses['state'] in {'failure', 'error'}:
                raise ReleaseError('A commit status failed; production was not changed.')
            ready = ready and statuses['state'] == 'success'
        return ready


@dataclass
class Service:
    id: str
    role: str
    env: dict[str, str] = field(repr=False)
    original: dict[str, str] = field(repr=False)
    sha: str
    deploy_id: str

    def probe_options(self):
        values = {k: v for k, v in self.original.items() if k not in MUTABLE}
        return {'sha': self.sha, 'role': self.role,
                'staged': self.env['RENDER_MIGRATION_STAGE'] == 'true',
                'drain': self.env['MAINTENANCE_DRAIN'] == 'true',
                'env_keys': sorted(values),
                'env_digest': hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()}


class SSHProbe:
    def __init__(self, key: Path):
        self.key = key

    def __call__(self, service: Service):
        options = json.dumps(service.probe_options(), separators=(',', ':'))
        # Both possible container UIDs retain the existing environment. Never
        # create a root-owned SQLite file or import app.main in this probe.
        command = ('cd /app && if [ "$(id -u)" = 0 ]; then exec runuser -u workspace -- '
                   '/app/.venv/bin/python - ' + shlex.quote(options) +
                   '; else exec /app/.venv/bin/python - ' + shlex.quote(options) + '; fi')
        args = ['ssh', '-T', '-i', str(self.key), '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
                '-o', 'StrictHostKeyChecking=yes', '-o', 'GlobalKnownHostsFile=/dev/null',
                '-o', 'UserKnownHostsFile=' + str(ROOT / 'render-known-hosts'), '-o', 'ConnectTimeout=10',
                '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2',
                service.id + '@ssh.oregon.render.com', command]
        try:
            result = subprocess.run(args, input=(ROOT / 'probe.py').read_text(), text=True,
                                    capture_output=True, timeout=50)
            if result.returncode:
                return {'ok': False}
            lines = [line.removeprefix('MOYAI_RELEASE_PROBE=') for line in result.stdout.splitlines()
                     if line.startswith('MOYAI_RELEASE_PROBE=')]
            return json.loads(lines[-1]) if len(lines) == 1 else {'ok': False}
        except (subprocess.TimeoutExpired, ValueError, OSError):
            return {'ok': False}


class Release:
    def __init__(self, render: API, github: GitHub, probe: Callable, receipt: Path, *,
                 wait_seconds=15, drain_timeout=1800, deploy_timeout=1200, clock=time.monotonic,
                 sleep=time.sleep, log=print, preflight_only=False):
        self.render, self.github, self.probe = render, github, probe
        self.receipt, self.wait_seconds = receipt, wait_seconds
        self.drain_timeout, self.deploy_timeout = drain_timeout, deploy_timeout
        self.clock, self.sleep, self.log = clock, sleep, log
        self.preflight_only = preflight_only
        self.record = {'status': 'preflight', 'phase': 'not_started', 'deploys': [], 'changes': []}
        self.expected: dict[str, Service] = {}

    def save(self):
        self.receipt.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.receipt.with_suffix('.next')
        temporary.write_text(json.dumps(self.record, indent=2) + '\n')
        temporary.replace(self.receipt)

    def phase(self, name):
        self.record['phase'] = name
        self.save()
        self.log(name, flush=True)

    def wait(self, test: Callable, reason: str, timeout: int):
        deadline = self.clock() + timeout
        while True:
            value = test()
            if value:
                return value
            if self.clock() >= deadline:
                raise ReleaseError(reason)
            self.sleep(self.wait_seconds)

    def environment(self, service_id):
        items = self.render.pages(f'/services/{service_id}/env-vars', 'envVar')
        env = {item['key']: item['value'] for item in items}
        if len(env) != len(items) or any(not isinstance(v, str) for v in env.values()):
            raise ReleaseError('Environment inventory is ambiguous.')
        return env

    def deploys(self, service_id):
        return self.render.pages(f'/services/{service_id}/deploys', 'deploy')

    def inspect(self, service_id, role):
        service = self.render.call('GET', '/services/' + service_id)
        details = service['serviceDetails']
        expected_name = SERVICE_NAMES[role]
        auto_off = service.get('autoDeployTrigger') == 'off' or (
            not service.get('autoDeployTrigger') and service.get('autoDeploy') == 'no')
        if (service['name'] != expected_name or service['type'] != 'private_service'
                or service['suspended'] != 'not_suspended' or not auto_off
                or service.get('branch') != 'main' or service.get('repo', '').removesuffix('.git') != 'https://github.com/' + REPOSITORY
                or details['region'] != 'oregon' or details['numInstances'] != 1 or details.get('autoscaling')
                or details.get('sshAddress') != service_id + '@ssh.oregon.render.com'):
            raise ReleaseError('Service topology changed. This workflow supports one coordinator, one worker and an optional separate broker only.')
        deploys = self.deploys(service_id)
        if any(d['status'] in ACTIVE_DEPLOYS for d in deploys):
            raise ReleaseError('Another Render deployment is active; no overlapping release is allowed.')
        live = [d for d in deploys if d['status'] == 'live']
        if len(live) != 1:
            raise ReleaseError('Expected exactly one live deployment per service.')
        env = self.environment(service_id)
        sha = live[0]['commit']['id']
        if (not re.fullmatch('[0-9a-f]{40}', sha) or env.get('MOYAI_BUILD_SHA') != sha
                or env.get('MOYAI_RUNTIME_ROLE') != role or env.get('MAINTENANCE_DRAIN') != 'false'
                or env.get('RENDER_MIGRATION_STAGE') != 'false'
                or env.get('TEMPORAL_ENABLED', '').lower() != 'true'
                or env.get('MOYAI_DATABASE_INITIALIZE', '').lower() != 'false'
                or not all(env.get(k) for k in ('MOYAI_DATABASE_URL', 'MOYAI_DATABASE_SCHEMA',
                    'OBJECT_STORAGE_BUCKET', 'SESSION_SECRET', 'ENCRYPTION_KEY'))
                or env.get('MOYAI_SEPARATE_BROKER', 'false').lower() not in {'true', 'false'}
                or (role == 'broker' and env.get('MOYAI_SEPARATE_BROKER', '').lower() != 'true')):
            raise ReleaseError('Production is not at a clean matching-build baseline. Inspect the prior release receipt before retrying.')
        item = Service(service_id, role, dict(env), dict(env), sha, live[0]['id'])
        self.expected[service_id] = item
        return item, service['ownerId']

    def unchanged(self):
        # Detect dashboard edits before every mutation, including before activation.
        # GitHub concurrency cannot lock a human out of the Render dashboard.
        for service in self.expected.values():
            if self.environment(service.id) != service.env:
                raise ReleaseError('Render environment changed outside this release; stopped without overwriting it.')
            deploys = self.deploys(service.id)
            if any(d['status'] in ACTIVE_DEPLOYS for d in deploys):
                raise ReleaseError('An unexpected Render deployment is active; release stopped.')
            live = [d for d in deploys if d['status'] == 'live']
            if len(live) != 1 or live[0]['id'] != service.deploy_id:
                raise ReleaseError('Render deployment changed outside this release; release stopped.')

    def configure(self, service, changes):
        self.unchanged()
        if not changes.keys() <= MUTABLE:
            raise ReleaseError('Refusing an unapproved environment change.')
        for key, value in changes.items():
            if service.env[key] == value:
                continue
            self.record['changes'].append({'service': service.id, 'key': key, 'value': value, 'confirmed': False})
            self.save()  # Record the intent even when the API response is lost.
            self.render.call('PUT', f'/services/{service.id}/env-vars/{key}', {'value': value})
            service.env[key] = value
            if self.environment(service.id) != service.env:
                raise ReleaseError('Saved environment did not match the intended change; release stopped.')
            self.record['changes'][-1]['confirmed'] = True
            self.save()

    def deploy(self, service, sha):
        self.unchanged()
        self.record['deploys'].append({'service': service.id, 'sha': sha, 'id': None, 'status': 'requested'})
        self.save()
        started = self.render.call('POST', f'/services/{service.id}/deploys',
                                   {'commitId': sha, 'clearCache': 'do_not_clear'})
        deploy_id = started['id']
        if not re.fullmatch('dep-[a-z0-9]+', deploy_id):
            raise ReleaseError('Render returned an invalid deployment identity.')
        self.record['deploys'][-1]['id'] = deploy_id
        self.save()
        def finished():
            state = self.render.call('GET', f'/services/{service.id}/deploys/{deploy_id}')
            if state.get('commit', {}).get('id') != sha:
                raise ReleaseError('Render selected a different commit; release stopped.')
            if state['status'] in FAILED_DEPLOYS:
                raise ReleaseError('Render deployment failed. Execution was not automatically resumed.')
            if state['status'] not in ACTIVE_DEPLOYS | {'live'}:
                raise ReleaseError('Render returned an unknown deployment state.')
            return state['status'] == 'live'
        self.wait(finished, 'Render deployment timed out; inspect its status before retrying.', self.deploy_timeout)
        service.sha, service.deploy_id = sha, deploy_id
        self.record['deploys'][-1]['status'] = 'live'
        self.save()
        self.wait(lambda: self.probe(service).get('ok'), 'Live deployment did not pass runtime checks.', 300)

    @staticmethod
    def settled(state, owners, *, drained=False, brokers=None):
        keys = ('owners', 'coordinators', 'brokers', 'unsafe_sessions', 'legacy_active_sessions', 'model_requests', 'live_leases')
        if not state.get('ok') or any(type(state.get(key)) is not int for key in keys):
            return False
        # Legacy combined builds predate the broker lock. New combined builds
        # hold it in the coordinator; split rollouts require an exact count.
        return (state['owners'] == owners and state['coordinators'] == 1
                and (state['brokers'] in (0, 1) if brokers is None else state['brokers'] == brokers)
                and (not drained or all(state[key] == 0 for key in keys[3:])))

    @classmethod
    def execution_stopped(cls, state, *, brokers=None):
        # Drain session state while workers can still complete it. After staging,
        # a new Stop request needs the replacement worker to finish; it must not
        # block the ownership handoff. Still fence actual workers and execution.
        return (cls.settled(state, 1 + (brokers or 0), brokers=brokers) and state['model_requests'] == 0
                and state['live_leases'] == 0)

    def run(self):
        try:
            sha = self.github.candidate()
            self.record['commit'] = sha
            self.phase('Check selected main commit and CI')
            self.wait(lambda: self.github.ready(sha), 'CI did not finish before the release deadline; no production changes made.', 1800)
            coordinator, owner = self.inspect(COORDINATOR, 'coordinator')
            worker, worker_owner = self.inspect(WORKER, 'worker')
            split = coordinator.env.get('MOYAI_SEPARATE_BROKER', 'false').lower() == 'true'
            services = [coordinator, worker]
            self.check_shared(coordinator, owner, worker, worker_owner, split)
            broker = None
            if split:
                broker, broker_owner = self.inspect(BROKER, 'broker')
                self.check_shared(coordinator, owner, broker, broker_owner, split)
                services.append(broker)
            owners, brokers = len(services), 1 if split else None
            self.record['topology'] = 'separate_broker' if split else 'combined_broker'
            self.record['previous_commit'] = coordinator.sha
            self.github.forward_from(coordinator.sha, sha)
            for service in services:
                if not self.settled(self.probe(service), owners, brokers=brokers):
                    raise ReleaseError('Preflight SSH, health, configuration or ownership check failed; no production changes made.')
            if self.preflight_only:
                self.record['status'] = 'preflight_passed'
                self.phase('Preflight passed; no production changes made')
                return
            if sha == coordinator.sha:
                self.phase('Selected main commit is already deployed; no restart needed')
            else:
                self.record['status'] = 'running'
                self.phase('Pause new execution on the current worker build')
                self.configure(worker, {'MAINTENANCE_DRAIN': 'true'})
                self.deploy(worker, worker.sha)
                self.phase('Wait for active work to reach saved boundaries')
                self.wait(lambda: self.settled(self.probe(worker), owners, brokers=brokers, drained=True),
                          'Drain timed out. New execution remains paused; no user sessions were cancelled.', self.drain_timeout)
                self.phase('Stage the replacement worker without consuming jobs')
                self.configure(worker, {'RENDER_MIGRATION_STAGE': 'true', 'MOYAI_BUILD_SHA': sha})
                self.deploy(worker, sha)
                self.phase('Confirm all old execution workers have exited')
                self.wait(lambda: self.execution_stopped(self.probe(coordinator), brokers=brokers),
                          'Old worker ownership did not clear; the coordinator was not changed.', 600)
                if broker:
                    self.phase('Stage the replacement broker after execution has stopped')
                    self.configure(broker, {'RENDER_MIGRATION_STAGE': 'true', 'MOYAI_BUILD_SHA': sha})
                    self.deploy(broker, sha)
                    self.phase('Confirm old broker ownership and requests have cleared')
                    self.wait(lambda: self.execution_stopped(self.probe(coordinator), brokers=0),
                              'Old broker ownership did not clear; the coordinator was not changed.', 600)
                self.phase('Deploy the coordinator at the selected commit')
                self.configure(coordinator, {'MOYAI_BUILD_SHA': sha})
                self.deploy(coordinator, sha)
                if broker:
                    self.phase('Activate and verify the matching broker before resuming execution')
                    self.configure(broker, {'RENDER_MIGRATION_STAGE': 'false'})
                    self.deploy(broker, sha)
                    self.wait(lambda: all(self.execution_stopped(self.probe(s), brokers=1)
                                          for s in (coordinator, broker)),
                              'Broker runtime checks failed; the worker remains staged.', 300)
                self.phase('Activate the matching worker and resume queued work')
                self.configure(worker, {'RENDER_MIGRATION_STAGE': 'false', 'MAINTENANCE_DRAIN': 'false'})
                self.deploy(worker, sha)
                self.phase('Verify every service, preserved settings and singleton ownership')
                self.wait(lambda: all(self.settled(self.probe(s), owners, brokers=brokers) for s in services),
                          'Final runtime checks failed; inspect the release receipt.', 300)
                self.unchanged()
            self.record['status'] = 'success'
            self.phase('Production release verified')
        except BaseException as exc:
            self.record['status'] = 'failed'
            self.record['reason'] = str(exc) if isinstance(exc, ReleaseError) else 'Release interrupted or unexpected response; inspect the last recorded phase.'
            self.save()
            raise

    @staticmethod
    def check_shared(coordinator, owner, service, service_owner, split):
        if (owner != service_owner or coordinator.sha != service.sha
                or (service.env.get('MOYAI_SEPARATE_BROKER', 'false').lower() == 'true') != split
                or any(coordinator.env.get(k, '') != service.env.get(k, '') for k in SHARED_KEYS)):
            raise ReleaseError('The services do not share the same build and runtime configuration.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipt', type=Path, default=Path('release-receipt.json'))
    parser.add_argument('--preflight', action='store_true', default=os.environ.get('PREFLIGHT_ONLY') == 'true',
                        help='Check CI, credentials and the current topology without changing production.')
    args = parser.parse_args()
    if os.environ.get('GITHUB_REPOSITORY') != REPOSITORY or os.environ.get('GITHUB_REF') != 'refs/heads/main':
        raise ReleaseError('Production deployment may only run from main in BerriAI/moyai.')
    key = os.environ.pop('RENDER_DEPLOY_SSH_KEY', '')
    if not key:
        raise ReleaseError('RENDER_DEPLOY_SSH_KEY is missing; no production changes made.')
    def interrupted(signum, frame):
        raise ReleaseError('Release cancelled. Inspect the receipt; paused or staged execution was not automatically resumed.')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    with tempfile.TemporaryDirectory(prefix='moyai-deploy-') as directory:
        key_path = Path(directory) / 'key'
        key_path.write_text(key.rstrip() + '\n')
        key_path.chmod(0o600)
        render = API('https://api.render.com/v1', os.environ.pop('RENDER_DEPLOY_API_KEY', ''))
        github = GitHub(API('https://api.github.com', os.environ.pop('GH_TOKEN', '')),
                        os.environ['GITHUB_RUN_ID'], os.environ.get('GITHUB_SHA', ''))
        release = Release(render, github, SSHProbe(key_path), args.receipt, preflight_only=args.preflight)
        try:
            release.run()
        finally:
            if summary := os.environ.get('GITHUB_STEP_SUMMARY'):
                with open(summary, 'a') as output:
                    output.write('## Production deployment\n\n')
                    output.write(f"Status: **{release.record['status']}**\n\nLast phase: {release.record['phase']}\n\n")
                    if 'commit' in release.record:
                        output.write(f"Commit: `{release.record['commit']}`\n\n")
                    if release.record['status'] not in {'success', 'preflight_passed'}:
                        output.write('Inspect the release-receipt artifact and Render before retrying. Do not deploy only one service or resume mismatched builds.\n')


if __name__ == '__main__':
    try:
        main()
    except ReleaseError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print('Unexpected deployment response. Inspect the release receipt; no automatic rollback was attempted.', file=sys.stderr)
        sys.exit(1)
