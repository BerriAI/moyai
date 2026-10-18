"""Native Pydantic AI capability composition; the SDK owns execution."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta
from functools import cached_property
from pathlib import Path
from typing import Annotated, Any

import anyio
import modal
from openai import AsyncOpenAI, DefaultAsyncHttpxClient
from opentelemetry.trace import get_tracer
from pydantic import Field, TypeAdapter
from pydantic_ai import Agent, DeferredToolRequests, RunContext
from pydantic_ai.capabilities import AbstractCapability, SetToolMetadata, durable_operation
from pydantic_ai.capabilities.abstract import WrapToolExecuteHandler
from pydantic_ai.durable_exec.temporal import TemporalDurability, TemporalRunContext
from pydantic_ai.exceptions import CallDeferred, ModelRetry, ToolFailed
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models import Model, ModelRequestContext
from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings
from pydantic_ai.providers.litellm import LiteLLMProvider
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset
from pydantic_ai.workspaces import WorkspaceRef
from pydantic_ai_harness import (
    AskUser,
    ConversationSearch,
    Memory,
    ModalSandbox,
    Planning,
    Skills,
    StepPersistence,
    SummarizingCompaction,
)
from pydantic_ai_harness.ask_user import AskUserRequest, Question
from pydantic_ai_harness.conversation_search import SnapshotHistorySource
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

from .artifacts import artifact_capability
from .browser import browser_capability
from .browser_login import BrowserLogin
from .bundled_skills import bundled_skill_catalog
from .commentary import Commentary
from .computer_startup import OnDemandDurability, on_demand_coder
from .connection_catalog import ConnectionToolSearch
from .connections import connection_tools
from .contracts import GuidanceChanged, RunScope
from .database import Database
from .delegation import InteractiveDelegation
from .effects import EffectReceipts
from .goal_tools import GoalCapability
from .inference_usage import InferenceUsageTransport
from .input_attachments import attachment_capability
from .input_identity import DELEGATED_INPUT, input_identity
from .live_events import root_event_stream
from .request_limits import UnlimitedModelRequests
from .settings import Settings
from .settings_runtime import settings_capability
from .storage import ObjectStore
from .stores import NativeStores
from .swarm_tools import SwarmCapability

PACKAGE_ROOT = Path(__file__).resolve().parent / "resources"
if not PACKAGE_ROOT.is_dir():
    PACKAGE_ROOT = Path(__file__).resolve().parents[2]
READ_TOOLS = ["read_file", "list_files", "grep", "read_memory", "search_memory", "read_plan",
              "search_conversation_history"]
PLAN_TOOLS = {
    "write_plan", "read_plan", "add_task", "update_task_status", "update_task_statuses",
    "remove_task", "add_subtask", "set_dependency", "get_available_tasks",
}


class HarnessActivityContext(TemporalRunContext[RunScope]):
    """Supply a worker-local tracer to native Harness memory operations.

    Tracers are live process resources, so they are reconstructed at the documented
    context boundary rather than serialized into Temporal history.
    """

    @classmethod
    def serialize_run_context(cls, ctx: RunContext[Any]) -> dict[str, Any]:
        serialized = super().serialize_run_context(ctx)
        serialized["metadata"] = {
            **(serialized.get("metadata") or {}), "moyai_input_id": input_identity(ctx),
        }
        return serialized

    @classmethod
    def deserialize_run_context(
        cls, ctx: dict[str, Any], deps: Any
    ) -> HarnessActivityContext:
        return cls(**ctx, deps=deps, tracer=get_tracer("moyai.harness"))


class DurableQuestions(AskUser[RunScope]):
    """Give 0.55's native question toolset the stable ID required by Temporal."""

    @cached_property
    def registered_toolset(self) -> AbstractToolset[RunScope]:
        native = super().get_toolset()
        if not isinstance(native, FunctionToolset):
            raise TypeError("The native question capability changed its toolset contract.")
        original = native.tools["ask_user_question"].function

        async def ask_user_question(
            ctx: RunContext[RunScope],
            questions: Annotated[list[Question], Field(min_length=1, max_length=10)],
            required: bool = True,
        ) -> dict[str, list[str]] | str:
            """Ask a clarification with options; the user can also type their answer.

            Ask only when the answer cannot be found in the conversation or workspace.
            Use required=False for optional preferences with a reasonable fallback.
            Optional questions may time out with no answer; state your assumption then.
            Use required=True when missing information prevents correct work.
            Never use this tool for permission, credentials, or approval of an action.

            Args:
                questions: One to ten questions with unique headers and concrete options.
                required: Whether an explicit answer is necessary to continue.
            """
            try:
                request = AskUserRequest.from_tool_call(ToolCallPart(
                    "ask_user_question", {"questions": [question.model_dump() for question in questions]},
                    tool_call_id=ctx.tool_call_id or "",
                ))
            except ValueError as error:
                raise ModelRetry("Use valid questions with unique headers and distinct option labels.") from error
            try:
                return await original(ctx, list(request.questions))
            except CallDeferred as error:
                if required:
                    raise
                raise CallDeferred(metadata={
                    **(error.metadata or {}), "moyai_question_required": False,
                }) from None

        # Retain the SDK's validation, events and result shape. Optional intent
        # belongs to the persisted deferred call, never a browser-only timer.
        return FunctionToolset(
            tools=[ask_user_question], id="moyai_questions"
        ).with_metadata(temporal=False)

    def get_toolset(self) -> AbstractToolset[RunScope]:
        return self.registered_toolset


