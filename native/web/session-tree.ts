import { sessionIsReadOnly, sessionKey, type SidebarSession } from "./session-actions"

/** Native ancestry is independent of source-qualified historical session IDs. */
export function sessionTree(sessions: SidebarSession[]) {
  const byKey = new Map(sessions.map(row => [sessionKey(row), row]))
  const parents = new Map<string, SidebarSession>()
  const children = new Map<string, SidebarSession[]>()
  const rank = new Map(sessions.map((row, index) => [sessionKey(row), index]))
  const candidate = (row: SidebarSession) => !sessionIsReadOnly(row) && !row.pinned && !row.folder && row.parent_session_id
    ? byKey.get(`native:${row.parent_session_id}`) : undefined
  for (const row of sessions) {
    const parent = candidate(row)
    if (!parent || sessionIsReadOnly(parent)) continue
    const seen = new Set([sessionKey(row)])
    let cursor: SidebarSession | undefined = parent, cycle = false
    while (cursor) {
      const key = sessionKey(cursor)
      if (seen.has(key)) { cycle = true; break }
      seen.add(key); cursor = candidate(cursor)
    }
    if (cycle) continue
    parents.set(sessionKey(row), parent)
    const siblings = children.get(sessionKey(parent)) ?? []
    siblings.push(row); children.set(sessionKey(parent), siblings)
  }
  // A child's recent activity keeps its family reachable near the top.
  for (const row of sessions) {
    let parent = parents.get(sessionKey(row))
    while (parent) {
      const key = sessionKey(parent)
      rank.set(key, Math.min(rank.get(key)!, rank.get(sessionKey(row))!))
      parent = parents.get(key)
    }
  }
  const roots = sessions.filter(row => !parents.has(sessionKey(row)))
    .sort((a, b) => rank.get(sessionKey(a))! - rank.get(sessionKey(b))!)
  return { roots, children, parents }
}
