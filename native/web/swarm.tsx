import React, { useEffect, useId, useRef, useState } from "react"
import { ExternalLink, Pause, Play, Square } from "lucide-react"
import Markdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { ApiError, api, csrfToken } from "./api"
import { AgentViewSwitch } from "./agent-space"
import { SwarmSpace } from "./swarm-space"
import "./swarm.css"

export type SwarmOptions = { workers: number | null; budget_seconds: number; worker_models: string[] }
type Member = {
  session_id: string; name: string; model: string; coordinator: boolean; status: string
  active_turn: string | null; assignment: string; deleted: boolean
}
type Post = { id: string; sequence: number; kind: string; author: string; author_session_id: string | null; content: string; created_at: string }
export type SwarmView = {
  id: string; status: string; reason: string; revision: number; generation: number; round: number
  goal: string; title?: string; budget_seconds: number; ends_at: string | null; leader_id: string | null
  can_control: boolean; sequence: number; members: Member[]; posts: Post[]; has_more: boolean; next_after: number
}

const windows = [
  { value: "0", label: "No time limit" }, { value: "300", label: "5 minutes" },
  { value: "1800", label: "30 minutes" }, { value: "3600", label: "1 hour" },
  { value: "14400", label: "4 hours" }, { value: "86400", label: "1 day" },
]

export function validWorkerCount(value: number | null): value is number {
  return value !== null && Number.isSafeInteger(value) && value >= 2
}

