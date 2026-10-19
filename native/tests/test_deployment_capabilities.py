"""Source configuration bounds are distinct from measured database headroom."""

from __future__ import annotations

import json

import psycopg
import pytest
from test_product_deployment import configured
from test_product_deployment import environment as product_environment

from moyai.deployment_capabilities import capabilities

environment = product_environment


@pytest.mark.parametrize("role,extra", [("api", 0), ("worker", 0), ("coordinator", 1), ("broker", 1)])
@pytest.mark.parametrize("pool_size", [1, 8])
def test_capacity_accounts_for_separate_pools_restore_locks_and_role_connections(
    environment, monkeypatch, role, extra, pool_size,
):
    def forbidden(*args, **kwargs):
        pytest.fail("A capability calculation must not connect to production.")

    monkeypatch.setattr(psycopg, "connect", forbidden)
    config = configured({
        **environment, "MOYAI_RUNTIME_ROLE": role, "MOYAI_DATABASE_POOL_SIZE": str(pool_size),
        "MOYAI_BUILD_SHA": "a" * 40, "MOYAI_WORKER_DEPLOYMENT": "release-fleet",
        "MOYAI_WORKER_BUILD_ID": "release-build", "MOYAI_WORKER_SHUTDOWN_SECONDS": "240",
    })
    value = capabilities(config)
    # Four independently bounded pools, one canonical owner, two unpooled
    # restore-lock connections, plus the role-specific dedicated connection.
    assert value["connections"]["configured_bound"] == 4 * pool_size + 3 + extra
    assert value["connections"]["operator_and_platform_reserve_included"] is False
    assert value["worker"]["deployment"] == "release-fleet" and value["worker"]["build"] == "release-build"
    serialized = json.dumps(value)
    for secret in (
        environment["LITELLM_API_KEY"], environment["ENCRYPTION_KEY"],
        environment["SESSION_SECRET"], environment["MOYAI_DATABASE_URL"],
    ):
        assert secret not in serialized


def test_standalone_is_not_advertised_as_a_qualified_fleet_role():
    with pytest.raises(ValueError, match="distributed"):
        capabilities(configured({"MOYAI_CORE": "pydantic"}))