class DurableConversationSearch(ConversationSearch[RunScope]):
    """Keep the native read tool registered across runs in Harness 0.55."""

    @cached_property
    def registered_toolset(self) -> AbstractToolset[RunScope]:
        native = super().get_toolset()
        if not isinstance(native, AbstractToolset):
            raise TypeError("The native history search capability changed its toolset contract.")
        return native

    def get_toolset(self) -> AbstractToolset[RunScope]:
        return self.registered_toolset


class StartupSkills(Skills[RunScope]):
    """Load packaged skills without a computer, retaining old workflow reads."""

    def __init__(self, directory: str, *, catalog: Skills[RunScope] | None = None) -> None:
        super().__init__(directory)
        self.catalog = catalog

    async def for_run(self, ctx: RunContext[RunScope]) -> AbstractCapability[RunScope]:
        if self.catalog is not None and (
            not workflow.in_workflow() or workflow.patched("moyai-bundled-skill-catalog-v1")
        ):
            return await self.catalog.for_run(ctx)
        if not workflow.in_workflow():
            return await super().for_run(ctx)
        # AnyIO level cancellation can repeatedly cancel Temporal's activity
        # handle before its cancellation command can leave the workflow task.
        # Shield only catalog preparation, never model/tool execution. Native
        # workspace activities retain their current authority checks and bounds.
        prepared: AbstractCapability[RunScope] | None = None
        with anyio.CancelScope(shield=True):
            prepared = await super().for_run(ctx)
        if prepared is None:
            raise RuntimeError("Native skill preparation did not settle.")
        return prepared