export function SwarmSetup({ value, onChange, models, disabled }: {
  value: SwarmOptions; onChange: (value: SwarmOptions) => void; models: string[]; disabled: boolean
}) {
  const workerHint = useId(), windowHint = useId()
  const valid = validWorkerCount(value.workers)
  return <fieldset className="swarm-setup" disabled={disabled}>
    <legend>Team settings</legend>
    <div className="swarm-settings-row">
      <label><span>Workers</span>
        <Input aria-label="Swarm workers" type="number" min={2} step={1} required disabled={disabled}
          value={value.workers ?? ""} aria-invalid={!valid} aria-describedby={workerHint}
          onChange={event => onChange({ ...value, workers: event.currentTarget.value === "" ? null : event.currentTarget.valueAsNumber })} />
        <span id={workerHint} className="swarm-field-hint" data-invalid={!valid}>
          {valid ? "At least 2 workers, plus a coordinator." : "Enter a whole number of at least 2."}
        </span>
      </label>
      <label><span>Work window</span><Select value={String(value.budget_seconds)} disabled={disabled} onValueChange={seconds => onChange({ ...value, budget_seconds: Number(seconds) })}>
        <SelectTrigger aria-label="Swarm work window" aria-describedby={windowHint}><SelectValue /></SelectTrigger>
        <SelectContent>{windows.map(option => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}</SelectContent>
      </Select><span id={windowHint} className="swarm-field-hint">
        {value.budget_seconds ? "Maximum time for the entire team." : "Runs until the goal is complete."}
      </span></label>
    </div>
    <p className="muted">Each worker has its own conversation and computer. The team shares a goal and board.</p>
    {models.length > 1 && <details><summary>Worker models · {value.worker_models.length || "inherit coordinator"}</summary>
      <div className="swarm-models">{models.map(model => <label key={model}>
        <Checkbox disabled={disabled} checked={value.worker_models.includes(model)} onCheckedChange={checked => onChange({
          ...value, worker_models: checked ? [...value.worker_models, model] : value.worker_models.filter(item => item !== model),
        })} />{model}
      </label>)}</div>
    </details>}
  </fieldset>
}

function SwarmWindow({ data, visible, busy, onChange }: {
  data: SwarmView; visible: boolean; busy: boolean; onChange: (seconds: number) => void
}) {
  const [now, setNow] = useState(Date.now)
  const countdown = Boolean(data.ends_at && ["active", "paused"].includes(data.status))
  useEffect(() => {
    if (!visible || !countdown) return
    setNow(Date.now())
    const timer = setInterval(() => { if (!document.hidden) setNow(Date.now()) }, 1000)
    return () => clearInterval(timer)
  }, [visible, countdown, data.ends_at])
  const seconds = Math.max(0, Math.ceil((Date.parse(data.ends_at ?? "") - now) / 1000)) || 0
  const duration = data.budget_seconds % 3600 === 0 ? `${data.budget_seconds / 3600}h`
    : data.budget_seconds % 60 === 0 ? `${data.budget_seconds / 60}m` : `${data.budget_seconds}s`
  const label = !data.budget_seconds ? "No time limit" : countdown
    ? `${Math.floor(seconds / 3600)}:${String(Math.floor(seconds / 60) % 60).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")} left`
    : `${duration} window`
  const timeOptions = windows.some(option => option.value === String(data.budget_seconds)) ? windows
    : [...windows, { value: String(data.budget_seconds), label: `${duration} window` }]
  if (!data.can_control) return <span className="swarm-meta">{label}</span>
  return <Popover><PopoverTrigger asChild>
    <Button className="swarm-clock" variant="ghost" size="sm" aria-label="Change work window">{label}</Button>
  </PopoverTrigger><PopoverContent className="swarm-time-card" aria-label="Work window">
    <label htmlFor={`window-${data.id}`}>Work window</label>
    <Select disabled={busy} value={String(data.budget_seconds)} onValueChange={value => onChange(Number(value))}>
      <SelectTrigger id={`window-${data.id}`} aria-label="Change swarm work window"><SelectValue /></SelectTrigger>
      <SelectContent>{timeOptions.map(option => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}</SelectContent>
    </Select>
    <p>{data.ends_at ? `Ends ${new Date(data.ends_at).toLocaleString()}. Includes startup, queueing and pauses.`
      : "Works until the goal is complete, or you pause or stop it."}</p>
  </PopoverContent></Popover>
}

export function SwarmPanel({ sessionId, visible = true, onChanged, space = false, spaceBlocked = false, onSpaceChange }: {
  sessionId: string; visible?: boolean; onChanged: () => Promise<unknown>
  space?: boolean; spaceBlocked?: boolean; onSpaceChange?: (value: boolean) => void
}) {
  const [data, setData] = useState<SwarmView | null>(null)
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  const [expanded, setExpanded] = useState(true)
  const version = useRef(0), mounted = useRef(true), busyNow = useRef(false)
  const changed = useRef(onChanged); changed.current = onChanged
  useEffect(() => {
    mounted.current = true
    setData(null); setError("")
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function load() {
      if (busyNow.current || document.hidden) {
        if (!controller.signal.aborted && visible) timer = setTimeout(() => void load(), 3000)
        return
      }
      const current = ++version.current
      try {
        const value = await api<SwarmView | null>(`/sessions/${encodeURIComponent(sessionId)}/swarm`, "GET", undefined, controller.signal)
        if (!controller.signal.aborted && current === version.current && !busyNow.current) { setData(value); setError("") }
      } catch (reason) {
        if (!controller.signal.aborted && current === version.current) {
          if (reason instanceof ApiError && [401, 403, 404].includes(reason.status)) setData(null)
          setError(String(reason))
        }
      } finally {
        if (!controller.signal.aborted && visible) timer = setTimeout(() => void load(), 3000)
      }
    }
    if (visible) void load()
    return () => { mounted.current = false; ++version.current; controller.abort(); clearTimeout(timer) }
  }, [sessionId, visible])

  async function control(action: "pause" | "resume" | "stop" | "time", seconds?: number) {
    if (!data || busyNow.current) return
    busyNow.current = true; setBusy(true); setError(""); ++version.current
    const token = csrfToken()
    try {
      const value = await api<SwarmView>(`/swarms/${encodeURIComponent(data.id)}/control`, "POST", {
        action, revision: data.revision, ...(seconds === undefined ? {} : { budget_seconds: seconds }),
      })
      if (mounted.current && csrfToken() === token) { ++version.current; setData(value); await changed.current() }
    } catch (reason) {
      if (mounted.current && csrfToken() === token) setError(String(reason))
    } finally { busyNow.current = false; if (mounted.current) setBusy(false) }
  }

  if (!data) return <p className="notice" role="status">{error ? `Team details are unavailable. ${error}` : "Loading team…"}</p>
  const active = data.status === "active", workers = data.members.filter(member => !member.coordinator)
  return <section className="swarm-panel" aria-label="Swarm mission">
    <div className="swarm-heading">
      <Button className="swarm-heading-title" variant="ghost" size="sm" aria-expanded={space || expanded} onClick={() => {
        if (space) { onSpaceChange?.(false); setExpanded(false) } else setExpanded(value => !value)
      }}>Swarm <span>{workers.length} workers</span></Button>
      <span className="swarm-state" data-state={data.status}>{data.status === "attention" ? "Needs attention" : data.status}</span>
      <span className="swarm-meta">Round {data.round}</span>
      <SwarmWindow data={data} visible={visible} busy={busy} onChange={seconds => void control("time", seconds)} />
      <div className="swarm-toolbar">
        {onSpaceChange && <AgentViewSwitch space={space} disabled={spaceBlocked} onChange={value => {
          if (!value) setExpanded(true)
          onSpaceChange(value)
        }} />}
        {sessionId !== data.id && <a className="swarm-mission-link" href={`#${data.id}`} aria-label="Open mission"><ExternalLink size={13} /><span>Mission</span></a>}
        {data.can_control && <div className="swarm-controls">
        <Button size="sm" variant="ghost" aria-label={active ? "Pause team" : "Resume team"} disabled={busy} onClick={() => void control(active ? "pause" : "resume")}>
          {active ? <Pause size={13} /> : <Play size={13} />}{active ? "Pause" : "Resume"}
        </Button>
        <Button className="swarm-stop" size="sm" variant="ghost" aria-label="Stop team" disabled={busy || data.status === "stopped"} onClick={() => void control("stop")}><Square size={12} />Stop</Button>
        </div>}
      </div>
    </div>
    {error && <p className="error" role="alert">{error}</p>}
    {data.reason && <p className="notice" role="status">{data.reason}</p>}
    {!active && data.members.some(member => member.status === "waiting") &&
      <p className="notice">Pending questions and approvals are saved. Resume the team before answering.</p>}
    {(space || expanded) && <div className={`swarm-details${space ? " swarm-space-details" : ""}`}>
      {space ? <SwarmSpace data={data} sessionId={sessionId} visible={visible} onConversation={() => { setExpanded(true); onSpaceChange?.(false) }} /> : <>
      <div className="swarm-roster" aria-label="Swarm members">{data.members.map(member => <a
        key={member.session_id} href={`#${member.session_id}`} aria-current={member.session_id === sessionId ? "page" : undefined}
        className="swarm-member">
        <strong>{member.name}{member.session_id === data.leader_id && <span className="swarm-lead">Lead</span>}</strong>
        <span>{member.deleted ? "Deleted" : member.status === "waiting" ? "Needs input" : member.status}</span>
        <small title={member.model}>{member.model}</small>
      </a>)}</div>
      <details><summary>Shared goal</summary><p className="swarm-goal">{data.goal}</p></details>
      <details className="swarm-board" open>
        <summary>Team chat · {data.sequence > 50 ? "latest 50 updates" : `${data.posts.length} updates`}</summary>
        <div className="swarm-feed" role="log" aria-live="off">
          {data.posts.filter(post => post.kind !== "goal").map(post => <article key={post.id} className="swarm-post">
            <div><strong>{post.author}</strong><span className="muted">{post.kind === "human" ? "Direction" : post.kind}</span>
              <time dateTime={post.created_at}>{new Date(post.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time></div>
            <div className="message-content"><Markdown remarkPlugins={[remarkGfm]}>{post.content}</Markdown></div>
            {post.kind === "result" && post.author_session_id && <a href={`#${post.author_session_id}`}>Open full conversation</a>}
          </article>)}
          {!data.posts.some(post => post.kind !== "goal") && <p className="muted">The team’s real findings and assignments will appear here.</p>}
        </div>
      </details>
      </>}
    </div>}
  </section>
}
