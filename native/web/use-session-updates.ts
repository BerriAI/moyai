import { useEffect, useMemo, useReducer } from "react"
import { updateSession } from "./generated/api"
import { SessionUpdates } from "./session-updates"

export function useSessionUpdates(scope: string) {
  const [, changed] = useReducer(value => value + 1, 0)
  const updates = useMemo(() => new SessionUpdates(
    (id, patch, signal) => updateSession({ path: { session_id: id }, body: patch, signal }), changed,
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
