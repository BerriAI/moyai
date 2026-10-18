import pytest

from app.config import Settings
from app.database_budget import connection_budget


def test_existing_pools_share_one_allowance_with_dedicated_connection_headroom():
    budget = connection_budget(8, 17, existing=True)
    assert budget.pool_size == 3
    assert budget.pool_count == 4
    assert budget.unpooled_reserve == 5
    assert budget.max_connections == 17
    assert 5 * budget.max_connections + 10 <= 100


def test_standalone_keeps_two_eight_connection_pools_and_listener():
    budget = connection_budget(8, 17, existing=False)
    assert budget.pool_count == 2 and budget.pool_size == 8
    assert budget.max_connections == 17


@pytest.mark.parametrize("requested,limit,expected", [(1, 17, 1), (8, 9, 1), (8, 13, 2), (8, 100, 8)])
def test_per_pool_setting_remains_a_ceiling(requested, limit, expected):
    budget = connection_budget(requested, limit, existing=True)
    assert budget.pool_size == expected
    assert budget.max_connections <= limit


@pytest.mark.parametrize("size,limit", [(0, 17), (True, 17), (8, 8), (8, False), (8, "17")])
def test_invalid_product_budget_is_rejected_before_any_connection(size, limit):
    with pytest.raises(ValueError):
        connection_budget(size, limit, existing=True)


def test_product_configuration_accepts_explicit_process_budget():
    settings = Settings(_env_file=None, moyai_database_connection_budget=13)
    assert settings.moyai_database_connection_budget == 13
    with pytest.raises(ValueError):
        Settings(_env_file=None, moyai_database_connection_budget=8)
