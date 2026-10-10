"""Deploy only a compatible, dedicated, diskless Render API service.

Render performs readiness-gated traffic switching and bounded SIGTERM draining.
This controller never changes coordinator/broker/worker settings or deployments.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from scripts.release.deploy import (ACTIVE_DEPLOYS, BROKER, COORDINATOR, WORKER,
                                    Release, ReleaseError, SHARED_DEFAULTS, SHARED_KEYS)


class FailedDeployment(ReleaseError):
    pass


class UnhealthyAPI(ReleaseError):
    pass


def candidate_contract(environment):
    # The runner's environment (including deployment tokens) must not affect
    # Settings or leak into the child. Never return validation exception text.
    result = subprocess.run([sys.executable, '-m', 'scripts.release.api_contract'],
        cwd=Path(__file__).resolve().parents[2],
        env={**environment, 'PATH': os.defpath}, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ReleaseError('Candidate API configuration is invalid; no changes made.')
    try:
        value = json.loads(result.stdout)
        if (not re.fullmatch('[0-9a-f]{64}', value['contract'])
                or type(value['schema']) is not int):
            raise ValueError()
        return value
    except (ValueError, KeyError, TypeError):
        raise ReleaseError('Candidate API contract could not be verified; no changes made.') from None


def topology(service):
    details = service['serviceDetails']
    return {**{key: service.get(key) for key in ('name', 'type', 'suspended', 'autoDeploy',
                'autoDeployTrigger', 'branch', 'repo', 'ownerId', 'rootDir')},
            'details': {key: details.get(key) for key in ('region', 'numInstances', 'autoscaling',
                'disk', 'runtime', 'sshAddress', 'envSpecificDetails', 'maxShutdownDelaySeconds')}}


class APIRelease(Release):
    def __init__(self, *args, api_service_id, contract=candidate_contract, readiness_timeout=300,
                 retirement_timeout=430, **kwargs):
        super().__init__(*args, **kwargs)
        self.api_service_id, self.contract = api_service_id, contract
        self.readiness_timeout, self.retirement_timeout = readiness_timeout, retirement_timeout
        self.topologies = {}
        self.background = set()
        self.baseline = None

    def inspect(self, service_id, role):
        item, owner = super().inspect(service_id, role)
        metadata = self.render.call('GET', '/services/' + service_id)
        details = metadata['serviceDetails']
        if item.env.get('MOYAI_SCHEMA_MODE') != 'verify' or item.env.get('MOYAI_SEPARATE_BROKER') != 'true':
            raise ReleaseError('API releases require an activated verified-schema, separate-broker cluster.')
        if role == 'api':
            docker = details.get('envSpecificDetails', {})
            if (details.get('disk') or details.get('runtime') != 'docker'
                    or details.get('maxShutdownDelaySeconds') != 300
                    or docker.get('preDeployCommand') or docker.get('dockerCommand')
                    or docker.get('dockerfilePath') not in {'Dockerfile', './Dockerfile'}
                    or docker.get('dockerContext') != '.' or metadata.get('rootDir', '') not in {'', '.'}):
                raise ReleaseError('API requires diskless Docker startup, no command overrides, and a 300-second shutdown allowance.')
        self.topologies[service_id] = topology(metadata)
        return item, owner

    def unchanged(self):
        super().unchanged()
        for sid, original in self.topologies.items():
            if topology(self.render.call('GET', '/services/' + sid)) != original:
                raise ReleaseError('Render service configuration changed outside this release; stopped without overwriting it.')
        if self.background:
            self.check_background(self.probe(self.expected[COORDINATOR]), require_ready=True)

    def configure(self, service, changes):
        if service.id != self.api_service_id or set(changes) != {'MOYAI_BUILD_SHA'}:
            raise ReleaseError('API releases may only change the API build identity.')
        super().configure(service, changes)

    def check_background(self, state, *, require_ready=False):
        if (not state.get('ok') or state.get('policy') != self.baseline['policy']
                or state.get('contract') != self.baseline['contract']
                or state.get('schema') != self.baseline['schema']
                or state.get('coordinators') != self.baseline['coordinators']
                or state.get('brokers') != self.baseline['brokers']
                or not self.background <= set(state.get('owners', []))
                or not 4 <= len(state['owners']) <= 5):
            raise ReleaseError('Background ownership or the API contract changed; inspect the receipt before proceeding.')
        # A known dependency failure must not trigger an API rollback either.
        if require_ready and state.get('ready') is not True:
            raise ReleaseError('A background service is unhealthy; release stopped.')

    def api_state(self, service, retired=()):
        state = self.probe(service)
        if not state.get('ok'):
            return None  # SSH may still connect to the old container.
        self.check_background(state)
        owner = state.get('api_owner')
        if not owner or owner in self.background or owner in retired:
            return None
        if not set(state['owners']) <= self.background | set(retired) | {owner}:
            raise ReleaseError('Unexpected API ownership appeared; release stopped.')
        return state

    def deploy_one(self, service, sha):
        self.unchanged()
        entry = {'service': service.id, 'sha': sha, 'id': None, 'status': 'requested'}
        self.record['deploys'].append(entry)
        self.save()  # Record intent before a possibly accepted/lost write.
        started = self.render.call('POST', f'/services/{service.id}/deploys',
                                   {'commitId': sha, 'clearCache': 'do_not_clear'})
        identity = started['id']
        if not re.fullmatch('dep-[a-z0-9]+', identity):
            raise ReleaseError('Render returned an invalid deployment identity.')
        entry['id'] = identity
        self.save()

        def finished():
            state = self.render.call('GET', f'/services/{service.id}/deploys/{identity}')
            if state.get('commit', {}).get('id') != sha:
                raise ReleaseError('Render selected a different commit; release stopped.')
            entry['status'] = state['status']
            self.save()
            self.check_background(self.probe(self.expected[COORDINATOR]), require_ready=True)
            if state['status'] in {'build_failed', 'pre_deploy_failed', 'update_failed'}:
                raise FailedDeployment('The API candidate failed before a verified release.')
            if state['status'] not in ACTIVE_DEPLOYS | {'live'}:
                raise ReleaseError('Render deployment was cancelled or has an unknown state; inspect it before retrying.')
            return state['status'] == 'live'
        self.wait(finished, 'Render deployment timed out; no speculative retry or rollback was attempted.', self.deploy_timeout)
        service.sha, service.deploy_id = sha, identity
        self.unchanged()

    def verify_api(self, service, retired):
        last = None
        deadline = self.clock() + self.readiness_timeout
        while True:
            last = self.api_state(service, retired)
            if last and last['ready'] is True:
                break
            if self.clock() >= deadline:
                if last and last['ready'] is False:
                    self.record['unhealthy_owner'] = last['api_owner']
                    self.save()
                    raise UnhealthyAPI('Candidate API explicitly reported unhealthy after the readiness deadline.')
                raise ReleaseError('Candidate readiness could not be established; no speculative rollback was attempted.')
            self.sleep(self.wait_seconds)
        identity = last['api_owner']
        self.phase('Wait for the old API owner to exit after native Render draining')

        def retired_cleanly():
            state = self.api_state(service, retired)
            if not state or state['ready'] is not True or state['api_owner'] != identity:
                return False
            return set(state['owners']) == self.background | {identity}
        self.wait(retired_cleanly, 'Old API ownership did not drain or new API health was lost; inspect the receipt.',
                  self.retirement_timeout)
        self.unchanged()
        self.record['api_owner'] = identity
        self.save()

    def restore_failed(self, service, previous_sha, old_owner):
        # A terminal startup failure normally leaves the previous deployment live.
        # Require the observed old instance to still be healthy and alone first.
        self.unchanged()
        state = self.api_state(service)
        if (not state or state['ready'] is not True or state['api_owner'] != old_owner
                or set(state['owners']) != self.background | {old_owner}):
            raise ReleaseError('Failed candidate left an unverified baseline; inspect Render before retrying.')
        self.configure(service, {'MOYAI_BUILD_SHA': previous_sha})
        self.unchanged()
        self.record['status'] = 'rolled_back'
        self.phase('Candidate failed; previous healthy API retained and saved build restored')
        raise ReleaseError('API release failed; the previous healthy deployment was retained.')

    def rollback_unhealthy(self, service, previous_sha, old_owner):
        unhealthy = self.record['unhealthy_owner']
        self.phase('Verify unchanged background owners before one rollback deployment')
        # Do not introduce a third overlapping API while the previous one drains.
        def alone():
            state = self.api_state(service, {old_owner})
            return (state and state['ready'] is False and state['api_owner'] == unhealthy
                    and set(state['owners']) == self.background | {unhealthy})
        self.wait(alone, 'Unhealthy candidate could not be isolated safely; automatic rollback stopped.', self.retirement_timeout)
        self.unchanged()
        self.configure(service, {'MOYAI_BUILD_SHA': previous_sha})
        self.deploy_one(service, previous_sha)
        self.verify_api(service, {unhealthy})
        self.record['status'] = 'rolled_back'
        self.phase('Previous API commit redeployed and verified; candidate release failed')
        raise ReleaseError('API release failed; the previous API commit was restored and verified.')

    def run(self):
        try:
            if (not re.fullmatch('srv-[a-z0-9]+', self.api_service_id)
                    or self.api_service_id in {COORDINATOR, WORKER, BROKER}):
                raise ReleaseError('Set MOYAI_API_SERVICE_ID to the activated dedicated API service; no changes made.')
            sha = self.github.candidate()
            self.record.update(commit=sha, topology='dedicated_api', release_type='api')
            self.phase('Check selected main commit and CI for the compatible API release')
            self.wait(lambda: self.github.ready(sha), 'CI did not finish; no production changes made.', 1800)
            coordinator, owner = self.inspect(COORDINATOR, 'coordinator')
            for sid, role in ((BROKER, 'broker'), (WORKER, 'worker')):
                item, service_owner = self.inspect(sid, role)
                self.check_shared(coordinator, owner, item, service_owner, True)
            api, api_owner = self.inspect(self.api_service_id, 'api')
            if (api_owner != owner or any(api.env.get(k, SHARED_DEFAULTS.get(k, '')) !=
                    coordinator.env.get(k, SHARED_DEFAULTS.get(k, '')) for k in SHARED_KEYS)):
                raise ReleaseError('API and background services do not share the required configuration.')
            self.github.forward_from(api.sha, sha)
            states = [self.probe(service) for service in self.expected.values()]
            if any(not state.get('ok') or state.get('ready') is not True for state in states):
                raise ReleaseError('Preflight health or release-support check failed; no changes made.')
            self.baseline = states[-1]
            old_owner = self.baseline['api_owner']
            if (not old_owner or len(set(self.baseline['owners'])) != 4
                    or len(self.baseline['coordinators']) != 1 or len(self.baseline['brokers']) != 1
                    or self.baseline['coordinators'] == self.baseline['brokers']
                    or old_owner in self.baseline['coordinators'] + self.baseline['brokers']):
                raise ReleaseError('Preflight requires one API, coordinator, broker and worker owner.')
            self.background = set(self.baseline['owners']) - {old_owner}
            if not set(self.baseline['coordinators'] + self.baseline['brokers']) <= self.background:
                raise ReleaseError('Singleton owners are missing runtime ownership.')
            for state in states:
                self.check_background(state)
                if set(state['owners']) != set(self.baseline['owners']):
                    raise ReleaseError('Runtime ownership changed during preflight; no changes made.')
            candidate = self.contract({**api.env, 'MOYAI_BUILD_SHA': sha})
            if candidate != {k: self.baseline[k] for k in ('contract', 'schema')}:
                raise ReleaseError('Candidate API contract is incompatible with the running cluster; use a maintenance release.')
            self.record.update(previous_commit=api.sha, previous_deploy=api.deploy_id,
                               background_owners=sorted(self.background), previous_api_owner=old_owner,
                               cluster_policy=self.baseline['policy'], api_contract=self.baseline['contract'],
                               schema_revision=self.baseline['schema'])
            self.unchanged()
            if self.preflight_only:
                self.record['status'] = 'preflight_passed'
                self.phase('Compatible API preflight passed; no changes made')
                return
            if sha != api.sha:
                previous_sha = api.sha
                self.record['status'] = 'running'
                self.phase('Deploy only the API; Render warms and switches the replacement')
                self.configure(api, {'MOYAI_BUILD_SHA': sha})
                try:
                    self.deploy_one(api, sha)
                except FailedDeployment:
                    self.restore_failed(api, previous_sha, old_owner)
                try:
                    self.verify_api(api, {old_owner})
                except UnhealthyAPI:
                    self.rollback_unhealthy(api, previous_sha, old_owner)
            self.record['status'] = 'success'
            self.phase('Compatible API release verified; background owners unchanged')
        except BaseException as exc:
            if self.record['status'] != 'rolled_back':
                self.record['status'] = 'failed'
            self.record['reason'] = str(exc) if isinstance(exc, ReleaseError) else 'Release interrupted or unexpected response; inspect the receipt.'
            self.save()
            raise
