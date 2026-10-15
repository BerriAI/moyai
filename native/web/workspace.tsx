import React, { useCallback, useEffect, useRef, useState } from "react"
import Markdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { ArrowUpRight, ChevronRight, Flower2, Orbit, Shapes, Sparkles, FileDown, GitPullRequest, Globe, Image, ListChecks, Monitor, Plus, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { api, ApiError } from "./api"
import type { SidebarSession } from "./session-actions"
type PRSummary = NonNullable<SidebarSession["pull_requests"]>[number]

type LinkKind = "pull_request" | "output" | "source"
type Link = { id?: string; kind: LinkKind; url: string; title: string; origin: "attached" | "mentioned"; can_remove: boolean }
type Subagent = { id: string; parent_id: string; name: string; avatar: number; status: string; can_inspect: boolean; started_at: string; depth: number }
type WorkspaceData = {
  links: Link[]; files: { id: string; name: string; url: string; size: number }[]
  sources: { id: string; name: string; media_type: string; url: string }[]
  subagents: Subagent[]; subagents_limited: boolean
}
type AgentDetail = { id: string; name: string; output: string; steps: { kind: string; tool?: string; timestamp: string }[] }
const statusText: Record<string, string> = { completed: "Ready", running: "Working", failed: "Failed", waiting: "Needs input", interrupted: "Interrupted" }

export function WorkspaceOverview({ id, onComputer, onPlan, pullRequests, onPullRequest, onUpdated }: { id: string; onComputer: () => void; onPlan: () => void; pullRequests: PRSummary[]; onPullRequest: (url: string) => void; onUpdated: () => void }) {
  const [data, setData] = useState<WorkspaceData | null>(null)
  const [error, setError] = useState("")
  const [linkKind, setLinkKind] = useState<LinkKind | null>(null)
  const [url, setUrl] = useState("")
  const [title, setTitle] = useState("")
  const [formError, setFormError] = useState("")
  const [busy, setBusy] = useState(false)
  const [selectedAgent, setSelectedAgent] = useState<Subagent | null>(null)
  const sequence = useRef(0)
  const refresh = useCallback(async (signal?: AbortSignal) => {
    const version = ++sequence.current
    try {
      const next = await api<WorkspaceData>(`/sessions/${id}/workspace`, "GET", undefined, signal)
      if (!signal?.aborted && version === sequence.current) { setData(next); setError("") }
    } catch (error) {
      if (!signal?.aborted && version === sequence.current) {
        if (error instanceof ApiError && [401, 403, 404].includes(error.status)) setData(null)
        setError(String(error))
      }
    }
  }, [id])
  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => { await refresh(controller.signal); if (!controller.signal.aborted) timer = setTimeout(poll, 4000) }
    void poll()
    return () => { controller.abort(); ++sequence.current; clearTimeout(timer) }
  }, [refresh])
  function add(kind: LinkKind) { setUrl(""); setTitle(""); setFormError(""); setLinkKind(kind) }
  async function remove(link: Link) {
    if (busy || !link.id) return
    setBusy(true)
    try { await api(`/sessions/${id}/links/${link.id}`, "DELETE"); await refresh(); onUpdated() }
    catch (error) { setError(String(error)) } finally { setBusy(false) }
  }
  function links(kind: LinkKind) {
    return data?.links.filter(link => link.kind === kind).map(link => <div className="resource-row" key={`${link.kind}:${link.url}`}>
      <a href={link.url} target="_blank" rel="noreferrer">{kind === "pull_request" ? <GitPullRequest size={17} /> : <Globe size={17} />}<span>{link.title}<small>{link.origin === "mentioned" ? "Mentioned in chat" : "Attached to this session"}</small></span><ArrowUpRight size={14} /></a>
      {link.can_remove && <Button size="icon-sm" variant="ghost" aria-label={`Remove link ${link.title}`} disabled={busy} onClick={() => void remove(link)}><X size={13} /></Button>}
    </div>)
  }
  function heading(label: string, kind?: LinkKind, count?: number) {
    return <div className="workspace-section-heading"><h3>{label}</h3>{count !== undefined && <small>{count}</small>}{kind && <Button size="icon-sm" variant="ghost" aria-label={`Add ${label.toLowerCase()}`} onClick={() => add(kind)}><Plus size={16} /></Button>}</div>
  }
  const prs = data?.links.filter(link => link.kind === "pull_request") ?? []
  for (const summary of pullRequests) {
    if (!prs.some(link => link.url.toLowerCase() === summary.url.toLowerCase())) prs.push({ kind: "pull_request", url: summary.url, title: summary.title, origin: "mentioned", can_remove: false })
  }
  return <div className="workspace-overview">
    <div className="workspace-shortcuts"><Button variant="outline" size="sm" onClick={onComputer}><Monitor size={15} />Computer</Button><Button variant="outline" size="sm" onClick={onPlan}><ListChecks size={15} />Plan</Button></div>
    {error && <p className="error" role="alert">{error}<Button variant="ghost" size="sm" onClick={() => void refresh()}>Retry</Button></p>}
    {!data && !error && <p className="muted" role="status">Loading workspace…</p>}
    {data && <>
      <section>{heading("Pull requests", "pull_request", prs.length)}{prs.map(link => {
        const summary = pullRequests.find(pr => pr.url.toLowerCase() === link.url.toLowerCase())
        const state = summary?.accessible ? summary.state === "merged" ? "Merged" : summary.state === "closed" ? "Closed" : summary.state === "open" ? summary.draft ? "Draft" : "Ready for review" : "Status unavailable" : "Status unavailable"
        return <div className="resource-row" key={link.url}><Button className="resource-link" variant="ghost" onClick={() => onPullRequest(link.url)}><GitPullRequest size={17} className={`pr-state-${summary?.state ?? "unknown"}`} /><span>{summary?.title || link.title}<small>{summary ? state : link.origin === "mentioned" ? "Mentioned in chat" : "Attached to this session"}</small></span><ChevronRight size={14} /></Button>{link.can_remove && <Button size="icon-sm" variant="ghost" aria-label={`Remove link ${link.title}`} disabled={busy} onClick={() => void remove(link)}><X size={13} /></Button>}</div>
      })}
        {!prs.length && <p className="workspace-empty">Attach a PR, or mention its GitHub URL in chat.</p>}
      </section>
      <section>{heading("Outputs", "output", data.files.length + data.links.filter(link => link.kind === "output").length)}{links("output")}
        {data.files.map(file => <div className="resource-row" key={file.id}><a href={file.url} download><FileDown size={17} /><span>{file.name}<small>{(file.size / 1024).toFixed(1)} KB · Saved file</small></span></a></div>)}
        {!data.files.length && !data.links.some(link => link.kind === "output") && <p className="workspace-empty">Saved files and attached previews will appear here.</p>}
      </section>
      <section>{heading("Subagents", undefined, data.subagents.length)}
        {!data.subagents.length && <p className="workspace-empty">Delegated work will appear here as Moyai starts subagents.</p>}
        {data.subagents.map(agent => { const Avatar = [Sparkles, Flower2, Orbit, Shapes][agent.avatar % 4]; return <Button key={agent.id} className="subagent-card" variant="ghost" disabled={!agent.can_inspect} onClick={() => setSelectedAgent(agent)}>
          <span className={`subagent-icon avatar-${agent.avatar}`}><Avatar size={20} /></span><span><strong>{agent.name}</strong><small>{statusText[agent.status] ?? agent.status}{agent.depth > 0 ? " · Nested subagent" : ""}{!agent.can_inspect ? " · Requester-only details" : ""}</small></span><ChevronRight size={15} />
        </Button> })}
        {data.subagents_limited && <p className="workspace-empty">Showing recent subagent work.</p>}
      </section>
      <section>{heading("Sources", "source", data.sources.length + data.links.filter(link => link.kind === "source").length)}{links("source")}
        {data.sources.map(source => <div className="resource-row" key={source.id}><a href={source.url} target="_blank" rel="noreferrer">{source.media_type.startsWith("image/") ? <img src={source.url} alt="" /> : <Image size={17} />}<span>{source.name}<small>Attached image</small></span><ArrowUpRight size={14} /></a></div>)}
        {!data.sources.length && !data.links.some(link => link.kind === "source") && <p className="workspace-empty">Images sent in this session and attached source links.</p>}
      </section>
    </>}
    <Dialog open={linkKind !== null} onOpenChange={open => { if (!open && !busy) setLinkKind(null) }}><DialogContent><DialogHeader><DialogTitle>{linkKind === "pull_request" ? "Attach pull request" : linkKind === "source" ? "Add source link" : "Add output link"}</DialogTitle><DialogDescription>The link will be visible to everyone who can access this session.</DialogDescription></DialogHeader>
      <form className="resource-form" onSubmit={async event => {
        event.preventDefault(); setBusy(true); setFormError("")
        try { await api(`/sessions/${id}/links`, "POST", { kind: linkKind, url, title }); setLinkKind(null); await refresh(); onUpdated() }
        catch (error) { setFormError(String(error)) } finally { setBusy(false) }
      }}>
        <label>URL<Input autoFocus required type="url" value={url} onChange={event => setUrl(event.target.value)} placeholder={linkKind === "pull_request" ? "https://github.com/owner/repo/pull/123" : "https://…"} /></label>
        <label>Label (optional)<Input value={title} maxLength={200} onChange={event => setTitle(event.target.value)} /></label>
        {formError && <p className="error" role="alert">{formError}</p>}
        <div className="dialog-actions"><Button type="button" variant="outline" disabled={busy} onClick={() => setLinkKind(null)}>Cancel</Button><Button type="submit" disabled={busy || !url.trim()}>{busy ? "Adding…" : "Add link"}</Button></div>
      </form>
    </DialogContent></Dialog>
    <SubagentDetail sessionId={id} agent={data?.subagents.find(agent => agent.id === selectedAgent?.id) ?? selectedAgent} onClose={() => setSelectedAgent(null)} />
  </div>
}

