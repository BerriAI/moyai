from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import sqlalchemy
from moyai.agent import inference_models, make_agent
from moyai.models import Session, SlackThread, Turn
from moyai.product_connections import ProductConnections
from moyai.product_continuation import ProductContinuationService
from moyai.product_continuation_workspace import ProductWorkspaceCompatibility
from moyai.product_credential_tools import capability as credential_capability
from moyai.product_history import ProductHistoryAdapter
from moyai.product_inference import ProductInferenceService
from moyai.product_schema import migrate_product_schema
from moyai.product_secrets import ProductSecrets
from moyai.product_slack import ProductSlack, ProductSlackAuthority
from moyai.questions import pending_questions
from moyai.runtime import configure
from moyai.settings import Settings
from moyai.skill_invocations import SkillInvocations
from moyai.storage import ObjectStore, encrypted_converter
from moyai.stores import NativeStores
from moyai.worker import make_worker
from moyai.workflow import TurnWorkflow
from product_database_fixtures import product_database_state
from pydantic import SecretStr, TypeAdapter
from pydantic_ai import ConversationTypeAdapter
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ToolCallPart, ToolReturnPart
from pydantic_ai_harness.ask_user import AskUserRequest
from sqlalchemy import text
from temporalio.testing import WorkflowEnvironment
from test_product_identity import google_cookie, request, settings_for

from app.credentials import SaveSecret
from app.db import Store
from app.security import Security as ProductSecurity

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


def completed_tool_call_ids(conversation_json: str | None, security: Any) -> set[str]:
    if not conversation_json:
        return set()
    conversation = ConversationTypeAdapter.validate_json(security.unseal(conversation_json))
    return {
        part.tool_call_id for message in conversation.messages for part in message.parts
        if isinstance(part, ToolReturnPart)
    }


