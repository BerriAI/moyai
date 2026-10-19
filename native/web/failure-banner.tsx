import { useEffect, useRef, useState } from "react"
import { AlertCircle, Check, ChevronDown, Copy, Download } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { ApiError, csrfToken } from "./api"
import type { Turn } from "./conversation-state"
import { downloadDebugTrace } from "./debug-trace"
import { getTurnFailureDetails, type TurnFailureDetails as FailureDetails } from "./generated/api"
import "./failure-banner.css"

function dateLabel(value?: string | null): string {
  if (!value) return "Not recorded"
  const date = new Date(value)
  return Number.isFinite(date.getTime()) ? date.toLocaleString() : value
}

export function FailureBanner({ sessionId, turn, canInspect, onDenied }: {
  sessionId: string; turn: Turn; canInspect: boolean; onDenied: () => void
}) {
  const [open, setOpen] = useState(false)
  const [details, setDetails] = useState<FailureDetails | null>(null)
  const [loadError, setLoadError] = useState("")
  const [actionError, setActionError] = useState("")
  const [revision, setRevision] = useState(0)
  const [copied, setCopied] = useState(false)
  const [downloading, setDownloading] = useState(false)
  const denied = useRef(onDenied)
  denied.current = onDenied
  useEffect(() => {
    setDetails(null); setLoadError(""); setActionError(""); setCopied(false)
    if (!open || !canInspect) return
    const controller = new AbortController()
    const token = csrfToken()
    void getTurnFailureDetails({
      path: { session_id: sessionId, turn_id: turn.id }, signal: controller.signal,
    }).then(({ data }) => {
      if (!controller.signal.aborted && csrfToken() === token) setDetails(data)
    }).catch(reason => {
      if (controller.signal.aborted || csrfToken() !== token) return
      setLoadError(String(reason))
      if (reason instanceof ApiError && [401, 403].includes(reason.status)) denied.current()
    })
    return () => controller.abort()
  }, [sessionId, turn.id, turn.error, open, canInspect, revision])
  const elapsed = turn.finished_at && turn.created_at
    ? (Date.parse(turn.finished_at) - Date.parse(turn.created_at)) / 1000 : null
  const contextDetails = canInspect ? details : null
  const metadata = [
    ["Model", contextDetails?.model ?? turn.model ?? "Not recorded"],
    ["Submitted", dateLabel(contextDetails?.created_at ?? turn.created_at)],
    ["Finished", dateLabel(contextDetails?.finished_at ?? turn.finished_at)],
    ["Total time", elapsed !== null && Number.isFinite(elapsed) ? `${Math.max(0, Math.round(elapsed))} seconds` : "Not recorded"],
    ["Session ID", sessionId], ["Turn ID", turn.id],
    ...(contextDetails ? [["Workflow ID", contextDetails.workflow_id]] : []),
  ]
  return <Collapsible open={open} onOpenChange={setOpen} className="failure-banner">
    <div role="alert"><CollapsibleTrigger asChild>
      <Button type="button" variant="ghost" className="failure-summary" aria-label={open ? "Hide failure details" : "Show failure details"}>
        <AlertCircle size={17} aria-hidden="true" /><span>{turn.error}</span>
        <small>Details</small><ChevronDown size={15} className={open ? "failure-expanded" : ""} aria-hidden="true" />
      </Button>
    </CollapsibleTrigger></div>
    <CollapsibleContent className="failure-body">
      {!canInspect ? <p className="muted">Detailed tool activity is visible only to the requester.</p>
        : loadError ? <div role="alert"><p className="error">{loadError}</p>
          <Button type="button" variant="outline" size="sm" onClick={() => setRevision(value => value + 1)}>Retry details</Button></div>
          : !details ? <p className="muted" role="status">Loading failure details…</p> : <>
            <div className="failure-actions">
              <Button type="button" variant="outline" size="sm" onClick={async () => {
                setActionError("")
                try { await navigator.clipboard.writeText(JSON.stringify(details, null, 2)); setCopied(true) }
                catch { setActionError("Could not copy details. Try downloading the debug trace.") }
              }}>{copied ? <Check size={14} /> : <Copy size={14} />}{copied ? "Copied details" : "Copy details"}</Button>
              <Button type="button" variant="outline" size="sm" disabled={downloading} onClick={async () => {
                setDownloading(true); setActionError("")
                try { await downloadDebugTrace(sessionId) }
                catch (reason) { setActionError(String(reason)) }
                finally { setDownloading(false) }
              }}><Download size={14} />{downloading ? "Downloading…" : "Download debug trace"}</Button>
            </div>
            {actionError && <p className="error" role="alert">{actionError}</p>}
            <section aria-label="Recent tool failures"><h3>Recent tool errors</h3>
              <p className="muted">Earlier tool errors may have been recovered.</p>
              {details.recent_failures.length === 0 ? <p className="muted">No failed tool result was recorded.</p>
                : details.recent_failures.map((failure, index) => <article className="failure-tool" key={`${failure.id}:${index}`}>
                  <header><code>{failure.tool}</code><time dateTime={failure.timestamp ?? undefined}>{dateLabel(failure.timestamp)}</time></header>
                  {failure.restricted ? <p className="muted">Details are private for this tool.</p>
                    : <pre tabIndex={0} aria-label={`${failure.tool} error`}>{failure.error || "No error text was recorded."}</pre>}
                  {failure.truncated && <p className="muted">Showing the first 6,000 characters.</p>}
                </article>)}
              {details.failures_limited && <p className="muted">Showing the five most recent failures. Download the trace for the full history.</p>}
            </section>
            {details.runtime && <section aria-label="Runtime cause">
              <h3>Runtime cause</h3><ol className="failure-causes">{details.runtime.exception_chain.map((cause, index) => <li key={index}>
                <code>{cause.application_type ?? cause.type}</code>
                {cause.activity_type && <span>Activity: <code>{cause.activity_type}</code>{cause.activity_id && <> · ID {cause.activity_id}</>}</span>}
              </li>)}</ol>
            </section>}
            {details.last_action && <p className="failure-last-action">Last recorded action: <code>{details.last_action.tool}</code> · {details.last_action.status}</p>}
            {details.usage.input_tokens !== undefined && <p className="muted">Cumulative input tokens: {details.usage.input_tokens.toLocaleString()}
              {details.usage.output_tokens !== undefined && <> · Output tokens: {details.usage.output_tokens.toLocaleString()}</>}</p>}
            {details.notes.map(note => <p className="muted" key={note}>{note}</p>)}
          </>}
      <h3>Run context</h3>
      <dl className="failure-metadata">{metadata.map(([label, value]) => <div key={label}>
        <dt>{label}</dt><dd>{label.endsWith("ID") || label === "Model" ? <code>{value}</code> : value}</dd>
      </div>)}</dl>
    </CollapsibleContent>
  </Collapsible>
}