class WorkflowPlanning(Planning[RunScope]):
    """Keep native plan events in the workflow; its store operations remain durable."""

    @cached_property
    def registered_toolset(self) -> AbstractToolset[RunScope]:
        native = super().get_toolset()
        if not isinstance(native, AbstractToolset):
            raise TypeError("The native planning capability changed its toolset contract.")
        return native.with_metadata(temporal=False)

    def get_toolset(self) -> AbstractToolset[RunScope]:
        return self.registered_toolset

    async def prepare_tools(
        self, ctx: RunContext[RunScope], tool_defs: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        # Workflow-side plan operations must not interleave by nondeterministic task timing.
        return [
            replace(tool, sequential=True) if tool.name in PLAN_TOOLS else tool
            for tool in tool_defs
        ]


class Authority(AbstractCapability[RunScope]):
    """The application's current-authority check, through the native operation API."""

    id = "moyai_authority"

    def __init__(self, database: Database):
        self.database = database

    def get_model_settings(self):
        def for_request(ctx: RunContext[RunScope]) -> OpenAIResponsesModelSettings:
            return OpenAIResponsesModelSettings(
                openai_user=ctx.deps.actor_id,
                extra_body={"metadata": {
                    "moyai_session_id": ctx.deps.session_id,
                    "moyai_turn_id": ctx.deps.turn_id,
                    "moyai_run_id": ctx.run_id,
                }},
            )
        return for_request

    @durable_operation("check")
    async def check(self, scope: RunScope, input_id: str | None = None) -> bool:
        try:
            await self.database.authorize_scope(scope, input_id=input_id)
        except GuidanceChanged:
            # Expected supersession crosses the durable boundary as data.
            return False
        return True

    @durable_operation("save_workspace")
    async def save_workspace(self, scope: RunScope, reference: dict[str, Any]) -> None:
        await self.database.save_workspace_reference(scope, reference)

    async def before_model_request(
        self, ctx: RunContext[RunScope], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        await self.check(ctx.deps)
        if ctx.workspace.ref:
            await self.save_workspace(
                ctx.deps, TypeAdapter(WorkspaceRef).dump_python(ctx.workspace.ref, mode="json")
            )
        return request_context

    async def before_tool_execute(
        self, ctx: RunContext[RunScope], *, call: ToolCallPart,
        tool_def: ToolDefinition, args: dict[str, Any],
    ) -> dict[str, Any]:
        if not await self.check(ctx.deps, input_identity(ctx)):
            # Pair the skipped call with a result so native pending guidance can be consumed.
            raise ToolFailed("New guidance arrived before dispatch; this action did not execute.")
        return args

    async def wrap_tool_execute(
        self, ctx: RunContext[RunScope], *, call: ToolCallPart, tool_def: ToolDefinition,
        args: dict[str, Any], handler: WrapToolExecuteHandler,
    ) -> Any:
        # Native self-delegation runs workflow-side and inherits this task-local
        # admission identity. Already-running delegates retain their old identity.
        token = DELEGATED_INPUT.set(input_identity(ctx)) if call.tool_name == "delegate_task" else None
        try:
            return await handler(args)
        except ActivityError as error:
            cause = error.cause
            if isinstance(cause, ApplicationError) and cause.type == "MoyaiGuidanceChanged":
                raise ToolFailed("New guidance arrived before dispatch; this action did not execute.") from error
            raise
        finally:
            if token is not None:
                DELEGATED_INPUT.reset(token)


def inference_models(settings: Settings) -> dict[str, Model]:
    # Temporal is the request-retry owner. Do not multiply attempts inside the SDK client.
    client = AsyncOpenAI(
        base_url=settings.gateway_url, api_key=settings.gateway_key.get_secret_value() or "not-configured",
        max_retries=0, timeout=float(settings.model_timeout_seconds),
        http_client=DefaultAsyncHttpxClient(
            transport=InferenceUsageTransport(require_admission=settings.product_mode == "existing"),
            headers={"Accept-Encoding": "identity"}, follow_redirects=False,
        ),
    )
    provider = LiteLLMProvider(openai_client=client)
    return {name: OpenAIResponsesModel(name, provider=provider) for name in settings.models}



def workspace_image() -> modal.Image:
    """The same image definition for lazy startup and deployment prewarming."""
    return (
        modal.Image.debian_slim(python_version="3.13")
        .apt_install("git", "ripgrep", "curl", "jq", "util-linux", "nodejs", "npm",
                     "xvfb", "openbox", "xterm", "xdotool", "chromium", "tint2", "x11vnc",
                     "x11-xserver-utils", "fonts-dejavu-core")
        .pip_install("uv", "pytest", "pillow==12.3.0", "websockify==0.13.0", "playwright==1.63.0")
        .add_local_dir(PACKAGE_ROOT / "skills", "/opt/moyai/skills", copy=True)
        .add_local_file(PACKAGE_ROOT / "desktop.py", "/opt/moyai/desktop.py", copy=True)
        .add_local_file(PACKAGE_ROOT / "browser_guest.py", "/opt/moyai/browser_guest.py", copy=True)
        .add_local_file(PACKAGE_ROOT / "browser_driver.py", "/opt/moyai/browser_driver.py", copy=True)
    )


def make_agent(
    settings: Settings, database: Database, stores: NativeStores, objects: ObjectStore,
    *, models: dict[str, Model] | None = None,
    workspace: AbstractCapability[RunScope] | None = None,
    skills_directory: str = "/opt/moyai/skills",
    product_capabilities: Sequence[AbstractCapability[RunScope]] = (),
    memory: AbstractCapability[RunScope] | None = None,
) -> tuple[Agent[RunScope, str | DeferredToolRequests], TemporalDurability[RunScope]]:
    models = models or inference_models(settings)
    if workspace is None:
        from .lambda_workspace import WorkspaceProviders
        workspace = WorkspaceProviders(
            settings, ModalSandbox(
                image=workspace_image(), app_name=settings.modal_app, working_dir="/workspace",
                sandbox_timeout=settings.computer_lifetime, idle_timeout=settings.computer_idle_timeout,
            ),
        )
    durability: TemporalDurability[RunScope] = OnDemandDurability(
        models=models,
        run_context_type=HarnessActivityContext,
        activity_config={
            "start_to_close_timeout": timedelta(minutes=10),
            "schedule_to_close_timeout": timedelta(minutes=10),
            "heartbeat_timeout": timedelta(seconds=30),
            "retry_policy": RetryPolicy(maximum_attempts=1),
        },
        model_activity_config={
            "start_to_close_timeout": timedelta(seconds=settings.model_timeout_seconds),
            "schedule_to_close_timeout": timedelta(seconds=settings.model_timeout_seconds * 3),
            "heartbeat_timeout": timedelta(seconds=30),
            "retry_policy": RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=1)),
        },
        event_stream_handler=root_event_stream,
    )
    authority = Authority(database)
    agent = Agent(
        models[settings.default_model],
        name="moyai",
        deps_type=RunScope,
        output_type=[str, DeferredToolRequests],
        retries=2,
        instructions=(PACKAGE_ROOT / "instructions.md").read_text(),
        capabilities=[
            UnlimitedModelRequests(),
            workspace,
            authority,
            InteractiveDelegation(database),
            SwarmCapability(database.swarms),
            GoalCapability(database.goals),
            Commentary(database),
            EffectReceipts(database),
            *product_capabilities,
            connection_tools(database.security),
            ConnectionToolSearch(
                strategy="keywords", max_results=6,
                tool_description=(
                    "Find tools for the selected connected accounts. Search by service, repository, "
                    "and action before calling a connected tool. Only matching definitions are loaded."
                ),
            ),
            artifact_capability(database, objects),
            attachment_capability(database, objects),
            browser_capability(database),
            BrowserLogin(database),
            on_demand_coder(authority.save_workspace),
            WorkflowPlanning(
                store_resolver=lambda ctx: stores.plan(
                    ctx.deps.session_id if ctx.run_id == ctx.deps.root_run_id
                    else f"{ctx.deps.session_id}/delegate/{ctx.run_id}"
                ),
                enable_subtasks=True,
            ),
            StartupSkills(
                skills_directory,
                catalog=bundled_skill_catalog(PACKAGE_ROOT / "skills", skills_directory)
                if skills_directory == "/opt/moyai/skills" else None,
            ),
            settings_capability(database),
            memory or Memory(
                store=stores.memory, namespace=lambda ctx: ctx.deps.memory_namespace, injection_errors="raise",
            ),
            StepPersistence(store=stores.steps, capture_frontier=True),
            DurableConversationSearch(SnapshotHistorySource(stores.steps), scope="conversation"),
            SummarizingCompaction(max_fraction=0.75, keep_messages=20, receipts=True),
            DurableQuestions(answerer=None, id="ask_user"),
            SetToolMetadata(tools=READ_TOOLS, temporal={
                "start_to_close_timeout": timedelta(seconds=60),
                "schedule_to_close_timeout": timedelta(minutes=3),
                "retry_policy": RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=1)),
            }),
            durability,
        ],
    )
    return agent, durability