function SubagentDetail({ sessionId, agent, onClose }: { sessionId: string; agent: Subagent | null; onClose: () => void }) {
  const [detail, setDetail] = useState<AgentDetail | null>(null)
  const [error, setError] = useState("")
  useEffect(() => {
    setDetail(null); setError("")
    if (!agent) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try { const value = await api<AgentDetail>(`/sessions/${sessionId}/subagents/${encodeURIComponent(agent.id)}`, "GET", undefined, controller.signal); if (!controller.signal.aborted) { setDetail(value); setError("") } }
      catch (error) { if (!controller.signal.aborted) { setDetail(null); setError(String(error)) } }
      if (!controller.signal.aborted && ["running", "waiting"].includes(agent.status)) timer = setTimeout(poll, 4000)
    }
    void poll()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [agent?.id, agent?.status, sessionId])
  return <Dialog open={Boolean(agent)} onOpenChange={open => { if (!open) onClose() }}><DialogContent className="subagent-dialog"><DialogHeader><DialogTitle>{agent?.name ?? "Subagent"}</DialogTitle><DialogDescription>{statusText[agent?.status ?? ""] ?? ""} · Native agent run</DialogDescription></DialogHeader>
    {error ? <p className="error" role="alert">{error}</p> : !detail ? <p role="status">Loading subagent…</p> : <>
      <div className="message-content subagent-result">{detail.output ? <Markdown remarkPlugins={[remarkGfm]}>{detail.output}</Markdown> : <p className="muted">No response has been saved yet.</p>}</div>
      <details><summary>Recorded steps · {detail.steps.length}</summary><ol className="subagent-steps">{detail.steps.map((step, index) => <li key={index}>{step.kind.replaceAll("_", " ")}{step.tool ? ` · ${step.tool}` : ""}</li>)}</ol></details>
    </>}
  </DialogContent></Dialog>
}
