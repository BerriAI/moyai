import type { ChartRow, ChartSeries } from "./analytics-render.js"
import { finiteValue } from "./analytics-format"
import type {
  AnalyticsPerformance, AnalyticsReport as GeneratedAnalyticsReport, AnalyticsReportUser, AnalyticsSummary, AnalyticsTimingCard,
  AnalyticsTimingCards, AnalyticsTimingInterval, AnalyticsUserAggregate,
} from "./generated/api"

export type PRState = keyof AnalyticsSummary["pr_status_counts"]
export type ReportUser = AnalyticsReportUser
export type ReportSummary = AnalyticsSummary
export type TimingCard = AnalyticsTimingCard
export type TimingCardKey = keyof AnalyticsTimingCards
export type TimingInterval = AnalyticsTimingInterval
export type NativePerformance = AnalyticsPerformance
export type UserAggregate = AnalyticsUserAggregate
export type UserReportRow = { user_id: string; label: string; secondary?: string; search: string; metrics: UserAggregate }
export type UserSortKey = "user" | "sessions" | "turns" | "requests" | "tokens" | "spend" | "prs_created" | "prs_merged" | "cost_per_merge"
export type UserSort = { key: UserSortKey; direction: "asc" | "desc" }
export type AnalyticsReport = GeneratedAnalyticsReport

function displayIdentity(value: string | null | undefined): string {
  const label = value?.trim() ?? ""
  return /^product:[a-f0-9]{32,}$/i.test(label) ? "" : label
}

/** Exact-ID union of aggregate projections; never join people by display name.
 * The current API already combines both domains in by_user. Separate usage/PR
 * fragments also merge without summing aggregates or turning absent fields into zero.
 */
export function combinedUserRows(aggregates: readonly UserAggregate[], users: readonly ReportUser[]): UserReportRow[] {
  const identities = new Map(users.map(user => [user.id, user]))
  const metrics = new Map<string, UserAggregate>()
  for (const row of aggregates) {
    const defined = Object.fromEntries(Object.entries(row).filter(([, value]) => value !== undefined))
    metrics.set(row.user_id, { ...metrics.get(row.user_id), ...defined, user_id: row.user_id })
  }
  return [...metrics].map(([user_id, row]) => {
    const user = identities.get(user_id)
    const name = displayIdentity(user?.display_name), email = displayIdentity(user?.email), username = displayIdentity(user?.username)
    const label = name || email || username || "Unidentified user"
    const secondary = [email, username].find(value => value && value.toLocaleLowerCase() !== label.toLocaleLowerCase())
    return {
      user_id, label, secondary, metrics: row,
      search: [label, secondary, user_id, user?.username, user?.email].filter(Boolean).join(" ").toLocaleLowerCase(),
    }
  })
}

/** This is the API's attributed ratio, not selected-range spend divided in the browser. */
export function reportedCostPerMerge(row: UserAggregate): number | null {
  return finiteValue(row.prs_merged) && row.prs_merged > 0 && finiteValue(row.cost_per_merged_pr_usd)
    ? row.cost_per_merged_pr_usd : null
}

export function sortUserRows(rows: readonly UserReportRow[], sort: UserSort, prAvailable: boolean): UserReportRow[] {
  function value(row: UserReportRow): unknown {
    const metrics = row.metrics
    switch (sort.key) {
      case "sessions": return metrics.sessions_active ?? metrics.sessions
      case "turns": return metrics.turns
      case "requests": return metrics.requests
      case "tokens": return metrics.total_tokens
      case "spend": return metrics.cost_usd
      case "prs_created": return prAvailable ? metrics.prs_created : null
      case "prs_merged": return prAvailable ? metrics.prs_merged : null
      case "cost_per_merge": return prAvailable ? reportedCostPerMerge(metrics) : null
    }
  }
  return [...rows].sort((left, right) => {
    const names = left.label.localeCompare(right.label) || left.user_id.localeCompare(right.user_id)
    if (sort.key === "user") return sort.direction === "asc" ? names : -names
    const a = value(left), b = value(right)
    // Unavailable values stay last in either direction; a reported zero remains real data.
    if (finiteValue(a) !== finiteValue(b)) return finiteValue(a) ? -1 : 1
    return finiteValue(a) && finiteValue(b) && a !== b ? (a - b) * (sort.direction === "asc" ? 1 : -1) : names
  })
}

export function partialCost(value: { pricing_status?: string; unpriced_requests?: number; unpriced_turns?: number; unknown_request_turns?: number; capture_gap_turns?: number; cost_coverage?: number | null }): boolean {
  return value.pricing_status === "partial" || (value.capture_gap_turns ?? 0) > 0 || (value.unknown_request_turns ?? 0) > 0 || (value.unpriced_requests ?? value.unpriced_turns ?? 0) > 0 || (finiteValue(value.cost_coverage) && value.cost_coverage < 1)
}

/** Sum only authoritative numeric observations; an empty/unpriced set stays null. */
export function reportedSubtotal(values: unknown[]): number | null {
  const known = values.filter(finiteValue)
  if (!known.length) return null
  const total = known.reduce((sum, value) => sum + value, 0)
  return Number.isFinite(total) ? total : null
}

/** Group chart colors without converting missing model costs to free usage. */
export function modelHistory(data: Pick<AnalyticsReport, "by_model" | "series">) {
  const models = [...(data.by_model ?? [])].sort((a, b) => {
    if (finiteValue(a.cost_usd) !== finiteValue(b.cost_usd)) return finiteValue(a.cost_usd) ? -1 : 1
    return (b.cost_usd ?? 0) - (a.cost_usd ?? 0) || a.model.localeCompare(b.model)
  })
  const groups = models.slice(0, 4).map((model, index) => ({ key: `model_${index}`, label: model.model, models: [model.model] }))
  if (models.length > 4) groups.push({ key: "other", label: "Other models", models: models.slice(4).map(model => model.model) })
  const series: ChartSeries[] = groups.map(({ key, label }) => ({ key, label }))
  let running: number | null = null, cumulativeMatches = true
  const rows: ChartRow[] = data.series.map(day => {
    const row: ChartRow = { date: day.date, partial: partialCost(day) || day.cumulative_pricing_status === "partial", cumulative_cost_usd: day.cumulative_cost_usd ?? null }
    const entries = new Map((day.models ?? []).map(model => [model.model, model]))
    for (const group of groups) row[group.key] = reportedSubtotal(group.models.map(model => entries.get(model)?.cost_usd))
    const subtotal = reportedSubtotal(groups.map(group => row[group.key]))
    if (subtotal !== null) running = (running ?? 0) + subtotal
    if (running === null ? day.cumulative_cost_usd != null : !finiteValue(day.cumulative_cost_usd) || Math.abs(day.cumulative_cost_usd - running) > 1e-6 * Math.max(1, running)) cumulativeMatches = false
    return row
  })
  return { models, series, rows, cumulative: cumulativeMatches && running !== null }
}

export const prStates: { key: PRState; label: string }[] = [
  { key: "merged", label: "Merged" }, { key: "open", label: "Open" }, { key: "draft", label: "Draft" },
  { key: "closed", label: "Closed" }, { key: "unknown", label: "Unknown" },
]

export function validPRDistribution(counts: ReportSummary["pr_status_counts"] | undefined, created?: number): counts is Record<PRState, number> {
  return Boolean(counts && finiteValue(created) && prStates.every(state => finiteValue(counts[state.key])) && prStates.reduce((sum, state) => sum + counts![state.key], 0) === created)
}
