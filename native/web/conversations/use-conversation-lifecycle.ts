import { useCallback, useEffect, useRef, useState } from "react"
import { ApiError, appPath, csrfToken } from "../api"
import { getSession, getSessionActivity, getSessionPlan, listSessionMessages, markSessionRead,
  type CreateSessionMessageResponse, type PlanStep, type SessionDetail } from "../generated/api"
import { firstMessage, reconcileHistory, type Session, type Turn, type Message, type NewConversation } from "./conversation-state"
import { applyLiveEvent, emptyLiveTranscript, type ActivityEvent, type LiveEvent } from "./live-transcript"
import { activeSession, pollVisible } from "../polling"
import type { UploadedFile } from "../attachment-uploads"
import type { SessionUpdates } from "./session-updates"

/** A loaded detail, or the optimistic view of a just-created conversation. */
export type ConversationView = Partial<Omit<SessionDetail, "session" | "turns" | "messages">>
  & { session: Session; turns: Turn[]; messages?: Message[] | null }
type View = ConversationView

/**
 * One mounted session/authority owns history, polling and its live cursor.
 * Remount on identity changes; hidden conversations pause reads and subscriptions.
 * onRead publishes authorized views (null on access loss), never optimistic receipts.
 * refresh rejects on read failure; refreshAfterAction reports it in the read notice.
 * Neither refresh path replays a write.
 */
