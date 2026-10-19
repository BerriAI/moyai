"""Shared accounting regressions; the actual PostgreSQL app proof is separate."""

from __future__ import annotations

import csv
import hashlib
import io
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
import test_product_billing as billing_fixtures
from fastapi import HTTPException
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import create_async_engine
from test_analytics_identity import observation
from test_native_analytics import END, START, ledger
from test_native_settings import client_for, member
from test_product_billing import SCOPE, old_row, seed

from moyai.api import COOKIE
from moyai.models import Session, User
from moyai.product_billing import IdentityMappings, NativeBinding, RequestIdentity, SourceRef
from moyai.product_billing_routes import ProductBillingService

historical_store = billing_fixtures.store


@pytest_asyncio.fixture
async def shared(runtime, actor, historical_store):
    session, turn = await observation(runtime, actor, known=False, model="native-root")
    native = await ledger(runtime, actor, session, turn, model="native-child", cost="0.200000000000000001")
    async with historical_store.begin() as db:
        await db.execute(text("ALTER TABLE runs ADD COLUMN model TEXT DEFAULT 'legacy-root'"))
        await db.execute(text("ALTER TABLE runs ADD COLUMN status TEXT DEFAULT 'completed'"))
        await db.execute(text("ALTER TABLE users ADD COLUMN name TEXT DEFAULT 'Test account'"))
        await db.execute(text("ALTER TABLE users ADD COLUMN email TEXT DEFAULT ''"))
        await db.execute(text("INSERT INTO users(id,kind) VALUES (:id,'google')"), {"id": actor.id})
        await db.execute(text("UPDATE runs SET private_owner_id='another-owner'"))
    # Equal IDs do not prove a shared charge. Both exact amounts must survive.
    await seed(historical_store, old=[old_row(
        id=native.id, user_id=actor.id, model="legacy-child",
        cost="0.123400000000000001", gateway_id="different-legacy-call",
    )])
    source = create_async_engine(
        f"sqlite+aiosqlite:///file:{historical_store.url.database}?mode=ro&uri=true",
    )
    mappings = IdentityMappings(
        "fixture-product-map", "explicit-test-mapping",
        actors={SourceRef(engine, actor.id): actor.id for engine in ("legacy", "native")},
    )

    class FixtureIdentity:
        """Test-only native-cookie authority; actual-product SSO is tested separately."""

        source_engine = source

        @asynccontextmanager
        async def _reader(self, engine):
            async with engine.connect() as db:
                yield db

        def _table(self, name):
            assert name == "users"
            return "users"

        async def authenticate(self, request, *, admin=False, mutation=False):
            token = runtime.security.read_session(request.cookies.get(COOKIE, ""))
            if token is None:
                raise HTTPException(401)
            current = await runtime.database.user(token["user"])
            if not current.enabled or current.auth_version != token["version"]:
                raise HTTPException(401)
            if admin and current.role != "admin":
                raise HTTPException(403)
            return SimpleNamespace(
                id=current.id, role=current.role, individual=True,
                grant=(current.id, current.auth_version),
            )

        async def read_native_user(self, db, principal):
            return await db.get(User, principal.id)

        async def require_current(self, grant):
            current = await runtime.database.user(grant[0])
            if current.auth_version != grant[1]:
                raise HTTPException(401)
            return current

    identity = FixtureIdentity()
    service = ProductBillingService(
        identity, source, runtime.database.engine, mappings=mappings,
        native_bindings={SourceRef("native", native.id): NativeBinding(
            SCOPE, RequestIdentity("broker_request", "different-native-request"), "fixture", "1",
        )},
    )
    runtime.product_billing = service
    runtime.product_identity = identity
    try:
        yield SimpleNamespace(service=service, native=native, turn=turn, source=historical_store)
    finally:
        runtime.product_billing = None
        runtime.product_identity = None
        await source.dispose()


def params(**extra):
    return {"preset": "custom", "start": START, "end": END, **extra}


