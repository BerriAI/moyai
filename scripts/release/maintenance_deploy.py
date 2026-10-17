"""Explicit four-role maintenance transition through a verified zero-owner boundary.

No policy or compatibility row is edited here. Normal coordinator startup admits
its new contracts only after every old owner has retired. Ambiguous control-plane
writes are recorded once and never retried or automatically rolled back.
"""
from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

from scripts.release.deploy import (ACTIVE_DEPLOYS, BROKER, COORDINATOR, WORKER,
    MUTABLE, Release, ReleaseError, Service, SHARED_DEFAULTS, SHARED_KEYS)
from scripts.release.rolling_deploy import (RollingRelease, candidate_migration_plan,
    valid_contract, valid_schema_support)
from scripts.release.native_migration import candidate_manifest
from scripts.release.native_probe import (COUNTS as NATIVE_COUNTS, CORE_COUNTS as NATIVE_CORE_COUNTS,
    migration_plan as native_migration_plan)
from scripts.release.native_control import owner as native_owner, validate as validate_native_control

COUNTS = ('unsafe_sessions', 'executing_sessions', 'legacy_active_sessions',
          'model_requests', 'live_leases')
INVENTORIES = ('owners', 'coordinators', 'brokers', 'broker_leaders')
DEFAULT_HARNESS = 'deepagents-temporal'


@dataclass
class Replacement:
    target: Service
    old_owner: str | None
    receipt: dict
    requested: bool = False
    new_owner: str | None = None


def maintenance_contract(environment):
    """Inspect the candidate using each preserved environment, without clients."""
    source = '''import json
from scripts.release.rolling_contract import contract
from app.config import Settings
from moyai.product_config import select_core
from app.database import runtime_fingerprint
from app.schema_migrations import SCHEMA_REVISION
from pathlib import Path
import app.schema_migrations
value = contract()
settings = Settings(_env_file=None)
value.update(source_policy=runtime_fingerprint(settings), default_harness=settings.default_harness(),
    core=select_core(),
    source_schema_digest=json.loads(Path(app.schema_migrations.__file__).with_name('schema_versions.json').read_text())[str(SCHEMA_REVISION)])
print(json.dumps(value))
'''
    try:
        result = subprocess.run([sys.executable, '-c', source],
            cwd=Path(__file__).resolve().parents[2], env={**environment, 'PATH': os.defpath},
            capture_output=True, text=True, timeout=30)
        value = json.loads(result.stdout)
        if result.returncode or not valid_maintenance_contract(value):
            raise ValueError()
        return value
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise ReleaseError('Candidate maintenance contracts could not be verified; no changes made.') from None


def valid_maintenance_contract(value):
    return (isinstance(value, dict)
        and set(value) in (
            {'schema', 'schema_support', 'contracts', 'source_policy', 'default_harness', 'source_schema_digest'},
            {'schema', 'schema_support', 'contracts', 'source_policy', 'default_harness', 'source_schema_digest', 'core'})
        and value.get('core', 'legacy') in {'legacy', 'pydantic'}
        and valid_contract({key: value[key] for key in ('schema', 'schema_support', 'contracts')})
        and isinstance(value['source_policy'], str) and re.fullmatch('[0-9a-f]{64}', value['source_policy'])
        and isinstance(value['default_harness'], str)
        and isinstance(value['source_schema_digest'], str)
        and re.fullmatch('[0-9a-f]{64}', value['source_schema_digest']))


