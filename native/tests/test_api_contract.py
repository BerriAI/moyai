"""The frontend's generated client is only as honest as the OpenAPI document.

See .agents/skills/e2e-typesafety/SKILL.md. These allowlists may only shrink.
"""

from __future__ import annotations

import re
import warnings
from collections import defaultdict
from pathlib import Path

from fastapi.routing import _IncludedRouter

from moyai.openapi_dump import schema_app

ROOT = Path(__file__).resolve().parents[2]

# Routes that do not return JSON: downloads, streams, redirects, webhooks, static files.
NOT_JSON = {
    ("GET", "/api/files/{artifact_id}"),
    ("GET", "/api/product/sessions/{run_id}/continuation/archive/{member_id}"),
    ("GET", "/api/product/skills/{skill_id}/files/{path}"),
    ("GET", "/api/runs/{run_id}/artifact"),
    ("GET", "/api/sessions/{session_id}/debug-trace"),
    ("GET", "/api/sessions/{session_id}/file-preview/{kind}/{resource_id}/content"),
    ("GET", "/api/sessions/{session_id}/file-preview/{kind}/{resource_id}/download"),
    ("GET", "/api/sessions/{session_id}/messages/{message_id}/source"),
    ("GET", "/api/uploads/{upload_id}"),
    ("GET", "/auth/github/register"),
    ("GET", "/auth/google/callback"),
    ("POST", "/broker/native/v1/{route}"),
    ("POST", "/hooks/automations/{identity}"),
    ("POST", "/hooks/automations/{identity}/{provider}"),
    ("POST", "/hooks/slack"),
    ("POST", "/hooks/slack/events"),
    ("POST", "/hooks/slack/interactions"),
    ("GET", "/oauth/github/app-callback"),
    ("GET", "/oauth/github/callback"),
    ("GET", "/oauth/{provider}/callback"),
    ("GET", "/static/models/moyai.glb"),
    ("GET", "/{path}"),
}

# Frontend modules that may still call the untyped api<T>() transport.
LEGACY_CALLERS = {"native/web/api.ts", "native/web/debug-trace.ts"}


def _routes(routes):
    for route in routes:
        if isinstance(route, _IncludedRouter):
            yield from _routes(route.effective_candidates())
        else:
            yield route


def test_every_json_operation_has_a_response_schema():
    with schema_app() as app, warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        document = app.openapi()
    untyped = set()
    for path, item in document["paths"].items():
        for method, operation in item.items():
            ok = next((value for code, value in operation["responses"].items() if code.startswith("2")), {})
            content = ok.get("content", {})
            schema = (content.get("application/json") or content.get("text/event-stream") or {}).get("schema")
            if not schema:
                untyped.add((method.upper(), path))
    assert untyped - NOT_JSON == set(), "Give these routes a Pydantic response model"
    assert NOT_JSON - untyped == set(), "These routes are typed now; remove them from NOT_JSON"


def test_one_handler_per_operation():
    with schema_app() as app:
        handlers = defaultdict(list)
        for route in _routes(app.routes):
            for method in getattr(route, "methods", None) or ():
                if method != "HEAD":
                    handlers[(method, route.path)].append(route.endpoint.__qualname__)
    # The document can describe only one handler per operation; a second one
    # (e.g. a product-mode override) would serve untyped responses.
    shadowed = {key: endpoints for key, endpoints in handlers.items() if len(endpoints) > 1}
    assert shadowed == {}


def test_frontend_calls_the_generated_client():
    legacy = re.compile(r"\bapi(?:Request)?(?:<[^>()]*>)?\(")
    callers = {
        str(path.relative_to(ROOT)) for path in (ROOT / "native/web").glob("*.ts*")
        if legacy.search(path.read_text())
    }
    assert callers - LEGACY_CALLERS == set(), "Call the generated SDK in native/web/generated/api instead"
