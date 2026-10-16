import type { SessionFolder, SidebarSession } from "./session-actions"

export const SESSION_DRAG_TYPE = "application/x-moyai-session"
export type SessionDrag = { version: 1; userId: string; sessionId: string; nonce: string }
export type SessionDropTarget = { kind: "folder"; folderId: string } | { kind: "unfiled" }
export type SidebarDragContext = {
  userId: string; actionsUserId: string; view: string; busy: boolean
  sessions: SidebarSession[]; folders: SessionFolder[]
}
export type SessionPlacement = { kind: "folder"; key: string; label: string } | {
  kind: "pinned" | "participated" | "recent" | "hidden"; key: string; label: string
}
const uuid = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i

export function movableSession(row: SidebarSession): boolean {
  return (row.source === undefined || row.source === "native") && !row.read_only &&
    !row.deleted && !row.deleting && uuid.test(row.id)
}

export function sessionCreatedByMe(row: SidebarSession, userId: string): boolean {
  return row.created_by_me ?? row.owner_id === userId
}

/** Filing changes placement, never the saved pin or participation preferences. */
export function sessionPlacement(row: SidebarSession, folders: SessionFolder[], userId: string): SessionPlacement {
  const folder = row.source !== "legacy" && !row.read_only && folders.find(item => item.name === row.folder)
  if (folder) return { kind: "folder", key: folder.id, label: folder.name }
  if (row.pinned) return { kind: "pinned", key: "pinned", label: "Pinned" }
  if (sessionCreatedByMe(row, userId)) return { kind: "recent", key: "recent", label: "Recent" }
  return row.participated
    ? { kind: "participated", key: "participated", label: "Participated" }
    : { kind: "hidden", key: "", label: "" }
}

export function parseSessionDrag(raw: string): SessionDrag | null {
  if (!raw || raw.length > 2048) return null
  try {
    const value: unknown = JSON.parse(raw)
    if (!value || typeof value !== "object" || Array.isArray(value)) return null
    const row = value as Record<string, unknown>
    if (Object.keys(row).some(key => !["version", "userId", "sessionId", "nonce"].includes(key)) ||
      row.version !== 1 || typeof row.userId !== "string" || !row.userId.trim() || row.userId.length > 512 ||
      typeof row.sessionId !== "string" || !uuid.test(row.sessionId) ||
      typeof row.nonce !== "string" || !uuid.test(row.nonce)) return null
    return row as SessionDrag
  } catch { return null }
}

export function currentDragSession(drag: SessionDrag | null, context: SidebarDragContext): SidebarSession | null {
  if (!drag || context.busy || context.view === "deleted" || !context.userId ||
    context.userId !== context.actionsUserId || drag.userId !== context.userId) return null
  const matches = context.sessions.filter(row => row.id === drag.sessionId && movableSession(row))
  return matches.length === 1 ? matches[0] : null
}

export function sessionDrop(
  raw: string, active: SessionDrag | null, context: SidebarDragContext, target: SessionDropTarget,
): { row: SidebarSession; body: { folder: string }; changed: boolean; message: string; expand: string } | null {
  const drag = parseSessionDrag(raw)
  if (!drag || !active || drag.nonce !== active.nonce || drag.sessionId !== active.sessionId ||
    drag.userId !== active.userId) return null
  const row = currentDragSession(drag, context)
  if (!row) return null
  const folder = target.kind === "folder" ? context.folders.find(item => item.id === target.folderId) : undefined
  if (target.kind === "folder" && !folder) return null
  const name = folder?.name ?? ""
  const placement = sessionPlacement({ ...row, folder: name }, context.folders, context.userId)
  const changed = (row.folder ?? "") !== name
  return {
    row, body: { folder: name }, changed, expand: placement.key,
    message: placement.kind === "hidden" ? "Removed from folder."
      : changed ? `Moved to ${placement.label}.` : `Already in ${placement.label}.`,
  }
}
