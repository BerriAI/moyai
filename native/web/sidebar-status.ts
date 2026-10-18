import type { SidebarSession } from "./session-actions"

export const statusGroups = [
  { key: "needs-you", label: "Needs you" },
  { key: "in-progress", label: "In progress" },
  { key: "in-review", label: "In review" },
  { key: "backlog", label: "Backlog" },
] as const
export type SessionStatusGroup = typeof statusGroups[number]["key"]

/** Read-only history cannot provide a current execution or review state. */
export function sessionStatusGroup(row: SidebarSession): SessionStatusGroup {
  if (row.source === "legacy" || row.read_only || row.archived || row.deleted) return "backlog"
  if (["waiting", "failed"].includes(row.status)) return "needs-you"
  if (["queued", "running", "saving"].includes(row.status)) return "in-progress"
  if (row.pull_requests?.some(pr => pr.accessible && pr.url && pr.state === "open" && pr.draft === false)) return "in-review"
  return "backlog"
}

/** Keep families together, surfacing a waiting or active child with its parent. */
export function groupSessionFamilies(roots: SidebarSession[], children: Map<string, SidebarSession[]>): Map<SessionStatusGroup, SidebarSession[]> {
  const groups = new Map<SessionStatusGroup, SidebarSession[]>(statusGroups.map(group => [group.key, []]))
  const rank = (group: SessionStatusGroup) => statusGroups.findIndex(item => item.key === group)
  function familyStatus(row: SidebarSession): SessionStatusGroup {
    let status = sessionStatusGroup(row)
    for (const child of children.get(row.key ?? `${row.source ?? "native"}:${row.id}`) ?? []) {
      const childStatus = familyStatus(child)
      if (rank(childStatus) < rank(status)) status = childStatus
    }
    return status
  }
  for (const row of roots) groups.get(familyStatus(row))!.push(row)
  for (const items of groups.values()) items.sort((a, b) => Number(Boolean(b.pinned)) - Number(Boolean(a.pinned)))
  return groups
}

export function compactSessionAge(value?: string, now = Date.now()): string {
  if (!value) return ""
  const timestamp = Date.parse(value)
  if (!Number.isFinite(timestamp)) return ""
  const minutes = Math.max(0, Math.floor((now - timestamp) / 60000))
  return minutes < 1 ? "now" : minutes < 60 ? `${minutes}m` : minutes < 1440 ? `${Math.floor(minutes / 60)}h` : `${Math.floor(minutes / 1440)}d`
}
