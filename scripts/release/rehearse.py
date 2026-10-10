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

from scripts.release.deploy import BROKER, COORDINATOR, WORKER, MUTABLE, SERVICE_NAMES, Release, ReleaseError

OLD, NEW = 'a' * 40, 'b' * 40


class Render:
    def __init__(self, *, split=False):
        common = {'MOYAI_BUILD_SHA': OLD, 'MAINTENANCE_DRAIN': 'false', 'RENDER_MIGRATION_STAGE': 'false',
                  'TEMPORAL_ENABLED': 'true', 'MOYAI_DATABASE_INITIALIZE': 'false',
                  'MOYAI_DATABASE_URL': 'postgresql://private/not-printed', 'MOYAI_DATABASE_SCHEMA': 'moyai_test',
                  'OBJECT_STORAGE_BUCKET': 'private-bucket', 'SESSION_SECRET': 'do-not-log-session',
                  'ENCRYPTION_KEY': 'do-not-log-encryption', 'MAX_CONCURRENT_RUNS': '100',
                  'MAX_PENDING_RUNS': '1000', 'MAX_CONCURRENT_MODEL_REQUESTS': '32'}
        if split:
            common['MOYAI_SEPARATE_BROKER'] = 'true'
        roles = [(COORDINATOR, 'coordinator'), (WORKER, 'worker')]
        if split:
            roles.append((BROKER, 'broker'))
        self.env = {sid: {**common, 'MOYAI_RUNTIME_ROLE': role} for sid, role in
                    roles}
        self.live = {sid: {'id': 'dep-old' + str(i), 'commit': {'id': OLD}, 'status': 'live'}
                     for i, sid in enumerate(self.env)}
        self.deployments, self.calls = {}, []
        self.worker_running = True
        self.split, self.broker_running = split, split
        self.fail_service = None
        self.unknown_write = False
        self.owner_delay = 0
        self.broker_owner_delay = 0
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
            return {'name': SERVICE_NAMES[self.env[sid]['MOYAI_RUNTIME_ROLE']],
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
                assert not self.broker_running, 'Never update coordinator with old broker alive'
            elif self.env[sid]['RENDER_MIGRATION_STAGE'] == 'false':
                assert body['commitId'] == self.live[COORDINATOR]['commit']['id'], 'Never activate mismatched service'
                if sid == WORKER and self.split:
                    assert self.broker_running and body['commitId'] == self.live[BROKER]['commit']['id']
            if sid == BROKER:
                assert not self.worker_running, 'Never replace broker while worker is consuming jobs'
            deploy = {'id': 'dep-new' + str(len(self.deployments)), 'commit': {'id': body['commitId']},
                      'status': 'update_failed' if sid == self.fail_service else 'live'}
            self.deployments[deploy['id']] = deepcopy(deploy)
            if self.unknown_write:
                raise ReleaseError('Deployment API request failed. A write may have been accepted; inspect the receipt before retrying.')
            if deploy['status'] == 'live':
                self.live[sid] = deepcopy(deploy)
                if sid == WORKER:
                    self.worker_running = self.env[sid]['RENDER_MIGRATION_STAGE'] != 'true'
                if sid == BROKER:
                    self.broker_running = self.env[sid]['RENDER_MIGRATION_STAGE'] != 'true'
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
        owners = 1 + int(self.worker_running) + int(self.broker_running)
        brokers = int(self.broker_running) if self.split else 1
        if not self.worker_running and self.owner_delay:
            self.owner_delay -= 1
            owners += 1
        if self.split and not self.broker_running and self.broker_owner_delay:
            self.broker_owner_delay -= 1
            owners += 1
            brokers += 1
        return {'ok': self.probe_ok, 'owners': owners, 'coordinators': 1, 'brokers': brokers,
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
    parser.add_argument('--separate-broker', action='store_true', help='Rehearse the activated three-service topology.')
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
            render = Render(split=args.separate_broker)
            if scenario != 'success':
                render.fail_service = COORDINATOR
            call = render.call
            def observed(method, path, body=None):
                result = call(method, path, body)
                if method == 'POST':
                    role = render.env[path.split('/')[2]]['MOYAI_RUNTIME_ROLE']
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
                assert all(render.env[sid]['MOYAI_BUILD_SHA'] == NEW for sid in render.env)
                assert render.env[WORKER]['MAINTENANCE_DRAIN'] == 'false'
                log('VERIFIED: ' + ('app + broker + worker' if args.separate_broker else 'app + worker')
                    + ' on bbbbbbb; queued execution resumed.')
            else:
                assert receipt['status'] == 'failed'
                assert render.env[WORKER]['RENDER_MIGRATION_STAGE'] == 'true'
                assert render.env[WORKER]['MAINTENANCE_DRAIN'] == 'true'
                assert render.live[COORDINATOR]['commit']['id'] == OLD
                if args.separate_broker:
                    assert render.env[BROKER]['RENDER_MIGRATION_STAGE'] == 'true'
                log('VERIFIED: old app still live; execution services remain staged and paused.')
    log('Both local scenarios verified. Production has not been contacted.')


if __name__ == '__main__':
    main()
