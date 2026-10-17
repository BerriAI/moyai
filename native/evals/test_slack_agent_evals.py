from __future__ import annotations

import asyncio
import html
import json
import os
import re
import threading
from collections.abc import AsyncIterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
import sqlalchemy
from app.credentials import SaveSecret
from app.db import Store
from app.security import Security as ProductSecurity
from openai import APIError, AsyncOpenAI
from pydantic import SecretStr
from pydantic_ai import ConversationTypeAdapter
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ToolCallPart, ToolReturnPart
from sqlalchemy import text
from temporalio.testing import WorkflowEnvironment
from test_computer_startup import lazy_local_workspace
from test_product_identity import settings_for

from moyai.agent import inference_models, make_agent
from moyai.models import SlackThread, Turn
from moyai.product_connections import ProductConnections
from moyai.product_credential_tools import capability as credential_capability
from moyai.product_secrets import ProductSecrets
from moyai.product_slack import ProductSlack, ProductSlackAuthority
from moyai.runtime import configure
from moyai.settings import Settings
from moyai.storage import ObjectStore, encrypted_converter
from moyai.stores import NativeStores
from moyai.worker import make_worker
from moyai.workflow import TurnWorkflow

FIXTURES = Path(__file__).parent / "fixtures"
CASES = sorted(
    (json.loads(path.read_text()) for path in (Path(__file__).parent / "cases").glob("*.json")),
    key=lambda item: item["id"],
)
REPEATS = int(os.getenv("MOYAI_EVAL_REPEATS", "3"))
MODEL_COMPLETION = {
    "id": "stub-1",
    "object": "chat.completion",
    "model": "deepseek-flash",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 1, "total_tokens": 13},
}
JUDGE_SYSTEM = """You grade one Slack reply written by a coding agent named Moyai. You get the Slack thread it answered, the agent's tool trace for this turn, the exact reply text it posted, a reference reply written by a strong agent for the same situation, and case notes describing what the situation allows. The trace is the only evidence of what the agent did. Facts in the reply must be supported by the trace, the thread, or files the trace shows it read.

Score each criterion 0, 1, or 2:
- verified: 2 if the answer rests on the check the case notes call for (a live call when one is allowed and useful, no call when the requester forbade it or no credential exists); 1 if it checked something weaker; 0 if it answered from assumption.
- verdict_first: 2 if the first sentence states the answer to the question; 1 if the answer appears later; 0 if there is no clear answer.
- correct: 2 if every factual claim (names, prices, results, limits) matches the trace or fixture; 1 for a minor slip; 0 for a material error.
- context: 2 if it answers what the requester meant given the thread; 0 if it answers a different question or hedges across products the requester did not ask about.
- value_add: 2 if it surfaces at least one useful finding beyond the bare answer (as the reference does) and offers a concrete next step when there is one; 1 if it only answers; 0 if it adds nothing and leaves the requester to do the work.
- action: 2 if it did the obvious authorized next step itself; 0 if it asked a question or deferred work it could have done.
- concise: 2 if it is direct and Slack-sized with no apologies, self-narration, or filler; 1 if somewhat padded; 0 if mostly filler. Literal markdown artifacts such as stray ** count against this.

Also set unsupported_claim to true if the reply says or implies it ran, tested, or observed something the trace does not show. The reference shows one good answer, not the only one; do not penalize different wording or a different but valid check. Return only JSON: {"verified":n,"verdict_first":n,"correct":n,"context":n,"value_add":n,"action":n,"concise":n,"unsupported_claim":bool,"reason":"<=3 sentences"}."""
CRITERIA = ("verified", "verdict_first", "correct", "context", "value_add", "action", "concise")


def json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def parse_judgment(raw: str) -> dict[str, Any]:
    candidate = raw.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Judge response did not contain a JSON object.")
    parsed = json.loads(candidate[start:end + 1])
    if not isinstance(parsed, dict):
        raise TypeError("Judge response was not a JSON object.")
    return parsed


async def probe(client: AsyncOpenAI, model: str) -> str:
    response = await client.responses.create(
        model=model, input="Reply with OK only.", max_output_tokens=8,
    )
    return response.output_text


