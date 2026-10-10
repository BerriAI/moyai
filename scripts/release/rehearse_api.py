"""Offline API deployment controller rehearsal; all control-plane calls are simulated."""
from copy import deepcopy
import argparse
import json
from pathlib import Path
import tempfile
import time

from scripts.release.api_deploy import APIRelease
from scripts.release.deploy import BROKER, COORDINATOR, WORKER, ReleaseError
from scripts.release.rehearse import Candidate, NEW, OLD, Render

API_ID = 'srv-syntheticapi'
CONTRACT = {'contract': 'c' * 64, 'schema': 3}


class APIRender(Render):
    def __init__(self):
        super().__init__(split=True)
        self.env[API_ID] = {**self.env[COORDINATOR], 'MOYAI_RUNTIME_ROLE': 'api'}
        for env in self.env.values():
            env['MOYAI_SCHEMA_MODE'] = 'verify'
        self.live[API_ID] = {'id': 'dep-oldapi', 'commit': {'id': OLD}, 'status': 'live'}
        self.api_owners = ['4@original']
        self.background_owners = ['1@coordinator', '2@worker', '3@broker']
        self.unhealthy = False
        self.unreachable = False
        self.retirement_polls = 2
        self.remaining = 0
        self.metadata = {}
        for sid in self.env:
            meta = super().call('GET', '/services/' + sid)
            meta['serviceDetails'].update(runtime='docker', maxShutdownDelaySeconds=300,
                envSpecificDetails={'dockerCommand': '', 'dockerContext': '.', 'dockerfilePath': './Dockerfile'})
            self.metadata[sid] = meta

    def pages(self, path, key):
        values = super().pages(path, key)
        if key == 'deploy' and API_ID in path:
            values += [deepcopy(d) for d in self.deployments.values() if d['id'] != self.live[API_ID]['id']]
        return values

    def call(self, method, path, body=None):
        parts = path.split('/')
        sid = parts[2]
        if method == 'GET' and len(parts) == 3:
            return deepcopy(self.metadata[sid])
        if method == 'POST':
            assert sid == API_ID, 'API controller attempted to deploy a background service'
            self.calls.append((method, sid, body['commitId']))
            deploy = {'id': 'dep-api' + str(len(self.deployments)), 'commit': {'id': body['commitId']},
                      'status': 'update_failed' if self.fail_service else 'live'}
            self.deployments[deploy['id']] = deepcopy(deploy)
            if deploy['status'] == 'live':
                self.api_owners.append(str(5 + len(self.deployments)) + '@replacement')
                self.live[API_ID] = deepcopy(deploy)
                self.remaining = self.retirement_polls
                for d in self.deployments.values():
                    if d['id'] != deploy['id'] and d['status'] == 'live':
                        d['status'] = 'deactivated'
            if self.unknown_write:
                raise ReleaseError('Deployment API request failed. A write may have been accepted; inspect the receipt before retrying.')
            return deepcopy(deploy)
        return super().call(method, path, body)

    def probe(self, service):
        if len(self.api_owners) > 1:
            if self.remaining:
                self.remaining -= 1
            else:
                self.api_owners = self.api_owners[-1:]
        is_api = service.id == API_ID
        ready = not (is_api and self.unhealthy and service.sha == NEW)
        if is_api and self.unreachable and service.sha == NEW:
            return {'ok': False}
        return {'ok': self.probe_ok, 'ready': ready, 'policy': 'p' * 64, **CONTRACT,
                'owners': sorted(self.background_owners + self.api_owners),
                'coordinators': ['1@coordinator'], 'brokers': ['3@broker'],
                'api_owner': self.api_owners[-1] if is_api else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    events, started = [], time.monotonic()
    def log(message, **kwargs):
        print(message, flush=True)
        events.append([round(time.monotonic() - started, 3), 'o', message + '\r\n'])
    log('LOCAL CONTROLLER REHEARSAL | Simulated Render/SSH/CI | No production changes')
    for scenario in ('success', 'startup failure', 'unhealthy candidate', 'lost deployment response'):
        log('SCENARIO ' + scenario)
        render = APIRender()
        if scenario == 'startup failure': render.fail_service = API_ID
        if scenario == 'unhealthy candidate': render.unhealthy = True
        if scenario == 'lost deployment response': render.unknown_write = True
        elapsed = [0]
        def sleep(seconds): elapsed[0] += seconds
        release = APIRelease(render, Candidate(), render.probe, args.output / (scenario.replace(' ', '-') + '.json'),
            api_service_id=API_ID, contract=lambda env: CONTRACT, clock=lambda: elapsed[0], sleep=sleep,
            wait_seconds=1, readiness_timeout=3, retirement_timeout=3, log=log)
        try:
            release.run()
        except ReleaseError:
            assert scenario != 'success'
        assert all(call[1] == API_ID for call in render.calls)
        assert all(render.env[sid]['MOYAI_BUILD_SHA'] == OLD for sid in (COORDINATOR, BROKER, WORKER))
        expected = {'success': 'success', 'startup failure': 'rolled_back',
                    'unhealthy candidate': 'rolled_back', 'lost deployment response': 'failed'}[scenario]
        assert release.record['status'] == expected
        log('PASS ' + expected + '; coordinator, broker and worker untouched')
    log('PASS 4 controller scenarios; ambiguous writes never retried')
    header = {'version': 2, 'width': 116, 'height': 20, 'timestamp': int(time.time())}
    (args.output / 'api-controller.cast').write_text('\n'.join(json.dumps(x) for x in [header, *events]) + '\n')


if __name__ == '__main__':
    main()
