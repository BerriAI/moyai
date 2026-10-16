import type { AutomationDefinition, AutomationHistory, AutomationTiming, AutomationTrigger } from "./product-automation-data"
export const automationSources: Record<string, string>
export const automationEventFields: Record<string, [string, string, string, boolean?][]>
export const automationSetupNotes: Record<string, string>
export function automationDefaultSchedule(): Pick<AutomationTiming, "frequency" | "time" | "weekday" | "timezone">
export function automationTriggers(definition: Pick<AutomationDefinition, "triggers">): AutomationTrigger[]
export function automationTiming(timing: AutomationTiming): string
export function automationHistoryMerge(first: AutomationHistory[], second: AutomationHistory[]): AutomationHistory[]
export function automationRunStatus(row: Pick<AutomationHistory, "status" | "outcome">): string