async def test_usage_financial_metrics_and_csv_share_full_billing_projection(runtime, actor, shared):
    billing = await shared.service.report(**params(), viewer_id=actor.id)
    assert billing["total"]["cost_usd_exact"] == "0.323400000000000002"
    assert billing["total"]["requests"] == 2
    assert billing["coverage"]["private_details_omitted"] == 1
    async with client_for(runtime, actor) as client:
        response = await client.get("/api/settings/usage", params=params())
        assert response.status_code == 200, response.text
        usage = response.json()
        assert usage["capabilities"]["unified_billing"]
        assert usage["summary"]["cost_usd_exact"] == billing["total"]["cost_usd_exact"]
        for key in ("requests", "input_tokens", "output_tokens", "total_tokens", "pricing_status"):
            assert usage["summary"][key] == billing["total"][key]
        assert usage["summary"]["sessions_active"] == 1  # Native activity has its own scope.
        assert usage["summary"]["unknown_request_turns"] == 0
        assert usage["coverage"]["legacy_observations"] == usage["coverage"]["native_observations"] == 1
        assert usage["models"] == ["legacy-root", "native-root"]
        assert {row["model"] for row in usage["by_model"]} == {"legacy-child", "native-child"}
        assert usage["by_user"][0]["cost_usd_exact"] == billing["total"]["cost_usd_exact"]
        assert sum(Decimal(row["cost_usd_exact"] or "0") for row in usage["series"]) == Decimal(
            billing["total"]["cost_usd_exact"],
        )
        for export in ("daily", "users", "models"):
            result = await client.get("/api/settings/usage", params=params(format="csv", export=export))
            assert result.status_code == 200
            rows = list(csv.DictReader(io.StringIO(result.text)))
            assert sum(Decimal(row["cost_usd_exact"] or "0") for row in rows) == Decimal(
                billing["total"]["cost_usd_exact"],
            )
            assert "historical-child" not in result.text and "historical-root" not in result.text
    async with shared.source.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM model_requests")) == 1


async def test_usage_root_filters_match_shared_billing_cohorts(runtime, actor, shared):
    async with client_for(runtime, actor) as client:
        for model in ("legacy-root", "native-root", "does-not-exist"):
            billing = await shared.service.report(
                **params(), root_model=model, turn_status="completed", user_id=actor.id,
            )
            response = await client.get("/api/settings/usage", params=params(
                model=model, status="completed", user_id=actor.id,
            ))
            assert response.status_code == 200, response.text
            assert response.json()["summary"]["cost_usd_exact"] == billing["total"]["cost_usd_exact"]
            assert response.json()["summary"]["requests"] == billing["total"]["requests"]
        excluded = await client.get("/api/settings/usage", params=params(status="failed"))
        assert excluded.status_code == 200
        assert excluded.json()["summary"]["requests"] == 0
    # Performance remains a native activity report and does not adopt billing filters.
    async with client_for(runtime, actor) as client:
        performance = await client.get("/api/settings/performance", params=params())
        assert performance.status_code == 200
        assert not performance.json()["capabilities"]["unified_billing"]
        assert performance.json()["summary"]["requests"] == 1


async def test_billing_failure_never_falls_back_to_native_subtotal(runtime, actor, shared, monkeypatch):
    async def unavailable(**kwargs):
        raise TimeoutError

    monkeypatch.setattr(shared.service, "report", unavailable)
    async with client_for(runtime, actor) as client:
        response = await client.get("/api/settings/usage", params=params())
        assert response.status_code == 503
        assert response.json() == {
            "detail": "The accounting database did not respond. Please try again.",
        }
    ordinary = await member(runtime)
    async with client_for(runtime, ordinary) as client:
        assert (await client.get("/api/settings/usage", params=params())).status_code == 403


async def test_private_activity_ids_are_omitted_without_erasing_accounting(runtime, actor, shared):
    async with runtime.database.sessions.begin() as db:
        await db.execute(update(Session).where(Session.id == shared.turn.session_id).values(visibility="private"))
    viewer = await member(runtime, role="admin")
    async with client_for(runtime, viewer) as client:
        response = await client.get("/api/settings/usage", params=params())
        assert response.status_code == 200
        report = response.json()
        assert report["summary"]["cost_usd_exact"] == "0.323400000000000002"
        assert report["rows"] == [] and report["total"] == 0
        assert report["coverage"]["private_activity_details_omitted"] == 1
        exported = await client.get("/api/settings/usage", params=params(format="csv", export="turns"))
        assert exported.status_code == 200 and shared.turn.id not in exported.text


