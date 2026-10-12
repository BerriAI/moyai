"""Offline rehearsal of the real maintenance controller using synthetic services.

No network clients, credentials, production sessions, or real schema are used.
The same controller used by the deployment workflow records and enforces every
transition; only Render, SSH, CI, and schema transport are simulated.
"""
from copy import deepcopy
import argparse
import json
from pathlib import Path
import time

from scripts.release.deploy import BROKER, COORDINATOR, WORKER, ReleaseError
from scripts.release.maintenance_deploy import MaintenanceRelease, COUNTS
from scripts.release.rehearse import Candidate, NEW, OLD
from scripts.release.rehearse_api import API_ID, APIRender
from scripts.release.rolling_deploy import WORKSPACE_IMAGE_PREDEPLOY

CONTRACT = {'schema': 5, 'contracts': {role: digit * 64 for role, digit in
             zip(('api', 'worker', 'broker', 'coordinator'), 'abcd')}}

OLD_CONTRACT = {**deepcopy(CONTRACT), 'schema_support': {'protocol': 1, 'required': 5, 'compatibility': 5},
                'source_policy': 'f' * 64, 'default_harness': 'codex', 'source_schema_digest': 'a' * 64}
CANDIDATE = {**deepcopy(OLD_CONTRACT), 'contracts': {role: str(index) * 64
    for index, role in enumerate(CONTRACT['contracts'], 1)}, 'source_policy': 'e' * 64,
    'default_harness': 'deepagents-temporal'}


