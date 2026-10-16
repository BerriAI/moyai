import { useEffect, useRef, useState, type ReactNode } from "react"
import Markdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { ArrowLeft, ArrowRight, Copy, Download, FileText, History, LockKeyhole, MessageSquare, Plus, RefreshCw } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { api, ApiError } from "./api"
import "./product-history.css"

export type HistorySessionRef = { source: "native" | "legacy"; id: string; url: string; source_session_id?: string }
export type ProductHistoryProps = {
  sessionId: string
  onOpenSession?: (session: HistorySessionRef) => void
  /** Open a fresh native composer. Do not copy history, files, credentials or execution state. */
  onStartNewNative?: () => void | Promise<void>
  active?: boolean
  /** Include user ID and authority/connection version; changes discard all loaded private data. */
  scopeKey?: string
  /** Render the immutable earlier transcript within an already continued native chat. */
  historicalOnly?: boolean
}
type Session = HistorySessionRef & {
  title: string; status: string; model: string; visibility: string; created_at: string; updated_at: string
  owner?: { id: string; name: string | null }; parent_session_id?: string | null; root_session_id?: string
  archived: boolean; pinned: boolean; folder: string; execution_engine: string; read_only: boolean
  read_marker_key?: string; completion_marker?: string
}
type Message = {
  id: number; role: string; author_id?: string; author_name?: string; status?: string; created_at: string
  content: string; content_truncated?: boolean; attachments?: Resource[]
}
type Resource = {
  id: string; name: string; kind: "upload" | "artifact"; size: number; media_type: string
  url: string; preview_url?: string; download_url?: string; original_url?: string
  preview_supported?: boolean; compatibility_download_supported?: boolean; original_download_available?: boolean
}
type Page<T> = { has_more: boolean; next_offset: number | null } & T
type Files = {
  files: Resource[]; sources: Resource[]; next_offset: number | null
  has_more: Record<string, boolean>; limitations?: string[]; capture_inventory_truncated?: boolean
}
type Preview = {
  name: string; preview_kind: string; text: string | null; language: string | null
  truncated: boolean; notice: string | null; size: number; media_type: string
}
type Load<T> = { key: string; data?: T; loading: boolean; error?: Error }

function useJson<T>(path: string | null, enabled: boolean, revision: number): Load<T> {
  const key = `${path ?? ""}:${revision}`
  const [result, setResult] = useState<Load<T>>({ key: "", loading: false })
  useEffect(() => {
    if (!enabled || !path) { setResult({ key, loading: false }); return }
    const controller = new AbortController()
    setResult({ key, loading: true })
    void api<T>(path, "GET", undefined, controller.signal).then(data => {
      if (!controller.signal.aborted) setResult({ key, data, loading: false })
    }, reason => {
      if (!controller.signal.aborted) setResult({
        key, loading: false, error: reason instanceof Error ? reason : new Error("History could not be loaded."),
      })
    })
    return () => controller.abort()
  }, [path, enabled, key])
  return enabled && result.key === key ? result : { key, loading: Boolean(enabled && path) }
}

function denied(error?: Error) {
  return error instanceof ApiError && [401, 403, 404].includes(error.status)
}
function Notice({ children, error = false }: { children: ReactNode; error?: boolean }) {
  return <Alert className="ph-notice" variant={error ? "destructive" : "default"} role={error ? "alert" : "note"}>
    <AlertDescription>{children}</AlertDescription>
  </Alert>
}
function timestamp(value?: string) {
  if (!value) return ""
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })
}
function bytes(value: number) {
  return value < 1024 ? `${value} B` : value < 1024 ** 2 ? `${(value / 1024).toFixed(1)} KB` : `${(value / 1024 ** 2).toFixed(1)} MB`
}

