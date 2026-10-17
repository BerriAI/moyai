"""Native canonical upgrades use the guarded offline maintenance boundary."""
import json
from copy import deepcopy
from pathlib import Path

import pytest
from test_native_cutover import configure_native_source
from test_native_cutover import cutover as cutover_fixture

from app import schema_migrations
from scripts.release.deploy import COORDINATOR, ReleaseError

cutover = cutover_fixture


@pytest.fixture(params=['pydantic', 'legacy'])
def schema_upgrade(cutover, request):
    release, render, _, _, _ = cutover
    if request.param == 'pydantic':
        configure_native_source(cutover)
    versions = json.loads(Path(schema_migrations.__file__).with_name('schema_versions.json').read_text())
    render.old_contract = {**deepcopy(render.old_contract), 'schema': 21,
        'schema_support': {'protocol': 1, 'required': 21, 'compatibility': 20},
        'source_schema_digest': versions['21']}
    render.candidate_contract.update(schema=22,
        schema_support={'protocol': 1, 'required': 22, 'compatibility': 20},
        source_schema_digest=versions['22'])
    render.schema = 21
    release.contract = lambda env: deepcopy(render.candidate_contract)
    migrations = []
    native_migrate = release.native_migrate

    def migrate(service, expected):
        assert service.id == COORDINATOR and not any(render.owners.values())
        assert all(env['RENDER_MIGRATION_STAGE'] == 'true' for env in render.installed.values())
        assert release.record['zero_owner_boundary_verified'] and release.drained
        assert expected['schema'] == 21 and expected['source_schema'] == 22
        assert expected['source_schema_digest'] == versions['22'] and expected['native_required']
        assert release.record['schema_migration']['status'] == 'requested'
        assert not release.record['schema_migration']['confirmed']
        migrations.append(deepcopy(expected))
        render.schema = 22
        return {'status': 'applied', 'from_revision': 21, 'to_revision': 22,
                'source_digest': versions['22']}

    def provision(service, plan, expected):
        assert len(migrations) == 1 and render.schema == 22
        assert release.record['schema_migration']['verified']
        return native_migrate(service, plan, expected)

    release.offline_migrate, release.native_migrate = migrate, provision
    return release, render, migrations


def test_native_billing_schema_upgrade_preflight_is_read_only(schema_upgrade):
    release, render, migrations = schema_upgrade
    release.preflight_only = True
    release.run()
    assert release.record['status'] == 'preflight_passed'
    assert release.record['schema_migration']['plan'] == {
        'mode': 'offline', 'from_revision': 21, 'to_revision': 22,
        'source_digest': render.candidate_contract['source_schema_digest']}
    assert not render.calls and not migrations
    assert all(command == 'status' for command, _ in release.test_control['calls'])


def test_native_canonical_upgrade_runs_before_native_provision_and_activation(schema_upgrade):
    release, render, migrations = schema_upgrade
    release.migration_plan = lambda *args: pytest.fail('Native maintenance requested an online plan')
    release.run()
    assert len(migrations) == 1 and release.record['status'] == 'success'
    assert release.record['schema_revision'] == 22
    assert release.record['schema_migration']['confirmed']
    assert release.record['schema_migration']['verified']
    assert release.record['native_migration']['verified']
    assert not release.test_control['receipt']['paused']
    assert all(len(owners) == 1 for owners in render.owners.values())


def test_missing_offline_transport_stops_native_upgrade_before_mutation(schema_upgrade):
    release, render, migrations = schema_upgrade
    release.offline_migrate = None
    with pytest.raises(ReleaseError, match='explicit offline'):
        release.run()
    assert not render.calls and not migrations
    assert all(command == 'status' for command, _ in release.test_control['calls'])


@pytest.mark.parametrize('fault', ['lost_ack', 'wrong_digest', 'wrong_revision'])
def test_unconfirmed_canonical_upgrade_leaves_native_roles_staged(schema_upgrade, fault):
    release, render, migrations = schema_upgrade
    original = release.offline_migrate
    provisions = []
    release.native_migrate = lambda *args: provisions.append(args)

    def migrate(service, expected):
        result = original(service, expected)
        if fault == 'lost_ack':
            raise ReleaseError('Synthetic lost migration acknowledgement')
        result['source_digest' if fault == 'wrong_digest' else 'to_revision'] = (
            'a' * 64 if fault == 'wrong_digest' else 23)
        return result

    release.offline_migrate = migrate
    with pytest.raises(ReleaseError):
        release.run()
    assert len(migrations) == 1 and not provisions
    assert not any(render.owners.values())
    assert all(env['RENDER_MIGRATION_STAGE'] == 'true' for env in render.installed.values())
    assert not release.record['schema_migration']['verified']
