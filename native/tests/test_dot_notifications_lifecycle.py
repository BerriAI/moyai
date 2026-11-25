"""Local rehearsal starts the existing Slack service; production retains ownership."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from moyai.api import create_app


@pytest.mark.parametrize(("role", "local", "dispatch", "expected"), [
    ("standalone", True, True, 1),
    ("api", True, True, 0),
    ("standalone", False, True, 0),
    ("standalone", True, False, 0),
])
async def test_slack_lifecycle_is_owned_only_by_combined_local_rehearsal(runtime, role, local, dispatch, expected):
    app = create_app(runtime, run_dispatcher=dispatch)
    service = SimpleNamespace(start=Mock(), close=AsyncMock())
    original = runtime.product_config
    runtime.product_config = SimpleNamespace(role=role, local_rehearsal=local)
    runtime.product_slack = service
    try:
        async with app.router.lifespan_context(app):
            assert service.start.call_count == expected
            assert service.close.await_count == 0
        assert service.close.await_count == expected
    finally:
        runtime.product_config = original
        runtime.product_slack = None
