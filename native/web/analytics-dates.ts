import { analyticsDate, analyticsPreset } from "../../app/static/analytics.js?native-analytics"

export type AnalyticsPreset = "24h" | "7d" | "30d" | "custom"
export type AnalyticsRange = { preset: AnalyticsPreset; start: string; end: string }
export const analyticsPresets = [
  { value: "24h", label: "Last 24 hours" },
  { value: "7d", label: "Last 7 days" },
  { value: "30d", label: "Last 30 days" },
  { value: "custom", label: "Custom dates" },
] as const
const day = 86400000

function utcDay(value: string): number {
  const timestamp = Date.parse(`${value}T00:00:00Z`)
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || !Number.isFinite(timestamp) || new Date(timestamp).toISOString().slice(0, 10) !== value) throw new Error("Choose a valid calendar date.")
  return timestamp
}

export function customAnalyticsRange(start: string, through: string, maxDays = 366): AnalyticsRange {
  const first = utcDay(start), end = utcDay(through) + day
  if (end <= first || end - first > maxDays * day) throw new Error(`Choose an ordered date range of at most ${maxDays} days.`)
  return { preset: "custom", start: new Date(first).toISOString(), end: new Date(end).toISOString() }
}

export function presetAnalyticsRange(preset: Exclude<AnalyticsPreset, "custom"> = "7d", now = new Date()): AnalyticsRange {
  if (!Number.isFinite(now.getTime())) throw new Error("The current date is unavailable.")
  if (preset === "24h") return { preset, start: new Date(now.getTime() - day).toISOString(), end: now.toISOString() }
  // Use the same UTC calendar-day calculation as the established Moyai reports.
  const existing = analyticsPreset(preset === "7d" ? "7" : "30", now)!
  return { ...customAnalyticsRange(existing.start, existing.end), preset }
}

export function rangeDates(range: AnalyticsRange) {
  return { start: range.start.slice(0, 10), through: new Date(Date.parse(range.end) - 1).toISOString().slice(0, 10) }
}

export function rangeLabel(range: AnalyticsRange): string {
  if (range.preset !== "custom") return analyticsPresets.find(item => item.value === range.preset)!.label
  const { start, through } = rangeDates(range)
  const sameYear = start.slice(0, 4) === through.slice(0, 4)
  return `${analyticsDate(start)}${sameYear ? "" : `, ${start.slice(0, 4)}`} – ${analyticsDate(through)}${sameYear ? "" : `, ${through.slice(0, 4)}`}`
}
