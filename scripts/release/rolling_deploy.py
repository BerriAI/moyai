"""Replace compatible runtime roles sequentially while admitted work continues.

Only build labels and exact-commit deployments change. Schema, capacities,
routes and shared policy are immutable. Ambiguous writes never trigger retries
or rollback. This controller requires the separately activated rolling topology.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from scripts.release.api_deploy import topology
from scripts.release.deploy import (ACTIVE_DEPLOYS, BROKER, COORDINATOR, WORKER,
                                    Release, ReleaseError, SHARED_DEFAULTS, SHARED_KEYS)

ROLES = ('api', 'worker', 'broker', 'coordinator')
WORKSPACE_IMAGE_PREDEPLOY = '/app/.venv/bin/python /app/build_workspace_image.py'


def candidate_contract(environment):
    result = subprocess.run([sys.executable, '-m', 'scripts.release.rolling_contract'],
        cwd=Path(__file__).resolve().parents[2], env={**environment, 'PATH': os.defpath},
        capture_output=True, text=True, timeout=30)
    try:
        value = json.loads(result.stdout)
        if (result.returncode or type(value['schema']) is not int
                or set(value['contracts']) != set(ROLES)
                or any(not re.fullmatch('[0-9a-f]{64}', digest) for digest in value['contracts'].values())):
            raise ValueError()
        return value
    except (ValueError, KeyError, TypeError):
        raise ReleaseError('Candidate rolling contracts could not be verified; no changes made.') from None


class RollingRelease(Release):
    def __init__(self, *args, api_service_id, contract=candidate_contract,
                 readiness_timeout=300, retirement_timeout=430, **kwargs):
        super().__init__(*args, **kwargs)
        self.api_service_id, self.contract = api_service_id, contract
        self.readiness_timeout, self.retirement_timeout = readiness_timeout, retirement_timeout
        self.topologies, self.identities = {}, {}
        self.baseline = None
        self.selected = None
        self.active_deploy = None
        self.replacement = None

    def inspect(self, service_id, role):
        item, owner = super().inspect(service_id, role)
        metadata = self.render.call('GET', '/services/' + service_id)
        details = metadata['serviceDetails']
        docker = details.get('envSpecificDetails', {})
        # Image preparation does not start runtime owners or migrate the database.
        predeploy_commands = ((None, '', WORKSPACE_IMAGE_PREDEPLOY)
                              if role in {'worker', 'coordinator'} else (None, ''))
        if (item.env.get('MOYAI_SCHEMA_MODE') != 'verify'
                or item.env.get('MOYAI_SEPARATE_BROKER') != 'true'
                or item.env.get('MOYAI_COORDINATOR_HANDOFF') != 'true'):
            raise ReleaseError('Rolling releases require the activated four-service handoff topology and verified schema.')
        if (details.get('disk') or details.get('runtime') != 'docker'
                or details.get('maxShutdownDelaySeconds') != 300
                or docker.get('preDeployCommand') not in predeploy_commands or docker.get('dockerCommand')
                or docker.get('dockerfilePath') not in {'Dockerfile', './Dockerfile'}
                or docker.get('dockerContext') != '.' or metadata.get('rootDir', '') not in {'', '.'}):
            raise ReleaseError('Rolling services require diskless Docker startup, no start override, only the approved worker/coordinator image-build hook, and a 300-second shutdown allowance.')
        self.topologies[service_id] = topology(metadata)
        return item, owner

    def check_state(self, state, *, settled=False):
        if (not state.get('ok') or any(state.get(key) != self.baseline[key]
                for key in ('policy', 'contracts', 'schema'))):
            raise ReleaseError('Cluster policy, schema or compatibility contracts changed; release stopped.')
        keys = ('owners', 'coordinators', 'brokers', 'broker_leaders')
        if any(not isinstance(state.get(key), list) or len(state[key]) != len(set(state[key])) for key in keys):
            raise ReleaseError('Incomplete or duplicate runtime ownership inventory.')
        owners = set(state['owners'])
        fixed = {identity for sid, identity in self.identities.items() if sid != self.selected}
        allowed = set(self.identities.values()) | ({self.replacement} if self.replacement else set())
        if (not fixed <= owners or not 4 <= len(owners) <= (5 if self.selected else 4)
                or (not self.selected and owners != allowed)
                or (self.replacement and not owners <= allowed)
                or (settled and owners != allowed - {self.identities[self.selected]})):
            raise ReleaseError('Unexpected runtime owner appeared or an unaffected owner disappeared.')
        coordinators, brokers, leaders = (set(state[key]) for key in keys[1:])
        coordinator_allowed = ({self.identities[COORDINATOR]} if self.selected != COORDINATOR
                               else owners - fixed)
        broker_allowed = ({self.identities[BROKER]} if self.selected != BROKER else owners - fixed)
        if (not coordinators <= coordinator_allowed or len(coordinators) > 1
                or not brokers <= broker_allowed or not brokers
                or not leaders <= brokers or len(leaders) > 1):
            raise ReleaseError('Coordinator or broker ownership moved outside the selected role.')
        if (self.selected != COORDINATOR or settled) and len(coordinators) != 1:
            raise ReleaseError('A healthy coordinator leader is required.')
        if (self.selected != BROKER or settled) and len(leaders) != 1:
            raise ReleaseError('A healthy broker maintenance leader is required.')

    def check_control_plane(self):
        for service in self.expected.values():
            if self.environment(service.id) != service.env:
                raise ReleaseError('Render environment changed outside this release; stopped without overwriting it.')
            if topology(self.render.call('GET', '/services/' + service.id)) != self.topologies[service.id]:
                raise ReleaseError('Render service topology changed outside this release.')
            deploys = self.deploys(service.id)
            active = {item['id'] for item in deploys if item['status'] in ACTIVE_DEPLOYS}
            permitted = {self.active_deploy} if service.id == self.selected and self.active_deploy else set()
            live = [item for item in deploys if item['status'] == 'live']
            if not active <= permitted or len(live) != 1 or live[0]['id'] not in {service.deploy_id, *permitted}:
                raise ReleaseError('An unexpected Render deployment appeared; inspect the release receipt.')

    def check_unaffected(self):
        for sid, service in self.expected.items():
            if sid == self.selected:
                continue
            state = self.probe(service)
            self.check_state(state)
            if state.get('ready') is not True or state.get('identity') != self.identities[sid]:
                raise ReleaseError('An unaffected service lost readiness or changed its process identity.')

    def unchanged(self):
        self.check_control_plane()
        if self.baseline is not None:
            self.check_unaffected()

    def configure(self, service, changes):
        if service.id != self.selected or set(changes) != {'MOYAI_BUILD_SHA'}:
            raise ReleaseError('Rolling releases may only change the selected service build identity.')
        super().configure(service, changes)

    def replace(self, service, sha):
        self.selected, self.replacement = service.id, None
        self.phase('Replace ' + service.role + ' while other roles continue serving work')
        self.configure(service, {'MOYAI_BUILD_SHA': sha})
        self.unchanged()
        entry = {'service': service.id, 'role': service.role, 'sha': sha, 'id': None, 'status': 'requested'}
        self.record['deploys'].append(entry)
        self.save()
        deployment = self.render.call('POST', f'/services/{service.id}/deploys',
                                      {'commitId': sha, 'clearCache': 'do_not_clear'})
        identity = deployment['id']
        if not re.fullmatch('dep-[a-z0-9]+', identity):
            raise ReleaseError('Render returned an invalid deployment identity.')
        self.active_deploy = entry['id'] = identity
        self.save()

        def live():
            self.unchanged()
            state = self.render.call('GET', f'/services/{service.id}/deploys/{identity}')
            if state.get('commit', {}).get('id') != sha:
                raise ReleaseError('Render selected a different commit; release stopped.')
            entry['status'] = state['status']
            self.save()
            if state['status'] not in ACTIVE_DEPLOYS | {'live'}:
                raise ReleaseError('Candidate deployment failed, was cancelled or has an unknown state; no automatic rollback was attempted.')
            return state['status'] == 'live'
        self.wait(live, 'Render deployment timed out; no speculative retry or rollback was attempted.', self.deploy_timeout)
        service.sha, service.deploy_id = sha, identity

        def ready():
            self.unchanged()
            state = self.probe(service)
            if not state.get('ok'):
                return False  # SSH may still reach the draining old container.
            self.check_state(state)
            owner = state.get('identity')
            if not owner or owner in self.identities.values():
                return False
            if self.replacement and self.replacement != owner:
                raise ReleaseError('Candidate process identity changed unexpectedly.')
            self.replacement = owner
            self.check_state(state)
            return state.get('ready') is True
        self.wait(ready, 'Candidate readiness could not be verified; no speculative rollback was attempted.', self.readiness_timeout)
        self.phase('Wait for old ' + service.role + ' ownership to retire after bounded Render draining')

        def retired():
            self.unchanged()
            state = self.probe(service)
            if not state.get('ok'):
                return False
            self.check_state(state)
            if (state.get('ready') is not True or state.get('identity') != self.replacement
                    or self.identities[service.id] in state['owners']):
                return False
            self.check_state(state, settled=True)
            if service.role == 'coordinator':
                return state.get('coordinator_state') == 'leader' and state['coordinators'] == [self.replacement]
            if service.role == 'broker':
                return (state.get('broker_draining') is False and state['brokers'] == [self.replacement]
                        and state['broker_leaders'] == [self.replacement])
            return True
        self.wait(retired, 'Old ownership did not retire or leadership did not transfer; release stopped.', self.retirement_timeout)
        self.record['replacements'].append({'service': service.id, 'role': service.role,
            'old_owner': self.identities[service.id], 'new_owner': self.replacement})
        self.identities[service.id] = self.replacement
        self.selected = self.replacement = self.active_deploy = None
        self.save()
        self.unchanged()

    def run(self):
        try:
            if (not re.fullmatch('srv-[a-z0-9]+', self.api_service_id)
                    or self.api_service_id in {COORDINATOR, WORKER, BROKER}):
                raise ReleaseError('Set MOYAI_API_SERVICE_ID to the activated dedicated API service; no changes made.')
            sha = self.github.candidate()
            self.record.update(commit=sha, topology='rolling_four_service', release_type='rolling', replacements=[])
            self.phase('Check exact selected main CI and activated rolling deployment contracts')
            self.wait(lambda: self.github.rolling_ready(sha), 'Required rolling release CI did not finish; no changes made.', 1800)
            coordinator, owner = self.inspect(COORDINATOR, 'coordinator')
            for sid, role in ((WORKER, 'worker'), (BROKER, 'broker'), (self.api_service_id, 'api')):
                item, item_owner = self.inspect(sid, role)
                if item_owner != owner or any(item.env.get(key, SHARED_DEFAULTS.get(key, '')) !=
                        coordinator.env.get(key, SHARED_DEFAULTS.get(key, '')) for key in SHARED_KEYS):
                    raise ReleaseError('Rolling services do not share the required configuration.')
            states = {sid: self.probe(service) for sid, service in self.expected.items()}
            if any(not state.get('ok') or state.get('ready') is not True or not state.get('identity')
                   for state in states.values()):
                raise ReleaseError('Preflight readiness or rolling release support is missing; no changes made.')
            self.baseline = states[COORDINATOR]
            self.identities = {sid: state['identity'] for sid, state in states.items()}
            if (len(set(self.identities.values())) != 4
                    or self.baseline.get('coordinator_state') != 'leader'
                    or states[BROKER].get('broker_draining') is not False):
                raise ReleaseError('Preflight requires four distinct owners and healthy active leadership.')
            for sid, state in states.items():
                self.check_state(state)
                service = self.expected[sid]
                self.github.forward_from(service.sha, sha)
                candidate = self.contract({**service.env, 'MOYAI_BUILD_SHA': sha})
                if candidate != {key: self.baseline[key] for key in ('schema', 'contracts')}:
                    raise ReleaseError('Candidate schema or role contracts are incompatible; use a planned maintenance transition.')
            self.record.update(previous={sid: {'role': service.role, 'sha': service.sha,
                'deploy_id': service.deploy_id, 'owner': self.identities[sid]} for sid, service in self.expected.items()},
                cluster_policy=self.baseline['policy'], contracts=self.baseline['contracts'], schema_revision=self.baseline['schema'])
            self.unchanged()
            if self.preflight_only:
                self.record['status'] = 'preflight_passed'
                self.phase('Rolling preflight passed; no changes made')
                return
            self.record['status'] = 'running'
            for sid in (WORKER, BROKER, COORDINATOR, self.api_service_id):
                service = self.expected[sid]
                if service.sha != sha:
                    self.replace(service, sha)
            self.unchanged()
            self.record['status'] = 'success'
            self.phase('All selected roles verified; shared policy preserved and prior owners retired')
        except BaseException as exc:
            self.record['status'] = 'failed'
            self.record['reason'] = str(exc) if isinstance(exc, ReleaseError) else 'Release interrupted or unexpected response; inspect the receipt.'
            self.save()
            raise
