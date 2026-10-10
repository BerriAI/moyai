"""Offline release rehearsal. Render, SSH, CI and commits are synthetic.

This module never constructs a network client or reads deployment credentials.
Run: python3 -m scripts.release.rehearse --pace 0.7
"""
from copy import deepcopy
from pathlib import Path
import argparse
import json
import tempfile
import time

from scripts.release.deploy import COORDINATOR, WORKER, MUTABLE, Release, ReleaseError

OLD, NEW = 'a' * 40, 'b' * 40


class Render:
    def __init__(self):
        common = {'MOYAI_BUILD_SHA': OLD, 'MAINTENANCE_DRAIN': 'false', 'RENDER_MIGRATION_STAGE': 'false',
                  'TEMPORAL_ENABLED': 'true', 'MOYAI_DATABASE_INITIALIZE': 'false',
                  'MOYAI_DATABASE_URL': 'postgresql://private/not-printed', 'MOYAI_DATABASE_SCHEMA': 'moyai_test',
                  'OBJECT_STORAGE_BUCKET': 'private-bucket', 'SESSION_SECRET': 'do-not-log-session',
                  'ENCRYPTION_KEY': 'do-not-log-encryption', 'MAX_CONCURRENT_RUNS': '100',
                  'MAX_PENDING_RUNS': '1000', 'MAX_CONCURRENT_MODEL_REQUESTS': '32'}
        self.env = {sid: {**common, 'MOYAI_RUNTIME_ROLE': role} for sid, role in
                    ((COORDINATOR, 'coordinator'), (WORKER, 'worker'))}
        self.live = {sid: {'id': 'dep-old' + str(i), 'commit': {'id': OLD}, 'status': 'live'}
                     for i, sid in enumerate(self.env)}
        self.deployments, self.calls = {}, []
        self.worker_running = True
        self.fail_service = None
        self.unknown_write = False
        self.owner_delay = 0
        self.unsafe = 0
        self.models = 0
        self.draining_seen = 0
        self.probe_ok = True
        self.external_change = False
        self.auto = 'off'

    def pages(self, path, key):
        sid = path.split('/')[2]
        if key == 'envVar':
            return [{'key': k, 'value': v} for k, v in self.env[sid].items()]
        return [deepcopy(self.live[sid])]

    def call(self, method, path, body=None):
        parts = path.split('/')
        sid = parts[2]
        if method == 'GET' and len(parts) == 3:
            return {'name': 'moyai-private' if sid == COORDINATOR else 'moyai-worker',
                    'type': 'private_service', 'suspended': 'not_suspended', 'autoDeployTrigger': self.auto,
                    'branch': 'main', 'repo': 'https://github.com/BerriAI/moyai', 'ownerId': 'same-owner',
                    'serviceDetails': {'region': 'oregon', 'numInstances': 1,
                                       'sshAddress': sid + '@ssh.oregon.render.com'}}
        if method == 'PUT':
            self.calls.append((method, sid, parts[-1], body['value']))
            assert parts[-1] in MUTABLE
            self.env[sid][parts[-1]] = body['value']
            return {'key': parts[-1], 'value': body['value']}
        if method == 'POST':
            self.calls.append((method, sid, body['commitId'], self.env[sid]['RENDER_MIGRATION_STAGE']))
            assert set(body) == {'commitId', 'clearCache'}
            if sid == COORDINATOR:
                assert not self.worker_running, 'Never update coordinator with old worker alive'
            elif self.env[sid]['RENDER_MIGRATION_STAGE'] == 'false':
                assert body['commitId'] == self.live[COORDINATOR]['commit']['id'], 'Never activate mismatched worker'
            deploy = {'id': 'dep-new' + str(len(self.deployments)), 'commit': {'id': body['commitId']},
                      'status': 'update_failed' if sid == self.fail_service else 'live'}
            self.deployments[deploy['id']] = deepcopy(deploy)
            if self.unknown_write:
                raise ReleaseError('Deployment API request failed. A write may have been accepted; inspect the receipt before retrying.')
            if deploy['status'] == 'live':
                self.live[sid] = deepcopy(deploy)
                if sid == WORKER:
                    self.worker_running = self.env[sid]['RENDER_MIGRATION_STAGE'] != 'true'
            return deepcopy(deploy)
        if method == 'GET' and parts[-2] == 'deploys':
            return deepcopy(self.deployments[parts[-1]])
        raise AssertionError((method, path))

    def probe(self, service):
        if service.role == 'worker' and service.env['MAINTENANCE_DRAIN'] == 'true':
            self.draining_seen += 1
        if service.env['RENDER_MIGRATION_STAGE'] == 'true':
            return {'ok': self.probe_ok, 'staged': True}
        if self.external_change and service.id == WORKER and self.draining_seen:
            self.env[COORDINATOR]['MAX_CONCURRENT_MODEL_REQUESTS'] = '64'
        owners = 1 + int(self.worker_running)
        if not self.worker_running and self.owner_delay:
            self.owner_delay -= 1
            owners += 1
        return {'ok': self.probe_ok, 'owners': owners, 'coordinators': 1,
                'unsafe_sessions': self.unsafe, 'legacy_active_sessions': 0,
                'model_requests': self.models, 'live_leases': 0, 'database_connections': 16}


