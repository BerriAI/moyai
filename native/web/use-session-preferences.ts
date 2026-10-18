import { useEffect, useMemo, useReducer } from "react"
import { api } from "./api"
import { SessionPreferenceUpdates } from "./session-preferences"

export function useSessionPreferences(scope: string) {
  const [, changed] = useReducer(value => value + 1, 0)
  const updates = useMemo(() => new SessionPreferenceUpdates(
    (id, patch, signal) => api(`/sessions/${id}`, "PATCH", patch, signal), changed,
  ), [scope])
  useEffect(() => {
    const clear = () => updates.clear()
    window.addEventListener("moyai:access-changed", clear)
    window.addEventListener("moyai:signed-out", clear)
    return () => {
      window.removeEventListener("moyai:access-changed", clear)
      window.removeEventListener("moyai:signed-out", clear)
      updates.clear()
    }
  }, [updates])
  return updates
}
