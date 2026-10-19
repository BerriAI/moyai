"""Response models for the routes defined in api.py.

These are the source of the generated frontend types (native/web/generated/api).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel

from .general_preferences import GeneralPreferences


class MeUser(BaseModel):
    id: str
    name: str
    role: str
    individual: bool


class Me(BaseModel):
    user: MeUser
    csrf: str
    models: list[str]
    default_model: str
    preferences: GeneralPreferences
    product_mode: Literal["existing", "standalone"]


class ApiView(BaseModel):
    """Response payload: fields with defaults are still always present in output."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class HealthStatus(ApiView):
    status: Literal["ok"]
    engine: str
    durability: str


class AuthConfig(ApiView):
    mode: Literal["existing", "standalone"]
    password_enabled: bool
    google_enabled: bool
    access_enabled: bool


class LoginResult(ApiView):
    csrf: str
    # Product (organization) password sign-in only.
    authenticated: bool | None = None
    role: Literal["admin", "member"] | None = None


class LogoutResult(ApiView):
    signed_out: bool
    logout_url: str | None = None


class InvocableSkill(ApiView):
    id: str
    name: str
    description: str
    scope: str
    reference: str
    icon: str
    # Canonical (product) skill catalog only.
    revision: int | None = None
    archived: bool | None = None
    created_at: str | None = None
    updated_at: str | None = None
    can_manage: bool | None = None
    source: str | None = None


class InvocableSkillPage(ApiView):
    skills: list[InvocableSkill]
    has_more: bool
    next_offset: int | None
    # Canonical (product) skill catalog only.
    can_publish: bool | None = None
    capabilities: dict[str, bool] | None = None
    source: str | None = None
    note: str | None = None


class SessionSummary(ApiView):
    id: str
    title: str
    owner_id: str
    visibility: str
    surface: str
    model: str
    status: str
    active_turn: str | None
    archived: bool
    deleted: bool
    # A pydantic-ai WorkspaceRef; opaque to the web app beyond presence.
    workspace: dict[str, JsonValue] | None
    save_warning: str | None
    updated_at: str


class SessionListItem(SessionSummary):
    can_manage: bool
    pinned: bool
    folder: str
    participated: bool
    unread: bool


class SessionDetailSession(SessionSummary):
    can_manage: bool
    pinned: bool
    folder: str
    unread: bool
    swarm_id: str | None = None
    parent_session_id: str | None = None


class TurnView(ApiView):
    id: str
    session_id: str
    actor_id: str
    model: str
    status: str
    mode: str
    parent_turn: str | None
    enqueue_id: str | None
    connections: list[str]
    accepted_at: str | None
    # pydantic-ai RunUsage counters.
    usage: dict[str, JsonValue]
    error: str | None
    created_at: str
    finished_at: str | None


class GoalView(ApiView):
    objective: str
    status: Literal["active", "paused", "complete", "blocked", "budget_limited"]
    owner_id: str
    generation: int
    tokens_used: int
    token_budget: int | None
    turns: int
    blocked_count: int
    reason: str
    created_at: str


class GoalCommandResult(ApiView):
    goal: GoalView | None
    help: str | None = None


class GoalStartedResult(TurnView):
    goal: GoalView


class SessionTurn(TurnView):
    guidance_at: str | None
    is_self: bool


class MessageAttachment(ApiView):
    id: str
    name: str
    media_type: str
    size: int


class MessageAttachmentStatus(ApiView):
    file_id: str | None = None
    upload_id: str | None = None
    name: str | None = None
    bytes: int | None = None
    media_type: str | None = None
    error: Literal["unavailable"] | None = None
    preview_error: Literal["preview_unavailable"] | None = None


class SessionMessage(ApiView):
    id: int
    turn_id: str
    role: str
    author_id: str | None
    author_name: str | None
    is_self: bool
    content: str
    created_at: str
    attachments: list[MessageAttachment]
    source: Literal["slack", "delegation"] | None = None
    # Slack provenance (message_sources.source_view) only.
    source_url: str | None = None
    source_timestamp: str | None = None
    attachment_status: list[MessageAttachmentStatus] | None = None
    context_status: str | None = None
    edited: bool | None = None
    edited_in: Literal["web", "slack"] | None = None
    deleted: bool | None = None
    edited_after_start: bool | None = None
    attachments_changed: bool | None = None
    legacy_review_required: bool | None = None


class SlackDeliveryStatus(ApiView):
    pending: int
    sending: int
    rejected: int


class SlackDeliveryRetry(ApiView):
    retried: int


class SessionEffect(ApiView):
    id: str
    turn_id: str
    tool_name: str
    arguments: JsonValue


class PendingEffect(SessionEffect):
    created_at: str


class PendingToolCall(ApiView):
    """A pydantic-ai ToolCallPart awaiting an answer or approval."""

    tool_name: str
    args: str | dict[str, JsonValue] | None
    tool_call_id: str
    tool_kind: str | None = None
    id: str | None = None
    provider_name: str | None = None
    provider_details: dict[str, JsonValue] | None = None
    part_kind: str


class PendingRequests(ApiView):
    calls: list[PendingToolCall]
    approvals: list[PendingToolCall]
    # Per tool_call_id metadata; its shape depends on the tool (e.g. browser sign-in).
    metadata: dict[str, dict[str, JsonValue]]