class Candidate:
    def __init__(self, sha=NEW):
        self.sha, self.resolutions = sha, 0

    def candidate(self):
        self.resolutions += 1
        return self.sha

    def ready(self, sha):
        assert sha == self.sha
        return True

    def forward_from(self, previous, selected):
        assert previous == OLD and selected == self.sha


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pace', type=float, default=0, help='Optional display delay between recorded events.')
    args = parser.parse_args()
    if not 0 <= args.pace <= 2:
        parser.error('--pace must be between 0 and 2 seconds')
    def log(message, **kwargs):
        print(message, flush=True)
        time.sleep(args.pace)
    log('LOCAL REHEARSAL | Simulated Render and SSH | No production changes')
    with tempfile.TemporaryDirectory(prefix='moyai-release-rehearsal-') as directory:
        for scenario in ('success', 'failed coordinator'):
            log('')
            log('Scenario: ' + scenario)
            render = Render()
            if scenario != 'success':
                render.fail_service = COORDINATOR
            call = render.call
            def observed(method, path, body=None):
                result = call(method, path, body)
                if method == 'POST':
                    role = 'app' if COORDINATOR in path else 'worker'
                    log('  Render simulator: ' + role + ' build ' + body['commitId'][:7] + ' -> ' + result['status'])
                return result
            render.call = observed
            release = Release(render, Candidate(), render.probe, Path(directory) / 'receipt.json', log=log)
            try:
                release.run()
            except ReleaseError as exc:
                if scenario == 'success':
                    raise
                log('Stopped safely: ' + str(exc))
            receipt = json.loads(release.receipt.read_text())
            if scenario == 'success':
                assert receipt['status'] == 'success'
                assert all(render.env[sid]['MOYAI_BUILD_SHA'] == NEW for sid in (COORDINATOR, WORKER))
                assert render.env[WORKER]['MAINTENANCE_DRAIN'] == 'false'
                log('VERIFIED: app + worker on bbbbbbb; queued execution resumed.')
            else:
                assert receipt['status'] == 'failed'
                assert render.env[WORKER]['RENDER_MIGRATION_STAGE'] == 'true'
                assert render.env[WORKER]['MAINTENANCE_DRAIN'] == 'true'
                assert render.live[COORDINATOR]['commit']['id'] == OLD
                log('VERIFIED: old app still live; worker remains staged and paused.')
    log('Both local scenarios verified. Production has not been contacted.')


if __name__ == '__main__':
    main()
