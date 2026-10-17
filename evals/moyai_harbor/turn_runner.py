from __future__ import annotations

import asyncio
import json
import os
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import sqlalchemy
from cryptography.fernet import Fernet
from moyai.models import SlackThread, Turn
from moyai.product_config import (
    NATIVE_KEY_FORMAT,
    NATIVE_OBJECT_FORMAT,
    load_product_config,
)
from moyai.product_schema import migrate_product_schema
from moyai.runtime import Runtime, configure
from moyai.settings import Settings
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.workflow import TurnWorkflow
from pydantic import SecretStr
from pydantic_ai import ConversationTypeAdapter, RunContext, RunUsage
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ToolCallPart, ToolReturnPart
from temporalio.testing import WorkflowEnvironment

from app.credentials import SaveSecret

from .workspace import HarborWorkspace

EVALS = Path(__file__).resolve().parents[1]
product_database_state = import_module("product_database_fixtures").product_database_state
CREDENTIAL_TOOLS = ("credentials_list", "credentials_request", "credentials_run")
BOT_USER = "UBOT00001"
SLACK_USER = "UTEST0001"
TEAM = "TTEST0001"
CHANNEL = "CTEST0001"


def conversation_trace(conversation_json: str | None, security: Any) -> list[dict[str, Any]]:
    if not conversation_json:
        return []
    conversation = ConversationTypeAdapter.validate_json(security.unseal(conversation_json))
    calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for message in conversation.messages:
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                calls[part.tool_call_id] = {
                    "name": part.tool_name, "args": part.args_as_dict(), "result": "",
                }
                order.append(part.tool_call_id)
            elif isinstance(part, ToolReturnPart):
                record = calls.get(part.tool_call_id)
                if record is not None:
                    record["result"] = json.dumps(part.content, ensure_ascii=False, default=str)[:4000]
    return [calls[identity] for identity in order if identity in calls]


def product_environment(
    data_dir: Path, database_url: str, native_schema: str, agent_model: str,
) -> dict[str, str]:
    gateway_url = os.getenv("LITELLM_API_BASE") or os.environ["LITELLM_GATEWAY_BASE_URL"]
    gateway_url = gateway_url.rstrip("/")
    if not gateway_url.endswith("/v1"):
        gateway_url += "/v1"
    gateway_key = os.getenv("LITELLM_API_KEY") or os.environ["LITELLM_GATEWAY_ADMIN_KEY"]
    return {
        "MOYAI_CORE": "pydantic",
        "MOYAI_PRODUCT_MODE": "existing",
        "MOYAI_RUNTIME_ROLE": "standalone",
        "MOYAI_PRODUCT_LOCAL_REHEARSAL": "true",
        "MOYAI_DATABASE_URL": database_url,
        "MOYAI_DATABASE_SCHEMA": "moyai",
        "MOYAI_NATIVE_SCHEMA": native_schema,
        "MOYAI_SCHEMA_MODE": "verify",
        "MOYAI_NATIVE_SCHEMA_MODE": "verify",
        "MOYAI_DATABASE_INITIALIZE": "false",
        "MOYAI_NATIVE_ENCRYPTION_KEY": "synthetic-native-authority-key-no-production",
        "MOYAI_NATIVE_KEY_FORMAT": NATIVE_KEY_FORMAT,
        "MOYAI_NATIVE_OBJECT_FORMAT": NATIVE_OBJECT_FORMAT,
        "MOYAI_NATIVE_OBJECT_PREFIX": "pydantic-v1",
        "MOYAI_NATIVE_TASK_QUEUE": "moyai-evals-" + uuid4().hex[:12],
        "MOYAI_SEPARATE_BROKER": "false",
        "MOYAI_COORDINATOR_HANDOFF": "false",
        "SESSION_SECRET": "synthetic-product-session-secret-only",
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "GOOGLE_CLIENT_ID": "synthetic-client",
        "GOOGLE_CLIENT_SECRET": "synthetic-client-secret",
        "GOOGLE_ALLOWED_DOMAINS": "example.test",
        "GOOGLE_ADMIN_EMAILS": "admin@example.test",
        "TEMPORAL_ENABLED": "true",
        "TEMPORAL_ADDRESS": "127.0.0.1:7233",
        "TEMPORAL_NAMESPACE": "default",
        "TEMPORAL_TASK_QUEUE": "existing-sessions-v1",
        "MOYAI_PUBLIC_URL": "http://127.0.0.1:8796",
        "LITELLM_API_BASE": gateway_url,
        "LITELLM_API_KEY": gateway_key,
        "AGENT_MODEL": agent_model,
        "DATA_DIR": str(data_dir),
        "OBJECT_STORAGE_PREFIX": "moyai",
        "OBJECT_STORAGE_BUCKET": "synthetic-bucket",
        "OBJECT_STORAGE_ENDPOINT": "http://127.0.0.1:9000",
        "OBJECT_STORAGE_ACCESS_KEY_ID": "synthetic-key-id",
        "OBJECT_STORAGE_SECRET_ACCESS_KEY": "synthetic-secret",
        "OBJECT_STORAGE_SESSION_TOKEN": "synthetic-session",
        "SLACK_BOT_ENABLED": "true",
        "SLACK_THREAD_CHAT_ENABLED": "true",
        "SLACK_SESSION_USERS": SLACK_USER,
        "SLACK_SIGNING_SECRET": "synthetic-eval-signing-secret",
    }