class MaintenanceRender(APIRender):
    def __init__(self, *, old_contract=None, candidate_contract=None):
        super().__init__()
        self.old_contract = OLD_CONTRACT if old_contract is None else old_contract
        self.candidate_contract = CANDIDATE if candidate_contract is None else candidate_contract
        for env in self.env.values():
            env['MOYAI_COORDINATOR_HANDOFF'] = 'true'
        for sid in (WORKER, COORDINATOR):
            self.metadata[sid]['serviceDetails']['envSpecificDetails']['preDeployCommand'] = WORKSPACE_IMAGE_PREDEPLOY
        self.owners = {sid: [str(index + 1) + '@original'] for index, sid in enumerate(self.env)}
        self.installed = deepcopy(self.env)
        self.retirement_polls = 2
        self.retiring = None
        self.remaining = 0
        self.counts = dict.fromkeys(COUNTS, 0)
        self.policy = self.old_contract['source_policy']
        self.contracts = deepcopy(self.old_contract['contracts'])
        self.schema = self.old_contract['schema']
        self.history = []
        self.block_retirement = False
        self.before_write = lambda *args: None
        self.after_write = lambda *args: None
        self.unknown_put = False

    def pages(self, path, key):
        sid = path.split('/')[2]
        if key == 'envVar':
            return [{'key': k, 'value': v} for k, v in self.env[sid].items()]
        return [deepcopy(self.live[sid]), *[deepcopy(deploy) for deploy in self.deployments.values()
            if deploy.get('service') == sid and deploy['id'] != self.live[sid]['id']]]

    def call(self, method, path, body=None):
        sid = path.split('/')[2]
        if method in {'POST', 'PUT'}:
            self.before_write(method, sid, body)
        if method == 'PUT':
            if path.endswith('/env-vars/AGENT_HARNESS'):
                assert body == {'value': 'deepagents-temporal'}
                assert 'AGENT_HARNESS' in self.env[sid]
                assert self.env[sid]['RENDER_MIGRATION_STAGE'] == 'true'
                assert self.env[sid]['MOYAI_BUILD_SHA'] == NEW
                self.calls.append((method, sid, 'AGENT_HARNESS', body['value']))
                self.env[sid]['AGENT_HARNESS'] = body['value']
                value = {'key': 'AGENT_HARNESS', 'value': body['value']}
            else:
                value = super().call(method, path, body)
            self.after_write(method, sid, body)
            if self.unknown_put:
                raise ReleaseError('Synthetic lost environment write response')
            return value
        if method != 'POST':
            return super().call(method, path, body)
        self.calls.append((method, sid, body['commitId']))
        self.history.append({'service': sid, 'env': deepcopy(self.env[sid]),
                             'owners': deepcopy(self.owners), 'counts': deepcopy(self.counts)})
        deployment = {'id': 'dep-maintenance' + str(len(self.deployments)), 'service': sid,
            'commit': {'id': body['commitId']}, 'status': 'update_failed' if self.fail_service == sid else 'live'}
        self.deployments[deployment['id']] = deepcopy(deployment)
        if deployment['status'] == 'live':
            self.live[sid] = deepcopy(deployment)
            self.installed[sid] = deepcopy(self.env[sid])
            self.retiring = (sid, self.owners[sid][:])
            self.remaining = self.retirement_polls
            if self.env[sid]['RENDER_MIGRATION_STAGE'] == 'false':
                self.owners[sid].append(str(100 + len(self.deployments)) + '@new')
                if sid == COORDINATOR and body['commitId'] == NEW:
                    assert not sum(self.history[-1]['owners'].values(), []), 'candidate activates only at zero owners'
                    if self.contracts['coordinator'] != self.candidate_contract['contracts']['coordinator']:
                        self.policy = self.candidate_contract['source_policy']
                    self.contracts = deepcopy(self.candidate_contract['contracts'])
            for key, previous in self.deployments.items():
                if key != deployment['id'] and previous.get('service') == sid and previous['status'] == 'live':
                    previous['status'] = 'deactivated'
        self.after_write(method, sid, body)
        if self.unknown_write:
            raise ReleaseError('Synthetic lost write response')
        return deepcopy(deployment)

    def probe(self, service):
        if self.retiring:
            sid, old = self.retiring
            if self.remaining:
                self.remaining -= 1
            elif not self.block_retirement:
                self.owners[sid] = [owner for owner in self.owners[sid] if owner not in old]
                self.retiring = None
        source = self.candidate_contract if self.installed[service.id]['MOYAI_BUILD_SHA'] == NEW else self.old_contract
        staged = self.installed[service.id]['RENDER_MIGRATION_STAGE'] == 'true'
        owner = None if staged else self.owners[service.id][-1]
        return {'ok': True, 'ready': True, 'identity': owner, 'staged': staged,
            'drain': self.installed[service.id]['MAINTENANCE_DRAIN'] == 'true',
            'source_schema': source['schema'], 'source_policy': source['source_policy'],
            'source_schema_digest': source['source_schema_digest'],
            'source_contracts': deepcopy(source['contracts']),
            'default_harness': self.installed[service.id].get('AGENT_HARNESS', source['default_harness']),
            'schema_support': deepcopy(source['schema_support']),
            'schema_compatible': source['schema_support']['compatibility'] <= self.schema,
            'policy': self.policy, 'contracts': deepcopy(self.contracts), 'schema': self.schema,
            'owners': sorted(sum(self.owners.values(), [])),
            'coordinators': self.owners[COORDINATOR][:1], 'brokers': self.owners[BROKER][:],
            'broker_leaders': self.owners[BROKER][:1], 'broker_draining': False,
            'coordinator_state': 'leader' if owner and self.owners[COORDINATOR] == [owner] else 'standby',
            **self.counts}



