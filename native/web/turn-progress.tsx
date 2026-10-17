import React, { useEffect, useState } from "react"
import Markdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { ChevronDown, ChevronRight } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { api, csrfToken } from "./api"
import type { Turn } from "./conversation-state"
import { progressMessages, type LiveTranscript, type ProgressMessage } from "./live-transcript"
import "./turn-progress.css"

function elapsed(turn: Turn, now: number): string {
  const start = Date.parse(turn.created_at ?? "")
  const end = turn.finished_at ? Date.parse(turn.finished_at) : now
  if (!Number.isFinite(start) || !Number.isFinite(end)) return ""
  const seconds = Math.max(0, Math.floor((end - start) / 1000))
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`
}

/** One working section per turn; the separately saved answer stays authoritative. */
export function TurnProgress({ sessionId, turn, live, hasSavedAnswer, visible }: {
  sessionId: string; turn: Turn; live?: LiveTranscript; hasSavedAnswer: boolean; visible: boolean
}) {
  const running = ["starting", "running", "saving", "stopping"].includes(turn.status)
  const [open, setOpen] = useState(false)
  const [now, setNow] = useState(Date.now)
  const [saved, setSaved] = useState<ProgressMessage[] | null>(null)
  const [error, setError] = useState("")
  const [retry, setRetry] = useState(0)
  useEffect(() => { if (!running) setOpen(false) }, [running])
  useEffect(() => {
    if (!running || !visible) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [running, visible])
  useEffect(() => {
    if (!open || !visible || saved !== null || running) return
    const controller = new AbortController(), auth = csrfToken()
    setError("")
    void api<{ messages: ProgressMessage[] }>(
      `/sessions/${sessionId}/progress/${turn.id}`, "GET", undefined, controller.signal,
    ).then(value => {
      if (!controller.signal.aborted && auth === csrfToken()) setSaved(value.messages)
    }, reason => {
      if (!controller.signal.aborted && auth === csrfToken()) setError(String(reason))
    })
    return () => controller.abort()
  }, [open, visible, saved, running, sessionId, turn.id, retry])
  const messages = saved ?? (live ? progressMessages(live, hasSavedAnswer) : [])
  const latest = messages.at(-1)
  const duration = elapsed(turn, now)
  const label = running ? "Working" : turn.status === "waiting" ? "Waiting for your response"
    : turn.status === "failed" ? "Work stopped with an error" : turn.status === "stopped" ? "Work stopped" : "Worked"
  if (turn.status === "queued") return null
  return <section className={`turn-progress ${running ? "is-working" : "is-finished"}`} aria-label="Work progress" data-turn-id={turn.id}>
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger asChild><Button type="button" variant="ghost" className="turn-progress-toggle">
        {open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
        <span>{label}{duration ? ` for ${duration}` : ""}</span>
        {running && <span className="running-dot" aria-hidden="true" />}
      </Button></CollapsibleTrigger>
      {!open && running && latest && <div className="turn-progress-latest message-content">
        <Markdown remarkPlugins={[remarkGfm]}>{latest.text}</Markdown>
      </div>}
      <CollapsibleContent className="turn-progress-history">
        {messages.map(message => <div className="turn-progress-entry message-content" key={message.id}>
          <Markdown remarkPlugins={[remarkGfm]}>{message.text}</Markdown>
        </div>)}
        {!messages.length && !error && <p className="muted">{running ? "Waiting for the first update…" : saved === null ? "Loading saved progress…" : "No progress updates were recorded."}</p>}
        {error && <p role="alert">{error} <Button variant="ghost" size="sm" onClick={() => setRetry(value => value + 1)}>Retry</Button></p>}
        {running && live?.action && <p className="turn-progress-action">{live.action}</p>}
      </CollapsibleContent>
    </Collapsible>
  </section>
}
