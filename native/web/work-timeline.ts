import type { Message, Turn } from "./conversation-state"
import { progressMessages, type LiveTranscript, type ProgressMessage } from "./live-transcript"

export type ToolProgress = {
  id: string; name: string; status: "running" | "completed" | "failed" | "interrupted"
  created_at: string; finished_at: string | null
}
export type AgentProgress = {
  id: string; session_id?: string | null; parent_id: string | null; parent_name: string | null; name: string; avatar: number
  depth: number; status: string; created_at: string; updated_at: string
  messages: (Omit<ProgressMessage, "phase"> & { phase: "commentary" | "final_answer" })[]
  tools: ToolProgress[]; messages_limited: boolean; tools_limited: boolean
}
export type WorkSnapshot = {
  messages: ProgressMessage[]; tools?: ToolProgress[]; subagents?: AgentProgress[]
  tools_limited?: boolean; subagents_limited?: boolean
}
export type WorkEntry = { data?: WorkSnapshot; status?: string; loading?: boolean; error?: string }
export type WorkSegment = {
  id: string; turn: Turn; after: number | null; before: number | null
  earlier: boolean; hasFinalAnswer: boolean
}
export type WorkItem =
  | { kind: "message"; id: string; at: number; message: ProgressMessage }
  | { kind: "tools"; id: string; at: number; tools: ToolProgress[] }
  | { kind: "agent"; id: string; at: number; agent: AgentProgress }

const instant = (value?: string | null) => {
  const parsed = value ? Date.parse(value) : NaN
  return Number.isFinite(parsed) ? parsed : null
}
const earlierTime = (a?: string, b?: string) => {
  const left = instant(a), right = instant(b)
  return left === null ? b : right === null || left <= right ? a : b
}

/** A steering message belongs to its original execution, not a new empty run. */
export function rootTurn(turn: Turn | undefined, turns: Turn[]): Turn | undefined {
  const seen = new Set<string>()
  while (turn?.parent_turn) {
    if (seen.has(turn.id)) return undefined
    seen.add(turn.id)
    const parent = turns.find(item => item.id === turn!.parent_turn)
    if (!parent || parent.actor_id !== turn.actor_id) return undefined
    turn = parent
  }
  return turn
}

/** A promoted queued input takes its place when admitted into the running work. */
export function orderedWorkMessages(messages: Message[], turns: Turn[]): Message[] {
  const result = [...messages]
  const groups = new Map<string, { index: number; message: Message; at: number }[]>()
  messages.forEach((message, index) => {
    if (message.role !== "user" && message.source !== "delegation") return
    const input = turns.find(turn => turn.id === message.turn_id)
    const root = rootTurn(input, turns)
    if (!input || !root || input.status === "queued") return
    const at = instant(input.parent_turn ? input.guidance_at ?? input.accepted_at : undefined)
      ?? instant(message.created_at) ?? instant(input.created_at)
    if (at === null) return
    const group = groups.get(root.id) ?? []
    group.push({ index, message, at })
    groups.set(root.id, group)
  })
  for (const group of groups.values()) {
    const ordered = [...group].sort((a, b) => a.at - b.at || a.index - b.index)
    group.forEach((item, index) => { result[item.index] = ordered[index].message })
  }
  return result
}

/** Keep a distinct activity segment on each side of an admitted steering input. */
export function workSegments(messages: Message[], turns: Turn[]): Map<number, WorkSegment> {
  const groups = new Map<string, { message: Message; turn: Turn; at: number | null }[]>()
  for (const message of messages) {
    if (message.role !== "user" && message.source !== "delegation") continue
    const input = turns.find(turn => turn.id === message.turn_id)
    const root = rootTurn(input, turns)
    if (!input || !root || input.status === "queued") continue
    const at = instant(input.parent_turn ? input.guidance_at ?? input.accepted_at : undefined)
      ?? instant(message.created_at) ?? instant(input.created_at)
    const group = groups.get(root.id) ?? []
    group.push({ message, turn: root, at })
    groups.set(root.id, group)
  }
  const segments = new Map<number, WorkSegment>()
  for (const group of groups.values()) {
    let previous: number | null = null
    const boundaries = group.map(item => {
      previous = item.at === null ? previous : previous === null ? item.at : Math.max(previous, item.at)
      return previous
    })
    group.forEach((item, index) => segments.set(item.message.id, {
      id: `${item.turn.id}:${item.message.id}`, turn: item.turn,
      after: index === 0 ? null : boundaries[index],
      before: boundaries[index + 1] ?? null,
      earlier: index < group.length - 1,
      hasFinalAnswer: index === group.length - 1 && messages.some(message =>
        message.role === "assistant" && message.turn_id === item.turn.id && Boolean(message.content.trim())),
    }))
  }
  return segments
}