async def test_activity_uses_supplied_accounting_snapshot_during_cost_recovery(runtime, actor, shared):
    from moyai.analytics_service import report

    financial = await shared.service.report(**params(), viewer_id=actor.id, include_root_cohorts=True)
    async with shared.source.begin() as db:
        await db.execute(text("UPDATE model_requests SET cost='99'"))
    activity = await report(
        runtime, actor, START, END, None, None, None, 100, 0, "json",
        billing=shared.service, billing_data=financial,
    )
    assert activity["summary"]["cost_usd_exact"] == financial["total"]["cost_usd_exact"]
    assert activity["by_user"][0]["cost_usd_exact"] == financial["total"]["cost_usd_exact"]
    latest = await shared.service.report(**params(), viewer_id=actor.id)
    assert latest["total"]["cost_usd_exact"] != financial["total"]["cost_usd_exact"]


async def test_canonical_endpoint_supplies_dashboard_and_exports_from_one_read(runtime, actor, shared, monkeypatch):
    original = shared.service.report
    reads = 0

    async def counted(**kwargs):
        nonlocal reads
        reads += 1
        return await original(**kwargs)

    monkeypatch.setattr(shared.service, "report", counted)
    async with client_for(runtime, actor) as client:
        response = await client.get("/api/settings/billing", params=params(include_activity="true"))
        assert response.status_code == 200, response.text
        body = response.json()
        assert reads == 1
        assert body["total"]["cost_usd_exact"] == body["activity"]["summary"]["cost_usd_exact"]
        alias = await client.get("/api/settings/usage", params=params())
        assert alias.status_code == 200
        assert alias.json()["summary"] == body["activity"]["summary"]
        for export in ("users", "daily", "models", "turns"):
            exported = await client.get("/api/settings/billing", params=params(
                include_activity="true", format="csv", export=export,
            ))
            assert exported.status_code == 200
            assert exported.headers["cache-control"] == "no-store"
            if export != "turns":
                rows = list(csv.DictReader(io.StringIO(exported.text)))
                assert sum(Decimal(row["cost_usd_exact"] or "0") for row in rows) == Decimal(
                    body["total"]["cost_usd_exact"],
                )
        filtered = await client.get("/api/settings/billing", params=params(
            include_activity="true", root_model="legacy-root", turn_status="completed",
        ))
        assert filtered.status_code == 200
        assert filtered.json()["total"]["requests"] == filtered.json()["activity"]["summary"]["requests"] == 1
    ordinary = await member(runtime)
    async with client_for(runtime, ordinary) as client:
        assert (await client.get("/api/settings/billing", params=params(include_activity="true"))).status_code == 403
        assert (await client.get("/api/spend", params=params(include_activity="true"))).status_code == 403


async def test_explicit_accounting_map_groups_activity_before_distinct_counts(runtime, actor, shared):
    other = await member(runtime)
    async with runtime.database.sessions() as db:
        session = await db.get(Session, shared.turn.session_id)
    _, turn = await observation(runtime, other, session=session, known=False, model="native-root")
    native = await ledger(runtime, other, session, turn, model="native-child", cost="0.3")
    shared.service.mappings = IdentityMappings(
        "fixture-combined-people", "explicit-test-mapping",
        actors={**shared.service.mappings.actors, SourceRef("native", other.id): actor.id},
    )
    shared.service.native_bindings = {
        **shared.service.native_bindings,
        SourceRef("native", native.id): NativeBinding(
            SCOPE, RequestIdentity("broker_request", "second-native-request"), "fixture", "1",
        ),
    }
    async with client_for(runtime, actor) as client:
        for query in ({}, {"user_id": actor.id}, {"user_id": other.id}):
            response = await client.get("/api/settings/usage", params=params(**query))
            assert response.status_code == 200
            body = response.json()
            assert len(body["by_user"]) == 1
            person, = body["by_user"]
            assert person["user_id"] == actor.id and person["turns"] == 2
            assert person["sessions_active"] == 1  # Both actors used the same session.
            assert person["requests"] == 3
            assert person["cost_usd_exact"] == body["summary"]["cost_usd_exact"] == "0.623400000000000002"


