import type { UploadedFile } from "./attachment-uploads"
import type { SidebarSession } from "./session-actions"
import type { SessionDetailSession, SessionMessage, SessionSummary, SessionTurn, TurnView } from "./generated/api"

export type Session = SidebarSession & {
  visibility: string; surface: string; model: string; active_turn: string | null
  read_marker_key?: string; completion_marker?: string; save_warning?: SessionSummary["save_warning"]
  workspace?: SessionSummary["workspace"]
  swarm_id?: SessionDetailSession["swarm_id"]
}
/** A turn from a send/steer/answer receipt; session detail adds the viewer fields. */
export type Turn = TurnView & Partial<Pick<SessionTurn, "is_self" | "guidance_at">>
type MessageCore = "id" | "turn_id" | "role" | "author_id" | "content"
/** A saved message, or the optimistic first message of a new conversation. */
export type Message = Pick<SessionMessage, MessageCore> & Partial<Omit<SessionMessage, MessageCore | "attachments">> & { attachments?: UploadedFile[] }
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

/** Pages update known rows; only a complete snapshot may remove absent saved rows. */
export function reconcileHistory(previous: Message[], page: Message[], complete = false): Message[] {
  const saved = new Map((complete ? [] : previous.filter(message => message.id >= 0))
    .map(message => [message.id, message]))
  for (const message of page) saved.set(message.id, message)
  const rows = [...saved.values()].sort((a, b) => a.id - b.id)
  const savedInputs = new Set(rows.filter(message => message.role === "user").map(message => message.turn_id))
  return [...rows, ...previous.filter(message => message.id < 0 && !savedInputs.has(message.turn_id))]
}