async def select_models() -> tuple[str, str, AsyncOpenAI]:
    base = os.environ["LITELLM_API_BASE"]
    key = os.environ["LITELLM_API_KEY"]
    client = AsyncOpenAI(base_url=base, api_key=key, max_retries=0, timeout=30)
    requested = os.getenv("MOYAI_EVAL_AGENT_MODEL", Settings(_env_file=None).default_model)
    agent = requested
    try:
        await probe(client, agent)
    except APIError:
        if requested != "openai/gpt-6-astra":
            raise
        agent = "bedrock_mantle/openai.gpt-6-sol"
        await probe(client, agent)
    judge = os.environ["MOYAI_EVAL_JUDGE_MODEL"]
    if judge == agent:
        raise pytest.UsageError("MOYAI_EVAL_JUDGE_MODEL must differ from the agent model.")
    await probe(client, judge)
    return agent, judge, client


class ProviderServer:
    def __init__(self, image: bytes):
        self.image = image
        self.requests: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                owner.requests.append(body)
                raw = json.dumps(MODEL_COMPLETION).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(owner.image)))
                self.end_headers()
                self.wfile.write(owner.image)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    @property
    def file_url(self) -> str:
        return "https://files.slack.com/files-pri/FROOT01/download"

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()


def slack_transport(history: list[dict[str, Any]], file_url: str, fail_history: bool = False):
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/conversations.replies"):
            if fail_history:
                return httpx.Response(503, json={"ok": False, "error": "unavailable"})
            return httpx.Response(200, json={"ok": True, "messages": history})
        if path.endswith("/files.info"):
            return httpx.Response(200, json={"ok": True, "file": {
                "id": "FROOT01", "name": "devin_reply.png", "mimetype": "image/png",
                "size": (FIXTURES / "devin_reply.png").stat().st_size,
                "url_private_download": file_url,
            }})
        if path.endswith(("/chat.postMessage", "/chat.update")):
            return httpx.Response(200, json={
                "ok": True, "channel": "CTEST0001", "ts": "1792100000.001",
            })
        return httpx.Response(200, json={"ok": True})
    return httpx.MockTransport(handle)


async def ask_judge(
    client: AsyncOpenAI, model: str, case: dict[str, Any], trace: list[dict[str, Any]], reply: str,
) -> tuple[str, dict[str, Any]]:
    payload = {
        "thread": case["trial_thread"],
        "trace": trace,
        "reply": reply,
        "reference_reply": case["reference_reply"],
        "case_notes": case["case_notes"],
    }
    response = await client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": json_value(payload)},
        ],
        max_output_tokens=500,
    )
    raw = response.output_text
    return raw, parse_judgment(raw)


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


def gate_results(
    case: dict[str, Any], trace: list[dict[str, Any]], requests: list[dict[str, Any]], reply: str,
) -> dict[str, bool]:
    names = [item["name"] for item in trace]
    gates = case["gates"]
    run_calls = names.count("credentials_run")
    checks = {
        "credentials_list": not gates["credentials_list"] or "credentials_list" in names,
        "stub_model": (
            len(requests) >= gates["min_stub_requests"]
            and (gates["max_stub_requests"] is None or len(requests) <= gates["max_stub_requests"])
            and (
                gates["stub_model"] is None or any(
                    str(item.get("model", "")).removeprefix("deepseek/") == gates["stub_model"]
                    for item in requests
                )
            )
        ),
        "credentials_run_limit": gates["max_credentials_run"] is None or run_calls <= gates["max_credentials_run"],
        "no_paste_request": (
            not gates["no_paste_request"]
            or not re.search(
                r"\bpaste\b.{0,100}\b(?:api\s+)?key\b.{0,80}\b(?:here|chat|slack|thread)\b",
                reply,
                re.IGNORECASE | re.DOTALL,
            )
        ),
    }
    return checks


def trial_pass(
    case: dict[str, Any], gates: dict[str, bool], judgment: dict[str, Any],
) -> tuple[bool, list[str]]:
    failures = [f"gate:{name}" for name, value in gates.items() if not value]
    failures.extend(
        f"criterion:{name}" for name in case["required"] if judgment.get(name) != 2
    )
    if judgment.get("unsupported_claim") is not False:
        failures.append("criterion:unsupported_claim")
    score = sum(
        value if isinstance(value, int) and not isinstance(value, bool) else 0
        for value in (judgment.get(name, 0) for name in CRITERIA)
    )
    if score < 11:
        failures.append("criterion:total_score")
    return not failures, failures


