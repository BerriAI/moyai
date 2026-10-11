"""Check a dedicated deployment before Lens creates tasks or evaluation runs."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError


class ReadinessError(ValueError):
    """An actionable setup error, with credentials and response bodies omitted."""


@dataclass(frozen=True)
class Deployment:
    url: str
    version: str
    password: str = field(repr=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Deployment":
        url = environ.get("MOYAI_EVAL_URL", "")
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            raise ReadinessError("MOYAI_EVAL_URL must be an HTTPS origin") from None
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or any(character.isspace() or ord(character) < 32 for character in url)
            or "\\" in url
            or port == 0
            or (parsed.scheme != "https" and not (local and parsed.scheme == "http"))
        ):
            raise ReadinessError(
                "MOYAI_EVAL_URL must be an HTTPS origin (loopback HTTP is allowed locally)"
            )
        version = environ.get("LENS_VERSION", "").strip()
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", version):
            raise ReadinessError(
                "LENS_VERSION must be the deployed full lowercase build SHA"
            )
        password = environ.get("MOYAI_EVAL_PASSWORD", "")
        if not password:
            raise ReadinessError(
                "Set MOYAI_EVAL_PASSWORD for the dedicated eval workspace"
            )
        return cls(url.rstrip("/"), version, password)


class EvaluationCapability(BaseModel):
    model_config = ConfigDict(strict=True)
    environment: str = ""
    agent_version: str = ""
    tracing_enabled: bool = False


class WorkspaceConfig(BaseModel):
    model_config = ConfigDict(strict=True)
    cloud_ready: bool = False
    execution_connected: bool = False
    evaluation: EvaluationCapability = EvaluationCapability()


def verify_ready(
    *,
    environ: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Authenticate and read readiness metadata; never submit an agent task."""
    deployment = Deployment.from_env(os.environ if environ is None else environ)
    try:
        with httpx.Client(
            base_url=deployment.url,
            headers={"Origin": deployment.url},
            timeout=30,
            follow_redirects=False,
            transport=transport,
        ) as client:
            response = client.post("/api/login", json={"password": deployment.password})
            response.raise_for_status()
            response = client.get("/api/session")
            response.raise_for_status()
            identity = response.json()
            if (
                not isinstance(identity, dict)
                or identity.get("authenticated") is not True
                or not isinstance(identity.get("csrf"), str)
                or not identity["csrf"]
                or not client.cookies.get("workspace_session")
            ):
                raise ReadinessError(
                    "Moyai did not establish an authenticated workspace session"
                )
            response = client.get("/api/config")
            response.raise_for_status()
            config = WorkspaceConfig.model_validate_json(response.content)
    except httpx.HTTPStatusError as error:
        raise ReadinessError(
            f"Moyai returned HTTP {error.response.status_code}; check the deployment and password login"
        ) from None
    except httpx.HTTPError:
        raise ReadinessError(
            "Could not reach Moyai; check MOYAI_EVAL_URL and network access"
        ) from None
    except (ValidationError, ValueError) as error:
        if isinstance(error, ReadinessError):
            raise
        raise ReadinessError(
            "Moyai returned invalid readiness metadata; deploy the evaluation support first"
        ) from None

    problems = [
        message
        for blocked, message in (
            (
                not config.cloud_ready,
                "Configure the model gateway and cloud sandbox runtime",
            ),
            (
                not config.execution_connected,
                "Moyai's execution worker is disconnected",
            ),
            (
                not config.evaluation.tracing_enabled,
                "Configure Moyai's Lens trace endpoint and tracing key",
            ),
            (
                config.evaluation.environment != "lens-eval",
                "Use a dedicated deployment with TRACE_ENVIRONMENT=lens-eval",
            ),
            (
                config.evaluation.agent_version != deployment.version,
                "MOYAI_BUILD_SHA on the server must match LENS_VERSION",
            ),
        )
        if blocked
    ]
    if problems:
        raise ReadinessError("; ".join(problems))


def main() -> int:
    try:
        verify_ready()
    except ReadinessError as error:
        print(f"Moyai is not ready: {error}")
        return 2
    print(
        "Moyai is ready: worker, trace configuration, eval environment, and build match"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