def seed_credentials(runtime: Runtime, values: dict[str, str]) -> None:
    store = runtime.product_store
    secrets = runtime.product_secrets
    assert store is not None and secrets is not None
    body = SaveSecret(
        provider="generic", name="deepseek", format="env", label="DeepSeek test access",
        scope="organization", lifetime="persistent", value=SecretStr(json.dumps(values)),
        client_id="moyai-eval-deepseek",
    )
    with store.connect() as connection:
        connection.begin_write()
        secrets.credentials.insert_secret(
            connection, body, f"slack:{TEAM}:{SLACK_USER}", admin=True,
        )


async def run_moyai_turn(
    case: dict[str, Any],
    *,
    thread: list[dict[str, Any]],
    credentials: dict[str, str],
    workspace: HarborWorkspace,
    agent_model: str,
    trial: int,
) -> dict[str, Any]:
    root_dir = EVALS / "jobs" / "runner" / f"{case['id']}-{trial}-{uuid4().hex[:8]}"
    root_dir.mkdir(parents=True, exist_ok=True)
    async with product_database_state(root_dir) as product:
        await migrate_product_schema(
            product.source, schema=product.schema, source_schema="moyai",
        )
        environment = product_environment(
            root_dir / "legacy-store", product.settings.database_url, product.schema, agent_model,
        )
        product_config = load_product_config(environment)
        if product_config is None:
            raise RuntimeError("Could not load the eval's existing-product configuration.")
        settings = Settings.model_validate(product_config.native_overrides())
        runtime = Runtime(settings, product_config=product_config)
        try:
            async with await WorkflowEnvironment.start_local(
                plugins=[PydanticAIPlugin()],
                data_converter=encrypted_converter(runtime.security, runtime.objects),
            ) as temporal:
                runtime.client = temporal.client
                skills = await workspace.environment.exec("mkdir -p skills", cwd="/workspace")
                if skills.return_code != 0:
                    raise RuntimeError("Could not prepare the workspace skill directory.")
                await runtime.start(workspace=workspace, skills_directory="skills")
                assert runtime.product_connections is not None
                assert runtime.product_slack is not None
                configure(runtime)
                bot_credentials = {
                    "access_token": "synthetic-slack-user-token",
                    "bot": {
                        "access_token": "synthetic-slack-bot-token",
                        "bot_user_id": BOT_USER,
                        "team": {"id": TEAM},
                        "scope": "channels:history,groups:history,chat:write",
                    },
                }
                runtime.product_connections.connectors.save("slack", bot_credentials, "Eval Slack")
                seed_credentials(runtime, credentials)
                async with make_worker(runtime):
                    result = await execute_turn(
                        case, trial=trial, thread=thread, runtime=runtime, temporal=temporal,
                    )
                    result["credential_tool_calls"] = {
                        name: any(call["name"] == name for call in result["tool_trace"])
                        for name in CREDENTIAL_TOOLS
                    }
                    result.pop("turn_id")
                    return result
        finally:
            await runtime.close()


async def offered_tool_names(runtime: Runtime, turn_id: str) -> list[str]:
    prepared = await runtime.database.prepare(turn_id)
    context = RunContext(
        deps=prepared.scope,
        model=runtime.models[prepared.scope.model],
        usage=RunUsage(),
    )
    names: set[str] = set()
    for toolset in runtime.agent.toolsets:
        names.update((await toolset.get_tools(context)).keys())
    return sorted(names)


