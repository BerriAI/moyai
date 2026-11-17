import httpx2
import pytest

from moyai.inference_identity import attribute_request


@pytest.mark.parametrize("organization", ["", "  ", "moyai", " moyai "])
def test_attribution_replaces_caller_customer_scope(organization):
    request = httpx2.Request("POST", "https://gateway.invalid/v1/responses", headers={
        "x-litellm-customer-id": "another-person",
        "x-litellm-customer-member-id": "another-member",
        "x-litellm-customer-org-alias": "another-org",
        "x-litellm-customer-team-id": "another-team",
    })
    attribute_request(request, actor_id="google:123", email=" Alice@Example.test ", organization=organization)
    assert "x-litellm-customer-team-id" not in request.headers
    if organization.strip():
        assert "x-litellm-customer-id" not in request.headers
        assert request.headers["x-litellm-customer-org-alias"] == "moyai"
        assert request.headers["x-litellm-customer-member-id"] == "moyai:alice@example.test"
    else:
        assert request.headers["x-litellm-customer-id"] == "alice@example.test"
        assert "x-litellm-customer-org-alias" not in request.headers
        assert "x-litellm-customer-member-id" not in request.headers


def test_non_email_accounts_have_stable_distinct_header_safe_identities():
    requests = [httpx2.Request("POST", "https://gateway.invalid/v1/responses") for _ in range(2)]
    for request, actor in zip(requests, ["first-user", "second\r\n用户"], strict=True):
        attribute_request(request, actor_id=actor, organization="moyai")
    assert requests[0].headers["x-litellm-customer-member-id"] == "moyai:moyai:first-user"
    assert requests[1].headers["x-litellm-customer-member-id"] == "moyai:moyai:second%0D%0A%E7%94%A8%E6%88%B7"
    assert all("x-litellm-customer-id" not in request.headers for request in requests)
