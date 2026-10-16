"""Pre-extraction public contracts and a fresh-process runtime import boundary."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
from app import automation_definitions as definitions
from app.automation_sources import EventTrigger
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]

# Captured from app.automations before the classes moved, using this locked environment.
SCHEMAS = {
    "Timing": "5d8e69b8a893a58ea2c38111ff2cbc8ef15334e61bc64d52422c65902402b9aa",
    "Trigger": "7827c8784dc6fe983b6e1d798ec8ba9a204069b2ef37259926dbd9b0146ffe71",
    "Definition": "0ee02561eaec4447d461328107f5fe37f3f8ff934103b9b01853c8c7df99d7c2",
    "Save": "a048d7711cba3ae5c750e98ff57c868fe4047aed3b4f13f1e02523f882648fdd",
    "Toggle": "c6ffe919534ad3d527da7aacc5e1d922f3ec491fa5daf783d1abc1c9debd1bee",
    "Launch": "2b265487e5f28e0288bd83a68006a6b6b5acbec0d713a8d0ae6c19a8e751272a",
    "Claim": "eff2fee5396f1339fb8a34bb235debead39d65b130bf7e855f4c274844187ff1",
}
SCHEDULES = {
    "hourly": "8099acb018ef9acb604a456982f3babcc0b73e0ac678cae2dbf6d8f5f3b4bde2",
    "daily": "22fdd4efafaa3ed0170acb2441961982f08de3af12c2aad9a34534ed7e1986f1",
    "weekdays": "b658b26ebb332b1ded7df7effea9c32ef78badbe1d3dc4be56bc54886fc41bb5",
    "weekly": "e1c027b4caeb48dfaa218a2282773199e571cf034151a2be18faa36d54b1cbaa",
    "cron": "996f2ef851a41d955b86d68300f97f17f6e4bd6b24700180a59f3a3b4509ce07",
    "once": "4a59a39b088af01cd62f47aee35303d04889e61663d44040054266fcc604a267",
}


@pytest.mark.parametrize("name", SCHEMAS)
def test_json_schema_matches_original_public_contract(name):
    schema = getattr(definitions, name).model_json_schema()
    # The separately authorized native-session trigger extension changes only
    # this bound; every other part of the extracted contract stays identical.
    event = schema.get("$defs", {}).get("EventTrigger")
    if event is not None:
        assert event["properties"]["session_id"]["maxLength"] == 36
        event["properties"]["session_id"]["maxLength"] = 32
    assert hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest() == SCHEMAS[name]


@pytest.mark.parametrize("session_id", ["a" * 32, "d2aeb2f5-4295-49c3-98a4-66f996546743"])
def test_session_event_preserves_exact_legacy_and_native_identity(session_id):
    trigger = EventTrigger(provider="session", event="message.posted", session_id=session_id)
    assert trigger.session_id == session_id
    assert EventTrigger.model_validate_json(trigger.model_dump_json()).session_id == session_id


@pytest.mark.parametrize("session_id", [
    "a" * 31, "a" * 35, "a" * 36, "a" * 37,
    "google:subject", "d2aeb2f5_4295_49c3_98a4_66f996546743",
])
def test_session_event_does_not_guess_or_rewrite_unknown_identifiers(session_id):
    with pytest.raises(ValidationError):
        EventTrigger(provider="session", event="message.posted", session_id=session_id)


@pytest.mark.parametrize("frequency", SCHEDULES)
def test_temporal_schedule_conversion_matches_original(frequency):
    timing = definitions.Timing.model_validate({
        "frequency": frequency, "time": "13:07", "weekday": 3, "timezone": "America/Los_Angeles",
        "cron": "*/5 1-3 * * 1,5", "run_at": "2027-02-01T18:37:00-08:00",
    })
    rendered = json.dumps(asdict(timing.spec()), sort_keys=True, default=str)
    assert hashlib.sha256(rendered.encode()).hexdigest() == SCHEDULES[frequency]


def test_legacy_draft_upgrade_keeps_defaults_and_shared_definition_semantics():
    draft = definitions.Definition.model_validate({
        "name": " Daily check ", "prompt": " Read the queue ", "timing": {"frequency": "daily"},
    })
    assert draft.name == "Daily check" and draft.prompt == "Read the queue"
    assert draft.triggers[0].id == "default"
    assert draft.triggers[0].schedule.frequency == "daily"
    assert draft.harness == "hermes" and draft.mode == "modal"
    assert draft.max_runs_per_hour == 50 and not draft.queue_events
    assert "visibility" not in draft.model_dump()
    assert draft.self_management.allowed() == []
    assert definitions.Definition.model_validate_json(draft.model_dump_json()) == draft
    event = definitions.Definition.model_validate({
        "name": "Slack check", "prompt": "Read the message",
        "event": {"provider": "slack", "event": "message.posted", "text_contains": "status", "max_runs_per_hour": 9},
    })
    assert event.max_runs_per_hour == 9 and event.triggers[0].event.provider == "slack"


@pytest.mark.parametrize(("name", "value"), [
    ("Timing", {"timezone": "../../bad"}),
    ("Timing", {"frequency": "cron", "cron": "61 * * * *"}),
    ("Timing", {"frequency": "once", "run_at": "2027-02-01T18:37:00"}),
    ("Trigger", {"id": "both", "schedule": {}, "event": {"provider": "slack", "event": "message.posted"}}),
    ("Trigger", {"id": "neither"}),
    ("Definition", {"name": "Task", "prompt": "Work", "visibility": "private"}),
    ("Definition", {"name": "Task", "prompt": "Work", "metadata": {"": "invalid"}}),
    ("Definition", {"name": "Task", "prompt": "Work", "repo_url": "https://attacker.invalid/repo"}),
    ("Launch", {"revision": 1, "client_id": "arbitrary:key"}),
    ("Claim", {"item_key": ""}),
])
def test_original_validation_still_rejects_invalid_inputs(name, value):
    with pytest.raises(ValidationError):
        getattr(definitions, name).model_validate(value)


def test_pure_import_avoids_database_and_executors_and_legacy_exports_are_identical():
    result = subprocess.run(
        [sys.executable, "-c", (
            "import sys; from app import automation_definitions as d; "
            "assert not any(n in sys.modules for n in "
            "('app.main','app.runner','app.db','app.automations','app.automation_events',"
            "'app.agent_runtime','app.durable_runner','agents','deepagents','langgraph')); "
            "from app import automations as a, automation_permissions as p; "
            "assert all(getattr(a,n) is getattr(d,n) for n in "
            "('Timing','Trigger','Definition','Save','Toggle','Launch','Claim')); "
            "assert a.SelfManagement is p.SelfManagement is d.SelfManagement; "
            "assert d.SelfManagement(instructions=True).allowed()==['instructions']; "
            "print('pure imports and legacy aliases preserved')"
        )],
        cwd=ROOT, env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT)},
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "pure imports and legacy aliases preserved"