async def execute_turn(
    case: dict[str, Any],
    *,
    trial: int,
    thread: list[dict[str, Any]],
    runtime: Runtime,
    temporal: Any,
) -> dict[str, Any]:
    service = runtime.product_slack
    assert service is not None and runtime.product_connections is not None
    prior = case["thread"]
    stamp = 1_700_000_000 + trial * 10
    root_ts = f"{stamp:.6f}" if prior else ""
    history = [{
        "ts": f"{stamp + index * 0.1:.6f}",
        "user": BOT_USER if message["role"] == "you" else SLACK_USER,
        "username": message["display_name"],
        "text": message["text"],
        **({"files": [{"id": "FROOT01"}]} if message.get("files") else {}),
    } for index, message in enumerate(prior)]
    current = f"{stamp + len(prior) + 1:.6f}"
    root_ts = root_ts or current
    history.append({"ts": current, "thread_ts": root_ts, "user": SLACK_USER, "text": case["prompt"]})
    slack_reads: list[dict[str, Any]] = []

    async def slack_request(method: str, url: str, *, allowed_errors=(), **kwargs: Any) -> dict[str, Any]:
        if url.endswith("/conversations.replies"):
            slack_reads.append({"method": method, "url": url, "params": kwargs.get("params", {})})
            return {"ok": True, "messages": history, "has_more": False}
        return {"ok": True}

    payload = {
        "type": "event_callback", "team_id": TEAM, "event_id": f"eval-{case['id']}-{trial}",
        "event": {
            "type": "app_mention", "user": SLACK_USER, "channel": CHANNEL,
            "channel_type": "channel", "ts": current, "thread_ts": root_ts, "text": case["prompt"],
        },
    }
    with patch.object(runtime.product_connections.connectors, "request", slack_request):
        await service.events.receive(payload)
    async with runtime.database.sessions() as db:
        slack_thread = await db.scalar(sqlalchemy.select(SlackThread).where(
            SlackThread.team_id == TEAM,
            SlackThread.channel_id == CHANNEL,
            SlackThread.thread_ts == root_ts,
        ))
        if slack_thread is None:
            raise RuntimeError(f"ProductSlack did not bind {case['id']}.")
        turn = await db.scalar(sqlalchemy.select(Turn).where(
            Turn.session_id == slack_thread.session_id,
        ).order_by(Turn.created_at.desc()))
    if turn is None:
        raise RuntimeError(f"ProductSlack did not submit a turn for {case['id']}.")
    await runtime.database.claim_next()
    available_tools = await offered_tool_names(runtime, turn.id)
    handle = await temporal.client.start_workflow(
        TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
    )
    await asyncio.wait_for(handle.result(), timeout=300)
    posted: list[dict[str, Any]] = []

    def post_stub(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        posted.append(body)
        return httpx.Response(200, json={"ok": True, "channel": body["channel"], "ts": "1792100000.001"})

    async with httpx.AsyncClient(
        base_url="https://slack.com/api/", transport=httpx.MockTransport(post_stub), timeout=20,
    ) as delivery:
        for _ in range(4):
            await service.pump(delivery)
    async with runtime.database.sessions() as db:
        completed = await db.get(Turn, turn.id)
    if completed is None:
        raise RuntimeError(f"Turn {turn.id} disappeared after completion.")
    reply = next((
        "\n\n".join(
            block["text"]["text"] for block in item.get("blocks", [])
            if block.get("type") == "section" and isinstance(block.get("text"), dict)
        )
        for item in reversed(posted)
        if any(block.get("type") == "section" for block in item.get("blocks", []))
    ), "")
    prompt = runtime.security.unseal(completed.prompt).decode()
    return {
        "case": case["id"],
        "trial": trial,
        "turn_id": turn.id,
        "agent_model": runtime.settings.default_model,
        "offered_tool_names": available_tools,
        "slack_event_input": payload,
        "slack_thread_input": thread,
        "slack_thread_reads": slack_reads,
        "prompt_received": prompt,
        "thread_messages_in_prompt": [
            message["text"] for message in prior if message["text"] in prompt
        ],
        "attached_image_names": [],
        "posted_reply": reply,
        "tool_trace": conversation_trace(completed.conversation, runtime.security),
        "tokens_cost": json.loads(completed.usage or "{}"),
    }
