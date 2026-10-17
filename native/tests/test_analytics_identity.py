"""Linked-person report regressions; synthetic records, no provider calls."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
from test_native_analytics import END, START, ledger
from test_product_identity import settings_for

from moyai.analytics import utc
from moyai.analytics_identity import report_identities
from moyai.analytics_service import report
from moyai.inference_usage import InferenceUsage
from moyai.models import Effect, Session, Turn, User, new_id
from moyai.pr_publications import PRPublication
from moyai.product_identity import ProductIdentityAdapter

WEB, SLACK, OTHER, UNLINKED = "google:person", "slack:team:person", "google:other", "slack:team:other"


@pytest_asyncio.fixture
async def linked(runtime, tmp_path):
    path = tmp_path / "accounts.db"
    writer = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with writer.begin() as db:
        await db.execute(text(
            "CREATE TABLE users (id TEXT PRIMARY KEY, kind TEXT, name TEXT, email TEXT, linked_user_id TEXT)"
        ))
        await db.execute(text("INSERT INTO users VALUES (:id,:kind,:name,:email,:linked)"), [
            {"id": identity, "kind": identity.split(":")[0], "name": "Same Name",
             "email": "person@example.test", "linked": WEB if identity == SLACK else None}
            for identity in (WEB, SLACK, OTHER, UNLINKED)
        ])
    source = create_async_engine(f"sqlite+aiosqlite:///file:{path}?mode=ro&uri=true")
    runtime.product_identity = ProductIdentityAdapter(settings_for(tmp_path), source)
    people = {
        identity: User(id=identity, username="product:" + hashlib.sha256(identity.encode()).hexdigest(),
                       display_name="Same Name", password="!product!", role="member")
        for identity in (WEB, SLACK, OTHER, UNLINKED)
    }
    async with runtime.database.sessions.begin() as db:
        db.add_all(people.values())
    try:
        yield SimpleNamespace(people=people, writer=writer)
    finally:
        runtime.product_identity = None
        await source.dispose()
        await writer.dispose()


async def observation(runtime, owner, *, session=None, cost="0.1", model="test", known=True):
    session = session or Session(
        id=new_id(), owner_id=owner.id, model=model, created_at=START, updated_at=START,
    )
    turn = Turn(
        id=new_id(), session_id=session.id, actor_id=owner.id, model=model, status="completed",
        request_key=new_id(), request_fingerprint="synthetic", workflow_id=new_id(),
        prompt=runtime.security.seal("PRIVATE_INPUT"), created_at=START, finished_at=START,
        preparation=runtime.security.seal({"conversation": None}) if known else None,
        usage=json.dumps({"requests": 1, "input_tokens": 20, "output_tokens": 10}) if known else "{}",
    )
    async with runtime.database.sessions.begin() as db:
        await db.merge(session)
        await db.flush()
        db.add(turn)
    if known:
        await ledger(runtime, owner, session, turn, cost=cost, model=model)
    return session, turn


async def read(runtime, actor, selected=None, *, format="json", export="users", model=None):
    return await report(runtime, actor, START, END, model, None, selected, 100, 0, format, export=export)


async def test_linked_people_aggregate_filter_and_export_without_changing_actor_records(runtime, actor, linked):
    web, slack = linked.people[WEB], linked.people[SLACK]
    shared, web_turn = await observation(runtime, web, cost="0.2")
    _, slack_turn = await observation(runtime, slack, session=shared, cost="0.3")
    await observation(runtime, slack, known=False)
    await observation(runtime, linked.people[OTHER], cost="1")
    await observation(runtime, linked.people[UNLINKED], cost="0.4")

    result = await read(runtime, actor)
    groups = {row["user_id"]: row for row in result["by_user"]}
    assert set(groups) == {WEB, OTHER, UNLINKED}
    person = groups[WEB]
    assert (person["sessions"], person["sessions_created"], person["turns"]) == (2, 2, 3)
    assert person["requests"] == 2 and person["total_tokens"] == 60
    assert person["cost_usd_exact"] == "0.5"
    assert person["pricing_status"] == "partial" and person["unknown_request_turns"] == 1
    assert result["summary"]["cost_usd_exact"] == "1.9"
    assert result["summary"]["sessions"] == 4
    assert SLACK not in {row["id"] for row in result["users"]}
    assert next(row for row in result["users"] if row["id"] == WEB)["email"] == "person@example.test"

    # A canonical selection and an older URL using the Slack ID select the same person.
    for selected in (WEB, SLACK):
        filtered = await read(runtime, actor, selected)
        assert filtered["by_user"] == [person]
        assert filtered["summary"]["cost_usd_exact"] == "0.5"
        assert filtered["summary"]["sessions_created"] == 2
        assert {row["user_id"] for row in filtered["rows"]} == {WEB}
        row = next(row for row in filtered["rows"] if row["id"] == slack_turn.id)
        assert row["actor_id"] == SLACK
    exported = await read(runtime, actor, format="csv")
    csv_rows = list(csv.DictReader(io.StringIO(exported.body.decode())))
    assert {row["user_id"] for row in csv_rows} == set(groups)
    assert next(row for row in csv_rows if row["user_id"] == WEB)["cost_usd_exact"] == "0.5"
    assert "PRIVATE_INPUT" not in json.dumps(result) and "PRIVATE_INPUT" not in exported.body.decode()
    async with runtime.database.sessions() as db:
        assert (await db.get(Turn, slack_turn.id)).actor_id == SLACK
        assert (await db.get(Turn, web_turn.id)).actor_id == WEB
        assert set(await db.scalars(select(InferenceUsage.actor_id))) == {WEB, SLACK, OTHER, UNLINKED}


async def test_pr_costs_combine_linked_actor_contributions_but_preserve_original_creator(
    runtime, actor, linked, monkeypatch,
):
    web, slack, other = (linked.people[key] for key in (WEB, SLACK, OTHER))
    session, creator_turn = await observation(runtime, slack, cost="0.2")
    _, web_turn = await observation(runtime, web, session=session, cost="0.3")
    await observation(runtime, other, session=session, cost="9")
    duplicate_session, duplicate_turn = await observation(runtime, other, cost="8")
    async with runtime.database.sessions.begin() as db:
        for index, owner, selected_session, turn in (
            (0, slack, session, creator_turn), (1, other, duplicate_session, duplicate_turn),
        ):
            db.add(PRPublication(
                id=f"receipt-{index}", actor_id=owner.id, session_id=selected_session.id, turn_id=turn.id,
                model="test", connection_id=new_id(), connection_revision=1,
                url_key="same-pr", target=runtime.security.seal({"url": "https://github.com/example/test/pull/1"}),
                number=1, github_id=123, repository_id=456,
                created_at=START if index == 0 else "2026-10-02T00:00:00+00:00",
            ))

    async def statuses(_runtime, _actor, publications, _period, _service):
        return {
            row.id: {"state": "merged", "created_at": utc(START), "merged_at": utc("2026-10-03T00:00:00Z")}
            for row in publications
        }, len(publications)
    monkeypatch.setattr("moyai.analytics_service.publication_statuses", statuses)
    for selected in (WEB, SLACK):
        result = await read(runtime, actor, selected)
        person = result["by_user"][0]
        assert person["prs_created"] == person["prs_merged"] == 1
        assert person["cost_per_merged_pr_usd"] == 0.5  # Neither other person's contribution is attributed.
    assert (await read(runtime, actor, OTHER))["summary"]["prs_created"] == 0

    # An unclassified creation under either alias still invalidates the person's ratio.
    async with runtime.database.sessions.begin() as db:
        db.add(Effect(
            id="unclassified", actor_id=web.id, session_id=session.id, turn_id=web_turn.id,
            tool_name="github_12345678_create_pull_request", state="confirmed",
            arguments=runtime.security.seal({}), fingerprint="synthetic", created_at=START,
        ))
    assert (await read(runtime, actor, SLACK))["by_user"][0]["cost_per_merged_pr_usd"] is None


async def test_target_without_native_activity_gets_a_label_and_invalid_links_stay_separate(runtime, linked):
    async with linked.writer.begin() as db:
        await db.execute(text(
            "INSERT INTO users VALUES ('cloudflare:existing','cloudflare','Existing Person','sso@example.test',NULL)"
        ))
        await db.execute(text("UPDATE users SET linked_user_id='cloudflare:existing' WHERE id=:id"), {"id": SLACK})
        await db.execute(text("UPDATE users SET linked_user_id=:target WHERE id=:id"), {"target": SLACK, "id": UNLINKED})
    identities = await report_identities(runtime)
    assert identities.canonical(SLACK) == "cloudflare:existing"
    assert identities.canonical(UNLINKED) == UNLINKED
    assert identities.actors("cloudflare:existing") == {"cloudflare:existing", SLACK}
    assert next(row for row in identities.users if row["id"] == "cloudflare:existing")["display_name"] == "Existing Person"
    async with linked.writer.begin() as db:
        await db.execute(text("DELETE FROM users WHERE id='cloudflare:existing'"))
    assert (await report_identities(runtime)).canonical(SLACK) == SLACK


async def test_unproven_native_id_collision_is_not_a_product_identity(runtime, linked):
    async with runtime.database.sessions.begin() as db:
        (await db.get(User, WEB)).username = "standalone-name"
    identities = await report_identities(runtime)
    assert identities.canonical(SLACK) == SLACK
    assert identities.canonical(WEB) == WEB


async def test_unavailable_product_directory_does_not_silently_split_people(runtime, actor, linked):
    async with linked.writer.begin() as db:
        await db.execute(text("DROP TABLE users"))
    from sqlalchemy.exc import SQLAlchemyError
    with pytest.raises(SQLAlchemyError):
        await read(runtime, actor)
    # Authorization is checked before reading product identities.
    with pytest.raises(HTTPException) as denied:
        await read(runtime, linked.people[WEB])
    assert denied.value.status_code == 403
