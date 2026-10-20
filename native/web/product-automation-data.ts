import type {
  EventTriggerInput, ProductAutomationDefinition, ProductAutomationHistoryEntry, ProductAutomationPreview,
  ProductAutomationSummary, TimingOutput, ProductAutomationTrigger,
  OrganizationConnection,
} from "./generated/api"

export type {
  ProductAutomationHistoryPage as AutomationHistoryPage, ProductAutomationInventory as AutomationInventory,
  ProductAutomationRunResult as AutomationRunResult, ProductAutomationWebhookInfo as AutomationWebhookInfo,
  ProductAutomationWebhookResult as AutomationWebhookResult, ProductAutomationTestResult as AutomationTestResult,
} from "./generated/api"

export type AutomationTiming = TimingOutput
/** Editor state: a saved event, or a partially filled one; the service validates every field. */
export type AutomationEvent = EventTriggerInput & { event: string; [key: string]: unknown }
export type AutomationTrigger = Pick<ProductAutomationTrigger, "id"> & { schedule?: AutomationTiming | null; event?: AutomationEvent | null }
export type AutomationDefinition = Omit<ProductAutomationDefinition, "triggers"> & { triggers: AutomationTrigger[] }
export type ProductAutomation = ProductAutomationSummary
export type AutomationPreview = ProductAutomationPreview
export type AutomationHistory = ProductAutomationHistoryEntry
export type { ProductEnvironment as AutomationEnvironment } from "./generated/api"
export type AutomationConnection = Pick<OrganizationConnection, "id" | "connected" | "enabled" | "read_only"> & { tools?: OrganizationConnection["tools"] }