def rehearse(output, *, pace=0, log=print):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    evidence = []
    def say(message, **kwargs):
        log(message, **kwargs)
        if pace:
            time.sleep(pace)
    say('OFFLINE MAINTENANCE REHEARSAL | Real controller; simulated Render, SSH, CI and migration')
    for scenario in ('success', 'late old owner', 'lost migration response'):
        old = {**deepcopy(OLD_CONTRACT), 'schema': 18,
               'schema_support': {'protocol': 1, 'required': 18, 'compatibility': 18}}
        candidate = {**deepcopy(CANDIDATE), 'schema': 19,
                     'schema_support': {'protocol': 1, 'required': 19, 'compatibility': 19},
                     'source_schema_digest': 'b' * 64}
        render, github, elapsed = MaintenanceRender(old_contract=old, candidate_contract=candidate), Candidate(), [0]
        github.rolling_ready = github.ready
        for sid in render.env:
            render.env[sid]['AGENT_HARNESS'] = 'codex'
            render.installed[sid]['AGENT_HARNESS'] = 'codex'
        migrations = []
        def migration(service, expected):
            assert service.env['RENDER_MIGRATION_STAGE'] == 'true'
            assert len(render.history) == 5 and not sum(render.owners.values(), [])
            assert expected['schema'] == 18 and expected['source_schema'] == 19
            migrations.append(deepcopy(expected))
            render.schema = 19
            say('SCHEMA FIXTURE | 18 -> 19 at zero owners; policy and contracts preserved')
            if scenario == 'lost migration response':
                raise ReleaseError('Synthetic lost migration response; applied outcome requires reconciliation.')
            return {'status': 'applied', 'from_revision': 18, 'to_revision': 19, 'source_digest': 'b' * 64}
        def sleep(seconds):
            elapsed[0] += seconds
        def after(method, sid, body):
            if method == 'POST' and scenario == 'late old owner' and len(render.history) == 5:
                render.block_retirement = True
        render.after_write = after
        release = MaintenanceRelease(render, github, render.probe,
            output / (scenario.replace(' ', '-') + '.json'), api_service_id=API_ID,
            contract=lambda env: deepcopy(candidate), offline_migrate=migration,
            wait_seconds=1, readiness_timeout=2, retirement_timeout=2, deploy_timeout=2,
            drain_timeout=2, clock=lambda: elapsed[0], sleep=sleep, log=say)
        say('SCENARIO | ' + scenario)
        try:
            release.run()
        except ReleaseError as error:
            assert scenario != 'success'
            say('EXPECTED SAFE STOP | ' + str(error))
        harness_receipts = [entry for entry in release.record['changes'] if entry['key'] == 'AGENT_HARNESS']
        assert len(harness_receipts) == 4 and all(entry['confirmed'] and entry['previous'] == 'codex'
            and entry['value'] == 'deepagents-temporal' for entry in harness_receipts)
        assert render.history[0]['env']['AGENT_HARNESS'] == 'codex'
        assert all(entry['env']['AGENT_HARNESS'] == 'deepagents-temporal' for entry in render.history[1:])
        say('DEFAULT MIGRATION | Old worker drained with Codex; four recorded overrides changed only for candidate staging')
        if scenario == 'success':
            assert release.record['status'] == 'success' and len(render.history) == 10
            assert len(migrations) == 1 and all(len(owners) == 1 for owners in render.owners.values())
            assert all(env['MOYAI_BUILD_SHA'] == NEW and env['RENDER_MIGRATION_STAGE'] == 'false'
                       and env['AGENT_HARNESS'] == 'deepagents-temporal' for env in render.env.values())
            assert render.env[WORKER]['MAINTENANCE_DRAIN'] == 'false'
            say('VERIFIED | Four matching roles default to Deep Agents; six old identities retired; execution resumed')
        else:
            assert release.record['status'] == 'failed' and len(render.history) == 5
            assert all(env['RENDER_MIGRATION_STAGE'] == 'true' for env in render.env.values())
            assert len(migrations) == (1 if scenario == 'lost migration response' else 0)
            say('VERIFIED | No activation, retry, rollback, or resumed execution')
        evidence.append({'scenario': scenario, 'status': release.record['status'],
                         'deployments': len(render.history), 'migration_attempts': len(migrations),
                         'confirmed_default_changes': len(harness_receipts),
                         'old_worker_drain_default': render.history[0]['env']['AGENT_HARNESS'],
                         'candidate_default': 'deepagents-temporal'})
    (output / 'results.json').write_text(json.dumps({'synthetic': True, 'scenarios': evidence}, indent=2) + '\n')
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('maintenance-rehearsal'))
    parser.add_argument('--pace', type=float, default=0)
    args = parser.parse_args()
    if not 0 <= args.pace <= 2:
        parser.error('--pace must be between 0 and 2 seconds')
    rehearse(args.output, pace=args.pace)


if __name__ == '__main__':
    main()