class CredentialRequestRef(ApiView):
    request_id: str


class SessionReference(ApiView):
    """The product read adapter's view of a native session (resolve=true)."""

    id: str
    title: str
    owner_id: str
    visibility: str
    surface: str
    model: str
    status: str
    active_turn: str | None
    archived: bool
    deleted: bool
    created_at: str
    updated_at: str
    pinned: bool
    folder: str
    created_by_me: bool
    participated: bool
    parent_session_id: str | None
    # Present when product continuations are configured.
    source_session_id: str | None = None
    source_format: str
    execution_engine: str
    read_only: bool
    unread: bool
    workspace: None
    save_warning: None
    capabilities: dict[str, bool]
    can_manage: bool
    source: Literal["native", "legacy"]
    key: str
    url: str


class ToolCallActivity(ApiView):
    type: Literal["tool_call"]
    id: str
    name: str
    arguments: str | dict[str, JsonValue] | None


class ToolResultActivity(ApiView):
    type: Literal["tool_result"]
    id: str
    name: str
    content: str | None
    failed: bool


SavedActivity = Annotated[ToolCallActivity | ToolResultActivity, Field(discriminator="type")]


class ResponseStartEvent(ApiView):
    type: Literal["response_start"]
    run_id: str
    step: int


class TextEvent(ApiView):
    type: Literal["text_start", "text_delta", "text_end"]
    index: int
    text: str
    phase: Literal["commentary", "final_answer"] | None


class AgentFinishedEvent(ApiView):
    type: Literal["agent_finished"]
    run_id: str


class CapabilityUpdateEvent(ApiView):
    type: Literal["capability"]
    name: str
    # asdict() of a public pydantic-ai capability update; shapes vary by capability.
    data: dict[str, Any]


class LiveEvent(RootModel[Annotated[
    ResponseStartEvent | TextEvent | ToolCallActivity | ToolResultActivity | AgentFinishedEvent | CapabilityUpdateEvent,
    Field(discriminator="type"),
]]):
    """One `data:` frame of GET /api/sessions/{id}/events (see presentation.public_event)."""


class SessionDetail(ApiView):
    goal: GoalView | None = None
    session: SessionDetailSession
    turns: list[SessionTurn]
    has_unsettled_effects: bool
    effects: list[SessionEffect]
    transcript_version: str | None = None
    slack_delivery: SlackDeliveryStatus | None = None
    reference: SessionReference | None = None
    activity: list[SavedActivity] | None = None
    can_answer: bool | None = None
    pending: PendingRequests | None = None
    can_reply_with_text: bool | None = None
    question_deadline: str | None = None
    credential_requests: list[CredentialRequestRef] | None = None
    messages: list[SessionMessage] | None = None


class WorkCommentary(ApiView):
    id: str
    text: str
    phase: Literal["commentary"]
    created_at: str


class WorkProgressMessage(ApiView):
    id: str
    text: str
    phase: Literal["commentary", "final_answer"]
    created_at: str


class WorkToolProgress(ApiView):
    id: str
    name: str
    status: Literal["running", "completed", "failed", "interrupted"]
    created_at: str
    finished_at: str | None


class WorkAgentProgress(ApiView):
    id: str
    session_id: str | None
    parent_id: str | None
    parent_name: str | None
    name: str
    avatar: int
    depth: int
    status: str
    created_at: str
    updated_at: str
    messages: list[WorkProgressMessage]
    tools: list[WorkToolProgress]
    messages_limited: bool
    tools_limited: bool


class TurnWorkProgress(ApiView):
    messages: list[WorkCommentary]
    # Present only with include_activity=true.
    tools: list[WorkToolProgress] | None = None
    subagents: list[WorkAgentProgress] | None = None
    tools_limited: bool | None = None
    subagents_limited: bool | None = None


class WorkActionDetail(WorkToolProgress):
    input: str | None
    output: str | None
    restricted: bool
    input_truncated: bool
    output_truncated: bool


class SessionUpdateResult(ApiView):
    updated: bool


class SessionDeleteResult(ApiView):
    deleted: bool


class SessionRestoreResult(ApiView):
    restored: bool


class QueuedTurnUpdateResult(ApiView):
    updated: bool


class QueuedTurnCancelResult(ApiView):
    cancelled: bool


class SessionStopResult(ApiView):
    status: Literal["stopping"]


class EffectResolveResult(ApiView):
    recorded: bool


class ConnectionPolicyView(ApiView):
    allowed_tools: list[str] | None = None
    require_approval: bool = True
    product: dict[str, JsonValue] | None = None


class ConnectionView(ApiView):
    id: str
    name: str
    kind: str
    owner_id: str
    shared: bool
    read_only: bool
    enabled: bool
    revision: int
    policy: ConnectionPolicyView
    source: Literal["product"] | None = None


class SessionFile(ApiView):
    id: str
    name: str
    media_type: str
    size: int
    created_at: str


class PlanStep(ApiView):
    id: str
    content: str
    status: Literal["pending", "in_progress", "completed", "cancelled", "blocked"]
    active_form: str
    parent_id: str | None
    depends_on: list[str]
