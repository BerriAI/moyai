import React, { useCallback, useEffect, useRef, useState } from "react"
import Markdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { ChevronRight, File, Flower2, GitMerge, GitPullRequest, GitPullRequestClosed, GitPullRequestDraft, Globe, Image, Orbit, PanelRightOpen, Plus, RefreshCw, Shapes, Sparkles, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { api, ApiError } from "./api"
import type { SidebarSession } from "./session-actions"
import type { OutputResource } from "./workspace-types"
import { previewFileLink, type OpenFile } from "./file-links"
import "./workspace.css"

type PRSummary = NonNullable<SidebarSession["pull_requests"]>[number]
type AttachmentKind = "pull_request" | "output"
type Link = { id?: string; kind: AttachmentKind | "source"; url: string; title: string; origin: "attached" | "mentioned"; can_remove: boolean }
export type Subagent = { id: string; parent_id: string; session_id?: string | null; name: string; avatar: number; status: string; can_inspect: boolean; started_at: string; depth: number }
type PanelAgent = Pick<Subagent, "id" | "name" | "status" | "avatar"> & Partial<Pick<Subagent, "can_inspect">>
type WorkspaceData = {
  links: Link[]; files: { id: string; name: string; url: string; size: number; media_type?: string }[]
  sources: { id: string; name: string; media_type: string; url: string; size?: number }[]
  subagents: Subagent[]; subagents_limited: boolean
}
type AgentDetail = { id: string; name: string; output: string; steps: { kind: string; tool?: string; timestamp: string }[] }
type OverviewProps = {
  id: string; pullRequests: PRSummary[]; onUpdated: () => void
  onPullRequest: (url: string, title: string) => void
  onSubagent: (agent: Subagent) => void; onOutput: (output: OutputResource) => void
  onSubagentSideChat?: (agent: Subagent) => void
}
const statusText: Record<string, string> = { completed: "Ready", running: "Working", failed: "Failed", waiting: "Needs input", interrupted: "Interrupted", stopped: "Stopped", starting: "Starting", queued: "Queued" }
const isAccessError = (error: unknown) => error instanceof ApiError && [401, 403, 404].includes(error.status)
const message = (error: unknown) => error instanceof Error ? error.message : "The request could not be completed."
const avatarIndex = (value: number) => Number.isFinite(value) ? Math.abs(Math.trunc(value)) % 4 : 0

function AgentAvatar({ avatar }: { avatar: number }) {
  const index = avatarIndex(avatar), Icon = [Sparkles, Flower2, Orbit, Shapes][index]
  return <span className={`workspace-agent-avatar avatar-${index}`}><Icon size={18} aria-hidden="true" /></span>
}

function fileSize(size?: number) {
  if (typeof size !== "number" || !Number.isFinite(size) || size < 0) return "Size unavailable"
  if (size < 1024) return `${size.toLocaleString()} ${size === 1 ? "byte" : "bytes"}`
  const units = ["KiB", "MiB", "GiB", "TiB"]
  let value = size / 1024, unit = 0
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++ }
  return `${value.toLocaleString(undefined, { maximumFractionDigits: 1 })} ${units[unit]}`
}

function attachmentUrl(value: string, kind: AttachmentKind) {
  const text = value.trim()
  if (new TextEncoder().encode(text).length > 2048 || /[\u0000-\u001f]/.test(text)) throw new Error("Use a valid web address of at most 2,048 bytes.")
  let parsed: URL
  try { parsed = new URL(text) } catch { throw new Error("Use a complete HTTP or HTTPS web address.") }
  if (!["https:", "http:"].includes(parsed.protocol) || !parsed.hostname || parsed.username || parsed.password) throw new Error("Use an HTTP(S) address without embedded credentials.")
  if (kind === "pull_request" && (parsed.protocol !== "https:" || parsed.hostname !== "github.com" || (parsed.port && parsed.port !== "443") || !/^\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/pull\/[1-9][0-9]*\/?$/.test(parsed.pathname))) throw new Error("Use a GitHub pull request URL.")
  return text
}

export function WorkspaceOverview(props: OverviewProps) {
  // A session switch must never paint the previous session's resources or editor.
  return <WorkspaceContents key={props.id} {...props} />
}

