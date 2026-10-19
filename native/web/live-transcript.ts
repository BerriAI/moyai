/** Presentation of the SDK's root-only stream. Saved messages remain authoritative. */
export type LiveEvent = {
  type: string; run_id?: string; step?: number; index?: number; text?: string
  phase?: "commentary" | "final_answer" | null
  id?: string; name?: string; content?: string | null; arguments?: unknown; data?: unknown; failed?: boolean
  created_at?: string
}
export type LiveTranscript = {
  turnId: string; offset: number; step: number | null
  rounds: { step: number; parts: Record<number, string>; phases?: Record<number, LiveEvent["phase"]>; created_at?: string }[]
  activity: LiveEvent[]; action: string; finished: boolean
}
export const emptyLiveTranscript = (turnId = ""): LiveTranscript => ({
  turnId, offset: -1, step: null, rounds: [], activity: [], action: "", finished: false,
})
export function applyLiveEvent(state: LiveTranscript, event: LiveEvent, offset: number, receivedAt = new Date().toISOString()): LiveTranscript {
  if (!Number.isSafeInteger(offset) || offset < 0 || offset <= state.offset || state.finished) return state
  const next = { ...state, offset }
  if (event.type === "response_start") {
    if (event.run_id !== state.turnId || !Number.isSafeInteger(event.step) || event.step! < 0) return { ...next, step: null }
    // A retry replaces this round, including any now-absent parts, but preserves
    // earlier commentary. Part indices restart on each model response.
    return { ...next, step: event.step!, action: "Writing a response…",
      rounds: [...state.rounds.filter(round => round.step < event.step!), {
        step: event.step!, parts: {},
        created_at: state.rounds.find(round => round.step === event.step)?.created_at ?? event.created_at ?? receivedAt,
      }] }
  }
  if (event.type === "text_start" || event.type === "text_delta" || event.type === "text_end") {
    if (state.step === null || !Number.isSafeInteger(event.index) || event.index! < 0 || typeof event.text !== "string") return next
    return { ...next, action: "Writing a response…", rounds: state.rounds.map(round => round.step !== state.step ? round : {
      ...round,
      phases: { ...round.phases, [event.index!]: event.phase ?? round.phases?.[event.index!] },
      parts: { ...round.parts, [event.index!]: event.type === "text_delta" ? (round.parts[event.index!] ?? "") + event.text! : event.text! },
    }) }
  }
  if (event.type === "tool_call" || event.type === "tool_result" || event.type === "capability") {
    const action = event.type === "tool_call" ? `Using ${(event.name ?? "a tool").replaceAll("_", " ")}…`
      : event.type === "tool_result" ? event.failed ? "Reviewing the tool result…" : "Working…" : state.action
    return { ...next, action, activity: [...state.activity.slice(-199), {
      ...event, created_at: event.created_at ?? receivedAt,
    }] }
  }
  // Only the transport's finished event ends the stream. SDK agent completion
  // is not evidence that the authoritative saved message is available yet.
  return next
}
export function liveCommentary(state: LiveTranscript, hasSavedAssistant: boolean): string {
  return progressMessages(state, hasSavedAssistant).map(message => message.text).join("\n\n")
}
export type ProgressMessage = { id: string; text: string; phase: "commentary"; created_at?: string }
export function progressMessages(state: LiveTranscript, hasSavedAssistant: boolean): ProgressMessage[] {
  return state.rounds.flatMap((round, roundIndex) => Object.entries(round.parts)
    .sort(([a], [b]) => Number(a) - Number(b))
    .filter(([index, text]) => {
      const phase = round.phases?.[Number(index)]
      return text.trim() && phase !== "final_answer"
        && !(hasSavedAssistant && !phase && roundIndex === state.rounds.length - 1)
    })
    .map(([, text], index) => ({
      id: `commentary:${state.turnId}:${round.step}:${index}`, text, phase: "commentary",
      created_at: round.created_at,
    })))
}
export function liveFinalAnswer(state: LiveTranscript): string {
  return state.rounds.flatMap(round => Object.entries(round.parts).sort(([a], [b]) => Number(a) - Number(b))
    .filter(([index]) => round.phases?.[Number(index)] === "final_answer").map(([, text]) => text)).join("\n\n")
}
export function mergeActivity(saved: LiveEvent[], live: LiveEvent[]): LiveEvent[] {
  const seen = new Set<string>()
  return [...saved, ...live].filter(item => {
    const key = item.id ? `${item.type}:${item.id}` : JSON.stringify(item)
    if (seen.has(key)) return false
    seen.add(key); return true
  })
}