export function mergeProgress(saved: ProgressMessage[], live: ProgressMessage[]): ProgressMessage[] {
  const messages = new Map(saved.map(message => [message.id, message]))
  const legacy = saved.filter(message => !message.id.startsWith("commentary:"))
  const matched = new Set<string>()
  for (const message of live) {
    const persisted = messages.get(message.id) ?? legacy.find(item =>
      !matched.has(item.id) && item.text === message.text)
    if (!persisted) messages.set(message.id, message)
    else {
      matched.add(persisted.id)
      messages.set(persisted.id, {
        ...persisted,
        // Old snapshot projections have provider IDs instead of step IDs.
        // Match those one-to-one, preserving genuinely repeated updates.
        created_at: earlierTime(persisted.created_at, message.created_at),
      })
    }
  }
  return [...messages.values()]
}

export function liveTools(live?: LiveTranscript): ToolProgress[] {
  const calls = new Map<string, ToolProgress>()
  for (const event of live?.activity ?? []) {
    if (!event.id || !event.name || !["tool_call", "tool_result"].includes(event.type)) continue
    const previous = calls.get(event.id)
    const at = event.created_at ?? previous?.created_at ?? ""
    calls.set(event.id, {
      id: event.id, name: event.name,
      status: event.type === "tool_call" ? "running" : event.failed ? "failed" : "completed",
      created_at: previous?.created_at ?? at,
      finished_at: event.type === "tool_call" ? null : at,
    })
  }
  return [...calls.values()]
}

function mergeTools(saved: ToolProgress[], live: ToolProgress[]): ToolProgress[] {
  const tools = new Map(saved.map(tool => [tool.id, tool]))
  for (const tool of live) {
    const persisted = tools.get(tool.id)
    tools.set(tool.id, persisted ? {
      ...tool, ...persisted,
      ...(persisted.status === "running" && tool.status !== "running"
        ? { status: tool.status, finished_at: tool.finished_at } : {}),
    } : tool)
  }
  return [...tools.values()]
}

/** Ordering and grouping are presentation only; no activity can complete a turn. */
export function workItems(segment: WorkSegment, saved?: WorkSnapshot, live?: LiveTranscript): WorkItem[] {
  const messages = mergeProgress(saved?.messages ?? [], live ? progressMessages(live, segment.hasFinalAnswer) : [])
  const tools = mergeTools(saved?.tools ?? [], liveTools(live))
  const belongs = (at: number | null) => {
    if (at === null) return !segment.earlier
    return (segment.after === null || at >= segment.after)
      && (segment.before === null ? !segment.earlier : at < segment.before)
  }
  const entries: WorkItem[] = [
    ...messages.filter(message => belongs(instant(message.created_at))).map(message => ({
      kind: "message" as const, id: message.id, at: instant(message.created_at) ?? Number.MAX_SAFE_INTEGER, message,
    })),
    ...tools.filter(tool => belongs(instant(tool.created_at))).map(tool => ({
      kind: "tools" as const, id: `tool:${tool.id}`, at: instant(tool.created_at) ?? Number.MAX_SAFE_INTEGER,
      tools: [tool],
    })),
    ...(saved?.subagents ?? []).filter(agent => belongs(instant(agent.created_at))).map(agent => ({
      kind: "agent" as const, id: `agent:${agent.id}`, at: instant(agent.created_at) ?? Number.MAX_SAFE_INTEGER, agent,
    })),
  ]
  entries.sort((a, b) => a.at - b.at || a.id.localeCompare(b.id))
  const grouped: WorkItem[] = []
  for (const item of entries) {
    const previous = grouped.at(-1)
    if (item.kind === "tools" && previous?.kind === "tools") previous.tools.push(...item.tools)
    else grouped.push(item)
  }
  return grouped
}
