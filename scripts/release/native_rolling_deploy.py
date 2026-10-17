"""Roll compatible native roles without changing schemas or pausing the cluster."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess

from scripts.release.deploy import COORDINATOR, WORKER, ReleaseError
from scripts.release.native_migration import candidate_manifest, native_python
from scripts.release.rolling_deploy import RollingRelease, candidate_contract


WORKER_SHUTDOWN_CONTRACT = {'seconds': 240, 'protocol': 'moyai-worker-shared-drain-v1'}


def valid_worker_shutdown(value):
    return (isinstance(value, dict) and type(value.get('seconds')) is int
            and value == WORKER_SHUTDOWN_CONTRACT)


def candidate_worker_shutdown(environment):
    root = Path(__file__).resolve().parents[2]
    source = '''
import json
from render_start import configure_environment
from scripts.release.native_rolling_probe import worker_shutdown_contract
configure_environment()
print(json.dumps(worker_shutdown_contract()))
'''
    try:
        result = subprocess.run([native_python(root), '-c', source], cwd=root,
            env={**environment, 'PATH': os.defpath, 'PYTHONPATH': str(root)},
            capture_output=True, text=True, timeout=30)
        value = json.loads(result.stdout)
        if (result.returncode or not isinstance(value, dict)
                or set(value) != {'seconds', 'protocol'} or type(value['seconds']) is not int
                or not isinstance(value['protocol'], str)):
            raise ValueError()
        return value
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise ReleaseError('Candidate native worker shutdown settings could not be verified; no changes made.') from None


class NativeRollingRelease(RollingRelease):
    native_release = True

    def __init__(self, *args, contract=candidate_contract, manifest=candidate_manifest,
                 worker_shutdown=candidate_worker_shutdown, **kwargs):
        self.source_contract, self.manifest = contract, manifest
        self.worker_shutdown = worker_shutdown
        super().__init__(*args, contract=self.compatible_contract, **kwargs)

    def inspect(self, service_id, role):
        service, owner = super().inspect(service_id, role)
        if (service.env.get('MOYAI_CORE') != 'pydantic'
                or service.env.get('MOYAI_PRODUCT_MODE') != 'existing'
                or service.env.get('MOYAI_NATIVE_SCHEMA_MODE', 'verify') != 'verify'
                or service.env.get('MOYAI_PRODUCT_LOCAL_REHEARSAL', 'false') != 'false'):
            raise ReleaseError('Rolling deployment requires an already-running native cluster with verified schemas.')
        if service_id == COORDINATOR:
            self.wait(lambda: self.github.native_ready(self.record['commit']),
                      'Native runtime CI did not finish for the selected commit; no changes made.', 1800)
        return service, owner

    def compatible_contract(self, environment):
        candidate = self.source_contract(environment)
        native = self.manifest(environment)
        expected = {key: self.baseline['native'][key] for key in ('schema', 'app', 'harness')}
        if native != expected:
            raise ReleaseError('Native application or Harness receipts differ; use a maintenance release.')
        if (environment.get('MOYAI_RUNTIME_ROLE') == 'worker'
                and not valid_worker_shutdown(self.worker_shutdown(environment))):
            raise ReleaseError('Candidate native worker requires the qualified shared 240-second shutdown contract; use maintenance.')
        return candidate

    def plan_schema(self, states):
        # Native online migrations and mixed protocol/framework revisions have
        # not been qualified. No migrator or drain transport is used here.
        if self.candidate['schema'] != self.baseline['schema']:
            raise ReleaseError('Native rolling releases cannot change the canonical schema; use maintenance.')
        return None

    def check_state(self, state, *, settled=False, schema=None):
        super().check_state(state, settled=settled, schema=schema)
        if (state.get('core') != 'pydantic-v1'
                or not isinstance(state.get('native'), dict)
                or state['native'].get('state') != 'receipted'
                or not isinstance(state.get('native_drain'), dict)
                or state['native_drain'].get('paused') is not False
                or type(state['native_drain'].get('generation')) is not int
                or state['native_drain']['generation'] < 1
                or any(state.get(key) != self.baseline.get(key) for key in ('native', 'native_drain'))
                or state.get('role') not in ('api', 'worker', 'broker', 'coordinator')):
            raise ReleaseError('Native schemas, core or maintenance generation changed; rolling release stopped.')
        registered = state.get('native_workers')
        if (not isinstance(registered, list) or not all(isinstance(owner, str) for owner in registered)
                or len(registered) != len(set(registered))):
            raise ReleaseError('Native worker membership is incomplete or ambiguous.')
        registered = set(registered)
        expected = {self.identities[WORKER]}
        if self.selected == WORKER:
            # A candidate can acquire canonical ownership before it registers
            # and begins polling. Only ready workers must already be registered.
            fixed = {identity for sid, identity in self.identities.items() if sid != WORKER}
            allowed = set(state['owners']) - fixed
            if not registered or not registered <= allowed or len(registered) > 2:
                raise ReleaseError('Native worker overlap has unexpected or missing membership.')
            if settled and registered != {self.replacement}:
                raise ReleaseError('Old native worker membership did not retire.')
        elif registered != expected:
            raise ReleaseError('An unaffected native worker changed or lost registration.')
        if state['role'] == 'worker' and state.get('ready') is True and state.get('identity') not in registered:
            raise ReleaseError('A ready native worker has not registered its exact process identity.')
        if state['role'] == 'worker':
            if not valid_worker_shutdown(state.get('worker_shutdown')):
                raise ReleaseError('Running native workers require the qualified shared 240-second shutdown contract; adopt it with maintenance first.')
            self.record.setdefault('worker_shutdown', deepcopy(WORKER_SHUTDOWN_CONTRACT))
        self.record.setdefault('core', 'pydantic-v1')
        self.record.setdefault('native', deepcopy(self.baseline['native']))
        self.record.setdefault('native_drain', deepcopy(self.baseline['native_drain']))

