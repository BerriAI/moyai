import json

import httpx
import pytest

from evals.preflight import Deployment, ReadinessError, verify_ready

BUILD = "a" * 40
ENV = {
    "MOYAI_EVAL_URL": "https://moyai.example",
    "MOYAI_EVAL_PASSWORD": "workspace-password",
    "LENS_VERSION": BUILD,
}
CAPABILITY = {
    "environment": "lens-eval",
    "agent_version": BUILD,
    "tracing_enabled": True,
}


def workspace(*, config=None, identity=None, cookie=True, login_status=200):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["Origin"] == "https://moyai.example"
        if request.url.path == "/api/login":
            assert request.method == "POST"
            assert json.loads(request.content) == {
                "password": ENV["MOYAI_EVAL_PASSWORD"]
            }
            headers = (
                {
                    "Set-Cookie": "workspace_session=test-session; Path=/; HttpOnly; Secure"
                }
                if cookie
                else {}
            )
            return httpx.Response(login_status, headers=headers, json={})
        assert request.method == "GET", "Preflight must never submit a task"
        if cookie:
            assert request.headers["Cookie"] == "workspace_session=test-session"
        if request.url.path == "/api/session":
            return httpx.Response(
                200,
                json=(
                    identity
                    if identity is not None
                    else {"authenticated": True, "csrf": "test-csrf"}
                ),
            )
        assert request.url.path == "/api/config"
        return httpx.Response(
            200,
            json={
                "cloud_ready": True,
                "execution_connected": True,
                "evaluation": CAPABILITY,
                **(config or {}),
            },
        )

    return httpx.MockTransport(handler), requests


def test_should_verify_deployment_without_creating_tasks_or_lens_runs():
    transport, requests = workspace()
    verify_ready(environ=ENV, transport=transport)
    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/api/login"),
        ("GET", "/api/session"),
        ("GET", "/api/config"),
    ]
    assert ENV["MOYAI_EVAL_PASSWORD"] not in repr(Deployment.from_env(ENV))


@pytest.mark.parametrize(
    "config, reason",
    [
        ({"cloud_ready": False}, "cloud sandbox runtime"),
        ({"execution_connected": False}, "worker is disconnected"),
        (
            {"evaluation": {**CAPABILITY, "environment": "production"}},
            "TRACE_ENVIRONMENT=lens-eval",
        ),
        (
            {"evaluation": {**CAPABILITY, "agent_version": "b" * 40}},
            "must match LENS_VERSION",
        ),
        (
            {"evaluation": {**CAPABILITY, "agent_version": ""}},
            "must match LENS_VERSION",
        ),
        (
            {"evaluation": {**CAPABILITY, "tracing_enabled": False}},
            "Lens trace endpoint",
        ),
        ({"evaluation": {}}, "TRACE_ENVIRONMENT=lens-eval"),
        ({"cloud_ready": "true"}, "invalid readiness metadata"),
        (
            {"evaluation": {**CAPABILITY, "tracing_enabled": "yes"}},
            "invalid readiness metadata",
        ),
    ],
)
def test_should_reject_unready_or_mislabeled_deployments(config, reason):
    transport, requests = workspace(config=config)
    with pytest.raises(ReadinessError, match=reason):
        verify_ready(environ=ENV, transport=transport)
    assert len(requests) == 3


@pytest.mark.parametrize(
    "options",
    [
        {"login_status": 401},
        {"login_status": 302},
        {"identity": {"authenticated": False, "csrf": "test-csrf"}},
        {"identity": {"authenticated": True, "csrf": ""}},
        {"identity": {"authenticated": "true", "csrf": "test-csrf"}},
        {"identity": []},
        {"cookie": False},
    ],
)
def test_should_reject_missing_authentication_before_reading_configuration(options):
    transport, requests = workspace(**options)
    with pytest.raises(ReadinessError):
        verify_ready(environ=ENV, transport=transport)
    assert all(request.url.path != "/api/config" for request in requests)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://moyai.example/api",
        "http://moyai.example",
        "https://user:secret@moyai.example",
        "https://moyai.example?token=secret",
        "https://moyai.example#secret",
        "https://moyai.example:invalid",
        "https://moyai.example:0",
        "https://moyai.example\\evil",
        "https://moyai.example\n",
        "//moyai.example",
    ],
)
def test_should_reject_unsafe_origins_before_contacting_server(url):
    def handler(request):
        pytest.fail("Invalid configuration must fail before any HTTP request")

    with pytest.raises(ReadinessError, match="MOYAI_EVAL_URL"):
        verify_ready(
            environ={**ENV, "MOYAI_EVAL_URL": url},
            transport=httpx.MockTransport(handler),
        )


@pytest.mark.parametrize("version", ["", "main", "abc1234", "A" * 40, "a" * 41])
def test_should_require_explicit_full_deployed_build(version):
    with pytest.raises(ReadinessError, match="full lowercase build SHA"):
        Deployment.from_env({**ENV, "LENS_VERSION": version})


def test_should_accept_sha256_build_and_loopback_origin_for_local_readiness():
    deployment = Deployment.from_env(
        {**ENV, "MOYAI_EVAL_URL": "http://127.0.0.1:8000/", "LENS_VERSION": "b" * 64}
    )
    assert deployment.url == "http://127.0.0.1:8000"
    assert deployment.version == "b" * 64


def test_should_require_dedicated_workspace_password():
    with pytest.raises(ReadinessError, match="MOYAI_EVAL_PASSWORD"):
        Deployment.from_env({**ENV, "MOYAI_EVAL_PASSWORD": ""})


def test_should_hide_transport_details_and_credentials_from_errors():
    def handler(request):
        raise httpx.ConnectError("secret-from-transport", request=request)

    with pytest.raises(ReadinessError, match="Could not reach Moyai") as caught:
        verify_ready(environ=ENV, transport=httpx.MockTransport(handler))
    assert "secret-from-transport" not in str(caught.value)
    assert ENV["MOYAI_EVAL_PASSWORD"] not in str(caught.value)


def test_should_reject_invalid_server_json_without_echoing_body():
    def handler(request):
        if request.url.path == "/api/login":
            return httpx.Response(
                200, headers={"Set-Cookie": "workspace_session=test; Path=/"}, json={}
            )
        return httpx.Response(200, text="secret-response-body")

    with pytest.raises(ReadinessError, match="invalid readiness metadata") as caught:
        verify_ready(environ=ENV, transport=httpx.MockTransport(handler))
    assert "secret-response-body" not in str(caught.value)
