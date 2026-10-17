import type { UploadedFile } from "./attachment-uploads"
import type { SidebarSession } from "./session-actions"

export type Session = SidebarSession & {
  visibility: string; surface: string; model: string; active_turn: string | null
  read_marker_key?: string; completion_marker?: string; save_warning?: string
  workspace?: { id: string; provider: string }
  swarm_id?: string
}
export type Turn = {
  id: string; actor_id: string; status: string; mode?: string; connections: string[]; error?: string
  created_at?: string; finished_at?: string | null
  usage: { requests?: number; input_tokens?: number; output_tokens?: number }
}
export type Message = { id: number; turn_id: string; role: string; author_id: string | null; content: string; attachments?: UploadedFile[] }
export type NewConversation = { session: Session; turn: Turn; prompt: string; attachments: UploadedFile[] }

/** Durable queued admission is normal startup, not necessarily a user queue. */
export function waitsBehindActive(turn: Turn, active: Turn | undefined, sendingNow = false): boolean {
  return turn.status === "queued" && turn.mode !== "asap" && !sendingNow
    && Boolean(active && active.id !== turn.id && ["starting", "running", "saving", "waiting", "stopping"].includes(active.status))
}

/** Only seed after the server has accepted the first message and assigned its turn. */
export function firstMessage(start: NewConversation): Message {
  return { id: -1, turn_id: start.turn.id, role: "user", author_id: start.turn.actor_id,
    content: start.prompt, attachments: start.attachments }
}
