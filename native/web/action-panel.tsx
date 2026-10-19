import { useEffect, useRef, useState } from "react"
import { RefreshCw, Terminal } from "lucide-react"
import { Button } from "@/components/ui/button"
import { ApiError, csrfToken } from "./api"
import { getTurnAction, type WorkActionDetail as ActionDetail } from "./generated/api"
import type { ActionSelection } from "./workspace-types"
import "./action-panel.css"

function ActionValue({ label, value, truncated, empty }: {
  label: string; value: string | null; truncated: boolean; empty: string
}) {
  let text = value
  if (value) {
    try {
      const parsed = JSON.parse(value)
      text = label === "Input" && typeof parsed?.command === "string" && Object.keys(parsed).length === 1
        ? parsed.command : JSON.stringify(parsed, null, 2)
    } catch { /* Plain command/output. */ }
  }
  return <section className="action-value" aria-label={label}>
    <h3>{label}</h3>
    {text !== null ? <pre tabIndex={0} aria-label={label}>{text || "(empty)"}</pre> : <p className="muted">{empty}</p>}
    {truncated && <p className="muted">Showing the first 100,000 characters.</p>}
  </section>
}

export function ActionPanel({ action, active, onDenied }: {
  action: ActionSelection; active: boolean; onDenied: () => void
}) {
  const [detail, setDetail] = useState<ActionDetail | null>(null)
  const [error, setError] = useState("")
  const [loading, setLoading] = useState(false)
  const [revision, setRevision] = useState(0)
  const denied = useRef(onDenied)
  denied.current = onDenied
  useEffect(() => {
    setDetail(null); setError("")
    if (!active) return
    const controller = new AbortController()
    const token = csrfToken()
    let timer: ReturnType<typeof setTimeout>
    let settlingReads = 0
    const options = {
      path: { session_id: action.sessionId, turn_id: action.turnId, action_id: action.toolId },
      query: action.agentId ? { agent_id: action.agentId } : undefined,
      signal: controller.signal,
    }
    async function read() {
      setLoading(true)
      try {
        const { data: value } = await getTurnAction(options)
        if (controller.signal.aborted || csrfToken() !== token) return
        setDetail(value); setError("")
        // Lifecycle events can arrive just before their result snapshot is saved.
        if (value.status === "running" || (!value.restricted && value.output === null && settlingReads++ < 3))
          timer = setTimeout(read, 2000)
      } catch (reason) {
        if (controller.signal.aborted || csrfToken() !== token) return
        setDetail(null); setError(String(reason))
        if (reason instanceof ApiError && [401, 403].includes(reason.status)) denied.current()
      } finally {
        if (!controller.signal.aborted && csrfToken() === token) setLoading(false)
      }
    }
    void read()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [action.sessionId, action.turnId, action.toolId, action.agentId, active, revision])
  const elapsed = detail?.finished_at ? (Date.parse(detail.finished_at) - Date.parse(detail.created_at)) / 1000 : null
  return <section className="action-panel" aria-label="Action details">
    <header><Terminal size={18} aria-hidden="true" /><div><h2>{action.name}</h2>
      {detail && <p role="status" className={detail.status === "failed" ? "error" : "muted"}>
        {detail.status[0].toUpperCase() + detail.status.slice(1)}{elapsed !== null && Number.isFinite(elapsed) ? ` · ${Math.max(0, Math.round(elapsed))}s` : ""}
      </p>}</div>
      <Button type="button" variant="ghost" size="icon-sm" aria-label="Refresh action" disabled={!active || loading}
        onClick={() => setRevision(value => value + 1)}><RefreshCw size={15} /></Button>
    </header>
    {error ? <div role="alert"><p className="error">{error}</p><Button variant="outline" size="sm" onClick={() => setRevision(value => value + 1)}>Retry</Button></div>
      : !detail ? <p className="muted" role="status">Loading action…</p>
        : detail.restricted ? <p className="muted">Details are private for this action.</p> : <>
          <ActionValue label="Input" value={detail.input} truncated={detail.input_truncated} empty={detail.status === "running" ? "Waiting for input to be saved…" : "No input was recorded."} />
          <ActionValue label="Output" value={detail.output} truncated={detail.output_truncated} empty={detail.status === "running" ? "Running…" : "No output was recorded."} />
        </>}
  </section>
}