/** Accept only the adapter's same-origin, session-scoped read endpoints. */
export function productFileUrl(value: string | undefined, sessionId: string): string | undefined {
  if (!value || !/^[a-f0-9]{32}$/.test(sessionId) || !value.startsWith("/") || /[\\\u0000-\u0020\u007f]/.test(value)) return
  try {
    const url = new URL(value, window.location.origin)
    if (url.origin !== window.location.origin || url.username || url.password || value.startsWith("//")) return
    const recovery = new RegExp(`^/api/product/sessions/${sessionId}/continuation/archive/[a-f0-9]{32}$`).test(url.pathname)
    if (!recovery && ![ `/api/product/sessions/${sessionId}/files/content`, `/api/runs/${sessionId}/artifact` ].includes(url.pathname)) return
    return url.pathname + url.search
  } catch { return }
}
function externalLink(value?: string) {
  if (!value || /[\u0000-\u0020\u007f\\]/.test(value)) return
  const authority = value.match(/^https?:\/\/([^/?#]*)/i)?.[1]
  if (!authority || authority.includes("@")) return
  try {
    const url = new URL(value)
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : undefined
  } catch { return }
}
function Prose({ text }: { text: string }) {
  return <div className="ph-prose"><Markdown skipHtml remarkPlugins={[remarkGfm]} components={{
    a: ({ href, children }) => {
      const local = href && (
        /^\/#run=[a-f0-9]{32}$/.test(href) ||
        /^\/api\/runs\/[a-f0-9]{32}\/artifact(?:\?revision=[a-f0-9]{24,64})?$/.test(href)
      )
      if (local) return <a href={href}>{children}</a>
      const url = externalLink(href)
      return url ? <a href={url} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>
    },
    img: ({ alt }) => <span className="ph-muted">[Image: {alt || "not embedded"}]</span>,
    table: ({ children }) => <div className="ph-table" role="region" aria-label="Message table" tabIndex={0}><table>{children}</table></div>,
  }}>{text}</Markdown></div>
}

export function ProductHistory(props: ProductHistoryProps) {
  return <HistoryView key={`${props.scopeKey ?? ""}:${props.sessionId}`} {...props} />
}

function HistoryView({ sessionId, onOpenSession, onStartNewNative, active = true, historicalOnly = false }: ProductHistoryProps) {
  const [visible, setVisible] = useState(!document.hidden)
  const [revision, setRevision] = useState(0), [authorityRevision, setAuthorityRevision] = useState(0)
  const [messageOffset, setMessageOffset] = useState(0), [childOffset, setChildOffset] = useState(0), [fileOffset, setFileOffset] = useState(0)
  const [selectedFile, setSelectedFile] = useState<Resource | null>(null)
  const [securityError, setSecurityError] = useState<Error | null>(null)
  const [actionError, setActionError] = useState(""), [copyState, setCopyState] = useState("")
  const [starting, setStarting] = useState(false)
  const heading = useRef<HTMLHeadingElement>(null), focused = useRef(false), startPending = useRef(false)
  const mounted = useRef(true)
  const enabled = active && visible && !securityError
  const interactive = useRef(enabled)
  interactive.current = enabled
  const root = `/product/sessions/${encodeURIComponent(sessionId)}`
  const detail = useJson<{ session: Session; continuation?: HistorySessionRef; continuation_supported?: boolean }>(
    `/history/sessions/${encodeURIComponent(sessionId)}?source=legacy`, enabled, revision + authorityRevision)
  const messages = useJson<Page<{ messages: Message[] }>>(`${root}/messages?limit=40&offset=${messageOffset}`, enabled, revision)
  const children = useJson<Page<{ sessions: Session[] }>>(`${root}/children?limit=20&offset=${childOffset}`, enabled, revision)
  const files = useJson<Files>(`${root}/files?limit=20&offset=${fileOffset}`, enabled, revision)
  const recovery = useJson<{ files: Resource[] }>(`${root}/continuation/files`, enabled && historicalOnly, revision)
  const fatal = securityError || [detail.error, messages.error, children.error, files.error, recovery.error].find(denied)
  const session = fatal ? undefined : detail.data?.session
  const openedContinuation = useRef("")

  useEffect(() => {
    const continued = detail.data?.continuation
    if (!historicalOnly && enabled && !fatal && continued && onOpenSession && openedContinuation.current !== continued.id) {
      openedContinuation.current = continued.id
      onOpenSession(continued)
    }
  }, [detail.data?.continuation, enabled, fatal, historicalOnly, onOpenSession])

  useEffect(() => {
    mounted.current = true
    const changed = () => setVisible(!document.hidden)
    document.addEventListener("visibilitychange", changed)
    return () => { mounted.current = false; document.removeEventListener("visibilitychange", changed) }
  }, [])
  useEffect(() => {
    if (!active || !visible) { setSelectedFile(null); return }
    const interval = window.setInterval(() => setAuthorityRevision(value => value + 1), 30_000)
    return () => window.clearInterval(interval)
  }, [active, visible])
  useEffect(() => {
    if (fatal) { setSecurityError(fatal); setSelectedFile(null) }
  }, [fatal])
  useEffect(() => {
    if (!session || !enabled) return
    if (!historicalOnly && !focused.current) { heading.current?.focus(); focused.current = true }
    if (session.read_marker_key && session.completion_marker) {
      try { localStorage.setItem(session.read_marker_key, session.completion_marker) } catch { /* Browser storage is optional. */ }
    }
  }, [session, enabled, historicalOnly])

  function refresh() {
    setSecurityError(null); setSelectedFile(null); setActionError("")
    setRevision(value => value + 1)
  }
  async function startNew() {
    if (!onStartNewNative || startPending.current || !active || !session || fatal) return
    startPending.current = true; setStarting(true); setActionError("")
    try {
      // Reauthorize the displayed history; the callback only opens a fresh composer.
      await api(`/history/sessions/${encodeURIComponent(sessionId)}?source=legacy`)
      if (mounted.current && interactive.current) await onStartNewNative()
    } catch (reason) {
      if (mounted.current) {
        if (reason instanceof Error && denied(reason)) setSecurityError(reason)
        else setActionError(reason instanceof Error ? reason.message : "A new chat could not be opened.")
      }
    } finally {
      startPending.current = false
      if (mounted.current) setStarting(false)
    }
  }
  async function copyLink() {
    try {
      await navigator.clipboard.writeText(new URL(`/#run=${encodeURIComponent(sessionId)}`, window.location.origin).href)
      if (mounted.current) setCopyState("Link copied")
    } catch { if (mounted.current) setCopyState("Copy failed. Use the session link below.") }
  }
  function openSession(id: string) {
    onOpenSession?.({ source: "legacy", id, url: `/#run=${id}` })
  }
  const resourceRows = (resources: Resource[]) => resources.map(resource => {
    const download = productFileUrl(resource.download_url ?? resource.url, sessionId)
    return <div className="ph-file" key={`${resource.kind}:${resource.id}`}>
      <Button variant="ghost" className="ph-file-name" onClick={() => setSelectedFile(resource)} disabled={!enabled}>
        <FileText size={16} /><span><strong>{resource.name}</strong><small>{bytes(resource.size)} · {resource.media_type}</small></span>
      </Button>
      {download && <Button asChild variant="ghost" size="icon" aria-label={`Download ${resource.name}`}>
        <a href={download} download><Download size={16} /></a>
      </Button>}
    </div>
  })
  if (!active) return null
  const Heading = historicalOnly ? "h2" : "h1"
  return <section className="product-history" aria-label="Existing session history">
    <header className="ph-header">
      <div className="ph-heading"><History size={19} /><Heading ref={heading} tabIndex={-1}>{session?.title || "Existing session"}</Heading></div>
      <div className="ph-meta"><Badge variant="outline">Read-only history</Badge>
        {session && <>
          <span>{session.visibility === "private" && <LockKeyhole size={13} />}{session.visibility === "private" ? "Private" : "Shared"}</span>
          <span>{session.status}</span><span>{timestamp(session.updated_at)}</span>
          {session.archived && <Badge variant="secondary">Archived</Badge>}
          {session.folder && <span>{session.folder}</span>}
        </>}
      </div>
      <div className="ph-actions">
        <Button variant="ghost" size="sm" onClick={refresh} disabled={!visible || detail.loading}><RefreshCw size={15} />Refresh</Button>
        {session && <Button variant="ghost" size="sm" onClick={() => void copyLink()}><Copy size={15} />Copy link</Button>}
        {onStartNewNative && session && <Button variant="outline" size="sm" onClick={() => void startNew()} disabled={starting || !enabled}>
          <Plus size={15} />{starting ? "Opening…" : "Start a new chat"}
        </Button>}
        <span className="ph-muted" role="status">{copyState}</span>
      </div>
    </header>
    <div className="ph-body">
      {fatal ? <Notice error>{fatal.message}<Button variant="outline" size="sm" onClick={refresh}>Retry</Button></Notice>
        : detail.error ? <Notice error>{detail.error.message}<Button variant="outline" size="sm" onClick={refresh}>Retry</Button></Notice>
        : !session ? <p className="ph-muted" role="status">{visible ? "Loading history…" : "History will refresh when you return."}</p>
        : <>
          <p className="ph-explanation">{historicalOnly ? "Saved earlier messages and files. New work continues in the conversation below." : "This conversation uses an earlier execution engine. Its messages and files are available to read."}
            {detail.data?.continuation_supported && !historicalOnly
              ? " Continue below to move idle work to the native engine while keeping this session link."
              : onStartNewNative && " Starting a new chat opens a separate native conversation; history and files are not copied."}</p>
          {!historicalOnly && detail.data?.continuation_supported && !session.parent_session_id && onOpenSession &&
            <ContinuationComposer sessionId={sessionId} active={enabled} revision={revision}
              onContinued={onOpenSession} onDenied={setSecurityError} />}
          {actionError && <Notice error>{actionError}</Notice>}
          <div className="ph-details ph-muted">
            <span>{session.owner?.name || session.owner?.id || "Earlier author"}</span><span>{session.model}</span>
            <a href={`/#run=${sessionId}`}>{sessionId}</a>
            {session.parent_session_id && <Button variant="link" size="sm" asChild>
              <a href={`/#run=${session.parent_session_id}`} onClick={event => {
                if (onOpenSession) { event.preventDefault(); openSession(session.parent_session_id!) }
              }}>Parent conversation</a>
            </Button>}
          </div>
          <section className="ph-section" aria-label="Messages">
            <h2>Conversation</h2>
            {messages.loading ? <p className="ph-muted" role="status">Loading messages…</p>
              : messages.error ? <Notice error>{messages.error.message}<Button variant="outline" size="sm" onClick={refresh}>Retry</Button></Notice>
              : messages.data?.messages.length ? messages.data.messages.map(message => <article className={`ph-message ph-${message.role}`} key={message.id}>
                <div className="ph-message-meta"><strong>{message.role === "assistant" ? "Moyai" : message.author_name || message.author_id || "Participant"}</strong>
                  <time dateTime={message.created_at}>{timestamp(message.created_at)}</time>
                  {message.status && !["completed", "done"].includes(message.status) && <span>{message.status}</span>}
                </div>
                {message.role === "assistant" ? <Prose text={message.content} /> : <div className="ph-user-text">{message.content}</div>}
                {message.content_truncated && <Notice>This long message is truncated in the history reader.</Notice>}
                {!!message.attachments?.length && <div className="ph-files">{resourceRows(message.attachments)}</div>}
              </article>) : <p className="ph-muted">No saved public messages.</p>}
            <Pager offset={messageOffset} step={40} next={messages.data?.next_offset} loading={messages.loading} label="messages" change={setMessageOffset} />
          </section>
          {historicalOnly && <section className="ph-section" aria-label="Recovered workspace files">
            <h2>Recovered workspace files</h2>
            {recovery.loading ? <p className="ph-muted" role="status">Loading retained workspace files…</p>
              : recovery.error ? <Notice error>{recovery.error.message}</Notice>
              : recovery.data?.files.length ? <><p className="ph-muted">Verified recovery packages from the transition, including captured unsaved edits.</p>
                <div className="ph-files">{resourceRows(recovery.data.files)}</div></>
              : <p className="ph-muted">No workspace recovery package was needed or retained.</p>}
          </section>}
          <section className="ph-section" aria-label="Child conversations">
            <h2>Related conversations</h2>
            {children.error ? <Notice error>{children.error.message}</Notice>
              : children.loading ? <p className="ph-muted" role="status">Loading conversations…</p>
              : children.data?.sessions.length ? children.data.sessions.map(child => <Button className="ph-child" variant="ghost" key={child.id} asChild>
                <a href={`/#run=${child.id}`} onClick={event => { if (onOpenSession) { event.preventDefault(); openSession(child.id) } }}>
                  <MessageSquare size={16} /><span><strong>{child.title}</strong><small>{child.status}</small></span><ArrowRight size={14} />
                </a>
              </Button>) : <p className="ph-muted">No child conversations.</p>}
            <Pager offset={childOffset} step={20} next={children.data?.next_offset} loading={children.loading} label="related conversations" change={setChildOffset} />
          </section>
          <section className="ph-section" aria-label="Saved files">
            <h2>Files</h2>
            {files.error ? <Notice error>{files.error.message}<Button variant="outline" size="sm" onClick={refresh}>Retry</Button></Notice>
              : files.loading ? <p className="ph-muted" role="status">Loading files…</p>
              : files.data ? <>
                {!files.data.files.length && !files.data.sources.length && <p className="ph-muted">No saved files.</p>}
                {!!files.data.files.length && <div className="ph-files"><h3>Outputs</h3>{resourceRows(files.data.files)}</div>}
                {!!files.data.sources.length && <div className="ph-files"><h3>Attachments</h3>{resourceRows(files.data.sources)}</div>}
                {files.data.capture_inventory_truncated && <Notice>The capture inventory is limited. More files may be available in the original ZIP.</Notice>}
                {!!files.data.limitations?.length && <details className="ph-muted"><summary>File compatibility</summary><ul>{files.data.limitations.map(note => <li key={note}>{note}</li>)}</ul></details>}
              </> : null}
            <Pager offset={fileOffset} step={20} next={files.data?.next_offset} loading={files.loading} label="files" change={setFileOffset} />
          </section>
        </>}
    </div>
    <Dialog open={Boolean(selectedFile && !fatal)} onOpenChange={open => { if (!open) setSelectedFile(null) }}>
      {selectedFile && !fatal && <FilePreview key={`${sessionId}:${selectedFile.id}`} sessionId={sessionId} resource={selectedFile}
        active={enabled} onDenied={setSecurityError} />}
    </Dialog>
  </section>
}

type ContinuationPreview = {
  state: "ready" | "blocked" | "continued"; source_fingerprint?: string; continuation?: HistorySessionRef
  blockers: { code: string; message: string; action: string }[]
  history_count: number; imported_count: number; visibility: string; file_notice: string
  requires_recent_history: boolean; requires_workspace_ack: boolean; requires_workspace_drain: boolean
}
type ContinuationConnection = { id: string; name: string; kind: string; enabled: boolean }

function ContinuationComposer({ sessionId, active, revision, onContinued, onDenied }: {
  sessionId: string; active: boolean; revision: number
  onContinued: (session: HistorySessionRef) => void; onDenied: (error: Error) => void
}) {
  const [open, setOpen] = useState(false), [attempt, setAttempt] = useState(0)
  const [prompt, setPrompt] = useState(""), [model, setModel] = useState("")
  const [selected, setSelected] = useState<string[]>([])
  const [recent, setRecent] = useState(false), [omissions, setOmissions] = useState(false), [drain, setDrain] = useState(false)
  const [busy, setBusy] = useState(false), [error, setError] = useState("")
  const pending = useRef(false), mounted = useRef(true), requestKey = useRef(crypto.randomUUID())
  const endpoint = `/product/sessions/${encodeURIComponent(sessionId)}/continuation`
  const review = useJson<ContinuationPreview>(endpoint, active && open, revision + attempt)
  const me = useJson<{ models: string[]; default_model: string }>("/me", active && open, revision)
  const connections = useJson<ContinuationConnection[]>(
    review.data ? `/connections?shared=${review.data.visibility === "shared"}` : null, active && open, revision + attempt)
  const data = review.data
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  useEffect(() => {
    setRecent(false); setOmissions(false); setDrain(false)
  }, [data?.source_fingerprint])
  useEffect(() => {
    if (review.error && denied(review.error)) onDenied(review.error)
  }, [review.error, onDenied])

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!active || pending.current || !data || data.state !== "ready" || !prompt.trim()) return
    pending.current = true; setBusy(true); setError("")
    try {
      const result = await api<HistorySessionRef>(endpoint, "POST", {
        prompt, model: model || me.data?.default_model, connections: selected, request_key: requestKey.current,
        expected_source_fingerprint: data.source_fingerprint, accept_recent_history: recent,
        accept_workspace_omissions: omissions, drain_idle_workspace: drain,
      })
      if (mounted.current) onContinued(result)
    } catch (reason) {
      if (mounted.current) {
        if (reason instanceof Error && denied(reason)) onDenied(reason)
        else setError(reason instanceof Error ? reason.message : "Continuation could not be admitted.")
      }
    } finally {
      pending.current = false
      if (mounted.current) setBusy(false)
    }
  }
  if (!open) return <Button variant="outline" disabled={!active} onClick={() => setOpen(true)}>
    <ArrowRight size={16} />Continue this conversation
  </Button>
  return <section className="ph-continuation" aria-label="Continue earlier conversation">
    <h2>Continue this conversation</h2>
    {review.loading ? <p role="status" className="ph-muted">Checking earlier work and files…</p>
      : review.error ? <Notice error>{review.error.message}</Notice>
      : data?.state === "continued" && data.continuation ? <Button onClick={() => onContinued(data.continuation!)}>Open continued conversation</Button>
      : data?.state === "blocked" ? <Notice>{data.blockers.map(blocker => <p key={blocker.code}>
        {blocker.message} {blocker.action}
      </p>)}</Notice>
      : data && <form onSubmit={event => void submit(event)} className="ph-continuation-form">
        <p className="ph-muted">{data.imported_count} of {data.history_count} saved public messages will provide context.
          Earlier tool calls and checkpoints will not run again. {data.file_notice}</p>
        {data.requires_recent_history && <Label className="ph-check"><Checkbox checked={recent} disabled={busy}
          onCheckedChange={value => setRecent(value === true)} />Use the bounded recent history shown above; older messages remain available here.</Label>}
        {data.requires_workspace_ack && <Label className="ph-check"><Checkbox checked={omissions} disabled={busy}
          onCheckedChange={value => setOmissions(value === true)} />Restore visible files with the exclusions described above.</Label>}
        {data.requires_workspace_drain && <Label className="ph-check"><Checkbox checked={drain} disabled={busy}
          onCheckedChange={value => setDrain(value === true)} />Save and close the idle earlier computer before starting native work.</Label>}
        <Label htmlFor={`continuation-model-${sessionId}`}>Model</Label>
        <Select value={model || me.data?.default_model || ""} onValueChange={setModel} disabled={busy || !active}>
          <SelectTrigger id={`continuation-model-${sessionId}`}><SelectValue placeholder="Select a model" /></SelectTrigger>
          <SelectContent>{me.data?.models.map(value => <SelectItem key={value} value={value}>{value}</SelectItem>)}</SelectContent>
        </Select>
        {connections.error && <Notice error>{connections.error.message}</Notice>}
        {!!connections.data?.length && <details><summary>Connections</summary>
          {connections.data.filter(item => item.enabled).map(item => <Label className="ph-check" key={item.id}>
            <Checkbox checked={selected.includes(item.id)} disabled={busy || !active}
              onCheckedChange={value => setSelected(rows => value === true ? [...rows, item.id] : rows.filter(id => id !== item.id))} />
            {item.name}
          </Label>)}
        </details>}
        <Label htmlFor={`continuation-prompt-${sessionId}`}>New message</Label>
        <Textarea id={`continuation-prompt-${sessionId}`} value={prompt} onChange={event => setPrompt(event.target.value)}
          placeholder="What should Moyai do next?" maxLength={500000} disabled={busy || !active} rows={4} />
        {error && <Notice error>{error}</Notice>}
        <Button type="submit" disabled={busy || !active || !prompt.trim() || !me.data ||
          (data.requires_recent_history && !recent) || (data.requires_workspace_ack && !omissions) ||
          (data.requires_workspace_drain && !drain)}>
          {busy ? "Continuing…" : "Continue and send"}<ArrowRight size={16} />
        </Button>
      </form>}
    <Button variant="ghost" size="sm" disabled={busy || review.loading || !active} onClick={() => {
      setAttempt(value => value + 1); setError("")
    }}><RefreshCw size={15} />Recheck</Button>
  </section>
}

/** Use in a collapsed section above new native turns; never invent old native Turns. */
export function ProductHistoryPrefix(props: Omit<ProductHistoryProps, "historicalOnly">) {
  const [open, setOpen] = useState(false)
  return <details className="product-history-prefix" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>Earlier conversation and files</summary>
    {open && <ProductHistory {...props} historicalOnly />}
  </details>
}

function Pager({ offset, step, next, loading, label, change }: {
  offset: number; step: number; next?: number | null; loading: boolean; label: string; change: (offset: number) => void
}) {
  if (offset === 0 && next == null) return null
  return <nav className="ph-pagination" aria-label={`${label} pages`}>
    <Button variant="outline" size="sm" onClick={() => change(Math.max(0, offset - step))} disabled={loading || offset === 0}>
      <ArrowLeft size={14} />Previous
    </Button>
    <span className="ph-muted">Page {Math.floor(offset / step) + 1}</span>
    <Button variant="outline" size="sm" onClick={() => { if (next != null) change(next) }} disabled={loading || next == null}>
      Next<ArrowRight size={14} />
    </Button>
  </nav>
}

function FilePreview({ sessionId, resource, active, onDenied }: {
  sessionId: string; resource: Resource; active: boolean; onDenied: (error: Error) => void
}) {
  const [revision, setRevision] = useState(0)
  const previewUrl = productFileUrl(resource.preview_url, sessionId)
  const downloadUrl = productFileUrl(resource.download_url ?? resource.url, sessionId)
  const preview = useJson<Preview>(previewUrl?.replace(/^\/api/, "") ?? null, active && resource.preview_supported !== false, revision)
  useEffect(() => {
    if (preview.error instanceof ApiError && [401, 403].includes(preview.error.status)) onDenied(preview.error)
  }, [preview.error, onDenied])
  return <DialogContent className="product-history product-history-file-dialog">
    <DialogTitle>{resource.name}</DialogTitle>
    <DialogDescription>{bytes(resource.size)} · {resource.media_type}</DialogDescription>
    <div className="ph-actions">
      {downloadUrl && <Button variant="outline" size="sm" asChild><a href={downloadUrl} download><Download size={15} />Download original</a></Button>}
      {previewUrl && <Button variant="ghost" size="sm" disabled={preview.loading || !active} onClick={() => setRevision(value => value + 1)}><RefreshCw size={15} />Refresh</Button>}
    </div>
    {preview.loading ? <p className="ph-muted" role="status">Loading preview…</p>
      : preview.error ? <Notice error>{preview.error.message}</Notice>
      : preview.data ? <>
        {preview.data.truncated && <Notice>Partial preview. Download the original for the complete file.</Notice>}
        {preview.data.text != null ? preview.data.preview_kind === "markdown" ? <Prose text={preview.data.text} />
          : <pre className="ph-source" tabIndex={0}><code>{preview.data.text}</code></pre>
          : <p className="ph-muted">This file is available by download.</p>}
      </> : <p className="ph-muted">Download the original file to view its contents.</p>}
  </DialogContent>
}