export function useConversationLifecycle({ id, visible, initial, resolved, sessionUpdates, auxiliaryPanel, onRead }: {
  id: string; visible: boolean; initial?: NewConversation; resolved?: View
  sessionUpdates: SessionUpdates; auxiliaryPanel?: string; onRead: (view: View | null) => void
}) {
  const [view, setView] = useState<View | null>(() => initial ? { session: initial.session, turns: [initial.turn] } : resolved?.messages != null ? resolved : null)
  const [messages, setMessages] = useState<Message[]>(() => initial ? [firstMessage(initial)] : resolved?.messages ?? [])
  const [historyState, setHistoryState] = useState(() => ({
    complete: Boolean(initial || resolved?.messages && resolved.messages.length < 200), loading: false, error: "",
  }))
  const [plan, setPlan] = useState<PlanStep[]>([])
  const [panelLoading, setPanelLoading] = useState(false)
  const [panelError, setPanelError] = useState("")
  const resolvedOnce = useRef(resolved)
  const viewNow = useRef(view)
  viewNow.current = view
  const sensitiveVersion = useRef(0)
  const [loadError, setLoadError] = useState("")
  const receiptMessageId = useRef(-3)
  const [activity, setActivity] = useState<ActivityEvent[]>([])
  const [liveText, setLiveText] = useState(emptyLiveTranscript)
  const liveNow = useRef(liveText)
  const [reconnecting, setReconnecting] = useState(false)
  const [streamAttempt, setStreamAttempt] = useState(0)
  const readThrough = useRef<number | null>(null)
  const loadVersion = useRef(0)
  const loadFlight = useRef<{ version: number; signal?: AbortSignal; promise: Promise<void> } | null>(null)
  const completeHistory = useRef<{ version: string; messages: Message[] } | null>(null)
  const historyRequest = useRef<{ version: View["transcript_version"]; controller: AbortController } | null>(null)
  const visibleNow = useRef(visible)
  visibleNow.current = visible
  const readCallback = useRef(onRead)
  readCallback.current = onRead
  const invalidate = useCallback((message = "Access to this conversation changed. Please reopen it.") => {
    sessionUpdates.invalidate(id)
    ++loadVersion.current
    ++sensitiveVersion.current
    historyRequest.current?.controller.abort(); historyRequest.current = null
    resolvedOnce.current = undefined
    readThrough.current = null
    viewNow.current = null
    completeHistory.current = null
    setHistoryState({ complete: false, loading: false, error: "" })
    liveNow.current = emptyLiveTranscript()
    setLiveText(liveNow.current); setView(null); setMessages([]); setPlan([]); setActivity([]); setPanelError("")
    setLoadError(message)
    readCallback.current(null)
  }, [id, sessionUpdates])
  useEffect(() => () => {
    ++loadVersion.current; ++sensitiveVersion.current
    historyRequest.current?.controller.abort()
  }, [])
  useEffect(() => {
    const pause = () => {
      if (!visible || document.hidden) {
        historyRequest.current?.controller.abort(); historyRequest.current = null
        setHistoryState(previous => ({ ...previous, loading: false }))
      }
    }
    pause()
    document.addEventListener("visibilitychange", pause)
    return () => document.removeEventListener("visibilitychange", pause)
  }, [visible])
  const readHistory = useCallback((next: View) => {
    const existing = historyRequest.current
    // Session polls do not restart the same transcript's pagination. Its owner
    // is the transcript revision and authority scope, not a metadata refresh.
    if (existing && existing.version === next.transcript_version && !existing.controller.signal.aborted) return
    existing?.controller.abort()
    const request = { version: next.transcript_version, controller: new AbortController() }
    historyRequest.current = request
    const { signal } = request.controller, auth = csrfToken(), scope = sensitiveVersion.current
    const current = () => historyRequest.current === request && !signal.aborted
      && auth === csrfToken() && scope === sensitiveVersion.current
    const cached = next.transcript_version && completeHistory.current?.version === next.transcript_version
      ? completeHistory.current.messages : undefined
    setHistoryState(previous => ({ ...previous, complete: Boolean(cached), loading: true }))
    void (async () => {
      try {
        // Older servers may omit the first page during a rolling deployment.
        const history = [...(cached ?? next.messages ?? (await listSessionMessages({ path: { session_id: id }, signal })).data)]
        if (!current()) return
        let pageSize = cached ? 0 : history.length
        setMessages(previous => reconcileHistory(previous, history, pageSize < 200))
        setHistoryState({ complete: pageSize < 200, loading: true, error: "" })
        while (pageSize === 200 && current()) {
          const after = history.at(-1)!.id
          const { data: page } = await listSessionMessages({ path: { session_id: id }, query: { after }, signal })
          if (!current()) return
          if (page.some(message => message.id <= after)) throw new Error("Conversation history changed while loading. Refresh to retry.")
          history.push(...page); pageSize = page.length
          setMessages(previous => reconcileHistory(previous, pageSize < 200 ? history : page, pageSize < 200))
          setHistoryState({ complete: pageSize < 200, loading: true, error: "" })
        }
        if (!current()) return
        if (next.transcript_version) completeHistory.current = { version: next.transcript_version, messages: history }
        const latestMessage = history.at(-1)?.id ?? 0
        if (visibleNow.current && readThrough.current !== latestMessage) {
          await markSessionRead({ path: { session_id: id }, body: { message_id: latestMessage }, signal })
          if (!current()) return
          readThrough.current = latestMessage
        }
        setHistoryState({ complete: true, loading: false, error: "" })
      } catch (reason) {
        if (!current()) return
        if (reason instanceof ApiError && [401, 403, 404].includes(reason.status)) invalidate(String(reason))
        else setHistoryState(previous => ({ ...previous, loading: false, error: String(reason) }))
      } finally {
        if (historyRequest.current === request) historyRequest.current = null
      }
    })()
  }, [id, invalidate])
  const load = useCallback(async (signal?: AbortSignal, share = false) => {
    if (signal?.aborted) return
    // Polling and reconnects share a read instead of superseding each other.
    // Explicit action refreshes always start a fresh read.
    const flight = loadFlight.current
    if (share && flight && flight.version === loadVersion.current && !flight.signal?.aborted) return flight.promise
    const version = ++loadVersion.current
    const updateCheckpoint = sessionUpdates.checkpoint()
    const auth = csrfToken()
    const current = () => !signal?.aborted && version === loadVersion.current && auth === csrfToken()
    const request = (async () => {
    try {
      const cached = resolvedOnce.current
      resolvedOnce.current = undefined
      const next: View = cached ?? (await getSession({ path: { session_id: id }, query: { include_activity: false, include_messages: true }, signal })).data
      if (!current()) return
      if (!cached) sessionUpdates.acceptRead(updateCheckpoint, [next.session], true)
      viewNow.current = next
      setView(next)
      if (next.activity) setActivity(next.activity)
      readCallback.current(next)
      readHistory(next)
      setLoadError("")
    } catch (reason) {
      if (!current()) return
      if (reason instanceof ApiError && [401, 403, 404].includes(reason.status)) {
        invalidate(String(reason))
      }
      throw reason
    }
    })()
    loadFlight.current = { version, signal, promise: request }
    try { await request }
    finally { if (loadFlight.current?.version === version) loadFlight.current = null }
  }, [id, sessionUpdates, readHistory, invalidate])
  const refresh = useCallback(() => load(), [load])
  async function refreshAfterAction(message?: string) {
    const version = sensitiveVersion.current, auth = csrfToken()
    try { await load() }
    catch (reason) {
      if (version !== sensitiveVersion.current || auth !== csrfToken()) return
      const denied = reason instanceof ApiError && [401, 403, 404].includes(reason.status)
      setLoadError(denied ? String(reason) : message ?? String(reason))
    }
  }
  useEffect(() => {
    if (!visible) return
    return pollVisible(signal => load(signal, true), () => {
      const current = viewNow.current
      return !current || activeSession(current.session.status) || current.turns.some(turn => activeSession(turn.status)) ? 1500 : 10000
    }, reason => setLoadError(String(reason)))
  }, [load, visible])
  useEffect(() => {
    if (!view || (auxiliaryPanel !== "plan" && auxiliaryPanel !== "activity")) return
    const version = sensitiveVersion.current
    setPanelLoading(true); setPanelError("")
    return pollVisible(async signal => {
      if (auxiliaryPanel === "plan") {
        const { data: rows } = await getSessionPlan({ path: { session_id: id }, signal })
        if (signal.aborted || version !== sensitiveVersion.current) return
        setPlan(rows)
      } else {
        const { data: rows } = await getSessionActivity({ path: { session_id: id }, signal })
        if (signal.aborted || version !== sensitiveVersion.current) return
        setActivity(rows)
      }
      setPanelLoading(false); setPanelError("")
    }, () => activeSession(viewNow.current?.session.status) ? 1500 : 10000, reason => {
      if (version !== sensitiveVersion.current) return
      if (reason instanceof ApiError && [401, 403, 404].includes(reason.status)) invalidate(String(reason))
      else { setPanelLoading(false); setPanelError(String(reason)) }
    })
  }, [id, auxiliaryPanel, Boolean(view), invalidate])
  const active = view?.turns.find(turn => turn.id === view.session.active_turn)
  const activeId = visible && active && ["running", "saving"].includes(active.status) ? active.id : null
  useEffect(() => {
    if (!activeId) return
    if (liveNow.current.turnId !== activeId) {
      liveNow.current = emptyLiveTranscript(activeId)
      setLiveText(liveNow.current)
    }
    const auth = csrfToken(), version = sensitiveVersion.current
    let closed = false
    let retry: ReturnType<typeof setTimeout> | undefined
    const stream = new EventSource(appPath(`/api/sessions/${encodeURIComponent(id)}/events?turn_id=${encodeURIComponent(activeId)}&offset=${liveNow.current.offset + 1}`))
    const current = () => !closed && auth === csrfToken() && version === sensitiveVersion.current && visibleNow.current
    const refreshSaved = () => { if (current()) void load(undefined, true).catch(reason => { if (current()) setLoadError(String(reason)) }) }
    stream.onmessage = event => {
      if (!current() || !/^\d+$/.test(event.lastEventId)) return
      try {
        const value: LiveEvent = JSON.parse(event.data)
        if (!value || typeof value.type !== "string") return
        const next = applyLiveEvent(liveNow.current, value, Number(event.lastEventId))
        if (next !== liveNow.current) { liveNow.current = next; setLiveText(next) }
      } catch { /* A malformed presentation event must not interrupt saved-state refresh. */ }
    }
    stream.addEventListener("connected", () => { if (current()) setReconnecting(false) })
    stream.addEventListener("refresh", refreshSaved)
    stream.addEventListener("finished", () => {
      if (!current()) return
      // A transport can finish before its workflow is available. Only refreshed
      // turn state ends subscription; keep the cursor and retry while still active.
      refreshSaved(); closed = true; stream.close(); setReconnecting(true)
      retry = setTimeout(() => {
        if (auth === csrfToken() && version === sensitiveVersion.current && visibleNow.current) setStreamAttempt(value => value + 1)
      }, 1500)
    })
    stream.addEventListener("access_revoked", () => {
      if (!current()) return
      closed = true; stream.close(); setReconnecting(false)
      invalidate()
      window.dispatchEvent(new Event("moyai:access-changed"))
      void load().catch(reason => { if (auth === csrfToken()) setLoadError(String(reason)) })
    })
    stream.onerror = () => { if (current()) { setReconnecting(true); refreshSaved() } }
    return () => { closed = true; stream.close(); clearTimeout(retry) }
  }, [activeId, id, load, streamAttempt, invalidate])

  // A guard belongs to the mounted conversation's authority, not a metadata poll.
  // Use it around mutation receipts so invalidation/unmount cannot revive old UI.
  function checkpoint() {
    const version = sensitiveVersion.current, auth = csrfToken()
    return () => version === sensitiveVersion.current && auth === csrfToken()
  }

  function acceptMessage(result: CreateSessionMessageResponse, prompt: string, attachments: UploadedFile[], replyTo?: string) {
    // Reads begun before admission cannot erase the accepted receipt.
    ++loadVersion.current
    if (replyTo || !("id" in result)) return
    const message: Message = { id: receiptMessageId.current--, turn_id: result.id, role: "user",
      author_id: result.actor_id, is_self: true, content: prompt, attachments }
    setMessages(previous => previous.some(item => item.role === "user" && item.turn_id === result.id)
      ? previous : [...previous, message])
    const previous = viewNow.current
    if (previous) {
      const next = { ...previous, turns: [result, ...previous.turns.filter(item => item.id !== result.id)] }
      viewNow.current = next; setView(next)
    }
  }

  function acceptGoal(result: CreateSessionMessageResponse, start?: { prompt: string; attachments: UploadedFile[] }) {
    if ("goal" in result) setView(previous => previous ? { ...previous, goal: result.goal } : previous)
    if (!start || !("id" in result) || !viewNow.current) return false
    const message = firstMessage({ session: viewNow.current.session, turn: result, ...start })
    message.id = receiptMessageId.current--
    setMessages(previous => [...previous, message])
    return true
  }

  function acceptSteering(turn: Turn) {
    setView(previous => previous ? { ...previous, turns: previous.turns.map(item => item.id === turn.id ? turn : item) } : previous)
  }

  function dismissError(kind: "session" | "history") {
    if (kind === "session") setLoadError("")
    else setHistoryState(previous => ({ ...previous, error: "" }))
  }

  function reportRefreshError(message: string) { setLoadError(message) }

  return {
    view, messages, historyState, liveText, reconnecting, plan, activity, panelLoading, panelError, loadError,
    checkpoint, invalidate, refresh, refreshAfterAction,
    acceptMessage, acceptGoal, acceptSteering, dismissError, reportRefreshError,
  }
}
