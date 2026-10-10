import { BookOpen, Brain, ChartColumn, Clock, FolderCode, KeyRound, Link, Terminal, Users, type LucideIcon } from "lucide-react"
import registry from "./settings-navigation.json"
import { isView, type Role, type View } from "../legacy/bridge"

// The JSON registry is shared with the remaining controllers (settings rail,
// command palette) through window.MoyaiConfig.
export type SettingsItem = { view: View; title: string; description: string; admin?: boolean; memberOnly?: boolean }
export type SettingsGroup = { id: string; title: string; admin?: boolean; items: SettingsItem[] }

export const settingsGroups = registry as SettingsGroup[]
for (const item of settingsGroups.flatMap(group => group.items)) {
  if (!isView(item.view)) throw new Error(`Unknown settings destination: ${item.view}`)
}

export const settingsIcons: Partial<Record<View, LucideIcon>> = {
  automations: Clock, skills: BookOpen, memory: Brain, connections: Link, secrets: KeyRound,
  spend: ChartColumn, runtime: Terminal, environments: FolderCode, users: Users,
}

export const visibleTo = (role: Role) => (entry: { admin?: boolean; memberOnly?: boolean }) =>
  (!entry.admin || role === "admin") && (!entry.memberOnly || role !== "admin")