function WorkspaceContents({ id, pullRequests, onPullRequest, onSubagent, onSubagentSideChat, onOutput, onUpdated }: OverviewProps) {
  const [data, setData] = useState<WorkspaceData | null>(null)
  const [error, setError] = useState("")
  const [linkKind, setLinkKind] = useState<AttachmentKind | null>(null)
  const [url, setUrl] = useState(""), [title, setTitle] = useState("")
  const [formError, setFormError] = useState(""), [busy, setBusy] = useState(false)
  const sequence = useRef(0), mounted = useRef(false)
  const read = useRef<AbortController | null>(null), write = useRef<AbortController | null>(null)
  const opener = useRef<HTMLElement | null>(null), root = useRef<HTMLDivElement>(null)
  const base = `/sessions/${encodeURIComponent(id)}`
  const refresh = useCallback(async () => {
    const version = ++sequence.current, controller = new AbortController()
    read.current?.abort(); read.current = controller
    try {
      const next = await api<WorkspaceData>(`${base}/workspace`, "GET", undefined, controller.signal)
      if (mounted.current && !controller.signal.aborted && version === sequence.current) { setData(next); setError("") }
    } catch (error) {
      if (mounted.current && !controller.signal.aborted && version === sequence.current) {
        if (isAccessError(error)) { setData(null); setLinkKind(null); setUrl(""); setTitle("") }
        setError(message(error))
      }
    }
  }, [base])
  useEffect(() => {
    mounted.current = true
    let stopped = false, timer: ReturnType<typeof setTimeout>
    const poll = async () => { await refresh(); if (!stopped) timer = setTimeout(poll, 4000) }
    void poll()
    return () => { stopped = true; mounted.current = false; ++sequence.current; read.current?.abort(); write.current?.abort(); clearTimeout(timer) }
  }, [refresh])
  function add(kind: AttachmentKind, trigger: HTMLElement) { opener.current = trigger; setUrl(""); setTitle(""); setFormError(""); setLinkKind(kind) }
  async function mutate(path: string, method: string, body?: object) {
    if (write.current) return
    const controller = new AbortController()
    write.current = controller; setBusy(true)
    try {
      await api(path, method, body, controller.signal)
      if (!mounted.current || controller.signal.aborted) return
      await refresh()
      if (mounted.current && !controller.signal.aborted) {
        setLinkKind(null); setUrl(""); setTitle(""); setFormError("")
        onUpdated()
      }
    } catch (error) {
      if (mounted.current && !controller.signal.aborted) {
        if (isAccessError(error)) { setData(null); setLinkKind(null); setError(message(error)) }
        else if (method === "POST") setFormError(message(error))
        else setError(message(error))
      }
    } finally { if (write.current === controller) write.current = null; if (mounted.current) setBusy(false) }
  }
  function removeControl(link: Link) {
    return link.can_remove && link.id ? <Button type="button" className="workspace-remove" size="icon-sm" variant="ghost" aria-label={`Remove link ${link.title}`} title={`Remove link ${link.title}`} disabled={busy} onClick={() => void mutate(`${base}/links/${encodeURIComponent(link.id!)}`, "DELETE")}><X size={14} /></Button> : null
  }
  function heading(label: string, count: number, kind?: AttachmentKind) {
    return <div className="workspace-picker-heading"><h3>{label}</h3><span>{count}</span>{kind && <Button type="button" size="icon-sm" variant="ghost" aria-label={`Add ${label.toLowerCase()}`} title={`Add ${label.toLowerCase()}`} disabled={busy} onClick={event => add(kind, event.currentTarget)}><Plus size={16} /></Button>}</div>
  }
  const prs = data?.links.filter(link => link.kind === "pull_request") ?? []
  for (const summary of pullRequests) {
    if (!prs.some(link => link.url.toLowerCase() === summary.url.toLowerCase())) prs.push({ kind: "pull_request", url: summary.url, title: summary.title, origin: "mentioned", can_remove: false })
  }
  const outputLinks = data?.links.filter(link => link.kind === "output") ?? []
  const outputs: OutputResource[] = data ? [
    ...data.files.map(file => ({ ...file, kind: "artifact" as const })),
    ...data.sources.map(file => ({ ...file, kind: "upload" as const })),
  ] : []
  return <div className="workspace-overview workspace-picker" ref={root} tabIndex={-1}>
    {error && <div className="workspace-picker-error" role="alert"><p>{error}</p><Button type="button" variant="outline" size="sm" onClick={() => void refresh()}><RefreshCw size={14} />Retry</Button></div>}
    {!data && !error && <p className="workspace-picker-empty" role="status">Loading workspace…</p>}
    {data && <>
      <section aria-label="Pull requests">{heading("Pull requests", prs.length, "pull_request")}
        {prs.map(link => {
          const summary = pullRequests.find(pr => pr.url.toLowerCase() === link.url.toLowerCase())
          const state = summary?.accessible ? summary.state === "open" ? summary.draft === true ? "draft" : summary.draft === false ? "open" : "unknown" : summary.state : "unknown"
          const label = state === "merged" ? "Merged" : state === "closed" ? "Closed" : state === "draft" ? "Draft" : state === "open" ? "Ready for review" : summary ? "Status unavailable" : link.origin === "mentioned" ? "Mentioned in chat" : "Attached to this session"
          const Icon = state === "merged" ? GitMerge : state === "closed" ? GitPullRequestClosed : state === "draft" ? GitPullRequestDraft : GitPullRequest
          const name = summary?.title || link.title || link.url
          return <div className="workspace-picker-row" key={link.url}><Button type="button" className="workspace-resource-action" variant="ghost" title={name} onClick={() => onPullRequest(link.url, name)}><Icon size={18} className={`workspace-pr-${state}`} aria-hidden="true" /><span><strong>{name}</strong><small>{label}</small></span><ChevronRight size={15} aria-hidden="true" /></Button>{removeControl(link)}</div>
        })}
        {!prs.length && <p className="workspace-picker-empty">Attach a PR or mention its GitHub URL in chat.</p>}
      </section>
      <section aria-label="Subagents">{heading("Subagents", data.subagents.length)}
        {data.subagents.map(agent => <div className="workspace-picker-row" key={agent.id}><Button type="button" className="workspace-resource-action workspace-agent-action" variant="ghost" disabled={!agent.can_inspect} title={agent.can_inspect ? agent.name : `${agent.name}: details are available only to the requester`} onClick={() => onSubagent(agent)}>
          <AgentAvatar avatar={agent.avatar} /><span><strong>{agent.name}</strong><small>{statusText[agent.status] ?? agent.status}{agent.depth > 0 ? " · Nested subagent" : ""}{!agent.can_inspect ? " · Requester-only details" : ""}</small></span><ChevronRight size={15} aria-hidden="true" />
        </Button>{agent.can_inspect && agent.session_id && onSubagentSideChat && <Button type="button" size="icon-sm" variant="ghost" aria-label={`Open ${agent.name} in side chat`} title="Open in side chat" onClick={() => onSubagentSideChat(agent)}><PanelRightOpen size={16} /></Button>}</div>)}
        {!data.subagents.length && <p className="workspace-picker-empty">Delegated work appears here when subagents start.</p>}
        {data.subagents_limited && <p className="workspace-picker-empty">Showing recent subagent work.</p>}
      </section>
      <section aria-label="Outputs">{heading("Outputs", outputs.length + outputLinks.length, "output")}
        {outputs.map(output => <div className="workspace-picker-row" key={`${output.kind}:${output.id}`}><Button type="button" className="workspace-resource-action" variant="ghost" title={output.name} onClick={() => onOutput(output)}>
          {output.media_type?.startsWith("image/") ? <Image size={18} aria-hidden="true" /> : <File size={18} aria-hidden="true" />}<span><strong>{output.name}</strong><small>{output.kind === "artifact" ? `${fileSize(output.size)} · Saved file` : `${output.size === undefined ? "" : `${fileSize(output.size)} · `}Uploaded file`}</small></span><ChevronRight size={15} aria-hidden="true" />
        </Button></div>)}
        {outputLinks.map(link => <div className="workspace-picker-row" key={link.url}><Button type="button" className="workspace-resource-action" variant="ghost" title={link.title || link.url} onClick={() => onOutput({ kind: "link", id: link.id, name: link.title || link.url, url: link.url })}>
          <Globe size={18} aria-hidden="true" /><span><strong>{link.title || link.url}</strong><small>Attached output link</small></span><ChevronRight size={15} aria-hidden="true" />
        </Button>{removeControl(link)}</div>)}
        {!outputs.length && !outputLinks.length && <p className="workspace-picker-empty">Saved files, uploads, and attached previews appear here.</p>}
      </section>
    </>}
    <Dialog open={linkKind !== null} onOpenChange={open => { if (!open && !busy) setLinkKind(null) }}><DialogContent className="workspace-link-dialog" onEscapeKeyDown={event => event.stopPropagation()} onCloseAutoFocus={event => {
      event.preventDefault()
      if (opener.current?.isConnected) opener.current.focus({ preventScroll: true })
      else root.current?.focus({ preventScroll: true })
    }}><DialogHeader><DialogTitle>{linkKind === "pull_request" ? "Attach pull request" : "Add output link"}</DialogTitle><DialogDescription>The link will be visible to everyone who can access this session.</DialogDescription></DialogHeader>
      <form className="workspace-link-form" onSubmit={event => {
        event.preventDefault()
        if (write.current || !linkKind) return
        setFormError("")
        try { const validated = attachmentUrl(url, linkKind); void mutate(`${base}/links`, "POST", { kind: linkKind, url: validated, title: title.trim() }) }
        catch (error) { setFormError(message(error)) }
      }}>
        <label>URL<Input autoFocus required type="url" maxLength={2048} disabled={busy} value={url} onChange={event => setUrl(event.target.value)} placeholder={linkKind === "pull_request" ? "https://github.com/owner/repo/pull/123" : "https://…"} /></label>
        <label>Label (optional)<Input value={title} disabled={busy} maxLength={200} onChange={event => setTitle(event.target.value)} /></label>
        {formError && <p className="workspace-picker-error" role="alert">{formError}</p>}
        <div className="workspace-link-actions"><Button type="button" variant="outline" disabled={busy} onClick={() => setLinkKind(null)}>Cancel</Button><Button type="submit" disabled={busy || !url.trim()}>{busy ? "Adding…" : "Add link"}</Button></div>
      </form>
    </DialogContent></Dialog>
  </div>
}

