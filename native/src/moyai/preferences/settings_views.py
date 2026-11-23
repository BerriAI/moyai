"""Response models for the routes defined in settings_api.py.

These are the source of the generated frontend types (native/web/generated/api).
"""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from pydantic import BaseModel, ConfigDict


class View(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class SettingsDeleted(View):
    deleted: bool


class SettingsCrudCapabilities(View):
    create: bool
    update: bool
    delete: bool


class SettingsSharedCapabilities(SettingsCrudCapabilities):
    shared: bool


class SettingsSecretCapabilities(SettingsSharedCapabilities):
    agent_access: bool


class SettingsAutomationCapabilities(View):
    create: bool
    update: bool
    delete: bool
    run: bool
    runtime_available: bool
    execution_backend: Literal["temporal"]
    reason: str


class SettingsOverviewUser(View):
    id: str
    name: str
    role: str


class SettingsOverviewCapabilities(View):
    skills: SettingsSharedCapabilities
    memory: SettingsCrudCapabilities
    secrets: SettingsSecretCapabilities
    automations: SettingsAutomationCapabilities
    users: bool
    performance: bool
    usage: bool


class SettingsEngine(View):
    agent: str
    harness: str
    durability: str
    sandbox: str


class SettingsOverview(View):
    user: SettingsOverviewUser
    # Keyed by settings destination: skills, secrets, automations, connections,
    # memory, and users (administrators only).
    counts: dict[str, int]
    capabilities: SettingsOverviewCapabilities
    engine: SettingsEngine


class SettingsSkill(View):
    id: str
    name: str
    description: str
    content: str
    scope: Literal["personal", "shared", "bundled"]
    enabled: bool
    editable: bool
    updated_at: str | None


class SettingsSkillList(View):
    skills: list[SettingsSkill]
    capabilities: SettingsSharedCapabilities


class SettingsSecret(View):
    id: str
    name: str
    description: str
    scope: Literal["personal", "shared"]
    editable: bool
    updated_at: str


class SettingsSecretList(View):
    secrets: list[SettingsSecret]
    capabilities: SettingsSecretCapabilities
    reason: str


class SettingsMemoryFile(View):
    namespace: str
    path: str
    content: str
    version: str
    truncated: bool


class SettingsMemoryNamespace(View):
    id: str
    label: str


class SettingsMemoryList(View):
    memories: list[SettingsMemoryFile]
    namespaces: list[SettingsMemoryNamespace]
    default_namespace: str
    namespace: str
    truncated: bool
    capabilities: SettingsCrudCapabilities


class SettingsMemoryWrite(View):
    namespace: str
    path: str
    content: str
    version: str | None


class SettingsAccount(View):
    id: str
    username: str
    display_name: str
    role: str
    enabled: bool
    auth_version: int


class SettingsAccountList(View):
    users: list[SettingsAccount]
    capabilities: SettingsCrudCapabilities


class SettingsAutomation(View):
    id: str
    name: str
    prompt: str
    session_id: str
    model: str
    connections: list[str]
    interval_minutes: int
    paused: bool
    state: str
    updated_at: str
    editable: bool


class SettingsAutomationList(View):
    automations: list[SettingsAutomation]
    capabilities: SettingsAutomationCapabilities
    reason: str


class SettingsAutomationLog(View):
    id: str
    turn_id: str | None
    status: str
    created_at: str


class SettingsAutomationLogs(View):
    logs: list[SettingsAutomationLog]


# Analytics (/api/settings/performance and /api/settings/usage).
#
# TypedDicts, not BaseModels: unified billing reshapes several aggregates, and
# NotRequired keeps "sometimes absent" distinct from "present but null" in the
# schema and in the serialized output.

PricingStatus = Literal["priced", "partial", "unpriced", "empty"]


class AnalyticsUsage(TypedDict):
    """Usage totals (analytics.UsageTotals.view, or analytics_billing.metrics)."""

    requests: int
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    cost_usd: float | None
    cost_usd_exact: str | None
    priced_requests: int
    unpriced_requests: int
    cost_coverage: float | None
    pricing_status: PricingStatus
    legacy_turns: int
    unknown_request_turns: int
    capture_gap_turns: int
    token_samples: int
    usage_samples: int
    token_coverage: float | None
    # Unified billing only.
    reasoning_tokens: NotRequired[int | None]
    unknown_requests: NotRequired[int]


class AnalyticsTurnMetrics(TypedDict):
    turns: int
    completed: int
    failed: int
    latency_samples: int
    mean_latency_ms: float | None
    p50_latency_ms: float | None
    p95_latency_ms: float | None
    priced_turns: int
    unpriced_turns: int


class AnalyticsPRStatusCounts(TypedDict):
    open: int
    draft: int
    closed: int
    merged: int
    unknown: int


class AnalyticsPRMetrics(TypedDict):
    prs_created: int
    prs_merged: int
    pr_status_counts: AnalyticsPRStatusCounts
    prs_open: int
    prs_draft: int
    prs_closed: int
    prs_unknown: int
    prs_merged_current: int
    pr_status_coverage: float | None


class AnalyticsSessionCounts(TypedDict):
    sessions: int
    sessions_active: int
    sessions_created: int


class AnalyticsSummary(AnalyticsUsage, AnalyticsTurnMetrics, AnalyticsSessionCounts, AnalyticsPRMetrics):
    cost_per_merged_pr_usd: float | None


class AnalyticsModelUsage(AnalyticsUsage):
    model: str
    # Unified billing only.
    priced_turns: NotRequired[int]
    unpriced_turns: NotRequired[int]


class AnalyticsDailyUsage(AnalyticsUsage, AnalyticsTurnMetrics, AnalyticsSessionCounts, AnalyticsPRMetrics):
    date: str
    models: list[AnalyticsModelUsage]
    cumulative_cost_usd: float | None
    cumulative_cost_usd_exact: str | None
    cumulative_pricing_status: PricingStatus


class AnalyticsUserAggregate(TypedDict):
    """A per-user row. Unified billing can add spend-only rows without activity."""

    user_id: str
    turns: NotRequired[int]
    completed: NotRequired[int]
    failed: NotRequired[int]
    latency_samples: NotRequired[int]
    mean_latency_ms: NotRequired[float | None]
    p50_latency_ms: NotRequired[float | None]
    p95_latency_ms: NotRequired[float | None]
    priced_turns: NotRequired[int]
    unpriced_turns: NotRequired[int]
    requests: NotRequired[int]
    input_tokens: NotRequired[int | None]
    output_tokens: NotRequired[int | None]
    total_tokens: NotRequired[int | None]
    cache_read_tokens: NotRequired[int | None]
    cache_write_tokens: NotRequired[int | None]
    reasoning_tokens: NotRequired[int | None]
    cost_usd: NotRequired[float | None]
    cost_usd_exact: NotRequired[str | None]
    priced_requests: NotRequired[int]
    unpriced_requests: NotRequired[int]
    unknown_requests: NotRequired[int]
    cost_coverage: NotRequired[float | None]
    pricing_status: NotRequired[PricingStatus]
    legacy_turns: NotRequired[int]
    unknown_request_turns: NotRequired[int]
    capture_gap_turns: NotRequired[int]
    token_samples: NotRequired[int]
    usage_samples: NotRequired[int]
    token_coverage: NotRequired[float | None]
    sessions: NotRequired[int]
    sessions_active: NotRequired[int]
    sessions_created: NotRequired[int]
    prs_created: NotRequired[int]
    prs_merged: NotRequired[int]
    pr_status_counts: NotRequired[AnalyticsPRStatusCounts]
    prs_open: NotRequired[int]
    prs_draft: NotRequired[int]
    prs_closed: NotRequired[int]
    prs_unknown: NotRequired[int]
    prs_merged_current: NotRequired[int]
    pr_status_coverage: NotRequired[float | None]
    cost_per_merged_pr_usd: NotRequired[float | None]


class AnalyticsTurnTiming(TypedDict):
    """performance_report.timing_view plus per-card timing statuses."""

    timing_observed: bool
    timing_start_source: str | None
    timing_surface: str | None
    api_received_at: str | None
    worker_picked_up_at: str | None
    broker_received_at: str | None
    first_dispatch_at: str | None
    dispatch_attempt_id: str | None
    dispatch_route: str | None
    api_to_dispatch_ms: float | None
    queue_pickup_ms: float | None
    broker_preparation_ms: float | None
    api_to_worker_ms: float | None
    worker_to_broker_ms: float | None
    broker_to_dispatch_ms: float | None
    computer_requested_at: str | None
    computer_ready_at: str | None
    computer_mode: str | None
    computer_status: str | None
    computer_ready_ms: float | None
    startup_timing_status: str
    queue_timing_status: str
    broker_timing_status: str
    computer_timing_status: str


class AnalyticsTurn(AnalyticsUsage, AnalyticsTurnTiming):
    id: str
    user_id: str
    # Present when user_id was mapped to a canonical identity.
    actor_id: NotRequired[str]
    model: str
    status: str
    cost_models: list[str]
    usage_role: Literal["steering_input", "inference_turn"]
    created_at: str
    accepted_at: str | None
    finished_at: str | None
    tool_calls: int
    queue_ms: float | None
    latency_ms: float | None
    total_ms: float | None


class AnalyticsReportUser(TypedDict):
    id: str
    username: str
    display_name: str
    email: NotRequired[str | None]
    filterable: NotRequired[bool]
    role: NotRequired[str]
    enabled: NotRequired[bool]
    auth_version: NotRequired[int]


class AnalyticsTimingDistribution(TypedDict):
    samples: int
    mean_ms: float | None
    p50_ms: float | None
    p95_ms: float | None
    max_ms: float | None


class AnalyticsTimingCard(AnalyticsTimingDistribution):
    pending: int
    missing: int
    excluded: int
    total: int
    reasons: dict[str, int]


class AnalyticsTimingCards(TypedDict):
    startup: AnalyticsTimingCard
    queue: AnalyticsTimingCard
    broker: AnalyticsTimingCard
    computer: AnalyticsTimingCard


class AnalyticsTimingInterval(AnalyticsTimingCards):
    start: str
    end: str


class AnalyticsTimingBreakdown(TypedDict):
    samples: int
    api_to_dispatch_ms: AnalyticsTimingDistribution
    api_to_worker_ms: AnalyticsTimingDistribution
    worker_to_broker_ms: AnalyticsTimingDistribution
    broker_to_dispatch_ms: AnalyticsTimingDistribution


class AnalyticsComputerTiming(TypedDict):
    cold: AnalyticsTimingDistribution
    restore: AnalyticsTimingDistribution
    reuse: AnalyticsTimingDistribution
    requested: int
    failed: int
    pending: int


class AnalyticsPerformance(TypedDict):
    eligible_turns: int
    api_received_samples: int
    dispatch: AnalyticsTimingDistribution
    missing_dispatch_samples: int
    breakdown: AnalyticsTimingBreakdown
    computer: AnalyticsComputerTiming
    accounted_turns: int
    cards: AnalyticsTimingCards
    trend: list[AnalyticsTimingInterval]


class AnalyticsCoverage(TypedDict):
    legacy_turns: int
    ledger_requests: int
    unknown_request_turns: int
    capture_gap_turns: int
    undispatched_failed_turns: int
    linked_cost_limited: bool
    priced_requests: int
    unpriced_requests: int
    pr_receipts: int
    pr_status_checked: int
    pr_status_unknown: int
    pr_merge_date_unknown: int
    unclassified_confirmed_create_calls: int
    pr_tracking: str
    # Unified billing only.
    legacy_observations: NotRequired[int]
    native_observations: NotRequired[int]
    unknown_requests: NotRequired[int]
    private_details_omitted: NotRequired[int]
    private_activity_details_omitted: NotRequired[int]
    activity_legacy_turns: NotRequired[int]
    activity_unknown_request_turns: NotRequired[int]
    activity_capture_gap_turns: NotRequired[int]


class AnalyticsMethodology(TypedDict):
    startup: str
    startup_breakdown: str
    computer_readiness: str
    timing_coverage: str
    timing_cards: str
    users: str
    sessions: str
    prs_created: str
    prs_merged: str
    cost: str
    requests: str
    coverage: str
    model: str
    cost_per_merged_pr: str


class AnalyticsCapabilities(TypedDict):
    pr_analytics: bool
    reported_costs: bool
    unified_billing: bool


class AnalyticsLimits(TypedDict):
    max_turns: int
    max_inferences: int
    max_pr_status_reads: int
    max_baseline_bytes_per_cohort: int


class AnalyticsReport(TypedDict):
    rows: list[AnalyticsTurn]
    summary: AnalyticsSummary
    series: list[AnalyticsDailyUsage]
    by_user: list[AnalyticsUserAggregate]
    by_model: list[AnalyticsModelUsage]
    models: list[str]
    users: list[AnalyticsReportUser]
    total: int
    limit: int
    offset: int
    start: str
    end: str
    preset: Literal["24h", "7d", "30d", "custom"]
    timezone: Literal["UTC"]
    currency: Literal["USD"]
    performance: AnalyticsPerformance
    coverage: AnalyticsCoverage
    methodology: AnalyticsMethodology
    capabilities: AnalyticsCapabilities
    limits: AnalyticsLimits
    cost_note: str
    latency_note: str