def seed_credentials(store: Store, credential_store: Any, stub_url: str) -> dict[str, Any]:
    secret_values = {
        "DEEPSEEK_API_KEY": "synthetic-deepseek-eval-key",
        "DEEPSEEK_API_BASE": stub_url,
    }
    body = SaveSecret(
        provider="generic", name="deepseek", format="env", label="DeepSeek test access",
        scope="organization", lifetime="persistent", value=SecretStr(json.dumps(secret_values)),
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


async def copy_seed_credential(product: Any, row: dict[str, Any]) -> None:
    async with product.source.begin() as connection:
        await connection.execute(text("""INSERT INTO moyai.provider_secrets(
            id,provider,label,scope,owner_id,root_id,encrypted,created_at,revoked_at,client_id,
            name,format,env_var,lifetime,expires_at,invalid_reason,revision
        ) VALUES (
            :id,:provider,:label,:scope,:owner_id,:root_id,:encrypted,:created_at,:revoked_at,:client_id,
            :name,:format,:env_var,:lifetime,:expires_at,:invalid_reason,:revision
        )"""), row)


async def seed_slack_source(product: Any) -> None:
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
            read_only BOOLEAN NOT NULL DEFAULT FALSE)"""))
        await connection.execute(text("""INSERT INTO moyai.users(
            id,kind,email,name,linked_user_id,created_at,updated_at
        ) VALUES ('slack:TTEST0001:UTEST0001','slack','','Slack UTEST0001',NULL,'2026-01-01','2026-01-01')
        ON CONFLICT (id) DO NOTHING"""))
        await connection.execute(text("""INSERT INTO moyai.connection_policies(provider,enabled,read_only)
            VALUES ('slack',TRUE,FALSE) ON CONFLICT (provider) DO UPDATE SET enabled=TRUE"""))


def slack_thread_input(case: dict[str, Any]) -> list[dict[str, Any]]:
    thread = list(case["thread"])
    if thread and thread[-1]["role"] == "human" and thread[-1]["text"] == case["prompt"]:
        return thread
    thread.append({
        "role": "human", "display_name": "Ishaan Jaffer", "text": case["prompt"],
        "files": [],
    })
    return thread


def case_workspace(root: Path, case: dict[str, Any], trial: int) -> Path:
    workspace = root / "workspace" / case["id"] / f"trial-{trial}"
    (workspace / "litellm").mkdir(parents=True)
    (workspace / "provider_docs").mkdir()
    for source, target in (
        (FIXTURES / "AGENTS.md", workspace / "AGENTS.md"),
        (FIXTURES / "litellm" / "model_prices_and_context_window.json",
         workspace / "litellm" / "model_prices_and_context_window.json"),
        (FIXTURES / "provider_docs" / "deepseek-pricing.md",
         workspace / "provider_docs" / "deepseek-pricing.md"),
    ):
        target.write_bytes(source.read_bytes())
    return workspace


def failed_trial(
    case: dict[str, Any], trial: int, error: Exception, requests: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "case": case["id"],
        "trial": trial,
        "slack_thread_input": slack_thread_input(case),
        "prompt_received": "",
        "attached_image_names": [
            filename for message in case["thread"] for filename in message["files"]
        ],
        "posted_reply": "",
        "tool_trace": [],
        "stub_requests": requests,
        "gates": {"execution": False},
        "judgment": {},
        "judge_raw_json": "",
        "judge_error": f"ExecutionError: {type(error).__name__}",
        "tokens_cost": {},
        "passed": False,
        "failures": ["execution:error"],
    }


async def run_trial(
    *, case: dict[str, Any], trial: int, service: ProductSlack, runtime: Any, env: Any,
    judge_client: AsyncOpenAI, judge_model: str, provider: ProviderServer,
    file_url: str, counter: int,
) -> dict[str, Any]:
    root = f"{1_700_000_000 + counter * 10}.000000"
    current = f"{1_700_000_000 + counter * 10 + 1}.000000"
    mid_thread = case["id"] in {"followup_act_now", "thread_root_screenshot"}
    thread_ts = root if mid_thread else current
    trial_thread = slack_thread_input(case)
    history: list[dict[str, Any]] = []
    for index, message in enumerate(case["thread"]):
        history.append({
            "ts": f"{1_700_000_000 + counter * 10 + index * 0.1:.6f}",
            "user": "UBOT00001" if message["role"] == "you" else "UTEST0001",
            "username": message["display_name"],
            "text": message["text"],
            **({"files": [{"id": "FROOT01"}]} if message["files"] else {}),
        })
    transport = slack_transport(history, file_url)
    old_client = httpx.AsyncClient

    class DownloadResponse:
        status = 200

        def __init__(self) -> None:
            self.content = self
            self.headers = {
                "Content-Length": str(len(provider.image)), "Content-Encoding": "identity",
            }

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def iter_chunked(self, _size: int) -> AsyncIterator[bytes]:
            yield provider.image

    class DownloadSession:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def get(self, _url: str, **_kwargs: Any) -> DownloadResponse:
            return DownloadResponse()

    def routed_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        if "transport" not in kwargs:
            kwargs["transport"] = transport
        return old_client(*args, **kwargs)

    payload = {
        "type": "event_callback",
        "team_id": "TTEST0001",
        "event_id": f"eval-{case['id']}-{trial}",
        "event": {
            "type": "app_mention", "user": "UTEST0001", "channel": "CTEST0001",
            "channel_type": "channel", "ts": current, "thread_ts": thread_ts,
            "text": case["prompt"],
        },
    }
    with patch("httpx.AsyncClient", routed_client), patch("aiohttp.ClientSession", DownloadSession):
        await service.receive_payload(payload)

    async with runtime.database.sessions() as db:
        thread = await db.scalar(
            sqlalchemy.select(SlackThread).where(
                SlackThread.team_id == "TTEST0001",
                SlackThread.channel_id == "CTEST0001",
                SlackThread.thread_ts == thread_ts,
            )
        )
        if thread is None:
            raise RuntimeError(f"ProductSlack did not bind {case['id']}.")
        turn = await db.scalar(
            sqlalchemy.select(Turn).where(
                Turn.session_id == thread.session_id,
            ).order_by(Turn.created_at.desc())
        )
    if turn is None:
        raise RuntimeError(f"ProductSlack did not submit a turn for {case['id']}.")
    await runtime.database.claim_next()
    handle = await env.client.start_workflow(
        TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
    )
    await asyncio.wait_for(handle.result(), timeout=300)
    post_responses: list[dict[str, Any]] = []

    def post_stub(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        post_responses.append(body)
        return httpx.Response(200, json={
            "ok": True, "channel": body["channel"], "ts": "1792100000.001",
        })

    async with old_client(
        base_url="https://slack.com/api/", transport=httpx.MockTransport(post_stub), timeout=20,
    ) as delivery_client:
        for _ in range(4):
            await service.pump(delivery_client)

    async with runtime.database.sessions() as db:
        final_turn = await db.get(Turn, turn.id)
    if final_turn is None:
        raise RuntimeError(f"Turn {turn.id} disappeared after completion.")
    reply = next(
        (
            item["text"] for item in reversed(post_responses)
            if "text" in item and item["text"] != "Moyai received your message."
        ),
        "",
    )
    trace = conversation_trace(final_turn.conversation, runtime.security)
    gates = gate_results(case, trace, provider.requests, reply)
    judgment: dict[str, Any]
    raw_judgment = ""
    judge_error = ""
    try:
        raw_judgment, judgment = await ask_judge(
            judge_client, judge_model, {**case, "trial_thread": trial_thread}, trace, reply,
        )
    except (APIError, ValueError, TypeError) as error:
        judgment, judge_error = {}, f"{type(error).__name__}: {error}"
    passed, failures = trial_pass(case, gates, judgment)
    if judge_error:
        failures.append("judge:error")
        passed = False
    prompt = runtime.security.unseal(final_turn.prompt).decode()
    return {
        "case": case["id"],
        "trial": trial,
        "slack_event_input": payload,
        "slack_thread_input": trial_thread,
        "prompt_received": prompt,
        "attached_image_names": [
            filename for message in trial_thread for filename in message["files"]
        ],
        "posted_reply": reply,
        "tool_trace": trace,
        "stub_requests": provider.requests.copy(),
        "gates": gates,
        "judgment": judgment,
        "judge_raw_json": raw_judgment,
        "judge_error": judge_error,
        "tokens_cost": json.loads(final_turn.usage or "{}"),
        "passed": passed,
        "failures": failures,
    }


def write_summary(report: dict[str, Any]) -> None:
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = ["| Case | Passes | Trials |", "| --- | ---: | ---: |"]
    counts = report["case_pass_counts"]
    for case in CASES:
        lines.append(f"| {case['id']} | {counts[case['id']]} / {report['repeats']} | {report['repeats']} |")
    for item in report["trials"]:
        lines.extend([
            "",
            f"<details><summary>{html.escape(item['case'])} trial {item['trial']}: {'PASS' if item['passed'] else 'FAIL'}</summary>",
            "",
            "Input:",
            "```json",
            json_value(item["slack_thread_input"]),
            "```",
            "",
            "Prompt received:",
            "```text",
            item["prompt_received"],
            "```",
            "",
            "Attached images:",
            ", ".join(item["attached_image_names"]),
            "",
            "Posted reply:",
            "```text",
            item["posted_reply"],
            "```",
            "",
            "Judge verdict:",
            "```json",
            json_value({
            "judgment": item["judgment"], "passed": item["passed"], "failures": item["failures"],
                "raw_json": item["judge_raw_json"], "judge_error": item["judge_error"],
            }),
            "```",
            "</details>",
        ])
    with Path(summary_path).open("a") as output:
        output.write("\n".join(lines) + "\n")


@pytest.mark.asyncio
@pytest.mark.live_eval
async def test_real_slack_agent_reply_evals(admitted_product: Any, tmp_path: Path) -> None:
    if not os.getenv("MOYAI_TEST_POSTGRES"):
        pytest.skip("Set MOYAI_TEST_POSTGRES to run the real product eval.")
    agent_model, judge_model, judge_client = await select_models()
    product = admitted_product
    image = (FIXTURES / "devin_reply.png").read_bytes()
    with ProviderServer(image) as provider:
        source_settings = settings_for(
            tmp_path,
            data_dir=tmp_path / "legacy-store",
            slack_bot_enabled=True,
            slack_thread_chat_enabled=True,
            slack_session_users="UTEST0001",
            slack_signing_secret="synthetic-eval-signing-secret",
            public_url="http://127.0.0.1:8787",
        )
        await seed_slack_source(product)
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
        connections = ProductConnections(product_services, product.database)
        bot_credentials = {
            "access_token": "synthetic-slack-user-token",
            "bot": {
                "access_token": "synthetic-slack-bot-token",
                "bot_user_id": "UBOT00001",
                "team": {"id": "TTEST0001"},
                "scope": "chat:write,files:read",
            },
        }
        connections.connectors.save("slack", bot_credentials, "Eval Slack")

        settings: Settings = product.settings
        settings.default_model = agent_model
        settings.model_catalog = agent_model
        settings.gateway_url = os.environ["LITELLM_API_BASE"]
        settings.gateway_key = SecretStr(os.environ["LITELLM_API_KEY"])
        settings.data_dir = tmp_path / "native-runtime"
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
        secrets = ProductSecrets(runtime)
        runtime.product_secrets = secrets
        runtime.database.product_connections = connections
        runtime.database.product_identity = product.adapter
        runtime.database.security = runtime.security
        secret_row = seed_credentials(store, secrets.credentials, provider.base_url)
        await copy_seed_credential(product, secret_row)
        models = inference_models(settings)
        runtime.models = models
        runtime.product_slack = ProductSlack(runtime)
        service = runtime.product_slack
        configure(runtime)
        report_trials: list[dict[str, Any]] = []
        converter = encrypted_converter(runtime.security, objects)
        counter = 0
        async with await WorkflowEnvironment.start_local(
            plugins=[PydanticAIPlugin()], data_converter=converter,
        ) as environment:
            runtime.client = environment.client
            async with make_worker(runtime):
                for case in CASES:
                    for trial in range(1, REPEATS + 1):
                        provider.requests.clear()
                        counter += 1
                        try:
                            workspace = case_workspace(tmp_path, case, trial)
                            skills = tmp_path / "skills"
                            skills.mkdir(exist_ok=True)
                            allocations: list[str] = []
                            runtime.agent, runtime.durability = make_agent(
                                settings,
                                runtime.database,
                                stores,
                                objects,
                                models=models,
                                workspace=lazy_local_workspace(workspace, allocations),
                                skills_directory=str(skills),
                                product_capabilities=[credential_capability(secrets.execution)],
                            )
                            result = await run_trial(
                                case=case, trial=trial, service=service, runtime=runtime, env=environment,
                                judge_client=judge_client, judge_model=judge_model, provider=provider,
                                file_url=provider.file_url, counter=counter,
                            )
                        except Exception as error:  # noqa: BLE001 - preserve a failure record for every trial
                            result = failed_trial(case, trial, error, provider.requests.copy())
                        report_trials.append(result)
        await stores.close()
        store.close()
    case_counts = {
        case["id"]: sum(
            item["passed"] for item in report_trials if item["case"] == case["id"]
        )
        for case in CASES
    }
    report = {
        "agent_model": agent_model,
        "judge_model": judge_model,
        "repeats": REPEATS,
        "suite_passed": all(value >= 2 for value in case_counts.values()),
        "case_pass_counts": case_counts,
        "trials": report_trials,
    }
    output = Path(__file__).parent / "out" / "report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    write_summary(report)
    assert report["suite_passed"], f"One or more eval cases failed; see {output}"
