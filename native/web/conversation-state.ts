import type { UploadedFile } from "./attachment-uploads"
import type { SidebarSession } from "./session-actions"

export type Session = SidebarSession & {
  visibility: string; surface: string; model: string; active_turn: string | null
  read_marker_key?: string; completion_marker?: string; save_warning?: string
  workspace?: { id: string; provider: string }
  swarm_id?: string
}
export type Turn = {
  id: string; actor_id: string; is_self?: boolean; status: string; mode?: string; model?: string; connections: string[]; error?: string
  parent_turn?: string | null; accepted_at?: string | null; guidance_at?: string | null
  created_at?: string; finished_at?: string | null
  usage: { requests?: number; input_tokens?: number; output_tokens?: number }
}
export type Message = { id: number; turn_id: string; role: string; author_id: string | null; content: string; author_name?: string | null; is_self?: boolean; source?: "delegation" | "slack" | null; source_url?: string | null; source_timestamp?: string; legacy_review_required?: boolean; edited?: boolean; edited_in?: "web" | "slack"; deleted?: boolean; edited_after_start?: boolean; attachments_changed?: boolean; attachment_status?: { file_id?: string; name?: string; error?: string; preview_error?: string }[]; created_at?: string; attachments?: UploadedFile[] }
export type NewConversation = { session: Session; turn: Turn; prompt: string; attachments: UploadedFile[] }

/** Durable queued admission is normal startup, not necessarily a user queue. */
export function waitsBehindActive(turn: Turn, active: Turn | undefined, sendingNow = false): boolean {
  const blocked = active?.status === "waiting"
  return turn.status === "queued" && (blocked || (turn.mode !== "asap" && !sendingNow))
    && Boolean(active && active.id !== turn.id && ["queued", "starting", "running", "saving", "waiting", "stopping"].includes(active.status))
}

/** Only seed after the server has accepted the first message and assigned its turn. */
export function firstMessage(start: NewConversation): Message {
  return { id: -1, turn_id: start.turn.id, role: "user", author_id: start.turn.actor_id,
    content: start.prompt, attachments: start.attachments }
}