@pytest.mark.parametrize("bound", [True, False])
@pytest.mark.parametrize("target", [None, "mapped-person"])
async def test_verified_product_mirror_shares_billing_activity_and_filters(runtime, actor, shared, bound, target):
    async with runtime.database.sessions.begin() as db:
        await db.execute(update(User).where(User.id == actor.id).values(
            username="product:" + hashlib.sha256(actor.id.encode()).hexdigest(),
        ))
    shared.service.mappings = IdentityMappings(
        "test-map", "explicit-test-mapping",
        actors={SourceRef("legacy", actor.id): target} if target else {},
    )
    original = shared.service.mappings
    if not bound:
        shared.service.native_bindings = {}
    canonical = target or f"legacy:{actor.id}"
    expected = "0.323400000000000002" if bound else "0.123400000000000001"
    async with client_for(runtime, actor) as client:
        for path in ("/api/settings/billing", "/api/settings/usage"):
            for selected in (None, actor.id, canonical):
                response = await client.get(path, params=params(
                    **({"include_activity": "true"} if path.endswith("billing") else {}),
                    **({"user_id": selected} if selected else {}),
                ))
                assert response.status_code == 200, response.text
                value = response.json()
                activity = value.get("activity", value)
                person, = activity["by_user"]
                assert person["user_id"] == canonical
                assert person["turns"] == person["sessions_active"] == 1
                assert person["cost_usd_exact"] == activity["summary"]["cost_usd_exact"] == expected
                assert person["requests"] == (2 if bound else 1)
                assert person["unknown_requests"] == (0 if bound else 1)
                if "activity" in value:
                    assert [row["id"] for row in value["users"]] == [canonical]
                    assert value["total"]["cost_usd_exact"] == expected
    # Report attribution is request-local, never a mutation of the configured mappings.
    assert shared.service.mappings is original


async def test_unverified_equal_ids_remain_separate_in_canonical_report(runtime, actor, shared):
    shared.service.mappings = IdentityMappings("test-map", "source-namespaces")
    async with client_for(runtime, actor) as client:
        response = await client.get("/api/settings/billing", params=params(include_activity="true"))
        assert response.status_code == 200
        value = response.json()
        people = {row["user_id"]: row for row in value["activity"]["by_user"]}
        assert set(people) == {f"legacy:{actor.id}", f"native:{actor.id}"}
        assert people[f"native:{actor.id}"]["turns"] == 1
        assert people[f"native:{actor.id}"]["cost_usd_exact"] == "0.200000000000000001"
        assert people[f"legacy:{actor.id}"]["cost_usd_exact"] == "0.123400000000000001"


async def test_verified_linked_alias_keeps_explicit_native_override(runtime, actor, shared):
    other = await member(runtime)
    async with runtime.database.sessions.begin() as db:
        for identity in (actor.id, other.id):
            await db.execute(update(User).where(User.id == identity).values(
                username="product:" + hashlib.sha256(identity.encode()).hexdigest(),
            ))
    async with shared.source.begin() as db:
        await db.execute(text("INSERT INTO users(id,kind,linked_user_id) VALUES (:id,'slack',:target)"),
                         {"id": other.id, "target": actor.id})
    session, turn = await observation(runtime, other, known=False)
    async with runtime.database.sessions.begin() as db:
        await db.execute(update(Session).where(Session.id == session.id).values(visibility="private"))
    native = await ledger(runtime, other, session, turn, cost="0.3")
    shared.service.mappings = IdentityMappings("override", "configured-override", actors={
        SourceRef("native", other.id): "separate-account",
    })
    shared.service.native_bindings = {
        **shared.service.native_bindings,
        SourceRef("native", native.id): NativeBinding(
            SCOPE, RequestIdentity("broker_request", "linked-native-request"), "fixture", "1",
        ),
    }
    async with client_for(runtime, actor) as client:
        for selected in (other.id, "separate-account"):
            response = await client.get("/api/settings/billing", params=params(
                include_activity="true", user_id=selected,
            ))
            assert response.status_code == 200, response.text
            value = response.json()
            person, = value["activity"]["by_user"]
            assert person["user_id"] == "separate-account"
            assert person["turns"] == person["requests"] == 1
            assert person["cost_usd_exact"] == value["total"]["cost_usd_exact"] == "0.3"
    # Removing the override restores the verified saved link in both views.
    shared.service.mappings = IdentityMappings("empty", "source-namespaces")
    async with client_for(runtime, actor) as client:
        response = await client.get("/api/settings/billing", params=params(include_activity="true"))
        assert response.status_code == 200, response.text
        value = response.json()
        person, = value["activity"]["by_user"]
        assert person["turns"] == 2 and person["requests"] == 3
        assert person["cost_usd_exact"] == value["total"]["cost_usd_exact"] == "0.623400000000000002"
        assert native.id not in str(value["request_details"])
        assert turn.id not in str(value["activity"]["rows"])