export const calendarFrequencies: { value: AutomationTiming["frequency"]; label: string }[] = [
  { value: "hourly", label: "Hourly" }, { value: "daily", label: "Daily" }, { value: "weekdays", label: "Weekdays" },
  { value: "weekly", label: "Weekly" }, { value: "cron", label: "Custom cron" }, { value: "once", label: "One time" },
]
export function automationStatus(row: Pick<ProductAutomation, "revision" | "legacy_paused" | "native">): string {
  if (!row.native) return row.legacy_paused ? "Not migrated · Paused" : "Not migrated"
  if (row.native.sync_state === "blocked") return "Blocked"
  if (row.native.sync_state !== "ready" || row.native.synced_generation !== row.native.generation) return "Sync pending"
  if (row.native.source_revision !== row.revision) return "Needs review"
  if (row.native.state === "deleted") return "Retired"
  return row.native.state === "active" ? "Enabled" : "Paused"
}
export function automationActions(preview: AutomationPreview) {
  const supported = preview.supported && preview.blockers.length === 0, binding = preview.native, caps = preview.capabilities
  const synchronized = binding?.sync_state === "ready" && binding.synced_generation === binding.generation && binding.source_revision === preview.revision
  return {
    edit: caps?.update === true,
    migrate: caps?.migrate === true && supported,
    enable: caps?.enable === true && supported && binding?.state === "paused" && synchronized,
    pause: caps?.pause === true && (binding?.state === "active" || !preview.legacy_paused),
    run: caps?.run === true && supported && synchronized && Boolean(binding) && binding?.state !== "deleted",
    retire: caps?.retire === true && Boolean(binding) && binding?.state !== "deleted",
  }
}
export function eventFieldVisible(event: AutomationEvent, name: string): boolean {
  if (event.provider === "slack") return event.event === "reaction.added"
    ? !["text_contains", "text_starts_with", "sender_type", "include_thread_replies"].includes(name) : name !== "reaction"
  if (event.provider === "github") {
    const kind = event.event.split(".")[0]
    if (name === "conclusion") return kind === "check_run"
    if (name === "label") return !["check_run", "push"].includes(kind)
    if (name === "branch") return kind.startsWith("pull_request") || ["check_run", "push"].includes(kind)
  }
  return true
}
/** Event sources come from the server's event_choices; only those providers are offered. */
export function isAutomationProvider(value: string, choices: Record<string, unknown>): value is AutomationEvent["provider"] {
  return Object.hasOwn(choices, value)
}
/** Changing event type clears only fields that the existing editor disables. */
export function changeAutomationEvent(event: AutomationEvent, name: string): AutomationEvent {
  const result: AutomationEvent = { ...event, event: name }
  for (const field of ["text_contains", "text_starts_with", "reaction", "conclusion", "label", "branch"]) {
    if (!eventFieldVisible(result, field)) result[field] = ""
  }
  if (result.provider === "slack" && name === "reaction.added") {
    result.sender_type = "any"; result.include_thread_replies = false
  }
  return result
}
/** A renamed repository must be resolved again instead of retaining an unrelated numeric ID. */
export function changeAutomationEventField(event: AutomationEvent, field: string, value: unknown): AutomationEvent {
  return { ...event, [field]: value, ...(field === "repository" && value !== event.repository ? { repository_id: null } : {}) }
}
export function changeAutomationRepository(definition: AutomationDefinition, repoUrl: string): AutomationDefinition {
  return { ...definition, repo_url: repoUrl, github_repository_id: repoUrl === definition.repo_url ? definition.github_repository_id : null }
}
export function automationMetadata(entries: { key: string; value: string }[]): Record<string, string> {
  const keys = entries.map(entry => entry.key.trim())
  if (keys.some(key => !key)) throw new Error("Give each metadata entry a key.")
  if (new Set(keys).size !== keys.length) throw new Error("Use a different key for each metadata entry.")
  return Object.fromEntries(entries.map((entry, index) => [keys[index], entry.value]))
}
export function automationSamplePayload(value: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(value)
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("The sample must be a JSON object.")
  if (new TextEncoder().encode(value).length > 256 * 1024) throw new Error("Keep the sample below 256 KB.")
  return parsed as Record<string, unknown>
}
const blockerMessages: Record<string, string> = {
  scheduled_chat_unavailable: "Choose an active shared chat you own, or start a new chat each run.",
  execution_mode_not_supported: "This execution mode is not supported by the new runtime.",
  too_many_triggers: "The new runtime supports at most 16 triggers.",
  event_trigger_adapter_required: "Event triggers are not supported by this migration yet.",
  connection_mapping_required: "Connected apps require an explicit migration mapping.",
  repository_bootstrap_adapter_required: "Repository preparation is unavailable in this deployment.",
  repository_identity_requires_verified_metadata: "Save a verified repository selection, or select an environment built with its permanent GitHub repository identity.",
  environment_adapter_required_choose_none_explicitly: "The saved environment needs migration. Select base tools explicitly only if this workflow does not need it.",
  automated_self_management_grants_not_supported: "Automatic changes to this workflow’s instructions or schedule are not supported yet.",
  delivery_adapter_required: "The saved delivery destination requires a delivery adapter.",
  prompt_exceeds_native_limit: "Instructions exceed the new runtime’s 500,000-character limit.",
  model_not_enabled_in_native_core: "This model is not enabled in the new runtime.",
  model_not_enabled: "This model is not enabled.",
  legacy_occurrences_must_finish: "Earlier executions must finish before migration.",
  owner_action_required: "Only the owner can edit or migrate this definition.",
  slack_event_source_requires_installation_scopes_and_shared_authority: "The Slack source needs an installed app, the required scopes, and shared account access.",
  specific_legacy_session_requires_continuation_mapping: "This saved session filter needs a continuation mapping before it can receive new messages.",
  selected_connections_require_onboarding_or_authorization: "Set up or authorize the selected connections before activation.",
  selected_environment_requires_its_current_repository_connection: "Select the current GitHub connection for this environment’s repository.",
}
export function automationBlocker(code: string): string {
  if (code.startsWith("webhook_receiver_not_configured:")) return `Configure the ${code.split(":")[1]} webhook receiver before activation.`
  if (code.startsWith("once_already_consumed:")) return `One-time trigger ${code.split(":")[1]} has already run.`
  if (code.startsWith("once_requires_future_time:")) return `Choose a future date for one-time trigger ${code.split(":")[1]}.`
  return blockerMessages[code] ?? code.replaceAll("_", " ")
}
export function historySessionHash(row: AutomationHistory): string | undefined {
  if (!row.session_id) return undefined
  if (row.engine === "legacy") return `#run=${encodeURIComponent(row.session_id)}`
  if (row.engine === "pydantic-v1") return `#${encodeURIComponent(row.session_id)}`
  return undefined
}
/** One-time editor uses explicit UTC wall time, so browser timezone and DST cannot shift it. */
export function onceUtcInput(value: string | null): string {
  if (!value) return ""
  const instant = new Date(value)
  return Number.isNaN(instant.valueOf()) ? "" : instant.toISOString().slice(0, 19)
}
export function onceUtcInstant(value: string): string {
  if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?$/.test(value)) throw new Error("Choose a valid one-time date and time in UTC.")
  const normalized = value.length === 16 ? `${value}:00` : value
  const instant = new Date(`${normalized}Z`)
  if (Number.isNaN(instant.valueOf()) || instant.toISOString().slice(0, 19) !== normalized) throw new Error("Choose a valid one-time date and time in UTC.")
  return instant.toISOString()
}
export function validateAutomation(definition: AutomationDefinition, now = Date.now(), requireFuture = true): void {
  if (definition.name.trim().length < 2 || definition.name.trim().length > 100) throw new Error("Use an automation name between 2 and 100 characters.")
  if (definition.prompt.trim().length < 3 || definition.prompt.trim().length > 500_000) throw new Error("Instructions must contain 3 to 500,000 characters.")
  if (definition.triggers.length < 1 || definition.triggers.length > 16) throw new Error("Use between 1 and 16 triggers.")
  if (new Set(definition.triggers.map(trigger => trigger.id)).size !== definition.triggers.length) throw new Error("Each trigger needs a unique identifier.")
  if (definition.max_runs_per_hour !== null && (!Number.isSafeInteger(definition.max_runs_per_hour) || definition.max_runs_per_hour < 1)) throw new Error("The hourly cap must be a positive whole number.")
  if (definition.repo_url && !/^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/?$/.test(definition.repo_url.trim())) throw new Error("Use a https://github.com/owner/repository URL.")
  for (const trigger of definition.triggers) {
    if (!/^[A-Za-z0-9_-]{1,40}$/.test(trigger.id)) throw new Error("A saved trigger identity is invalid. Refresh this definition.")
    if (Boolean(trigger.schedule) === Boolean(trigger.event)) throw new Error("Each trigger needs exactly one schedule or event source.")
    const timing = trigger.schedule
    if (!timing) continue // Canonical provider validation remains on the service.
    if (!calendarFrequencies.some(item => item.value === timing.frequency)) throw new Error("This schedule type is not supported by the editor.")
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(timing.time)) throw new Error("Choose a valid 24-hour time.")
    if (!Number.isInteger(timing.weekday) || timing.weekday < 0 || timing.weekday > 6) throw new Error("Choose a valid weekday.")
    try { new Intl.DateTimeFormat("en", { timeZone: timing.timezone }).format() } catch { throw new Error("Choose a valid IANA timezone, such as America/Los_Angeles.") }
    if (timing.frequency === "cron") {
      const fields = timing.cron.trim().split(/\s+/), ranges = [[0, 59], [0, 23], [1, 31], [1, 12], [0, 7]]
      if (fields.length !== 5) throw new Error("Use five cron fields: minute hour day month weekday.")
      fields.forEach((field, index) => field.split(",").forEach(part => {
        if (!/^(\*|\d+(?:-\d+)?)(?:\/\d+)?$/.test(part)) throw new Error("Cron supports numbers, *, lists, ranges, and / steps.")
        const [value, step] = part.split("/"), numbers = value === "*" ? [] : value.split("-").map(Number), [low, high] = ranges[index]
        if (numbers.some(number => number < low || number > high) || (numbers.length === 2 && numbers[0] > numbers[1]) || (step !== undefined && (+step < 1 || +step > high - low + 1))) throw new Error("Cron contains an out-of-range value.")
      }))
    }
    if (timing.frequency === "once" && (!timing.run_at || !Number.isFinite(Date.parse(timing.run_at)) || (requireFuture && Date.parse(timing.run_at) <= now))) throw new Error("Choose a future one-time date.")
  }
}
/** Transport retries keep one identity; changed intent gets a new receipt key. */
export class AutomationRequestKeys {
  private previous: { signature: string; key: string } | null = null
  forIntent(path: string, body: unknown): string {
    const signature = JSON.stringify([path, body])
    if (this.previous?.signature !== signature) this.previous = { signature, key: crypto.randomUUID() }
    return this.previous.key
  }
  complete(): void { this.previous = null }
}
