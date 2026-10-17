import { useCallback, useEffect, useRef, useState } from "react"
import { api, ApiError, csrfToken } from "./api"
import type { Turn } from "./conversation-state"
import { pollVisible } from "./polling"
import type { WorkEntry, WorkSnapshot } from "./work-timeline"

/** One shared read per execution, even when steering splits its visible history. */
export function useWorkProgress(
  sessionId: string, scope: string, active: Turn | undefined, visible: boolean,
  onDenied: () => void,
) {
  const [state, setState] = useState<{ scope: string; entries: Record<string, WorkEntry> }>({ scope, entries: {} })
  const currentScope = useRef(scope), activeNow = useRef(active), denied = useRef(onDenied)
  currentScope.current = scope; activeNow.current = active; denied.current = onDenied
  const requests = useRef(new Map<string, { controller: AbortController; promise: Promise<void> }>())
  const sequence = useRef(new Map<string, number>())
  const update = useCallback((turnId: string, value: Partial<WorkEntry>) => {
    if (scope !== currentScope.current) return
    setState(previous => {
      const entries = previous.scope === scope ? previous.entries : {}
      return { scope, entries: { ...entries, [turnId]: { ...entries[turnId], ...value } } }
    })
  }, [scope])
  const load = useCallback((turn: Turn, signal?: AbortSignal): Promise<void> => {
    const key = `${scope}:${turn.id}:${turn.status}`
    const existing = requests.current.get(key)
    if (existing) return existing.promise
    const controller = new AbortController(), auth = csrfToken()
    const version = (sequence.current.get(turn.id) ?? 0) + 1
    sequence.current.set(turn.id, version)
    const cancel = () => controller.abort()
    signal?.addEventListener("abort", cancel, { once: true })
    if (signal?.aborted) controller.abort()
    const current = () => !controller.signal.aborted && auth === csrfToken()
      && currentScope.current === scope && sequence.current.get(turn.id) === version
    update(turn.id, { loading: true })
    const promise = (async () => {
      try {
        const data = await api<WorkSnapshot>(
          `/sessions/${encodeURIComponent(sessionId)}/progress/${encodeURIComponent(turn.id)}?include_activity=true`,
          "GET", undefined, controller.signal,
        )
        if (current()) update(turn.id, { data, status: turn.status, error: "", loading: false })
      } catch (error) {
        if (!current()) return
        const revoked = error instanceof ApiError && [401, 403, 404].includes(error.status)
        update(turn.id, { ...(revoked ? { data: undefined } : {}), error: String(error), loading: false })
        if (revoked) denied.current()
      } finally {
        signal?.removeEventListener("abort", cancel)
        if (requests.current.get(key)?.controller === controller) requests.current.delete(key)
      }
    })()
    requests.current.set(key, { controller, promise })
    return promise
  }, [sessionId, scope, update])
  useEffect(() => {
    const pending = requests.current
    return () => {
      for (const [key, request] of pending) {
        if (key.startsWith(`${scope}:`)) { request.controller.abort(); pending.delete(key) }
      }
    }
  }, [scope])
  const activeId = active?.id
  useEffect(() => {
    if (!visible || !activeId) return
    return pollVisible(async signal => {
      const turn = activeNow.current
      if (turn?.id === activeId) await load(turn, signal)
    }, () => 4000, () => { /* Each scoped request owns its visible error. */ })
  }, [activeId, visible, load])
  return { entries: state.scope === scope ? state.entries : {}, load }
}
