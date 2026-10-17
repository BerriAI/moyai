from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import sqlalchemy
from app.credentials import SaveSecret
from app.db import Store
from app.security import Security as ProductSecurity
from product_database_fixtures import product_database_state
from pydantic import SecretStr
from pydantic_ai import ConversationTypeAdapter
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ToolCallPart, ToolReturnPart
from sqlalchemy import text
from temporalio.testing import WorkflowEnvironment
from test_product_identity import google_cookie, request, settings_for

from moyai.agent import inference_models, make_agent
from moyai.models import SlackThread, Turn
from moyai.product_connections import ProductConnections
from moyai.product_credential_tools import capability as credential_capability
from moyai.product_schema import migrate_product_schema
from moyai.product_secrets import ProductSecrets
from moyai.product_slack import ProductSlack, ProductSlackAuthority
from moyai.runtime import configure
from moyai.settings import Settings
from moyai.storage import ObjectStore, encrypted_converter
from moyai.stores import NativeStores
from moyai.worker import make_worker
from moyai.workflow import TurnWorkflow

EVALS = Path(__file__).resolve().parents[1]
MODEL_COMPLETION = {
    "id": "stub-1",
    "object": "chat.completion",
    "model": "deepseek-flash",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 1, "total_tokens": 13},
}


def json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def conversation_trace(conversation_json: str | None, security: Any) -> list[dict[str, Any]]:
    if not conversation_json:
        return []
    conversation = ConversationTypeAdapter.validate_json(security.unseal(conversation_json))
    calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for message in conversation.messages:
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                identity = part.tool_call_id
                calls[identity] = {
                    "name": part.tool_name,
                    "args": part.args_as_dict(),
                    "result": "",
                }
                order.append(identity)
            elif isinstance(part, ToolReturnPart):
                record = calls.get(part.tool_call_id)
                if record is not None:
                    record["result"] = json_value(part.content)[:4000]
    return [calls[identity] for identity in order if identity in calls]