export function SubagentPanel({ sessionId, agent, active = true, onOpenFile }: { sessionId: string; agent: PanelAgent; active?: boolean; onOpenFile?: OpenFile }) {
  return <SubagentContent key={`${sessionId}:${agent.id}:${agent.can_inspect !== false}`} sessionId={sessionId} agent={agent} active={active} onOpenFile={onOpenFile} />
}

function SubagentContent({ sessionId, agent, active, onOpenFile }: { sessionId: string; agent: PanelAgent; active: boolean; onOpenFile?: OpenFile }) {
  const [detail, setDetail] = useState<AgentDetail | null>(null)
  const [error, setError] = useState(""), [loading, setLoading] = useState(false)
  const [revision, setRevision] = useState(0)
  const canInspect = agent.can_inspect !== false
  useEffect(() => {
    if (!active || !canInspect) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      let terminal = ["completed", "failed", "interrupted", "stopped"].includes(agent.status), denied = false
      setLoading(true)
      try {
        const value = await api<AgentDetail>(`/sessions/${encodeURIComponent(sessionId)}/subagents/${encodeURIComponent(agent.id)}`, "GET", undefined, controller.signal)
        if (controller.signal.aborted) return
        setDetail(value); setError("")
        terminal = terminal || value.steps.some(step => ["run_completed", "run_failed"].includes(step.kind))
      } catch (error) {
        if (controller.signal.aborted) return
        setDetail(null); denied = isAccessError(error)
        setError(denied ? "These requester-only details are no longer available to this account." : message(error))
      } finally { if (!controller.signal.aborted) setLoading(false) }
      if (!controller.signal.aborted && !terminal && !denied) timer = setTimeout(poll, 4000)
    }
    void poll()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [active, canInspect, agent.id, agent.status, sessionId, revision])
  const last = detail?.steps.filter(step => ["run_completed", "run_failed"].includes(step.kind)).at(-1)
  const status = last?.kind === "run_completed" ? "completed" : last?.kind === "run_failed" ? "failed" : agent.status
  return <section className="workspace-subagent-panel" aria-label={`${agent.name} details`}>
    <header><AgentAvatar avatar={agent.avatar} /><div><h2>{detail?.name || agent.name}</h2><p aria-live="polite">{statusText[status] ?? status} · Requester-only details</p></div>
      {canInspect && <Button type="button" size="icon-sm" variant="ghost" aria-label="Refresh subagent details" title="Refresh subagent details" disabled={!active || loading} onClick={() => setRevision(value => value + 1)}><RefreshCw size={16} /></Button>}</header>
    {!canInspect ? <p className="workspace-picker-empty">Only the person who requested this subagent can inspect its details.</p>
      : error ? <div className="workspace-picker-error" role="alert"><p>{error}</p><Button type="button" variant="outline" size="sm" disabled={!active || loading} onClick={() => setRevision(value => value + 1)}>Retry</Button></div>
        : !detail ? <p className="workspace-picker-empty" role="status">{active ? "Loading subagent…" : "Details load when this tab is selected."}</p> : <>
          <div className="message-content subagent-result" onClickCapture={event => previewFileLink(event, sessionId, onOpenFile)}>{detail.output ? <Markdown remarkPlugins={[remarkGfm]}>{detail.output}</Markdown> : <p className="workspace-picker-empty">No response has been saved yet.</p>}</div>
          <details><summary>Recorded steps · {detail.steps.length}</summary><ol className="subagent-steps">{detail.steps.map((step, index) => <li key={`${step.timestamp}:${index}`}><span>{step.kind.replaceAll("_", " ")}{step.tool ? ` · ${step.tool}` : ""}</span>{Number.isFinite(Date.parse(step.timestamp)) && <time dateTime={step.timestamp}>{new Date(step.timestamp).toLocaleTimeString()}</time>}</li>)}</ol></details>
        </>}
  </section>
}
