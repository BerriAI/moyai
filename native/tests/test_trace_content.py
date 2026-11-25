import json

import pytest

from moyai.trace_content import redact_content


def test_structured_credentials_are_redacted_without_removing_neighboring_diagnostics():
    source = {"query": "authentication failures", "token_count": 7, "headers": {
        "Authorization": "Bearer not-a-real-token", "Content-Type": "application/json",
    }, "details": {"access_token": "private-value", "code": "missing_scope"}}
    result = redact_content(json.dumps(source), [])
    output = json.loads(result)
    assert output["query"] == source["query"]
    assert output["token_count"] == 7
    assert output["details"] == {"access_token": "[redacted]", "code": "missing_scope"}
    assert output["headers"] == {"Authorization": "[redacted]", "Content-Type": "application/json"}
    assert source["details"]["access_token"] == "private-value"


@pytest.mark.parametrize("text", [
    'Request failed: password="multi word value"; missing_scope',
    "Request failed: password='multi word value'; missing_scope",
    "Request failed: Bearer private-value; missing_scope",
    "Request failed: Authorization: Basic private-value; missing_scope",
    "Request failed: https://example.com/?token=private-value&error=missing_scope",
    "Request failed: postgresql://user:private-value@localhost/db missing_scope",
    "Request failed: data:image/png;base64,private-value missing_scope",
    "Request failed: clientSecret=private-value missing_scope",
    "Request failed: secret_key=private-value missing_scope",
])
def test_inline_credentials_leave_error_context_readable(text):
    cleaned = redact_content(text, [])
    assert "private-value" not in cleaned
    assert "multi word value" not in cleaned
    assert "Request failed" in cleaned
    assert "missing_scope" in cleaned


def test_configured_secret_with_json_escaping_is_redacted():
    secret = 'private-"quoted"-credential'
    source = json.dumps({"result": f"Request rejected: {secret}"})
    assert json.loads(redact_content(source, [secret]))["result"] == "Request rejected: [redacted]"


@pytest.mark.parametrize("field", ["accessToken", "clientSecret", "apiKey", "privateKey", "secretKey"])
def test_camelcase_credentials_are_redacted_without_erasing_diagnostics(field):
    source = {field: "private-value", "tokenCount": 4, "message": "authentication failed"}
    assert redact_content(source, []) == {
        field: "[redacted]", "tokenCount": 4, "message": "authentication failed",
    }
