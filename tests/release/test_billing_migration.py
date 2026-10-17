"""Billing receipt validation and maintenance ordering; app proof is separate."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_native_cutover import configure_native_source
from test_native_cutover import cutover as cutover_fixture

from scripts.release import billing_migration
from scripts.release.deploy import COORDINATOR, ReleaseError
from scripts.release.rehearse import OLD

cutover = cutover_fixture


def billing_status(phase='shadow'):
    return {
        'source_schema': 'fixture', 'native_schema': 'native_fixture', 'version': 1,
        'phase': phase, 'physical_billing_stores': 1 if phase == 'consolidated' else 3,
        'backfill': {'legacy': True, 'native': True}, 'records': {'legacy': 3, 'native': 2},
        'comparison': {source: {'source_rows': count, 'mismatches': 0, 'extra_rows': 0}
                       for source, count in [('legacy', 3), ('native', 2)]},
    }


@pytest.mark.parametrize('fault', [
    None, 'source', 'native', 'phase', 'version_bool', 'stores', 'backfill', 'backfill_int',
    'records_bool', 'missing_source', 'mismatch', 'extra', 'row_count', 'extra_field',
])
def test_billing_receipt_requires_both_complete_exact_sources(fault):
    value = billing_status('consolidated')
    if fault == 'source': value['source_schema'] = 'other'
    elif fault == 'native': value['native_schema'] = 'other'
    elif fault == 'phase': value['phase'] = 'active'
    elif fault == 'version_bool': value['version'] = True
    elif fault == 'stores': value['physical_billing_stores'] = 3
    elif fault == 'backfill': value['backfill']['legacy'] = False
    elif fault == 'backfill_int': value['backfill']['legacy'] = 1
    elif fault == 'records_bool': value['records']['native'] = True
    elif fault == 'missing_source': value['records'].pop('native')
    elif fault == 'mismatch': value['comparison']['native']['mismatches'] = 1
    elif fault == 'extra': value['comparison']['legacy']['extra_rows'] = 1
    elif fault == 'row_count': value['comparison']['legacy']['source_rows'] = 4
    elif fault == 'extra_field': value['untrusted'] = 'private-value'
    if fault:
        with pytest.raises((ValueError, KeyError, TypeError)):
            billing_migration.validate(value, source_schema='fixture',
                                       native_schema='native_fixture', consolidated=True)
    else:
        assert billing_migration.validate(value, source_schema='fixture',
                                         native_schema='native_fixture', consolidated=True) == value


@pytest.fixture
def billing_release(cutover):
    release, render = configure_native_source(cutover)
    for source in (render.old_contract, render.candidate_contract):
        source.update(schema=22, schema_support={'protocol': 1, 'required': 22, 'compatibility': 20})
    render.schema = 22
    release.contract = lambda env: deepcopy(render.candidate_contract)
    release.consolidate_billing = True
    phases, calls = ['shadow'], []

    def storage(service, command, expected=None):
        calls.append(command)
        result = billing_status(phases[0])
        result['source_schema'] = service.env['MOYAI_DATABASE_SCHEMA']
        if command == 'consolidate':
            assert service.id == COORDINATOR
            assert release.drained and not any(render.owners.values())
            assert len(render.history) == 4
            assert all(env['RENDER_MIGRATION_STAGE'] == 'true' for env in render.installed.values())
            assert release.record['native_migration']['verified']
            assert release.record['billing_consolidation']['status'] == 'requested'
            assert not release.record['billing_consolidation']['confirmed']
            assert expected['native_drain']['paused'] and expected['schema'] == 22
            assert expected['sha'] == release.record['commit']
            phases[0] = result['phase'] = 'consolidated'
            result['physical_billing_stores'] = 1
        return result

    release.billing_storage = storage
    return release, render, calls, phases


def test_cutover_follows_schema_verification_and_precedes_every_activation(billing_release):
    release, render, calls, _ = billing_release
    release.run()
    assert calls == ['status', 'consolidate']
    assert release.record['status'] == 'success' and len(render.history) == 8
    entry = release.record['billing_consolidation']
    assert entry['confirmed'] and entry['verified'] and entry['status'] == 'consolidated'
    assert entry['after']['physical_billing_stores'] == 1
    assert not release.test_control['receipt']['paused']


def test_billing_preflight_only_reads_storage_without_pausing(billing_release):
    release, render, calls, _ = billing_release
    release.preflight_only = True
    release.run()
    assert calls == ['status'] and not render.calls
    assert all(command == 'status' for command, _ in release.test_control['calls'])
    assert release.record['billing_consolidation']['status'] == 'planned'


@pytest.mark.parametrize('fault', ['unfinished', 'mismatch', 'transport'])
def test_unprepared_billing_stops_before_drain_or_hosting_writes(billing_release, fault):
    release, render, _, _ = billing_release
    original = release.billing_storage
    if fault == 'transport':
        release.billing_storage = None
    else:
        def invalid(*args):
            result = original(*args)
            if fault == 'unfinished':
                result['backfill']['native'] = False
            else:
                result['comparison']['legacy']['mismatches'] = 1
            return result
        release.billing_storage = invalid
    with pytest.raises(ReleaseError):
        release.run()
    assert not render.calls
    assert all(command == 'status' for command, _ in release.test_control['calls'])


@pytest.mark.parametrize('fault', ['lost_ack', 'wrong_phase', 'extra_rows', 'scope', 'new_work'])
def test_uncertain_cutover_never_retries_or_activates(billing_release, fault):
    release, render, calls, _ = billing_release
    original = release.billing_storage

    def fail(service, command, expected=None):
        result = original(service, command, expected)
        if command != 'consolidate':
            return result
        if fault == 'lost_ack':
            raise ReleaseError('Synthetic lost cutover acknowledgement')
        if fault == 'wrong_phase':
            result['phase'] = 'active'
        elif fault == 'extra_rows':
            result['comparison']['native']['extra_rows'] = 1
        elif fault == 'scope':
            result['native_schema'] = 'other'
        else:
            render.counts['native_requests'] = 1
        return result

    release.billing_storage = fail
    with pytest.raises(ReleaseError):
        release.run()
    assert calls.count('consolidate') == 1 and len(render.history) == 4
    assert not any(render.owners.values()) and release.test_control['receipt']['paused']
    assert all(env['RENDER_MIGRATION_STAGE'] == 'true' for env in render.installed.values())


def use_same_build(release, render):
    release.github.sha = OLD
    release.github.candidate = lambda: OLD
    render.candidate_contract = deepcopy(render.old_contract)
    release.contract = lambda env: deepcopy(render.candidate_contract)


def test_requested_same_build_cutover_is_not_a_noop(billing_release):
    release, render, calls, _ = billing_release
    use_same_build(release, render)
    release.run()
    assert calls == ['status', 'consolidate'] and len(render.history) == 8
    assert release.record['status'] == 'success'


def test_verified_existing_consolidation_is_a_read_only_same_build_noop(billing_release):
    release, render, calls, phases = billing_release
    use_same_build(release, render)
    phases[0] = 'consolidated'
    release.run()
    assert calls == ['status'] and not render.calls
    assert release.record['billing_consolidation']['status'] == 'already_consolidated'
    assert release.record['billing_consolidation']['verified']


@pytest.mark.parametrize('command', ['status', 'consolidate'])
def test_transport_is_bounded_and_never_retries_unconfirmed_cutover(monkeypatch, command):
    calls = []

    class Transport:
        def __init__(self, key, source, **kwargs):
            self.source = source

        def run(self, *args, **kwargs):
            calls.append(kwargs)
            return {'status': 'unconfirmed'}

    monkeypatch.setattr('scripts.release.deploy.SSHProbe', Transport)
    service = SimpleNamespace(
        env={'MOYAI_DATABASE_SCHEMA': 'fixture', 'MOYAI_NATIVE_SCHEMA': 'native_fixture'},
        probe_options=lambda: {'synthetic': True})
    with pytest.raises(ReleaseError, match='no retry'):
        billing_migration.SSHBillingStorage(None)(service, command)
    assert len(calls) == 1 and calls[0]['read_only'] is (command == 'status')
    assert calls[0]['timeout'] == 180
