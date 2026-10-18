"""Allocate a process's PostgreSQL allowance without opening any connections.

Existing-product runtimes have four independent pools. Reserve five additional
connections conservatively in every role: ownership, dispatch LISTEN, model-slot
ownership and the two bounded workspace-restoration locks. Some roles do not
use all five. Native standalone has two pools and one dispatch listener.
"""
from dataclasses import asdict, dataclass

CONNECTION_BUDGET_PROTOCOL = 1
DEFAULT_CONNECTION_BUDGET = 17
EXISTING_POOL_COUNT = 4
EXISTING_UNPOOLED_RESERVE = 5
STANDALONE_POOL_COUNT = 2
STANDALONE_UNPOOLED_RESERVE = 1


@dataclass(frozen=True)
class ConnectionBudget:
    limit: int
    requested_pool_size: int
    pool_count: int
    pool_size: int
    unpooled_reserve: int

    @property
    def max_connections(self) -> int:
        return self.pool_count * self.pool_size + self.unpooled_reserve

    def as_dict(self) -> dict[str, int]:
        return {
            "protocol": CONNECTION_BUDGET_PROTOCOL, **asdict(self),
            "max_connections": self.max_connections,
        }


def connection_budget(pool_size: int, limit: int, *, existing: bool) -> ConnectionBudget:
    pools = EXISTING_POOL_COUNT if existing else STANDALONE_POOL_COUNT
    reserve = EXISTING_UNPOOLED_RESERVE if existing else STANDALONE_UNPOOLED_RESERVE
    if type(pool_size) is not int or pool_size < 1:
        raise ValueError("MOYAI_DATABASE_POOL_SIZE must be a positive integer.")
    if type(limit) is not int or limit < pools + reserve:
        raise ValueError(
            "MOYAI_DATABASE_CONNECTION_BUDGET must cover the runtime's pools and dedicated connections."
        )
    return ConnectionBudget(
        limit=limit, requested_pool_size=pool_size, pool_count=pools,
        pool_size=min(pool_size, (limit - reserve) // pools), unpooled_reserve=reserve,
    )
