import { BookOpen, Brain, Clock, FolderCode, KeyRound, Link, Terminal, Users, ChartColumn, type LucideIcon } from "lucide-react"
import type { View } from "../legacy/bridge"

type Item = { view: View; title: string; description: string; icon: LucideIcon; admin?: boolean; memberOnly?: boolean }
type Group = { id: string; title: string; admin?: boolean; items: Item[] }
export const groups: Group[] = [
  { id: "workflows", title: "Workflows", items: [
    { view: "automations", title: "Automations", description: "Schedule recurring work and review past runs.", icon: Clock },
    { view: "skills", title: "Skills", description: "Save instructions for yourself or your team.", icon: BookOpen },
    { view: "memory", title: "Memory", description: "Review what Moyai remembers for your future sessions.", icon: Brain },
  ] },
  { id: "integrations", title: "Integrations", items: [
    { view: "connections", title: "Connections", description: "Connect GitHub, Slack, Linear, and Notion.", icon: Link },
    { view: "secrets", title: "Secrets", description: "Manage personal and shared service access.", icon: KeyRound },
  ] },
  { id: "workspace", title: "Workspace", items: [
    { view: "spend", title: "Spend", description: "See your own LLM usage and costs.", memberOnly: true, icon: ChartColumn },
    { view: "runtime", title: "Runtime", description: "Check cloud setup and session limits.", icon: Terminal },
    { view: "environments", title: "Environments", description: "Prepare repositories and workspace tools.", admin: true, icon: FolderCode },
  ] },
  { id: "administration", title: "Administration", admin: true, items: [
    { view: "users", title: "Users", description: "Manage workspace members and roles.", admin: true, icon: Users },
    { view: "spend", title: "Spend & usage", description: "Review team costs, model usage, and human activity.", admin: true, icon: ChartColumn },
  ] },
]
export const visible = (item: Item, role: "admin" | "member") => (!item.admin || role === "admin") && (!item.memberOnly || role !== "admin")