def conversation_trace(
    conversation_json: str | None, security: Any, *, exclude_call_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    if not conversation_json:
        return []
    conversation = ConversationTypeAdapter.validate_json(security.unseal(conversation_json))
    calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for message in conversation.messages:
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                identity = part.tool_call_id
                if exclude_call_ids and identity in exclude_call_ids:
                    continue
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
            return httpx.Response(200, json={"ok": True, "messages": history, "has_more": False})
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
    schema = f'"{product.adapter.schema}"'

    def table(name: str) -> str:
        return f'{schema}."{name}"'

    async with product.source.begin() as connection:
        await connection.execute(text(f"""CREATE TABLE IF NOT EXISTS {table('connections')}(
            provider TEXT PRIMARY KEY,encrypted TEXT NOT NULL,label TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '')"""))
        await connection.execute(text(f"""CREATE TABLE IF NOT EXISTS {table('provider_secrets')}(
            id TEXT PRIMARY KEY,provider TEXT NOT NULL,label TEXT NOT NULL,scope TEXT NOT NULL,
            owner_id TEXT NOT NULL,root_id TEXT NOT NULL DEFAULT '',encrypted TEXT NOT NULL,
            created_at TEXT NOT NULL,revoked_at TEXT NOT NULL DEFAULT '',client_id TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',format TEXT NOT NULL DEFAULT 'env',env_var TEXT NOT NULL DEFAULT '',
            lifetime TEXT NOT NULL DEFAULT 'persistent',expires_at TEXT NOT NULL DEFAULT '',
            invalid_reason TEXT NOT NULL DEFAULT '',revision INTEGER NOT NULL DEFAULT 1,
            UNIQUE(owner_id,client_id))"""))
        await connection.execute(text(f"""CREATE TABLE IF NOT EXISTS {table('credential_audit')}(
            id BIGSERIAL PRIMARY KEY,actor_id TEXT NOT NULL,secret_id TEXT NOT NULL,
            action TEXT NOT NULL,run_id TEXT NOT NULL,created_at TEXT NOT NULL)"""))
        await connection.execute(text(f"""CREATE TABLE IF NOT EXISTS {table('connection_policies')}(
            provider TEXT PRIMARY KEY,enabled BIGINT NOT NULL DEFAULT 1,
            read_only BIGINT NOT NULL DEFAULT 0,checked_at TEXT NOT NULL DEFAULT '',
            check_status TEXT NOT NULL DEFAULT 'unknown')"""))
        await connection.execute(text(f"""CREATE TABLE IF NOT EXISTS {table('connection_audit')}(
            id BIGSERIAL PRIMARY KEY,provider TEXT NOT NULL)"""))
        await connection.execute(text(f"""CREATE TABLE IF NOT EXISTS {table('personal_slack_grants')}(
            owner_id TEXT PRIMARY KEY)"""))
        await connection.execute(text(f"""CREATE TABLE IF NOT EXISTS {table('user_roles')}(
            email TEXT PRIMARY KEY,role TEXT NOT NULL CHECK(role IN ('admin','member')),
            revision INTEGER NOT NULL,updated_by TEXT NOT NULL,updated_at TEXT NOT NULL)"""))
        await connection.execute(text(f"""CREATE TABLE IF NOT EXISTS {table('user_role_audit')}(
            id BIGSERIAL PRIMARY KEY,email TEXT NOT NULL,previous_role TEXT NOT NULL,
            role TEXT NOT NULL,actor TEXT NOT NULL,created_at TEXT NOT NULL)"""))
        await connection.execute(text(f"""CREATE TABLE IF NOT EXISTS {table('access_identities')}(
            issuer TEXT NOT NULL,subject TEXT NOT NULL,email TEXT NOT NULL,
            user_id TEXT NOT NULL REFERENCES {table('users')}(id),created_at TEXT NOT NULL,
            PRIMARY KEY(issuer,subject),UNIQUE(issuer,user_id),UNIQUE(issuer,email))"""))
        await connection.execute(text(
            f"ALTER TABLE {table('users')} ADD COLUMN IF NOT EXISTS profile_eligible BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        await connection.execute(text(
            f"ALTER TABLE {table('users')} ADD COLUMN IF NOT EXISTS profile_checked_at TEXT NOT NULL DEFAULT ''"
        ))
        await connection.execute(text(
            f"ALTER TABLE {table('users')} ADD COLUMN IF NOT EXISTS profile_conflict BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        await connection.execute(text(
            f"ALTER TABLE {table('users')} ADD COLUMN IF NOT EXISTS profile_next_check BIGINT NOT NULL DEFAULT 0"
        ))
        await connection.execute(text(
            f"ALTER TABLE {table('users')} ADD COLUMN IF NOT EXISTS link_status TEXT NOT NULL DEFAULT ''"
        ))
        await connection.execute(text(
            f"ALTER TABLE {table('users')} ADD COLUMN IF NOT EXISTS link_method TEXT NOT NULL DEFAULT ''"
        ))
        await connection.execute(text(f"""INSERT INTO {table('users')}(
            id,kind,email,name,linked_user_id,created_at,updated_at
        ) VALUES ('slack:TTEST0001:UTEST0001','slack','','Slack UTEST0001',NULL,'2026-01-01','2026-01-01')
        ON CONFLICT (id) DO NOTHING"""))
        await connection.execute(text(f"""INSERT INTO {table('users')}(
            id,kind,email,name,linked_user_id,created_at,updated_at
        ) VALUES (:id,'google','admin@example.test','Product admin',NULL,'2026-01-01','2026-01-01')
        ON CONFLICT (id) DO NOTHING"""), {"id": product.user_id})
        await connection.execute(text(f"""INSERT INTO {table('user_roles')}(
            email,role,revision,updated_by,updated_at
        ) VALUES ('admin@example.test','admin',1,'eval','2026-01-01')
        ON CONFLICT (email) DO NOTHING"""))
        await connection.execute(text(f"""INSERT INTO {table('connection_policies')}(provider,enabled,read_only)
            VALUES ('slack',1,0) ON CONFLICT (provider) DO UPDATE SET enabled=1"""))
        encrypted = product.adapter.legacy_security.encrypt(json.dumps(bot_credentials))
        await connection.execute(text(f"""INSERT INTO {table('connections')}(
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
    root_dir = EVALS / "jobs" / "runner" / f"{case['id']}-{trial}-{uuid4().hex[:8]}"
    async with open_moyai_runtime(
        workspace=workspace, credentials=credentials, agent_model=agent_model, root_dir=root_dir,
    ) as running:
        result = await execute_turn(
            case, trial=trial, thread=thread, service=running.service,
            runtime=running.runtime, temporal=running.temporal,
        )
        result["offered_tool_names"] = running.offered_tool_names
        return result


@asynccontextmanager
async def open_moyai_runtime(
    *,
    workspace: Any,
    credentials: dict[str, str],
    agent_model: str,
    root_dir: Path,
) -> AsyncIterator[SimpleNamespace]:
    """Keep the real agent runtime alive for one eval or serial local chat turns."""
    root_dir.mkdir(parents=True, exist_ok=True)
    async with product_database_state(root_dir) as product:
        source_schema = "moyai_eval_source"
        async with product.source.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{source_schema}"'))
        product.adapter.schema = source_schema
        await migrate_product_schema(
            product.source, schema=product.schema, source_schema=source_schema,
        )
        await product.database.start()
        source_settings = settings_for(
            root_dir,
            data_dir=root_dir / "legacy-store",
            moyai_database_url=product.settings.database_url,
            moyai_database_schema=source_schema,
            slack_bot_enabled=True,
            slack_thread_chat_enabled=True,
            slack_session_users="UTEST0001",
            slack_signing_secret="synthetic-eval-signing-secret",
            public_url="https://moyai.invalid",
        )
        legacy_security = ProductSecurity(source_settings)
        store = Store(
            source_settings.data_dir,
            source_settings.agent_model,
            database_url=product.settings.database_url,
            database_schema=source_schema,
            database_initialize=True,
            schema_mode="auto",
            database_pool_size=source_settings.moyai_database_pool_size,
            application_instance=source_settings.moyai_runtime_role != "standalone",
            runtime_role=source_settings.moyai_runtime_role,
            runtime_settings=source_settings,
        )
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
        cookie, _ = google_cookie(product.adapter, sub=product.subject)
        principal = await product.adapter.authenticate(request(cookie))
        async with product.database.sessions.begin() as db:
            product.actor = await product.adapter.project_native_user(db, principal)
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
            product_history=ProductHistoryAdapter(
                product.adapter, product.source, schema=source_schema,
            ),
            product_connections=connections,
            product_automations=None,
            product_memory_review=None,
            product_drain=None,
            product_automation_events=None,
            product_secrets=None,
            client=None,
        )
        runtime.skill_invocations = SkillInvocations(runtime)
        runtime.product_workspace = ProductWorkspaceCompatibility(runtime)
        runtime.product_continuations = ProductContinuationService(
            runtime, workspace=runtime.product_workspace,
        )
        secrets_service = ProductSecrets(runtime)
        runtime.product_secrets = secrets_service
        runtime.database.product_connections = connections
        runtime.database.product_identity = product.adapter
        runtime.database.security = runtime.security
        product_settings = product.adapter.settings.model_copy(update={
            "agent_model": agent_model,
            "litellm_api_base": settings.gateway_url,
            "litellm_api_key": settings.gateway_key.get_secret_value(),
        })
        product_inference = ProductInferenceService(product.database, product_settings, store)
        runtime.product_inference = product_inference
        runtime.database.product_inference = product_inference
        seed_credentials(
            store,
            secrets_service.credentials,
            credentials,
        )
        models = inference_models(settings)
        runtime.models = models
        service = ProductSlack(runtime)
        runtime.product_slack = service
        configure(runtime)
        converter = encrypted_converter(runtime.security, objects)
        try:
            await product_inference.start(recovery=False)
            async with await WorkflowEnvironment.start_local(
                plugins=[PydanticAIPlugin()],
                data_converter=converter,
            ) as temporal:
                runtime.client = temporal.client
                skills = await workspace.environment.exec("mkdir -p skills", cwd="/workspace")
                if skills.return_code != 0:
                    raise RuntimeError("Could not prepare the workspace skill directory.")
                credential_tools = credential_capability(secrets_service.execution)
                runtime.agent, runtime.durability = make_agent(
                    settings,
                    runtime.database,
                    stores,
                    objects,
                    models=models,
                    workspace=workspace,
                    skills_directory="skills",
                    product_capabilities=[credential_tools],
                )
                workspace_context = getattr(workspace, "runtime_context", None)
                with workspace_context(runtime.database) if workspace_context else nullcontext():
                    async with make_worker(runtime):
                        yield SimpleNamespace(
                            runtime=runtime,
                            service=service,
                            temporal=temporal,
                            offered_tool_names=sorted(credential_tools.get_toolset().tools),
                        )
        finally:
            await product_inference.close()
            await stores.close()
            store.close()


async def execute_turn(
    case: dict[str, Any],
    *,
    trial: int,
    thread: list[dict[str, Any]],
    service: ProductSlack,
    runtime: Any,
    temporal: Any,
    thread_ts: str | None = None,
    current: str | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    stamp = 1_700_000_000 + trial * 10
    current = current or f"{stamp + len(thread) + 1:.6f}"
    thread_ts = thread_ts or current
    history = [{
        "ts": message.get("ts") or f"{stamp + index * 0.1:.6f}",
        "user": "UBOT00001" if message["role"] == "you" else "UTEST0001",
        "username": message["display_name"],
        "text": message["text"],
        **({"files": [{"id": "FROOT01"}]} if message["files"] else {}),
    } for index, message in enumerate(case["thread"])]
    # Slack's replies API includes the current event. The host verifies that
    # the root exists and removes the current message from model history.
    if not any(message["ts"] == current for message in history):
        history.append({"ts": current, "user": "UTEST0001", "text": case["prompt"]})
    transport = slack_transport(history)
    original_client = httpx.AsyncClient

    def routed_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.setdefault("transport", transport)
        return original_client(*args, **kwargs)

    payload = {
        "type": "event_callback",
        "team_id": "TTEST0001",
        "event_id": event_id or f"eval-{case['id']}-{trial}",
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
        session = await db.get(Session, slack_thread.session_id)
        # Keep pending calls: a deferred question can receive its result during
        # this continuation. IDs remain meaningful if the SDK compacts history.
        previous_calls = completed_tool_call_ids(session.conversation if session else None, runtime.security)
    # Answer commands also create a completed acknowledgement. Dispatch the
    # queued continuation, not whichever row was inserted most recently.
    turn = await runtime.database.claim_next()
    if turn is not None:
        if turn.session_id != slack_thread.session_id:
            raise RuntimeError("Another chat has a queued turn; local turns must run serially.")
        handle = await temporal.client.start_workflow(
            TurnWorkflow.run,
            turn.id,
            id=turn.workflow_id,
            task_queue=runtime.settings.temporal_queue,
        )
        try:
            await asyncio.wait_for(handle.result(), timeout=300)
        except BaseException:
            await handle.cancel()
            raise
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
        if turn is not None:
            completed = await db.get(Turn, turn.id)
            if completed is None:
                raise RuntimeError(f"Turn {turn.id} disappeared after completion.")
        else:
            # Invalid /answer, /status, and other controls can produce a reply
            # without running an agent. Preserve any still-pending question.
            session = await db.get(Session, slack_thread.session_id)
            completed = await db.get(Turn, session.active_turn) if session and session.active_turn else None
    reply = next((
        "\n\n".join(
            block["text"]["text"] for block in item.get("blocks", [])
            if block.get("type") == "section" and isinstance(block.get("text"), dict)
        ) or item.get("text", "")
        for item in reversed(posted)
        if item.get("text") or any(block.get("type") == "section" for block in item.get("blocks", []))
    ), "")
    questions = []
    if completed is not None and completed.status == "waiting":
        pending = pending_questions(runtime.database, completed)
        if pending:
            questions = [
                TypeAdapter(AskUserRequest).dump_python(AskUserRequest.from_tool_call(call), mode="json")
                for call in pending.calls if call.tool_name == "ask_user_question"
            ]
    return {
        "case": case["id"],
        "trial": trial,
        "slack_event_input": payload,
        "slack_thread_input": thread,
        "prompt_received": runtime.security.unseal(completed.prompt).decode() if completed else "",
        "attached_image_names": [],
        "posted_reply": reply,
        "tool_trace": conversation_trace(
            completed.conversation, runtime.security, exclude_call_ids=previous_calls,
        ) if turn is not None and completed else [],
        "tokens_cost": json.loads(completed.usage or "{}") if completed else {},
        "status": completed.status if completed else "completed",
        "error": completed.error or "" if completed else "",
        "questions": questions,
        "turn_id": completed.id if completed else None,
        "session_id": slack_thread.session_id,
    }
