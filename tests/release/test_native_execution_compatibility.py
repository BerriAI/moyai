"""Real release-controller transitions with synthetic services and receipt manifests.

Actual v13/v14 receipt persistence is covered by the native PostgreSQL regression.
"""

from copy import deepcopy

import pytest
from test_native_cutover import MANIFEST, configure_native_source
from test_native_cutover import cutover as cutover_fixture

from scripts.release.deploy import COORDINATOR, ReleaseError
from scripts.release.native_probe import migration_plan

cutover = cutover_fixture
V13 = {
    **deepcopy(MANIFEST),
    "app": {f"v{version}": f"{version:064x}" for version in range(1, 14)},
}
V14 = {
    **deepcopy(V13),
    "app": {**V13["app"], "2026-10-16-product-execution-v14": "e" * 64},
}


def test_v14_receipt_rejects_same_core_v13_revert_before_any_transition(cutover):
    release, render = configure_native_source(cutover)
    _, _, observed, migrations, _ = cutover
    observed.clear()
    observed.update(deepcopy(V14), state="receipted")
    release.native_manifest = lambda environment: deepcopy(V13)
    original_environment = deepcopy(render.env)
    original_owners = deepcopy(render.owners)
    original_control = deepcopy(release.test_control["receipt"])

    with pytest.raises(ReleaseError, match="missing, unknown or incompatible"):
        release.run()

    assert not release.core_changes
    assert not render.calls and not render.history and not migrations
    assert not release.test_control["calls"]
    assert render.env == original_environment and render.owners == original_owners
    assert release.test_control["receipt"] == original_control
    assert observed == {**V14, "state": "receipted"}


def test_v13_to_v14_maintenance_migrates_only_after_all_source_owners_retire(cutover):
    release, render = configure_native_source(cutover)
    _, _, observed, migrations, _ = cutover
    observed.clear()
    observed.update(deepcopy(V13), state="receipted")
    release.native_manifest = lambda environment: deepcopy(V14)
    render.counts["native_queued"] = 2
    boundary = []

    def migrate(service, plan, expected):
        assert service.id == COORDINATOR
        assert release.drained and release.record["zero_owner_boundary_verified"]
        assert not any(render.owners.values())
        assert len(render.history) == 5
        assert all(env["RENDER_MIGRATION_STAGE"] == "true" for env in render.installed.values())
        assert expected["native_required"] and not expected.get("core_switch")
        assert plan == migration_plan({**V13, "state": "receipted"}, V14)
        assert plan["apply"]
        control_before = deepcopy(release.test_control["receipt"])
        migrations.append(deepcopy(plan))
        boundary.append(deepcopy(render.owners))
        observed.clear()
        observed.update(deepcopy(V14), state="receipted")
        assert release.test_control["receipt"] == control_before
        return {"status": "verified", "target": deepcopy(V14)}

    release.native_migrate = migrate
    release.run()

    assert release.record["status"] == "success" and len(migrations) == 1
    assert len(boundary) == 1 and not any(boundary[0].values())
    assert not release.core_changes and render.counts["native_queued"] == 2
    assert observed == {**V14, "state": "receipted"}
    assert release.record["native_migration"]["verified"]
    assert [entry["command"] for entry in release.record["native_control"]] == ["request", "resume"]
