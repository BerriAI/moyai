"""Server-owned LiteLLM attribution for one admitted inference request."""

from __future__ import annotations

from urllib.parse import quote

import httpx2


def attribute_request(
    request: httpx2.Request, *, actor_id: str, organization: str, email: str = "",
) -> None:
    """Replace caller attribution; portal member IDs are scoped to the deployment's org."""
    customer = email.strip().lower() or f"moyai:{actor_id}"
    # HTTP headers are ASCII. Keep ordinary email addresses readable while
    # encoding Unicode and control characters rather than accepting header injection.
    customer = quote(customer, safe="@:+._-")
    for header in (
        "x-litellm-customer-id", "x-litellm-customer-member-id",
        "x-litellm-customer-org-alias", "x-litellm-customer-team-id",
    ):
        request.headers.pop(header, None)
    if organization := organization.strip():
        request.headers["x-litellm-customer-org-alias"] = organization
        request.headers["x-litellm-customer-member-id"] = f"{organization}:{customer}"
    else:
        request.headers["x-litellm-customer-id"] = customer
