"""Provider contract diagnostics must not expose the provider's response data."""

import pytest
from pydantic_ai.exceptions import ToolFailed

from moyai.github_workflow_types import RepositoryIdentity
from moyai.product_github_workflows import parse


def test_identity_failure_logs_contract_and_field_without_provider_values(caplog):
    with pytest.raises(ToolFailed, match="expected repository"):
        parse(RepositoryIdentity, {
            "id": "private-provider-value", "full_name": "owner/repo",
            "default_branch": "main", "access_token": "never-log-this-token",
        })
    assert "schema=RepositoryIdentity" in caplog.text
    assert "node_id" in caplog.text and "missing" in caplog.text
    assert "int_type" in caplog.text
    assert "private-provider-value" not in caplog.text
    assert "never-log-this-token" not in caplog.text
