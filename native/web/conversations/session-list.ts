import type { PullRequestStatus, SessionFolderView } from "../generated/api"

export type SidebarSession = {
  id: string; title: string; owner_id: string; status: string; archived: boolean; deleted?: boolean
  can_manage?: boolean; pinned?: boolean; folder?: string; participated?: boolean; created_by_me?: boolean; unread?: boolean; updated_at?: string
  surface?: string
  parent_session_id?: string | null
  source?: "native" | "legacy"; key?: string; url?: string; read_only?: boolean; deleting?: boolean; folder_id?: string | null
  pull_requests?: PullRequestStatus[]
}
export const sessionIsReadOnly = (row: SidebarSession) => row.source === "legacy" || row.read_only === true
export const sessionKey = (row: SidebarSession) => row.key ?? `${row.source ?? "native"}:${row.id}`
export const sessionHash = (row: SidebarSession) => `${row.source === "legacy" ? "run=" : ""}${encodeURIComponent(row.id)}`
export type SessionFolder = SessionFolderView
export type SessionListView = "active" | "archived" | "deleted"