class MaintenanceRelease(RollingRelease):
    native_release = True

    def __init__(self, *args, contract=maintenance_contract,
                 migration_plan=candidate_migration_plan, offline_migrate=None,
                 target_core='preserve', native_manifest=candidate_manifest, native_migrate=None,
                 native_control=None, automation_probe=None, **kwargs):
        super().__init__(*args, contract=contract, migration_plan=migration_plan, **kwargs)
        self.offline_migrate = offline_migrate
        self.running = {}
        self.sources = {}
        self.harness_changes = {}
        self.drained = False
        self.candidate_policy_active = False
        self.retired = set()
        if target_core not in {'preserve', 'pydantic'}:
            raise ReleaseError('Only the Pydantic candidate core is supported; no changes made.')
        self.target_core = target_core
        self.native_manifest, self.native_migrate = native_manifest, native_migrate
        self.core_changes = {}
        self.native_required = False
        self.native_inventory = None
        self.native_plan = None
        self.native_control = native_control
        self.native_control_receipt = None
        self.source_core = 'legacy'
        self.automation_probe = automation_probe
        self.legacy_schedule_count = None
        self.legacy_automation_receipt = None
        self.batch: dict[str, Replacement] = {}

    @property
    def native_maintenance(self):
        return (self.source_core == 'pydantic' and self.candidate is not None
                and self.candidate.get('core') == 'pydantic' and not self.core_changes)

    def selected_roles(self):
        if self.batch:
            return {sid for sid, item in self.batch.items() if item.requested}
        return {self.selected} if self.selected else set()

    def replacement_owners(self):
        if self.batch:
            return {sid: item.new_owner for sid, item in self.batch.items() if item.new_owner}
        return {self.selected: self.replacement} if self.replacement else {}

    def permitted_deploys(self, service_id):
        if self.batch:
            item = self.batch.get(service_id)
            return {item.receipt['id']} if item and item.receipt['id'] else set()
        return super().permitted_deploys(service_id)

    def source(self, service):
        candidate_settings = (self.candidate is not None
            and service.env.get('MOYAI_CORE', 'legacy') == self.candidate.get('core', 'legacy')
            and service.env.get('AGENT_HARNESS', DEFAULT_HARNESS) == self.candidate['default_harness'])
        return (self.candidate if service.sha == self.record['commit'] and candidate_settings
                else self.sources[service.id])

    def check_source(self, service, state, *, require_ready=True):
        source = self.source(service)
        if (state.get('staged') is not (service.env['RENDER_MIGRATION_STAGE'] == 'true')
                or state.get('drain') is not (service.env['MAINTENANCE_DRAIN'] == 'true')
                or state.get('source_schema') != source['schema']
                or state.get('source_schema_digest') != source['source_schema_digest']
                or state.get('source_contracts') != source['contracts']
                or state.get('source_policy') != source['source_policy']
                or state.get('core', 'legacy') != source.get('core', 'legacy')
                or state.get('default_harness') != source['default_harness']
                or state.get('schema_support') != source.get('schema_support')):
            raise ReleaseError('Running source, maintenance flags or effective harness changed; release stopped.')
        if type(state.get('ready')) is not bool or (require_ready and not state['ready']):
            raise ReleaseError('A verified maintenance service lost readiness.')
        if type(state.get('schema_compatible')) is not bool or (not state['staged'] and not state['schema_compatible']):
            raise ReleaseError('An active service lacks verified schema compatibility.')
        if state['staged']:
            if state.get('identity') is not None:
                raise ReleaseError('A staged service unexpectedly owns runtime work.')
        elif not isinstance(state.get('identity'), str) or not state['identity']:
            raise ReleaseError('An active service did not report its exact runtime identity.')

    def check_state(self, state, *, settled=False, schema=None):
        if not isinstance(state, dict) or state.get('ok') is not True:
            raise ReleaseError('Runtime probe did not return verified state; release stopped.')
        if any(not isinstance(state.get(key), list)
               or any(not isinstance(v, str) or not v for v in state[key])
               or len(state[key]) != len(set(state[key])) for key in INVENTORIES):
            raise ReleaseError('Incomplete or duplicate runtime ownership inventory.')
        count_keys = self.drain_keys()
        if any(type(state.get(key)) is not int or state[key] < 0 for key in count_keys):
            raise ReleaseError('Incomplete maintenance drain inventory.')
        if self.native_required and state.get('native') != self.native_inventory:
            raise ReleaseError('Native migration receipts changed outside the verified maintenance boundary.')
        if self.native_required and state.get('native_drain') != self.native_control_receipt:
            raise ReleaseError('Native maintenance generation changed outside this release.')
        if (self.core_changes and self.legacy_schedule_count is not None and not self.candidate_policy_active
                and state.get('legacy_schedule_count') != self.legacy_schedule_count):
            raise ReleaseError('Source schedule inventory changed during core cutover.')
        changes_contract = self.candidate['contracts']['coordinator'] != self.baseline['contracts']['coordinator']
        expected_policy = (self.candidate['source_policy'] if self.candidate_policy_active and changes_contract
                           else self.baseline['policy'])
        expected_contracts = self.candidate['contracts'] if self.candidate_policy_active else self.baseline['contracts']
        if (state.get('schema') != (self.baseline['schema'] if schema is None else schema)
                or state.get('policy') != expected_policy or state.get('contracts') != expected_contracts):
            raise ReleaseError('Cluster policy, schema or contracts changed outside the verified maintenance boundary.')
        if self.drained and any(state[key] for key in count_keys):
            raise ReleaseError('Execution or leases reappeared after the maintenance drain.')
        owners = set(state['owners'])
        selected = self.selected_roles()
        replacements = self.replacement_owners()
        fixed = {identity for sid, identity in self.identities.items() if sid not in selected}
        old = {identity for sid, identity in self.identities.items() if sid in selected}
        extra = set(replacements.values())
        allowed = fixed | old | extra
        if (not fixed <= owners or not owners <= allowed
                or (not selected and owners != allowed)
                or (settled and owners != fixed | extra)):
            self.record['ownership_failure'] = {
                'observed_count': len(owners), 'allowed_count': len(allowed),
                'unexpected_count': len(owners - allowed), 'missing_required_count': len(fixed - owners),
                'settled_required': settled,
            }
            raise ReleaseError('Unexpected runtime owner appeared or a required owner disappeared.')
        if owners & self.retired:
            raise ReleaseError('A retired runtime owner reappeared.')
        coordinator = ({self.identities[COORDINATOR]} if COORDINATOR in self.identities else set())
        broker = ({self.identities[BROKER]} if BROKER in self.identities else set())
        if COORDINATOR in replacements:
            coordinator.add(replacements[COORDINATOR])
        if BROKER in replacements:
            broker.add(replacements[BROKER])
        actual_coordinators, actual_brokers, leaders = (set(state[k]) for k in INVENTORIES[1:])
        if (not actual_coordinators <= (coordinator & owners) or len(actual_coordinators) > 1
                or actual_brokers != (broker & owners) or not leaders <= actual_brokers or len(leaders) > 1):
            raise ReleaseError('Coordinator or broker ownership moved outside the selected role.')
        if COORDINATOR not in selected and actual_coordinators != coordinator:
            raise ReleaseError('The expected coordinator leader disappeared.')
        if BROKER not in selected and leaders != broker:
            raise ReleaseError('The expected broker maintenance leader disappeared.')
        if settled:
            expected_coordinator = (
                {replacements[COORDINATOR]} if COORDINATOR in replacements else set()
            ) if COORDINATOR in selected else coordinator
            expected_broker = (
                {replacements[BROKER]} if BROKER in replacements else set()
            ) if BROKER in selected else broker
            if actual_coordinators != expected_coordinator or leaders != expected_broker:
                raise ReleaseError('Selected role leadership did not settle.')

    def drain_keys(self):
        keys = COUNTS + (NATIVE_COUNTS if self.native_required else ())
        if self.core_changes:
            keys += ('legacy_core_runs',)
            if not (self.candidate_policy_active and self.candidate.get('core') == 'pydantic'):
                keys += ('legacy_automations', 'legacy_events')
            if self.native_required:
                # Once the new native policy owns admission, new inputs and
                # automation definitions may wait for the final resume. They
                # must never be stranded by retiring the native core.
                keys += tuple(key for key in NATIVE_CORE_COUNTS
                    if not (key in {'native_queued', 'native_automations', 'native_events',
                                    'native_automation_history'} and self.candidate_policy_active
                            and self.candidate.get('core') == 'pydantic'))
        return keys

    def check_unaffected(self):
        for sid, service in self.running.items():
            if sid in self.selected_roles():
                continue
            state = self.required_state(service)
            self.check_state(state)
            self.check_source(service, state)
            if state.get('identity') != self.identities.get(sid):
                raise ReleaseError('An unaffected service changed its process identity.')

    def configure(self, service, changes):
        # Check again before every individual PUT, not merely before a group.
        for key, value in changes.items():
            if key not in MUTABLE:
                raise ReleaseError('Refusing an unapproved environment change.')
            Release.configure(self, service, {key: value})

    def migrate_default_harness(self, service):
        """The one reviewed settings migration; generic release writes stay fixed."""
        previous = self.harness_changes.get(service.id)
        if previous is None:
            return
        if (not self.drained or self.selected is not None
                or self.expected.get(service.id) is not service
                or service.env.get('RENDER_MIGRATION_STAGE') != 'true'
                or service.env.get('MOYAI_BUILD_SHA') != self.record['commit']
                or service.env.get('AGENT_HARNESS') != previous
                or self.candidate['default_harness'] != DEFAULT_HARNESS):
            raise ReleaseError('Default harness may change only for a drained, staged candidate; release stopped.')
        self.unchanged()
        entry = {'service': service.id, 'key': 'AGENT_HARNESS', 'previous': previous,
                 'value': DEFAULT_HARNESS, 'confirmed': False}
        self.record['changes'].append(entry)
        self.save()
        self.render.call('PUT', f'/services/{service.id}/env-vars/AGENT_HARNESS', {'value': DEFAULT_HARNESS})
        service.env['AGENT_HARNESS'] = DEFAULT_HARNESS
        if self.environment(service.id) != service.env:
            raise ReleaseError('Saved environment did not match the reviewed default harness change; release stopped.')
        # The new candidate must verify the changed non-secret env digest. The
        # already-running process keeps its independent original env snapshot.
        service.original['AGENT_HARNESS'] = DEFAULT_HARNESS
        entry['confirmed'] = True
        self.save()

    def select_candidate_core(self, service):
        """Only the selector changes; prepared credentials/codecs remain intact."""
        if service.id not in self.core_changes:
            return
        previous = self.core_changes[service.id]
        target = self.candidate['core']
        if (not self.drained or self.selected is not None
                or self.expected.get(service.id) is not service
                or service.env.get('RENDER_MIGRATION_STAGE') != 'true'
                or service.env.get('MOYAI_BUILD_SHA') != self.record['commit']
                or service.env.get('MOYAI_CORE') != previous):
            raise ReleaseError('Core selection requires a drained, staged candidate; release stopped.')
        self.unchanged()
        entry = {'service': service.id, 'key': 'MOYAI_CORE', 'previous': previous,
                 'value': target, 'confirmed': False}
        self.record['changes'].append(entry)
        self.save()
        self.render.call('PUT', f'/services/{service.id}/env-vars/MOYAI_CORE', {'value': target})
        service.env['MOYAI_CORE'] = target
        if self.environment(service.id) != service.env:
            raise ReleaseError('Saved environment did not match the reviewed core selection; release stopped.')
        service.original['MOYAI_CORE'] = target
        entry['confirmed'] = True
        self.save()

    def plan_schema(self, states):
        current, target = self.baseline['schema'], self.candidate['schema']
        if target == current:
            return None
        support = self.candidate.get('schema_support')
        if target < current or not valid_schema_support(support):
            raise ReleaseError('Maintenance schema downgrade or invalid capability is not supported; no changes made.')
        compatible = all(valid_schema_support(state.get('schema_support'))
            and state['schema_support']['compatibility'] == support['compatibility']
            and state['schema_support']['required'] <= current for state in states.values())
        # Native maintenance upgrades canonical and native receipts together at
        # zero owners. An unchanged compatibility floor does not imply that an
        # additive online plan exists (the billing v21 -> v22 upgrade has none).
        # Select this path explicitly; never turn a failed online plan into an
        # offline fallback for the historical legacy controller.
        if compatible and self.candidate.get('core', 'legacy') != 'pydantic':
            return super().plan_schema(states)
        if self.offline_migrate is None:
            raise ReleaseError('This schema upgrade requires the explicit offline maintenance migration transport; no changes made.')
        return {'mode': 'offline', 'from_revision': current, 'to_revision': target,
                'source_digest': self.candidate['source_schema_digest']}

    def migrate_offline(self, plan):
        self.unchanged()
        if self.identities or not all(service.env['RENDER_MIGRATION_STAGE'] == 'true'
                                      for service in self.running.values()):
            raise ReleaseError('Offline migration requires all four services staged and every owner retired.')
        expected = {key: deepcopy(self.baseline[key]) for key in ('schema', 'policy', 'contracts')}
        expected.update({key: [] for key in INVENTORIES})
        expected.update(sha=self.record['commit'], source_schema=self.candidate['schema'],
                        source_schema_digest=self.candidate['source_schema_digest'])
        if self.native_required:
            expected['native_required'] = True
        if self.core_changes:
            expected['core_switch'] = True
        entry = self.record['schema_migration']
        entry.update(status='requested', confirmed=False)
        self.phase('Apply installed candidate schema migration with all runtime owners stopped')
        receipt = self.offline_migrate(self.expected[COORDINATOR], expected)
        wanted = {'status': 'applied', **{key: plan[key] for key in
                                        ('from_revision', 'to_revision', 'source_digest')}}
        if not isinstance(receipt, dict) or receipt != wanted:
            raise ReleaseError('Offline schema migration result is uncertain or mismatched; no activation was attempted.')
        entry.update(status='applied', confirmed=True)
        self.save()
        self.check_control_plane()
        for service in self.running.values():
            state = self.required_state(service)
            self.check_state(state, schema=plan['to_revision'])
            self.check_source(service, state)
        self.baseline['schema'] = plan['to_revision']
        self.record['schema_revision'] = plan['to_revision']
        entry['verified'] = True
        self.save()
        self.unchanged()

    def migrate_native(self):
        self.unchanged()
        if (not self.drained or self.identities or self.selected is not None
                or not all(s.env['RENDER_MIGRATION_STAGE'] == 'true' for s in self.running.values())):
            raise ReleaseError('Native provisioning requires all four owners retired and services staged.')
        expected = {key: deepcopy(self.baseline[key]) for key in ('schema', 'policy', 'contracts')}
        expected.update({key: [] for key in INVENTORIES})
        expected.update(sha=self.record['commit'], source_schema=self.candidate['schema'], native_required=True)
        if self.core_changes:
            expected['core_switch'] = True
        entry = self.record['native_migration']
        entry.update(status='requested', confirmed=False)
        self.phase('Verify or provision native application and Harness receipts at zero owners')
        receipt = self.native_migrate(self.expected[COORDINATOR], deepcopy(self.native_plan), expected)
        if receipt != {'status': 'verified', 'target': self.native_plan['target']}:
            raise ReleaseError('Native provisioning result is uncertain or mismatched; no activation was attempted.')
        entry.update(status='verified', confirmed=True)
        self.native_inventory = {**deepcopy(self.native_plan['target']), 'state': 'receipted'}
        # The versioned native migration seeds a paused bootstrap. No candidate
        # coordinator may admit work before this durable pause is observed.
        staged = self.expected[COORDINATOR]
        status = self.native_status(staged, staged=True)
        receipt = self.control_receipt(status, staged=True)
        if (not receipt['paused'] or status['workers']
                or (self.native_control_receipt is not None and receipt != self.native_control_receipt)):
            raise ReleaseError('Native bootstrap is not paused or its generation changed; no activation was attempted.')
        self.native_control_receipt = receipt
        entry['drain_generation'] = receipt['generation']
        self.save()
        self.unchanged()
        entry['verified'] = True
        self.save()

    def verify_source_automation_pause(self, service):
        if not self.core_changes or not self.legacy_schedule_count:
            return
        from scripts.release.automation_probe import valid_receipt
        if self.automation_probe is None:
            raise ReleaseError('Core switching with source schedules requires their read-only Temporal pause probe.')
        receipt = self.automation_probe(service)
        if (not valid_receipt(receipt) or receipt['count'] != self.legacy_schedule_count
                or (self.legacy_automation_receipt is not None and receipt != self.legacy_automation_receipt)):
            raise ReleaseError('Source automation pause or tracked schedule inventory changed; core activation stopped.')
        self.legacy_automation_receipt = deepcopy(receipt)
        self.record['legacy_automation_pause'] = deepcopy(receipt)
        self.save()

    def control_body(self, service, *, staged=False):
        body = {'protocol': 1, 'core': 'pydantic-v1',
                'native_schema': service.env['MOYAI_NATIVE_SCHEMA'],
                'task_queue': service.env['MOYAI_NATIVE_TASK_QUEUE']}
        if not staged:
            policy = (self.candidate['source_policy']
                if self.candidate_policy_active
                and self.candidate['contracts']['coordinator'] != self.baseline['contracts']['coordinator']
                else self.baseline['policy'])
            body['expected_worker'] = {
                **native_owner(self.identities[WORKER]), 'source_policy': policy,
                'worker_policy': self.source(service)['contracts']['worker'],
            }
        return body

    def control_receipt(self, status, *, staged=False):
        service = self.expected[COORDINATOR if staged else WORKER]
        try:
            receipt = validate_native_control(status, self.control_body(service, staged=staged), 'status')
            if not staged:
                worker = self.control_body(service)['expected_worker']
                if status['workers'] != [{**worker,
                        'maintenance_drain': service.env['MAINTENANCE_DRAIN'] == 'true'}]:
                    raise ValueError()
            return receipt
        except (ValueError, KeyError, TypeError):
            raise ReleaseError('Native status did not verify the exact registered worker and maintenance receipt.') from None

    def native_status(self, service, *, staged=False):
        if self.native_control is None:
            raise ReleaseError('The native in-place maintenance transport is unavailable; no release changes may proceed.')
        try:
            return self.native_control(service, 'status', self.control_body(service, staged=staged))
        except (ValueError, KeyError, TypeError):
            raise ReleaseError('Native maintenance status is incomplete or mismatched.') from None

    def change_native_control(self, command):
        """Persist one CAS intent; an unconfirmed response never permits retry."""
        self.unchanged()
        service = self.expected[WORKER]
        status = self.native_status(service)
        receipt = self.control_receipt(status)
        if receipt != self.native_control_receipt:
            raise ReleaseError('Native maintenance generation changed before the requested operation.')
        body = {**self.control_body(service), 'expected_generation': receipt['generation'],
                'request_key': f"release:{self.record['commit']}:{uuid4().hex}:{command}"}
        entry = {'command': command, 'request': deepcopy(body), 'confirmed': False}
        self.record.setdefault('native_control', []).append(entry)
        self.save()
        value = self.native_control(service, command, deepcopy(body))
        try:
            confirmed = validate_native_control(value, body, command)
        except (ValueError, KeyError, TypeError):
            raise ReleaseError('Native maintenance outcome is uncertain or mismatched; no retry was attempted.') from None
        self.native_control_receipt = confirmed
        entry.update(confirmed=True, receipt=deepcopy(confirmed))
        if command == 'resume':
            # Work may resume as soon as CAS commits, before its response arrives.
            self.drained = False
        self.save()
        self.unchanged()

    def begin_native_drain(self):
        self.phase('Request native SDK safe boundaries before replacing the running worker')
        self.change_native_control('request')
        def drained():
            self.unchanged()
            status = self.native_status(self.expected[WORKER])
            if self.control_receipt(status) != self.native_control_receipt:
                raise ReleaseError('Native drain generation changed while waiting for safe boundaries.')
            states = [self.required_state(service) for service in self.running.values()]
            for state in states:
                self.check_state(state)
            keys = self.drain_keys()
            return status['state'] == 'drained' and all(state[key] == 0 for state in states for key in keys)
        self.wait(drained, 'Native drain timed out; the original worker remains alive and paused, with saved work preserved.',
                  self.drain_timeout)
        self.drained = True
        self.record['native_in_place_drain_verified'] = True
        self.save()

    def migrate_schema(self, plan):
        # Existing transport verifies the same old four owners/contracts inside
        # the DDL transaction and never retries an uncertain schema write.
        super().migrate_schema(plan)

    def transition_service(self, service, sha, changes, *, release_drain=False):
        self.unchanged()
        self.configure(service, changes)
        if sha == self.record['commit'] and service.env['RENDER_MIGRATION_STAGE'] == 'true':
            if service.id in self.core_changes:
                self.select_candidate_core(service)
            if service.env.get('AGENT_HARNESS') != DEFAULT_HARNESS:
                self.migrate_default_harness(service)
        self.unchanged()
        target = deepcopy(service)
        target.sha = sha
        staged = target.env['RENDER_MIGRATION_STAGE'] == 'true'
        old = self.identities.get(service.id)
        # Persist intent before POST. From this point an ambiguous answer stops.
        self.selected, self.replacement = service.id, None
        self.rotate_probe(service)
        entry = {'service': service.id, 'role': service.role, 'sha': sha,
                 'staged': staged, 'drain': target.env['MAINTENANCE_DRAIN'] == 'true',
                 'id': None, 'status': 'requested', 'old_owner': old}
        self.record['deploys'].append(entry)
        self.save()
        result = self.render.call('POST', f'/services/{service.id}/deploys',
                                  {'commitId': sha, 'clearCache': 'do_not_clear'})
        identity = result.get('id') if isinstance(result, dict) else None
        if not isinstance(identity, str) or not re.fullmatch('dep-[a-z0-9]+', identity):
            raise ReleaseError('Render returned an invalid deployment identity; inspect the recorded intent.')
        self.active_deploy = entry['id'] = identity
        if release_drain:
            self.drained = False  # Only this accepted deployment may resume execution.
        self.save()

        def live():
            self.check_control_plane()
            value = self.render.call('GET', f'/services/{service.id}/deploys/{identity}')
            if value.get('commit', {}).get('id') != sha:
                raise ReleaseError('Render selected a different commit; release stopped.')
            entry['status'] = value.get('status')
            self.save()
            if entry['status'] not in ACTIVE_DEPLOYS | {'live'}:
                raise ReleaseError('Candidate deployment failed or returned an unknown state; no automatic rollback was attempted.')
            return entry['status'] == 'live'
        self.wait(live, 'Render deployment timed out; inspect its status before retrying.', self.deploy_timeout)
        target.deploy_id = service.deploy_id = identity
        service.sha = sha
        activating = service.id == COORDINATOR and not staged and not old

        def ready():
            self.check_control_plane()
            state = self.candidate_state(target, 'maintenance_readiness')
            if state is None:
                return False
            # Identify this exact HTTP process before permitting a new owner.
            owner = state.get('identity')
            if staged:
                if owner is not None:
                    raise ReleaseError('Staged candidate acquired a runtime owner.')
            else:
                if not isinstance(owner, str) or not owner or owner == old:
                    self.rotate_probe(target)
                    return False
                if owner in self.identities.values() or owner in self.retired:
                    raise ReleaseError('Candidate claimed an existing or retired process identity.')
                if self.replacement is not None and self.replacement != owner:
                    raise ReleaseError('Candidate process identity changed unexpectedly.')
                self.replacement = owner
            if activating:
                # Normal coordinator startup may publish the new fingerprints
                # only from the zero-owner state checked before this POST.
                self.candidate_policy_active = True
            self.check_state(state)
            self.check_source(target, state, require_ready=False)
            self.check_unaffected()
            if not state['ready']:
                return False
            if old and old in state['owners']:
                return False
            self.check_state(state, settled=True)
            if not staged and service.role == 'coordinator' and state.get('coordinator_state') != 'leader':
                return False
            if not staged and service.role == 'broker' and state.get('broker_draining') is not False:
                return False
            return True
        self.wait(ready, 'Candidate readiness or exact old-owner retirement was not confirmed; release stopped.',
                  self.readiness_timeout + self.retirement_timeout)
        if old:
            self.retired.add(old)
        if self.replacement:
            self.identities[service.id] = self.replacement
        else:
            self.identities.pop(service.id, None)
        self.running[service.id] = deepcopy(target)
        self.schema_supports[service.id] = deepcopy(self.source(target).get('schema_support'))
        entry.update(status='verified', new_owner=self.replacement, old_owner_retired=bool(old))
        self.record.setdefault('replacements', []).append(deepcopy(entry))
        self.selected = self.replacement = self.active_deploy = None
        self.save()
        self.unchanged()

    def observe_batch(self):
        states = {}
        for sid, item in self.batch.items():
            if not item.receipt['id']:
                continue
            state = self.candidate_state(item.target, 'maintenance_batch_readiness')
            if state is None:
                continue
            self.check_source(item.target, state, require_ready=False)
            identity = state.get('identity')
            staged = item.target.env['RENDER_MIGRATION_STAGE'] == 'true'
            if not staged:
                if not isinstance(identity, str) or not identity or identity == item.old_owner:
                    self.rotate_probe(item.target)
                    continue
                others = {value.new_owner for key, value in self.batch.items() if key != sid}
                if identity in self.identities.values() or identity in self.retired or identity in others:
                    raise ReleaseError('Candidate claimed an existing or retired process identity.')
                if item.new_owner is not None and item.new_owner != identity:
                    raise ReleaseError('Candidate process identity changed unexpectedly.')
                item.new_owner = identity
                item.receipt['observed_owner'] = identity
            states[sid] = state
        # Discover every candidate identity before validating shared ownership
        # snapshots; a peer may have started before this role's probe completed.
        if len(states) == len(self.batch):
            for state in states.values():
                self.check_state(state)
        self.save()
        return states

    def transition_batch(self, service_ids, sha, *, staged):
        """Overlap independent Render replacements, recording each mutation once."""
        allowed = {WORKER, BROKER, COORDINATOR} if staged else {WORKER, self.api_service_id}
        if (set(service_ids) != allowed or len(service_ids) != len(allowed)
                or not self.native_maintenance or not self.drained
                or not self.native_control_receipt or not self.native_control_receipt['paused']
                or self.selected is not None or self.batch or sha != self.record['commit']):
            raise ReleaseError('Concurrent replacement requires the verified native maintenance boundary.')
        if staged:
            if (self.api_service_id in self.identities
                    or self.running[self.api_service_id].env['RENDER_MIGRATION_STAGE'] != 'true'):
                raise ReleaseError('Stage and retire the API before replacing background roles together.')
        elif (not self.record.get('zero_owner_boundary_verified')
                or not {COORDINATOR, BROKER} <= self.identities.keys()
                or any(sid in self.identities for sid in service_ids)):
            raise ReleaseError('Activate coordinator and broker before replacing the staged worker and API together.')

        self.unchanged()
        # Configure all targets while their old processes are still independently
        # checked. Environment writes keep the existing per-write intent protocol.
        for sid in service_ids:
            service = self.expected[sid]
            changes = {'RENDER_MIGRATION_STAGE': str(staged).lower()}
            if staged:
                changes['MOYAI_BUILD_SHA'] = sha
            self.configure(service, changes)
        self.unchanged()
        group = {'roles': [self.expected[sid].role for sid in service_ids],
                 'staged': staged, 'status': 'preparing', 'deployments': []}
        self.record.setdefault('batches', []).append(group)
        for sid in service_ids:
            target = deepcopy(self.expected[sid])
            target.sha = sha
            entry = {'service': sid, 'role': target.role, 'sha': sha,
                     'staged': staged, 'drain': target.env['MAINTENANCE_DRAIN'] == 'true',
                     'id': None, 'status': 'prepared', 'old_owner': self.identities.get(sid)}
            self.batch[sid] = Replacement(target, self.identities.get(sid), entry)
            group['deployments'].append(entry)
        self.save()

        # POSTs remain bounded, ordered mutations. Render performs their builds
        # and replacements concurrently; no threads share controller state.
        # The complete source ownership boundary was checked above. Once a
        # batch starts, a peer can acquire its new owner before SSH reaches it.
        # Recheck control-plane drift before each POST, then require every
        # candidate's authenticated observation before advancing the batch.
        for sid, item in self.batch.items():
            self.check_control_plane()
            self.rotate_probe(item.target)
            item.requested = True
            item.receipt['status'] = 'requested'
            self.record['deploys'].append(item.receipt)
            self.save()
            result = self.render.call('POST', f'/services/{sid}/deploys',
                                      {'commitId': sha, 'clearCache': 'do_not_clear'})
            identity = result.get('id') if isinstance(result, dict) else None
            if not isinstance(identity, str) or not re.fullmatch('dep-[a-z0-9]+', identity):
                raise ReleaseError('Render returned an invalid deployment identity; inspect the recorded intent.')
            if any(peer.receipt['id'] == identity for key, peer in self.batch.items() if key != sid):
                raise ReleaseError('Render returned a reused deployment identity; inspect the recorded intents.')
            item.target.deploy_id = item.receipt['id'] = identity
            self.save()
            if result.get('status') is not None and result['status'] not in ACTIVE_DEPLOYS | {'live'}:
                item.receipt['status'] = result['status']
                self.save()
                raise ReleaseError('A concurrent deployment failed; no further deployment was requested.')
        group['status'] = 'replacing'
        self.save()

        def live():
            self.check_control_plane()
            complete = True
            for sid, item in self.batch.items():
                value = self.render.call('GET', f"/services/{sid}/deploys/{item.receipt['id']}")
                if value.get('commit', {}).get('id') != sha:
                    raise ReleaseError('Render selected a different commit; release stopped.')
                item.receipt['status'] = value.get('status')
                self.save()
                if item.receipt['status'] not in ACTIVE_DEPLOYS | {'live'}:
                    raise ReleaseError('A concurrent deployment failed; inspect every recorded intent before recovery.')
                complete = complete and item.receipt['status'] == 'live'
            return complete

        self.wait(live, 'Concurrent Render deployments timed out; inspect every recorded intent before recovery.',
                  self.deploy_timeout)

        def ready():
            self.check_control_plane()
            states = self.observe_batch()
            if len(states) != len(self.batch):
                return False
            self.check_unaffected()
            if any(not state['ready'] for state in states.values()):
                return False
            old_owners = {item.old_owner for item in self.batch.values() if item.old_owner}
            if any(old_owners & set(state['owners']) for state in states.values()):
                return False
            for state in states.values():
                self.check_state(state, settled=True)
            return True

        self.wait(ready, 'Concurrent readiness or exact old-owner retirement was not confirmed; release stopped.',
                  self.readiness_timeout + self.retirement_timeout)
        for sid, item in self.batch.items():
            if item.old_owner:
                self.retired.add(item.old_owner)
            if item.new_owner:
                self.identities[sid] = item.new_owner
            else:
                self.identities.pop(sid, None)
            service = self.expected[sid]
            service.sha, service.deploy_id = item.target.sha, item.target.deploy_id
            self.running[sid] = deepcopy(item.target)
            self.schema_supports[sid] = deepcopy(self.source(item.target).get('schema_support'))
            item.receipt.update(status='verified', new_owner=item.new_owner,
                                old_owner_retired=bool(item.old_owner))
            self.record.setdefault('replacements', []).append(deepcopy(item.receipt))
        group['status'] = 'verified'
        self.batch.clear()
        self.save()
        self.unchanged()

    def preflight(self):
        if (not re.fullmatch('srv-[a-z0-9]+', self.api_service_id)
                or self.api_service_id in {COORDINATOR, WORKER, BROKER}):
            raise ReleaseError('Set MOYAI_API_SERVICE_ID to the activated dedicated API service; no changes made.')
        sha = self.github.candidate()
        self.record.update(commit=sha, topology='maintenance_four_service', release_type='maintenance', replacements=[])
        self.phase('Check exact selected main CI and all four maintenance services')
        self.wait(lambda: self.github.rolling_ready(sha), 'Required runtime and release CI did not finish; no changes made.', 1800)
        coordinator, owner = self.inspect(COORDINATOR, 'coordinator')
        for sid, role in ((WORKER, 'worker'), (BROKER, 'broker'), (self.api_service_id, 'api')):
            item, item_owner = self.inspect(sid, role)
            if (item_owner != owner or item.sha != coordinator.sha or any(
                    item.env.get(key, SHARED_DEFAULTS.get(key, '')) !=
                    coordinator.env.get(key, SHARED_DEFAULTS.get(key, '')) for key in SHARED_KEYS)):
                raise ReleaseError('Maintenance requires four matching builds and preserved shared configuration.')
        states = {sid: self.required_state(service) for sid, service in self.expected.items()}
        self.baseline = deepcopy(states[COORDINATOR])
        self.source_core = coordinator.env.get('MOYAI_CORE', 'legacy')
        self.native_required = bool(coordinator.env.get('MOYAI_NATIVE_SCHEMA'))
        self.native_inventory = deepcopy(self.baseline.get('native'))
        self.native_control_receipt = deepcopy(self.baseline.get('native_drain'))
        if self.native_required and not isinstance(self.native_inventory, dict):
            raise ReleaseError('Prepared native schema has no verified receipt inventory; no changes made.')
        if (not valid_contract({key: self.baseline.get(key) for key in ('schema', 'contracts')})
                or not isinstance(self.baseline.get('policy'), str)
                or not re.fullmatch('[0-9a-f]{64}', self.baseline['policy'])):
            raise ReleaseError('Preflight runtime policy or schema inventory is incomplete; no changes made.')
        self.identities = {sid: state.get('identity') for sid, state in states.items()}
        if (any(not isinstance(v, str) or not v for v in self.identities.values())
                or len(set(self.identities.values())) != 4):
            raise ReleaseError('Preflight requires four distinct active runtime owners.')
        for sid, service in self.expected.items():
            state = states[sid]
            self.sources[sid] = {'schema': state.get('source_schema'), 'contracts': state.get('source_contracts'),
                'source_policy': state.get('source_policy'), 'default_harness': state.get('default_harness'),
                'source_schema_digest': state.get('source_schema_digest'),
                **({'core': state['core']} if 'core' in state else {}),
                **({'schema_support': state['schema_support']} if 'schema_support' in state else {})}
            if (not valid_maintenance_contract(self.sources[sid])
                    or self.sources[sid]['contracts'] != self.baseline['contracts']):
                raise ReleaseError('Preflight installed-source contracts are incomplete or incompatible; no changes made.')
            self.schema_supports[sid] = deepcopy(state.get('schema_support'))
            self.github.forward_from(service.sha, sha)
            candidate_env = {**service.env, 'MOYAI_BUILD_SHA': sha}
            source_core = service.env.get('MOYAI_CORE', 'legacy')
            selected_core = source_core if self.target_core == 'preserve' else self.target_core
            if selected_core != source_core:
                self.core_changes[sid] = service.env.get('MOYAI_CORE')
            if self.target_core != 'preserve':
                candidate_env['MOYAI_CORE'] = selected_core
            if (selected_core == source_core == 'legacy'
                    and 'AGENT_HARNESS' in candidate_env and candidate_env['AGENT_HARNESS'] != DEFAULT_HARNESS):
                self.harness_changes[sid] = candidate_env['AGENT_HARNESS']
                candidate_env['AGENT_HARNESS'] = DEFAULT_HARNESS
            candidate = self.contract(candidate_env)
            if not valid_maintenance_contract(candidate) or (self.candidate is not None and self.candidate != candidate):
                raise ReleaseError('Candidate contracts differ across preserved role settings; no changes made.')
            if candidate.get('core', 'legacy') != selected_core:
                raise ReleaseError('Candidate source did not select the requested core; no changes made.')
            if selected_core == source_core == 'legacy' and candidate['default_harness'] != DEFAULT_HARNESS:
                raise ReleaseError('Candidate settings did not select the reviewed Deep Agents default; no changes made.')
            if selected_core == 'pydantic':
                if not self.native_required or self.native_migrate is None or self.native_control is None:
                    raise ReleaseError('Pydantic requires prepared native settings and guarded migration transport; no changes made.')
                try:
                    native_plan = native_migration_plan(self.native_inventory, self.native_manifest(candidate_env))
                except (ValueError, KeyError, TypeError):
                    raise ReleaseError('Native app or Harness migration receipts are missing, unknown or incompatible; no changes made.') from None
                if self.native_plan is not None and self.native_plan != native_plan:
                    raise ReleaseError('Native migration plans differ across preserved role settings; no changes made.')
                self.native_plan = native_plan
            self.candidate = deepcopy(candidate)
            self.running[sid] = deepcopy(service)
        for sid, state in states.items():
            self.check_state(state)
            self.check_source(self.running[sid], state)
        if (self.core_changes and self.native_required
                and any(state['native_automation_history'] for state in states.values())):
            raise ReleaseError(
                'Core switching after native automation adoption requires a qualified Temporal control-workflow '
                'drain receipt. Preserve the native core and roll forward; no changes made.')
        if self.core_changes and any(state['legacy_automations'] for state in states.values()):
            raise ReleaseError(
                'Pause source automations and confirm their installed schedule revision before core switching; '
                'no changes made.')
        if self.core_changes:
            counts = [state.get('legacy_schedule_count') for state in states.values()]
            if any(type(count) is not int or count < 0 for count in counts) or len(set(counts)) != 1:
                raise ReleaseError('Source schedule inventory is incomplete or inconsistent; no changes made.')
            self.legacy_schedule_count = counts[0]
            self.verify_source_automation_pause(self.expected[COORDINATOR])
        if self.source_core == 'pydantic':
            if self.native_control_receipt is None:
                raise ReleaseError('The native source lacks its versioned drain receipt; no changes made.')
            status = self.native_status(self.expected[WORKER])
            if self.control_receipt(status) != self.native_control_receipt or status['state'] != 'running':
                raise ReleaseError('Native production is not at a clean running maintenance generation; inspect the prior receipt.')
        elif self.native_plan and self.native_control_receipt is not None and not self.native_control_receipt['paused']:
            raise ReleaseError('The existing native destination is not paused; inspect its previous owner and release receipt.')
        if self.core_changes and self.candidate.get('core') == 'pydantic':
            self.wait(lambda: self.github.native_ready(sha),
                      'Native runtime CI did not complete on the selected main commit; no changes made.', 1800)
        if (states[COORDINATOR].get('coordinator_state') != 'leader'
                or states[BROKER].get('broker_draining') is not False):
            raise ReleaseError('Preflight requires healthy coordinator and broker leadership.')
        if (self.candidate['contracts']['coordinator'] == self.baseline['contracts']['coordinator']
                and self.candidate['contracts'] != self.baseline['contracts']):
            raise ReleaseError('A changed role contract requires coordinator compatibility to change as well; no changes made.')
        plan = self.plan_schema(states)
        self.record.update(previous={sid: {'role': service.role, 'sha': service.sha,
            'deploy_id': service.deploy_id, 'owner': self.identities[sid],
            'default_harness': states[sid]['default_harness']} for sid, service in self.expected.items()},
            cluster_policy=self.baseline['policy'], contracts=deepcopy(self.baseline['contracts']),
            candidate_contracts=deepcopy(self.candidate['contracts']),
            candidate_default_harness=self.candidate['default_harness'],
            candidate_core=self.candidate.get('core', 'legacy'),
            core_migration=[{'service': sid, 'previous': previous, 'value': self.candidate['core']}
                            for sid, previous in self.core_changes.items()],
            native_migration=({'plan': deepcopy(self.native_plan), 'status': 'planned',
                               'confirmed': False, 'verified': False} if self.native_plan else None),
            default_harness_migration=[{'service': sid, 'role': self.expected[sid].role,
                'previous': value, 'value': DEFAULT_HARNESS} for sid, value in self.harness_changes.items()],
            schema_revision=self.baseline['schema'], schema_migration=(
                {'plan': plan, 'status': 'planned', 'confirmed': False, 'verified': False} if plan else None))
        self.unchanged()
        return sha, plan

    def run(self):
        try:
            sha, plan = self.preflight()
            if self.preflight_only:
                for _ in range(2):
                    self.unchanged()
                self.record.update(status='preflight_passed', transport_check_rounds=3)
                self.phase('Four-role maintenance preflight passed; no changes made')
                return
            if (not self.core_changes and not self.harness_changes
                    and not (self.native_plan and self.native_plan['apply'])
                    and all(service.sha == sha for service in self.expected.values())):
                self.record['status'] = 'success'
                self.phase('Selected build is already active on all four roles')
                return
            self.record['status'] = 'running'
            if plan and plan.get('mode') != 'offline':
                self.migrate_schema(plan)
            if self.source_core == 'pydantic':
                self.begin_native_drain()
            worker = self.expected[WORKER]
            if not self.native_maintenance:
                self.phase('Pause new execution on the existing worker build')
                self.transition_service(worker, worker.sha, {'MAINTENANCE_DRAIN': 'true'})
            self.phase('Wait for saved work, model requests and execution leases to drain')
            def drained():
                self.unchanged()
                states = [self.required_state(service) for service in self.running.values()]
                for state in states:
                    self.check_state(state)
                keys = self.drain_keys()
                return all(state[key] == 0 for state in states for key in keys)
            self.wait(drained, 'Drain timed out; new execution remains paused and no sessions were cancelled.', self.drain_timeout)
            self.drained = True
            self.record['drain_verified'] = True
            self.save()
            # Staging API first gates intake during the incompatible transition.
            staging = (self.api_service_id,) if self.native_maintenance else (
                self.api_service_id, WORKER, BROKER, COORDINATOR)
            for sid in staging:
                service = self.expected[sid]
                self.phase('Stage candidate ' + service.role + ' and retire its old runtime owner')
                self.transition_service(service, sha, {'RENDER_MIGRATION_STAGE': 'true', 'MOYAI_BUILD_SHA': sha})
            if self.native_maintenance:
                self.phase('Stage native worker, broker and coordinator together; require every old owner to retire')
                self.transition_batch((WORKER, BROKER, COORDINATOR), sha, staged=True)
            self.unchanged()
            if self.identities:
                raise ReleaseError('Old runtime owners remain; candidate coordinator was not activated.')
            self.record['zero_owner_boundary_verified'] = True
            self.save()
            self.verify_source_automation_pause(self.expected[COORDINATOR])
            if self.legacy_automation_receipt is not None:
                self.record['legacy_control_disposition'] = 'preserved_on_original_queue_with_paused_admission'
                self.save()
            if plan and plan.get('mode') == 'offline':
                self.migrate_offline(plan)
            if self.native_plan:
                self.migrate_native()
            activation = (COORDINATOR, BROKER) if self.native_maintenance else (
                COORDINATOR, BROKER, WORKER, self.api_service_id)
            for sid in activation:
                service = self.expected[sid]
                self.phase('Activate matching candidate ' + service.role + ' while execution remains drained')
                self.transition_service(service, sha, {'RENDER_MIGRATION_STAGE': 'false'})
            if self.native_maintenance:
                self.phase('Activate native worker and API together while the maintenance generation remains paused')
                self.transition_batch((WORKER, self.api_service_id), sha, staged=False)
            self.unchanged()
            native = self.candidate.get('core') == 'pydantic'
            if not self.native_maintenance:
                self.phase('Release worker drain after all four candidate roles are verified')
                self.transition_service(worker, sha, {'MAINTENANCE_DRAIN': 'false'}, release_drain=not native)
            if native:
                self.phase('Resume the native maintenance generation after every candidate role and retirement verifies')
                self.change_native_control('resume')
            self.unchanged()
            self.record['status'] = 'success'
            self.phase('All four roles match; prior owners retired and queued execution resumed')
        except BaseException as exc:
            self.record['status'] = 'failed'
            self.record['reason'] = str(exc) if isinstance(exc, ReleaseError) else 'Release interrupted or unexpected response; inspect the recorded intent.'
            self.save()
            raise
