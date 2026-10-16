/** Presentation of the SDK's root-only stream. Saved messages remain authoritative. */
export type LiveEvent = {
  type: string; run_id?: string; step?: number; index?: number; text?: string
  id?: string; name?: string; content?: string; arguments?: unknown; data?: unknown; failed?: boolean
}
export type LiveTranscript = {
  turnId: string; offset: number; step: number | null
  rounds: { step: number; parts: Record<number, string> }[]
  activity: LiveEvent[]; action: string; finished: boolean
}
export const emptyLiveTranscript = (turnId = ""): LiveTranscript => ({
  turnId, offset: -1, step: null, rounds: [], activity: [], action: "", finished: false,
})
export function applyLiveEvent(state: LiveTranscript, event: LiveEvent, offset: number): LiveTranscript {
  if (!Number.isSafeInteger(offset) || offset < 0 || offset <= state.offset || state.finished) return state
  const next = { ...state, offset }
  if (event.type === "response_start") {
    if (event.run_id !== state.turnId || !Number.isSafeInteger(event.step) || event.step! < 0) return { ...next, step: null }
    // A retry replaces this round, including any now-absent parts, but preserves
    // earlier commentary. Part indices restart on each model response.
    return { ...next, step: event.step!, action: "Writing a response…",
      rounds: [...state.rounds.filter(round => round.step < event.step!), { step: event.step!, parts: {} }] }
  }
  if (event.type === "text_start" || event.type === "text_delta") {
    if (state.step === null || !Number.isSafeInteger(event.index) || event.index! < 0 || typeof event.text !== "string") return next
    return { ...next, action: "Writing a response…", rounds: state.rounds.map(round => round.step !== state.step ? round : {
      ...round, parts: { ...round.parts, [event.index!]: event.type === "text_start" ? event.text! : (round.parts[event.index!] ?? "") + event.text! },
    }) }
  }
  if (event.type === "tool_call" || event.type === "tool_result" || event.type === "capability") {
    const action = event.type === "tool_call" ? `Using ${(event.name ?? "a tool").replaceAll("_", " ")}…`
      : event.type === "tool_result" ? event.failed ? "Reviewing the tool result…" : "Working…" : state.action
    return { ...next, action, activity: [...state.activity.slice(-199), event] }
  }
  // Only the transport's finished event ends the stream. SDK agent completion
  // is not evidence that the authoritative saved message is available yet.
  return next
}
export function liveCommentary(state: LiveTranscript, hasSavedAssistant: boolean): string {
  const rounds = hasSavedAssistant ? state.rounds.slice(0, -1) : state.rounds
  return rounds.map(round => Object.entries(round.parts).sort(([a], [b]) => Number(a) - Number(b)).map(([, text]) => text).join("")).filter(Boolean).join("\n\n")
}
export function mergeActivity(saved: LiveEvent[], live: LiveEvent[]): LiveEvent[] {
  const seen = new Set<string>()
  return [...saved, ...live].filter(item => {
    const key = item.id ? `${item.type}:${item.id}` : JSON.stringify(item)
    if (seen.has(key)) return false
    seen.add(key); return true
  })
}