def slack_transport(history: list[dict[str, Any]]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/conversations.replies"):
            return httpx.Response(200, json={"ok": True, "messages": history})
        if path.endswith(("/chat.postMessage", "/chat.update")):
            return httpx.Response(200, json={
                "ok": True, "channel": "CTEST0001", "ts": "1792100000.001",
            })
        return httpx.Response(200, json={"ok": True})
    return httpx.MockTransport(handle)


def seed_credentials(
    store: Store,
    credential_store: Any,
    credential_values: dict[str, str],
) -> dict[str, Any]:
    body = SaveSecret(
        provider="generic",
        name="deepseek",
        format="env",
        label="DeepSeek test access",
        scope="organization",
        lifetime="persistent",
        value=SecretStr(json.dumps(credential_values)),
        client_id="moyai-eval-deepseek",
    )
    with store.connect() as connection:
        connection.begin_write()
        credential_store.insert_secret(
            connection, body, "slack:TTEST0001:UTEST0001", admin=True,
        )
    return store.rows(
        "SELECT * FROM provider_secrets WHERE client_id=?", ("moyai-eval-deepseek",),
    )[0]


async def seed_slack_source(
    product: Any,
    bot_credentials: dict[str, Any],
) -> None:
    async with product.source.begin() as connection:
        await connection.execute(text("""CREATE TABLE IF NOT EXISTS moyai.connections(
            provider TEXT PRIMARY KEY,encrypted TEXT NOT NULL,label TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '')"""))
        await connection.execute(text("""CREATE TABLE IF NOT EXISTS moyai.provider_secrets(
            id TEXT PRIMARY KEY,provider TEXT NOT NULL,label TEXT NOT NULL,scope TEXT NOT NULL,
            owner_id TEXT NOT NULL,root_id TEXT NOT NULL DEFAULT '',encrypted TEXT NOT NULL,
            created_at TEXT NOT NULL,revoked_at TEXT NOT NULL DEFAULT '',client_id TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',format TEXT NOT NULL DEFAULT 'env',env_var TEXT NOT NULL DEFAULT '',
            lifetime TEXT NOT NULL DEFAULT 'persistent',expires_at TEXT NOT NULL DEFAULT '',
            invalid_reason TEXT NOT NULL DEFAULT '',revision INTEGER NOT NULL DEFAULT 1,
            UNIQUE(owner_id,client_id))"""))
        await connection.execute(text("""CREATE TABLE IF NOT EXISTS moyai.credential_audit(
            id BIGSERIAL PRIMARY KEY,actor_id TEXT NOT NULL,secret_id TEXT NOT NULL,
            action TEXT NOT NULL,run_id TEXT NOT NULL,created_at TEXT NOT NULL)"""))
        await connection.execute(text("""CREATE TABLE IF NOT EXISTS moyai.connection_policies(
            provider TEXT PRIMARY KEY,enabled BOOLEAN NOT NULL DEFAULT TRUE,
            read_only BOOLEAN NOT NULL DEFAULT FALSE,checked_at TEXT NOT NULL DEFAULT '',
            check_status TEXT NOT NULL DEFAULT 'unknown')"""))
        await connection.execute(text("""CREATE TABLE IF NOT EXISTS moyai.connection_audit(
            id BIGSERIAL PRIMARY KEY,provider TEXT NOT NULL)"""))
        await connection.execute(text("""CREATE TABLE IF NOT EXISTS moyai.personal_slack_grants(
            owner_id TEXT PRIMARY KEY)"""))
        await connection.execute(text(
            "ALTER TABLE moyai.users ADD COLUMN IF NOT EXISTS profile_eligible BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        await connection.execute(text(
            "ALTER TABLE moyai.users ADD COLUMN IF NOT EXISTS profile_checked_at TEXT NOT NULL DEFAULT ''"
        ))
        await connection.execute(text(
            "ALTER TABLE moyai.users ADD COLUMN IF NOT EXISTS profile_conflict BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        await connection.execute(text(
            "ALTER TABLE moyai.users ADD COLUMN IF NOT EXISTS profile_next_check BIGINT NOT NULL DEFAULT 0"
        ))
        await connection.execute(text(
            "ALTER TABLE moyai.users ADD COLUMN IF NOT EXISTS link_status TEXT NOT NULL DEFAULT ''"
        ))
        await connection.execute(text(
            "ALTER TABLE moyai.users ADD COLUMN IF NOT EXISTS link_method TEXT NOT NULL DEFAULT ''"
        ))
        await connection.execute(text("""INSERT INTO moyai.users(
            id,kind,email,name,linked_user_id,created_at,updated_at
        ) VALUES ('slack:TTEST0001:UTEST0001','slack','','Slack UTEST0001',NULL,'2026-01-01','2026-01-01')
        ON CONFLICT (id) DO NOTHING"""))
        await connection.execute(text("""INSERT INTO moyai.connection_policies(provider,enabled,read_only)
            VALUES ('slack',TRUE,FALSE) ON CONFLICT (provider) DO UPDATE SET enabled=TRUE"""))
        encrypted = product.adapter.legacy_security.encrypt(json.dumps(bot_credentials))
        await connection.execute(text("""INSERT INTO moyai.connections(
            provider,encrypted,label,updated_at
        ) VALUES ('slack',:encrypted,'Eval Slack','2026-01-01')
        ON CONFLICT (provider) DO UPDATE SET encrypted=EXCLUDED.encrypted,
            label=EXCLUDED.label,updated_at=EXCLUDED.updated_at"""), {
            "encrypted": encrypted,
        })


async def run_moyai_turn(
    case: dict[str, Any],
    *,
    thread: list[dict[str, Any]],
    credentials: dict[str, str],
    workspace: Any,
    agent_model: str,
    trial: int,
) -> dict[str, Any]:
    root_dir = EVALS / "out" / "runner" / f"{case['id']}-{trial}-{uuid4().hex[:8]}"
    root_dir.mkdir(parents=True, exist_ok=True)
    async with product_database_state(root_dir) as product:
        await migrate_product_schema(product.source, schema=product.schema, source_schema="moyai")
        await product.database.start()
        cookie, _ = google_cookie(product.adapter, sub=product.subject)
        principal = await product.adapter.authenticate(request(cookie))
        async with product.database.sessions.begin() as db:
            product.actor = await product.adapter.project_native_user(db, principal)
        source_settings = settings_for(
            root_dir,
            data_dir=root_dir / "legacy-store",
            slack_bot_enabled=True,
            slack_thread_chat_enabled=True,
            slack_session_users="UTEST0001",
            slack_signing_secret="synthetic-eval-signing-secret",
            public_url="http://127.0.0.1:8787",
        )
        legacy_security = ProductSecurity(source_settings)
        store = Store(source_settings.data_dir, source_settings.agent_model)
        product_services = SimpleNamespace(
            identity=product.adapter,
            security=legacy_security,
            settings=source_settings,
            store_proxy=store,
        )
        product.adapter.settings = source_settings
        product.adapter.legacy_security = legacy_security
        product.adapter.slack_authority = ProductSlackAuthority(product.adapter)
        bot_credentials = {
            "access_token": "synthetic-slack-user-token",
            "bot": {
                "access_token": "synthetic-slack-bot-token",
                "bot_user_id": "UBOT00001",
                "team": {"id": "TTEST0001"},
                "scope": "chat:write,files:read",
            },
        }
        await seed_slack_source(product, bot_credentials)
        connections = ProductConnections(product_services, product.database)
        connections.connectors.save("slack", bot_credentials, "Eval Slack")
        settings: Settings = product.settings
        settings.default_model = agent_model
        settings.model_catalog = agent_model
        gateway_url = os.getenv("LITELLM_API_BASE") or os.environ["LITELLM_GATEWAY_BASE_URL"]
        settings.gateway_url = gateway_url.rstrip("/")
        if not settings.gateway_url.endswith("/v1"):
            settings.gateway_url += "/v1"
        gateway_key = os.getenv("LITELLM_API_KEY") or os.environ["LITELLM_GATEWAY_ADMIN_KEY"]
        settings.gateway_key = SecretStr(gateway_key)
        settings.data_dir = root_dir / "native-runtime"
        settings.temporal_queue = "moyai-evals-" + uuid4().hex[:12]
        settings.worker_shutdown_seconds = 0
        stores = NativeStores(settings)
        await stores.start(provision=True)
        objects = ObjectStore(settings, product.database.security)
        runtime = SimpleNamespace(
            settings=settings,
            database=product.database,
            security=product.database.security,
            objects=objects,
            stores=stores,
            product=product_services,
            product_identity=product.adapter,
            product_connections=connections,
            product_automations=None,
            product_memory_review=None,
            product_drain=None,
            product_automation_events=None,
            product_secrets=None,
            client=None,
        )
        secrets_service = ProductSecrets(runtime)
        runtime.product_secrets = secrets_service
        runtime.database.product_connections = connections
        runtime.database.product_identity = product.adapter
        runtime.database.security = runtime.security
        secret_row = seed_credentials(
            store,
            secrets_service.credentials,
            credentials,
        )
        await copy_seed_credential(product, secret_row)
        models = inference_models(settings)
        runtime.models = models
        service = ProductSlack(runtime)
        runtime.product_slack = service
        configure(runtime)
        converter = encrypted_converter(runtime.security, objects)
        try:
            async with await WorkflowEnvironment.start_local(
                plugins=[PydanticAIPlugin()],
                data_converter=converter,
            ) as temporal:
                runtime.client = temporal.client
                skills = root_dir / "skills"
                skills.mkdir()
                runtime.agent, runtime.durability = make_agent(
                    settings,
                    runtime.database,
                    stores,
                    objects,
                    models=models,
                    workspace=workspace,
                    skills_directory=str(skills),
                    product_capabilities=[credential_capability(secrets_service.execution)],
                )
                async with make_worker(runtime):
                    return await execute_turn(
                        case,
                        trial=trial,
                        thread=thread,
                        service=service,
                        runtime=runtime,
                        temporal=temporal,
                    )
        finally:
            await stores.close()
            store.close()


async def copy_seed_credential(product: Any, row: dict[str, Any]) -> None:
    async with product.source.begin() as connection:
        await connection.execute(text("""INSERT INTO moyai.provider_secrets(
            id,provider,label,scope,owner_id,root_id,encrypted,created_at,revoked_at,client_id,
            name,format,env_var,lifetime,expires_at,invalid_reason,revision
        ) VALUES (
            :id,:provider,:label,:scope,:owner_id,:root_id,:encrypted,:created_at,:revoked_at,:client_id,
            :name,:format,:env_var,:lifetime,:expires_at,:invalid_reason,:revision
        )"""), row)


async def execute_turn(
    case: dict[str, Any],
    *,
    trial: int,
    thread: list[dict[str, Any]],
    service: ProductSlack,
    runtime: Any,
    temporal: Any,
) -> dict[str, Any]:
    stamp = 1_700_000_000 + trial * 10
    current = f"{stamp + len(thread) + 1:.6f}"
    thread_ts = current
    history = [{
        "ts": f"{stamp + index * 0.1:.6f}",
        "user": "UBOT00001" if message["role"] == "you" else "UTEST0001",
        "username": message["display_name"],
        "text": message["text"],
        **({"files": [{"id": "FROOT01"}]} if message["files"] else {}),
    } for index, message in enumerate(case["thread"])]
    transport = slack_transport(history)
    original_client = httpx.AsyncClient

    def routed_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.setdefault("transport", transport)
        return original_client(*args, **kwargs)

    payload = {
        "type": "event_callback",
        "team_id": "TTEST0001",
        "event_id": f"eval-{case['id']}-{trial}",
        "event": {
            "type": "app_mention",
            "user": "UTEST0001",
            "channel": "CTEST0001",
            "channel_type": "channel",
            "ts": current,
            "thread_ts": thread_ts,
            "text": case["prompt"],
        },
    }
    with patch("httpx.AsyncClient", routed_client):
        await service.receive_payload(payload)
    async with runtime.database.sessions() as db:
        slack_thread = await db.scalar(sqlalchemy.select(SlackThread).where(
            SlackThread.team_id == "TTEST0001",
            SlackThread.channel_id == "CTEST0001",
            SlackThread.thread_ts == thread_ts,
        ))
        if slack_thread is None:
            raise RuntimeError(f"ProductSlack did not bind {case['id']}.")
        turn = await db.scalar(sqlalchemy.select(Turn).where(
            Turn.session_id == slack_thread.session_id,
        ).order_by(Turn.created_at.desc()))
    if turn is None:
        raise RuntimeError(f"ProductSlack did not submit a turn for {case['id']}.")
    await runtime.database.claim_next()
    handle = await temporal.client.start_workflow(
        TurnWorkflow.run,
        turn.id,
        id=turn.workflow_id,
        task_queue=runtime.settings.temporal_queue,
    )
    await asyncio.wait_for(handle.result(), timeout=300)
    posted: list[dict[str, Any]] = []

    def post_stub(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        posted.append(body)
        return httpx.Response(200, json={
            "ok": True,
            "channel": body["channel"],
            "ts": "1792100000.001",
        })

    async with original_client(
        base_url="https://slack.com/api/",
        transport=httpx.MockTransport(post_stub),
        timeout=20,
    ) as delivery:
        for _ in range(4):
            await service.pump(delivery)
    async with runtime.database.sessions() as db:
        completed = await db.get(Turn, turn.id)
    if completed is None:
        raise RuntimeError(f"Turn {turn.id} disappeared after completion.")
    reply = next((
        item["text"] for item in reversed(posted)
        if "text" in item and item["text"] != "Moyai received your message."
    ), "")
    prompt = runtime.security.unseal(completed.prompt).decode()
    return {
        "case": case["id"],
        "trial": trial,
        "slack_event_input": payload,
        "slack_thread_input": thread,
        "prompt_received": prompt,
        "attached_image_names": [],
        "posted_reply": reply,
        "tool_trace": conversation_trace(completed.conversation, runtime.security),
        "tokens_cost": json.loads(completed.usage or "{}"),
    }
