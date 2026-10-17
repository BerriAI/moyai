from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parents[1] / "tests"
sys.path.insert(0, str(TESTS))

pytest_plugins = ["product_database_fixtures", "test_product_authority"]


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--run-evals", action="store_true", help="Run the live Moyai agent evals.")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live_eval: requires the configured gateway and PostgreSQL.")


def pytest_sessionstart(session: pytest.Session) -> None:
    if not session.config.getoption("--run-evals"):
        return
    if not os.getenv("MOYAI_TEST_POSTGRES"):
        raise pytest.UsageError("--run-evals requires MOYAI_TEST_POSTGRES.")
    base = (
        os.getenv("LITELLM_API_BASE") or os.getenv("MOYAI_EVAL_LITELLM_API_BASE")
        or os.getenv("LITELLM_GATEWAY_BASE_URL")
    )
    key = (
        os.getenv("LITELLM_API_KEY") or os.getenv("MOYAI_EVAL_LITELLM_API_KEY")
        or os.getenv("LITELLM_GATEWAY_ADMIN_KEY")
    )
    judge = os.getenv("MOYAI_EVAL_JUDGE_MODEL")
    if not base or not key or not judge:
        raise pytest.UsageError(
            "--run-evals requires LITELLM_API_BASE, LITELLM_API_KEY, and MOYAI_EVAL_JUDGE_MODEL."
        )
    base = base.rstrip("/")
    os.environ["LITELLM_API_BASE"] = base if base.endswith("/v1") else base + "/v1"
    os.environ["LITELLM_API_KEY"] = key


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-evals"):
        return
    skip = pytest.mark.skip(reason="Pass --run-evals to run the live model eval suite.")
    for item in items:
        if item.get_closest_marker("live_eval"):
            item.add_marker(skip)
