import React, { useCallback, useEffect, useRef, useState } from "react"
import { createRoot } from "react-dom/client"
import Markdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { ArrowUp, Archive, Check, FileDown, FolderOpen, Link2, LogOut, Menu, MessageSquare, PanelRightClose, Pin, Plus, Search, Square, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Checkbox } from "@/components/ui/checkbox"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import "@/theme.css"
import "./style.css"
import logo from "../../app/static/moyai-logo.png"
import { api, setCsrf } from "./api"
import { AttachButton, ImagePreviews, UploadedImage, useImages } from "./attachments"
import { ComputerPanel } from "./computer"
import { SessionSidebar, SessionFolder, SidebarSession } from "./sidebar"

const Home = React.lazy(() => import("./home").then(module => ({ default: module.Home })))

type Me = { user: { id: string; name: string; role: string }; csrf: string; models: string[]; default_model: string }
type Session = SidebarSession & { id: string; title: string; owner_id: string; visibility: string; surface: string; model: string; status: string; active_turn: string | null; archived: boolean; pinned?: boolean; save_warning?: string; workspace?: { id: string; provider: string } }
type Turn = { id: string; actor_id: string; status: string; connections: string[]; error?: string; usage: { requests?: number; input_tokens?: number; output_tokens?: number } }
type Message = { id: number; turn_id: string; role: string; author_id: string | null; content: string; attachments?: UploadedImage[] }
type Connection = { id: string; name: string; kind: string; owner_id: string; read_only: boolean; enabled: boolean; shared: boolean }
type File = { id: string; name: string; size: number }
type Plan = { id: string; content: string; status: string }
type Call = { tool_call_id: string; tool_name: string; args: Record<string, unknown> | string }
type Pending = { calls: Call[]; approvals: Call[] }
type Effect = { id: string; turn_id: string; tool_name: string; arguments: unknown }
type View = { session: Session; turns: Turn[]; pending?: Pending; activity?: Activity[]; effects?: Effect[]; has_unsettled_effects?: boolean }
type Activity = { type: string; name?: string; text?: string; content?: string; arguments?: unknown; data?: unknown }
type Question = { header: string; question: string; multi_select?: boolean; options: { label: string; description?: string }[] }

function Choice({ value, onChange, options, label }: { value: string; onChange: (value: string) => void; options: { value: string; label: string }[]; label: string }) {
  return <Select value={value} onValueChange={onChange}>
    <SelectTrigger aria-label={label}><SelectValue /></SelectTrigger>
    <SelectContent>{options.map(option => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}</SelectContent>
  </Select>
}

function App() {
  const [me, setMe] = useState<Me | null>(null)
  const [ready, setReady] = useState(false)
  const [error, setError] = useState("")
  const [folders, setFolders] = useState<SessionFolder[]>([])
  const refreshVersion = useRef(0)
  const [sessions, setSessions] = useState<Session[]>([])
  const [selected, setSelected] = useState(location.hash.slice(1))
  const [connections, setConnections] = useState<Connection[]>([])
  const [showConnections, setShowConnections] = useState(false)
  const [archived, setArchived] = useState(false)
  const [search, setSearch] = useState("")
  const [mobileNav, setMobileNav] = useState(false)
  const searchInput = useRef<HTMLInputElement>(null)
  const refresh = useCallback(async () => {
    const version = ++refreshVersion.current
    const [rows, accounts, groups] = await Promise.all([
      api<Session[]>(`/sessions?archived=${archived}`), api<Connection[]>("/connections"), api<SessionFolder[]>("/session-folders"),
    ])
    if (version !== refreshVersion.current) return
    setSessions(rows); setConnections(accounts); setFolders(groups)
  }, [archived])

  useEffect(() => {
    api<Me>("/me").then(value => { setCsrf(value.csrf); setMe(value) }).catch(() => {}).finally(() => setReady(true))
    const signedOut = () => { ++refreshVersion.current; setCsrf(""); setMe(null); setSessions([]); setFolders([]); setConnections([]) }
    const clearSearch = () => setSearch("")
    window.addEventListener("moyai:clear-search", clearSearch)
    const navigate = () => { setSelected(location.hash.slice(1)); setMobileNav(false) }
    const shortcut = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return
      if (!(event.metaKey || event.ctrlKey)) return
      if (event.key.toLowerCase() === "k") { event.preventDefault(); setMobileNav(true); searchInput.current?.focus() }
      if (event.shiftKey && event.key.toLowerCase() === "o") { event.preventDefault(); location.hash = "" }
    }
    window.addEventListener("moyai:signed-out", signedOut)
    window.addEventListener("hashchange", navigate)
    window.addEventListener("keydown", shortcut)
    return () => { window.removeEventListener("moyai:clear-search", clearSearch); window.removeEventListener("moyai:signed-out", signedOut); window.removeEventListener("hashchange", navigate); window.removeEventListener("keydown", shortcut) }
  }, [])
  useEffect(() => {
    if (!me) return
    let live = true
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try { await refresh() } catch (e) { if (live) setError(String(e)) }
      if (live) timer = setTimeout(poll, 4000)
    }
    void poll()
    return () => { live = false; ++refreshVersion.current; clearTimeout(timer) }
  }, [me, refresh])

  if (!ready) return <div className="center muted" role="status">Opening Moyai…</div>
  if (!me) return <Login onLogin={value => { setCsrf(value.csrf); setMe(value); setError("") }} />
  return <div className="app">
    <aside className={`sidebar ${mobileNav ? "open" : ""}`}>
      <a href="#" className="brand"><img src={logo} alt="" /><span>Moyai<small>LiteLLM</small></span></a>
      <Button variant="secondary" className="new-chat" onClick={() => { location.hash = "" }}><Plus />New session<span className="key-hint">⌘⇧O</span></Button>
      <div className="rail-search"><Search size={16} /><Input ref={searchInput} aria-label="Find conversations" placeholder="Search sessions…" value={search} onChange={e => setSearch(e.target.value)} /><span className="key-hint">⌘K</span></div>
      <Button variant="ghost" className="rail-button" onClick={() => setShowConnections(true)}><Link2 />Connections</Button>
      <SessionSidebar key={me.user.id} sessions={sessions} folders={folders} selected={selected} userId={me.user.id} search={search} archived={archived} refresh={refresh} onArchiveView={() => { ++refreshVersion.current; setArchived(!archived) }} />
      <div className="account"><span>{me.user.name}</span><Button variant="ghost" size="icon" aria-label="Sign out" onClick={async () => { await api("/logout", "POST"); window.dispatchEvent(new Event("moyai:signed-out")) }}><LogOut /></Button></div>
    </aside>
    <main className="main">
      <Button className="mobile-menu" variant="outline" size="icon" aria-label="Toggle conversations" onClick={() => setMobileNav(!mobileNav)}>{mobileNav ? <X /> : <Menu />}</Button>
      {error && <div className="error global-error" role="alert">{error}<Button variant="ghost" onClick={() => setError("")}>Dismiss</Button></div>}
      {selected
        ? <Conversation key={`${me.user.id}:${selected}`} id={selected} me={me} connections={connections} refreshList={refresh} />
        : <React.Suspense fallback={<div className="center muted">Opening workspace…</div>}><Home models={me.models} defaultModel={me.default_model} onCreated={refresh} /></React.Suspense>}
    </main>
    <Connections open={showConnections} onOpenChange={setShowConnections} me={me} connections={connections} refresh={refresh} />
  </div>
}

function Login({ onLogin }: { onLogin: (me: Me) => void }) {
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  return <main className="center"><form className="login" onSubmit={async e => {
    e.preventDefault(); setBusy(true); setError("")
    const form = new FormData(e.currentTarget)
    try {
      const result = await api<{ csrf: string }>("/login", "POST", Object.fromEntries(form))
      setCsrf(result.csrf); onLogin(await api<Me>("/me"))
    } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }}>
    <img src={logo} alt="" /><h1>Welcome to Moyai</h1><p className="muted">Sign in to your workspace.</p>
    <label>Username<Input name="username" autoComplete="username" required autoFocus /></label>
    <label>Password<Input name="password" type="password" autoComplete="current-password" required /></label>
    {error && <p className="error" role="alert">{error}</p>}
    <Button type="submit" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</Button>
  </form></main>
}

function Conversation({ id, me, connections, refreshList }: { id: string; me: Me; connections: Connection[]; refreshList: () => Promise<void> }) {
  const [view, setView] = useState<View | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [files, setFiles] = useState<File[]>([])
  const [plan, setPlan] = useState<Plan[]>([])
  const [draft, setDraft] = useState("")
  const [model, setModel] = useState(me.default_model)
  const [selectedAccounts, setSelectedAccounts] = useState<string[]>([])
  const [mode, setMode] = useState("when_idle")
  const [error, setError] = useState("")
  const pictures = useImages(setError)
  const [busy, setBusy] = useState(false)
  const [activity, setActivity] = useState<Activity[]>([])
  const [panel, setPanel] = useState("computer")
  const [panelOpen, setPanelOpen] = useState(true)
  const [computerExpanded, setComputerExpanded] = useState(false)
  const [editing, setEditing] = useState<Message | null>(null)
  const [editText, setEditText] = useState("")
  const composerInput = useRef<HTMLTextAreaElement>(null)
  const [renaming, setRenaming] = useState(false)
  const [title, setTitle] = useState("")
  const heading = useRef<HTMLHeadingElement>(null)
  const transcript = useRef<HTMLDivElement>(null)
  const lastRequest = useRef<{ payload: string; key: string } | null>(null)
  const loaded = useRef(false)
  const readThrough = useRef<number | null>(null)
  const load = useCallback(async (signal?: AbortSignal) => {
    const [next, history, outputs, tasks] = await Promise.all([
      api<View>(`/sessions/${id}`, "GET", undefined, signal),
      api<Message[]>(`/sessions/${id}/messages`, "GET", undefined, signal),
      api<File[]>(`/sessions/${id}/files`, "GET", undefined, signal),
      api<Plan[]>(`/sessions/${id}/plan`, "GET", undefined, signal),
    ])
    if (signal?.aborted) return
    setView(next); setMessages(history); setFiles(outputs); setPlan(tasks)
    const latestMessage = history.at(-1)?.id ?? 0
    if (readThrough.current !== latestMessage) {
      await api(`/sessions/${id}/read`, "POST", { message_id: latestMessage }, signal)
      if (signal?.aborted) return
      readThrough.current = latestMessage
    }
    if (next.activity) setActivity(next.activity)
    if (!loaded.current) {
      setModel(next.session.model)
      const latest = next.turns[0]
      if (latest?.actor_id === me.user.id) setSelectedAccounts(latest.connections ?? [])
      loaded.current = true
    }
  }, [id, me.user.id])
  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try { await load(controller.signal) } catch (e) { if (!controller.signal.aborted) setError(String(e)) }
      if (!controller.signal.aborted) timer = setTimeout(poll, 1500)
    }
    void poll()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [load])
  useEffect(() => {
    const openComputer = (event: Event) => {
      if ((event as CustomEvent<string>).detail === id) { setPanel("computer"); setPanelOpen(true) }
    }
    window.addEventListener("moyai:open-computer", openComputer)
    return () => window.removeEventListener("moyai:open-computer", openComputer)
  }, [id])
  useEffect(() => { heading.current?.focus() }, [Boolean(view)])
  useEffect(() => { document.title = `${view?.session.title ?? "Conversation"} · Moyai` }, [view?.session.title])
  useEffect(() => {
    const element = transcript.current
    if (element) element.scrollTop = element.scrollHeight
  }, [messages.length])
  const active = view?.turns.find(turn => turn.id === view.session.active_turn)
  const activeId = active?.status === "running" && active.actor_id === me.user.id ? active.id : null
  useEffect(() => {
    if (!activeId) return
    setActivity([])
    const stream = new EventSource(`/api/sessions/${id}/events?turn_id=${activeId}`)
    stream.onmessage = event => {
      const value: Activity = JSON.parse(event.data)
      setActivity(previous => {
        if (value.type === "text_delta" && previous.at(-1)?.type === "text_start") {
          return [...previous.slice(0, -1), { ...previous.at(-1)!, text: (previous.at(-1)!.text ?? "") + value.text }]
        }
        return [...previous.slice(-79), value.type === "text_delta" ? { ...value, type: "text_start" } : value]
      })
    }
    stream.addEventListener("finished", () => { stream.close(); void load() })
    return () => stream.close()
  }, [activeId, id, load])

  async function send() {
    if (editing) {
      if (busy || !editText.trim()) return
      setBusy(true); setError("")
      try { await api(`/turns/${editing.turn_id}`, "PATCH", { prompt: editText }); setEditing(null); await load() }
      catch (error) { setError(String(error)) } finally { setBusy(false) }
      return
    }
    if ((!draft.trim() && !pictures.images.length) || busy || pictures.uploading) return
    setBusy(true); setError("")
    const body = { prompt: draft, model, mode, connections: selectedAccounts, attachments: pictures.images.map(image => image.id) }
    const serialized = JSON.stringify(body)
    if (lastRequest.current?.payload !== serialized) lastRequest.current = { payload: serialized, key: crypto.randomUUID() }
    try {
      await api(`/sessions/${id}/messages`, "POST", { ...body, request_key: lastRequest.current.key })
      setDraft(""); pictures.clear(); lastRequest.current = null; await load(); await refreshList()
    } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }
  async function editSession(body: object) {
    try { await api(`/sessions/${id}`, "PATCH", body); await load(); await refreshList() }
    catch (e) { setError(String(e)) }
  }
  if (!view) return <div className="center" role="status">{error || "Opening conversation…"}</div>
  const session = view.session
  const queued = messages.filter(message => message.author_id === me.user.id && view.turns.some(turn => turn.id === message.turn_id && turn.status === "queued"))
  return <div className="conversation">
    <header className="topbar">
      <div><h1 ref={heading} tabIndex={-1}>{session.title}</h1><span className={`status ${session.status}`}>{session.status === "waiting" ? "Waiting for your response" : session.status}</span><span className="muted privacy">{session.surface === "slack" ? "Replies are delivered to Slack" : session.visibility === "private" ? "Private conversation" : "Shared conversation"}</span></div>
      <div className="header-actions">
        {session.owner_id === me.user.id && <Button variant="ghost" size="sm" onClick={() => { setTitle(session.title); setRenaming(true) }}>Rename</Button>}
        <Button variant="ghost" size="icon" aria-label={session.pinned ? "Unpin conversation" : "Pin conversation"} aria-pressed={Boolean(session.pinned)} onClick={() => editSession({ pinned: !session.pinned })}><Pin /></Button>
        {session.owner_id === me.user.id && <Button variant="ghost" size="icon" aria-label={session.archived ? "Restore conversation" : "Archive conversation"} onClick={() => editSession({ archived: !session.archived })}><Archive /></Button>}
        <Button variant="ghost" size="icon" aria-label={panelOpen ? "Hide workspace panel" : "Show workspace panel"} onClick={() => setPanelOpen(!panelOpen)}><PanelRightClose /></Button>
      </div>
    </header>
    <div className={`workspace ${panelOpen ? "with-panel" : "panel-closed"} ${computerExpanded && panel === "computer" ? "computer-expanded" : ""}`}>
      <section className="chat">
        <div className="transcript" ref={transcript}>
          {!messages.length && <div className="empty-chat"><img src={logo} alt="" /><h2>Start with a task or a question.</h2><p className="muted">Your work and saved outputs stay with this conversation.</p></div>}
          {messages.filter(message => !queued.some(item => item.id === message.id)).map(message => {
            const turn = view.turns.find(item => item.id === message.turn_id)
            return <article key={message.id} className={`message ${message.role}`}>
              <div className="message-author">{message.role === "assistant" ? "Moyai" : message.author_id === me.user.id ? "You" : "Teammate"}
                {message.role === "user" && turn?.status === "queued" && <span className="status">Queued</span>}
                {message.role === "user" && turn?.status === "stopped" && <span className="muted">Stopped</span>}
              </div>
              <div className="message-content">{message.role === "assistant" ? <Markdown remarkPlugins={[remarkGfm]}>{message.content}</Markdown> : message.content}</div>
              <ImagePreviews images={message.attachments ?? []} />

            </article>
          })}
          {session.status === "running" && <p className="working" role="status"><span className="running-dot" />Moyai is working. You can send a correction or stop the task.</p>}
          {view.pending && active?.actor_id === me.user.id && <PendingRequests key={active.id} pending={view.pending} turnId={active.id} sessionId={id} onDone={load} />}
          {view.turns[0]?.error && <p className="error" role="alert">{view.turns[0].error}</p>}
          {!session.active_turn && view.effects?.map(effect => <EffectReview key={effect.id} effect={effect} onDone={load} />)}
          {!session.active_turn && view.has_unsettled_effects && !view.effects?.length && <p className="notice">An earlier action needs review by its requester before more changes can be made.</p>}
          {session.save_warning && <p className="notice" role="status">{session.save_warning}</p>}
        </div>
        <form className="composer-wrap" onSubmit={e => { e.preventDefault(); void send() }}>
          {error && <div className="error" role="alert">{error}<Button size="sm" variant="ghost" onClick={() => setError("")}>Dismiss</Button></div>}
          {queued.length > 0 && <div className="queued-messages" aria-label="Queued messages">{queued.map(message => <div className="queued-message" key={message.id}>
            <span><small>{session.active_turn ? "Queued after the current task" : "Starting shortly…"}</small><span>{message.content || "Image message"}</span></span>
            <Button type="button" size="sm" variant="ghost" disabled={busy || Boolean(editing)} onClick={async () => { try { await api(`/turns/${message.turn_id}/steer`, "POST"); await load() } catch (error) { setError(String(error)) } }}>{session.active_turn ? "Steer now" : "Send now"}</Button>
            <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => { setEditing(message); setEditText(message.content); requestAnimationFrame(() => composerInput.current?.focus()) }}>Edit</Button>
            <Button type="button" size="icon-sm" variant="ghost" aria-label="Cancel queued message" disabled={busy} onClick={async () => { try { await api(`/turns/${message.turn_id}`, "DELETE"); if (editing?.id === message.id) setEditing(null); await load() } catch (error) { setError(String(error)) } }}><X size={14} /></Button>
          </div>)}</div>}
          {editing && <div className="editing-message" role="status"><span>Editing queued message · attachments are kept</span><Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => setEditing(null)}>Cancel edit</Button></div>}
          <div className="composer">
            <ImagePreviews images={editing ? editing.attachments ?? [] : pictures.images} remove={editing ? undefined : pictures.remove} />
            <Textarea ref={composerInput} aria-label="Message Moyai" placeholder="Describe the work…" value={editing ? editText : draft} onChange={e => editing ? setEditText(e.target.value) : setDraft(e.target.value)} onPaste={editing ? undefined : pictures.paste} onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send() } }} />
            <div className="composer-actions">
              <AttachButton add={pictures.add} disabled={Boolean(editing) || pictures.uploading || busy} />
              <Choice value={model} onChange={setModel} label="Model" options={me.models.map(item => ({ value: item, label: item }))} />
              <div className="send-actions">
                {session.active_turn && <><Choice value={mode} onChange={setMode} label="Delivery timing" options={[{ value: "when_idle", label: "Queue next" }, { value: "asap", label: "Send now" }]} /><Button type="button" variant="outline" aria-label="Stop task" onClick={async () => { try { await api(`/sessions/${id}/stop`, "POST"); await load() } catch (e) { setError(String(e)) } }}><Square size={13} />Stop</Button></>}
                <Button type="submit" size={editing ? "sm" : "icon"} aria-label={editing ? "Save queued message" : "Send message"} disabled={(editing ? !editText.trim() : !draft.trim() && !pictures.images.length) || busy || pictures.uploading || (!editing && session.status === "waiting")}>{editing ? "Save message" : <ArrowUp />}</Button>
              </div>
            </div>
          </div>
          {session.surface !== "slack" && connections.some(item => item.enabled) && <details className="account-selection"><summary>Context & connections{selectedAccounts.length ? ` · ${selectedAccounts.length} selected` : ""}</summary><div className="connection-options">{connections.filter(item => item.enabled && (session.visibility === "private" || item.shared)).map(item => <label key={item.id}><Checkbox checked={selectedAccounts.includes(item.id)} onCheckedChange={checked => setSelectedAccounts(values => checked ? [...values, item.id] : values.filter(value => value !== item.id))} />{item.name}<span className="muted">{item.read_only ? "Read only" : "Can make changes"}</span></label>)}</div></details>}
          <p className="composer-hint">{pictures.uploading ? "Uploading images…" : "Paste images · Enter to send · Shift+Enter for a new line"}</p>
        </form>
      </section>
      {panelOpen && <aside className="details-panel" aria-label="Conversation details">
        <div className="panel-tabs">{["computer", "activity", "plan", "files"].map(tab => <Button key={tab} variant={panel === tab ? "secondary" : "ghost"} size="sm" aria-pressed={panel === tab} onClick={() => setPanel(tab)}>{tab === "files" ? `Files${files.length ? ` (${files.length})` : ""}` : tab === "plan" ? "Plan" : tab === "computer" ? "Computer" : "Activity"}</Button>)}</div>
        <div className="panel-body">
          {panel === "computer" && <ComputerPanel id={id} shared={session.visibility === "shared"} hasWorkspace={Boolean(session.workspace)} expanded={computerExpanded} onExpand={() => setComputerExpanded(!computerExpanded)} />}
          {panel === "activity" && <>{!activity.length && <div className="panel-empty"><MessageSquare /><p>{session.status === "running" ? "Connecting to live activity…" : "Live steps appear here while Moyai works."}</p></div>}{activity.map((item, index) => <ActivityRow key={index} item={item} />)}{session.workspace && <div className="computer-info"><span className="muted">Conversation computer</span><p>Attached</p><code>{session.workspace.id}</code></div>}</>}
          {panel === "plan" && <>{!plan.length && <div className="panel-empty"><Check /><p>Moyai’s task plan will appear here.</p></div>}{plan.map(task => <div className="plan-row" key={task.id}><span className={`plan-mark ${task.status}`}>{task.status === "completed" ? "✓" : "○"}</span><div>{task.content}<small>{task.status.replaceAll("_", " ")}</small></div></div>)}</>}
          {panel === "files" && <>{!files.length && <div className="panel-empty"><FolderOpen /><p>Finished files appear here when Moyai saves them for you.</p></div>}{files.map(file => <a className="file-row" key={file.id} href={`/api/files/${file.id}`} download><FileDown size={18} /><span>{file.name}<small>{(file.size / 1024).toFixed(1)} KB</small></span></a>)}</>}
        </div>
      </aside>}
    </div>
    <Dialog open={renaming} onOpenChange={setRenaming}><DialogContent><DialogHeader><DialogTitle>Rename conversation</DialogTitle><DialogDescription>Choose a title you can find later.</DialogDescription></DialogHeader><Input aria-label="Conversation title" value={title} onChange={e => setTitle(e.target.value)} /><Button disabled={!title.trim()} onClick={async () => { await editSession({ title }); setRenaming(false) }}>Save title</Button></DialogContent></Dialog>
  </div>
}

function ActivityRow({ item }: { item: Activity }) {
  if (item.type === "agent_finished") return null
  if (item.type === "text_start") return <div className="activity-text">{item.text}</div>
  return <details className="activity-row"><summary>{item.type === "tool_result" ? "Finished" : item.type === "tool_call" ? "Using" : "Updated"} {item.name?.replaceAll("_", " ")}</summary><pre>{typeof item.content === "string" ? item.content : JSON.stringify(item.arguments ?? item.data ?? {}, null, 2)}</pre></details>
}

function EffectReview({ effect, onDone }: { effect: Effect; onDone: () => Promise<void> }) {
  const [outcome, setOutcome] = useState("completed")
  const [evidence, setEvidence] = useState("")
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  return <form className="pending" onSubmit={async event => {
    event.preventDefault(); setBusy(true); setError("")
    try { await api(`/effects/${effect.id}/resolve`, "POST", { outcome, evidence }); await onDone() }
    catch (error) { setError(String(error)) } finally { setBusy(false) }
  }}>
    <h3>An action needs review</h3><p>Its outcome was not confirmed. Inspect the command or destination before allowing more changes.</p>
    <details><summary>{effect.tool_name.replaceAll("_", " ")}</summary><pre>{JSON.stringify(effect.arguments, null, 2)}</pre></details>
    <label>What did you verify?<Choice label="Verified outcome" value={outcome} onChange={setOutcome} options={[{ value: "completed", label: "The action completed" }, { value: "not_executed", label: "The action did not run" }]} /></label>
    <label>Evidence<Textarea value={evidence} onChange={event => setEvidence(event.target.value)} placeholder="Describe what you checked and what you found." minLength={10} required /></label>
    {error && <p className="error" role="alert">{error}</p>}<Button disabled={busy || evidence.trim().length < 10} type="submit">Record verified outcome</Button>
  </form>
}

function PendingRequests({ pending, turnId, sessionId, onDone }: { pending: Pending; turnId: string; sessionId: string; onDone: () => Promise<void> }) {
  const [values, setValues] = useState<Record<string, string[]>>({})
  const [custom, setCustom] = useState<Record<string, string>>({})
  const [approvals, setApprovals] = useState<Record<string, boolean>>({})
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  const requestKey = useRef(crypto.randomUUID())
  const questions = pending.calls.map(call => ({ call, questions: (typeof call.args === "string" ? JSON.parse(call.args) : call.args).questions as Question[] }))
  return <form className="pending" onSubmit={async e => {
    e.preventDefault(); setBusy(true); setError("")
    const answers = Object.fromEntries(questions.map(({ call, questions }) => [call.tool_call_id, { answers: questions.map(question => {
      const key = `${call.tool_call_id}:${question.header}`
      return { header: question.header, selected: custom[key]?.trim() ? [] : values[key] ?? [], custom_answer: custom[key]?.trim() || null }
    }) }]))
    try { await api(`/sessions/${sessionId}/answers`, "POST", { turn_id: turnId, request_key: requestKey.current, approvals, answers }); await onDone() }
    catch (e) { setError(String(e)) } finally { setBusy(false) }
  }}>
    <h3>Your input is needed</h3>
    {questions.map(({ call, questions }) => questions.map(question => {
      const key = `${call.tool_call_id}:${question.header}`
      return <fieldset key={key}><legend>{question.question}</legend><div className="question-options">{question.options.map(option => <Button type="button" key={option.label} variant={(values[key] ?? []).includes(option.label) && !custom[key] ? "secondary" : "outline"} aria-pressed={(values[key] ?? []).includes(option.label) && !custom[key]} onClick={() => { setCustom(previous => ({ ...previous, [key]: "" })); setValues(previous => ({ ...previous, [key]: question.multi_select ? (previous[key] ?? []).includes(option.label) ? previous[key].filter(item => item !== option.label) : [...previous[key] ?? [], option.label] : [option.label] })) }} title={option.description}>{option.label}</Button>)}</div><Input aria-label={`Custom answer for ${question.header}`} placeholder="Or write your own answer…" value={custom[key] ?? ""} onChange={e => setCustom(previous => ({ ...previous, [key]: e.target.value }))} /></fieldset>
    }))}
    {pending.approvals.map(call => <fieldset key={call.tool_call_id}><legend>Allow {call.tool_name.replaceAll("_", " ")}?</legend><pre>{JSON.stringify(typeof call.args === "string" ? JSON.parse(call.args) : call.args, null, 2)}</pre><div className="question-options">{[true, false].map(value => <Button key={String(value)} type="button" variant={approvals[call.tool_call_id] === value ? "secondary" : "outline"} onClick={() => setApprovals(previous => ({ ...previous, [call.tool_call_id]: value }))}>{value ? "Allow this action" : "Decline"}</Button>)}</div></fieldset>)}
    {error && <p className="error" role="alert">{error}</p>}<Button type="submit" disabled={busy}>{busy ? "Submitting…" : "Continue"}</Button>
  </form>
}

function Connections({ open, onOpenChange, me, connections, refresh }: { open: boolean; onOpenChange: (open: boolean) => void; me: Me; connections: Connection[]; refresh: () => Promise<void> }) {
  const [kind, setKind] = useState("github")
  const [readOnly, setReadOnly] = useState(true)
  const [shared, setShared] = useState(false)
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  const form = useRef<HTMLFormElement>(null)
  useEffect(() => { if (!open) { form.current?.reset(); setError("") } }, [open])
  return <Dialog open={open} onOpenChange={onOpenChange}><DialogContent className="connections-dialog"><DialogHeader><DialogTitle>Connections</DialogTitle><DialogDescription>Choose the accounts Moyai can use. Select them for each message in Context & connections.</DialogDescription></DialogHeader>
    <div className="saved-connections">{connections.map(connection => <div className="connection-row" key={connection.id}><Link2 size={18} /><div><strong>{connection.name}</strong><small>{connection.kind} · {connection.enabled ? connection.read_only ? "Read only" : "Changes require approval" : "Disabled"}</small></div>{connection.owner_id === me.user.id && connection.enabled && <Button variant="ghost" size="sm" onClick={async () => { try { await api(`/connections/${connection.id}`, "PUT", { ...connection, credential: "", enabled: false }); await refresh() } catch (e) { setError(String(e)) } }}>Disconnect</Button>}</div>)}</div>
    <form ref={form} className="connection-form" onSubmit={async e => {
      e.preventDefault(); setBusy(true); setError("")
      const data = new FormData(e.currentTarget)
      try { await api("/connections", "POST", { ...Object.fromEntries(data), kind, read_only: readOnly, shared }); form.current?.reset(); await refresh() }
      catch (e) { setError(String(e)) } finally { setBusy(false) }
    }}>
      <h3>Add an account</h3><Choice label="Service" value={kind} onChange={setKind} options={["github", "linear", "notion", "slack"].map(value => ({ value, label: ({ github: "GitHub", linear: "Linear", notion: "Notion", slack: "Slack" })[value]! }))} />
      <label>Account name<Input name="name" required placeholder="My account" /></label>
      <label>{kind === "slack" ? "Slack user token" : kind === "notion" ? "Notion OAuth access token" : "Access token"}<Input type="password" name="credential" autoComplete="off" required /></label>
      <p className="muted">{kind === "slack" ? "Use a user token. Bot tokens do not connect to Slack’s agent tools." : kind === "notion" ? "Use an OAuth access token for Notion’s hosted tools. Integration tokens are not supported." : "Your token’s permissions determine which resources this account can reach."}</p>
      <label className="check-label"><Checkbox checked={readOnly} onCheckedChange={value => setReadOnly(value === true)} />Read only</label>
      {me.user.role === "admin" && <label className="check-label"><Checkbox checked={shared} onCheckedChange={value => setShared(value === true)} />Share with workspace members</label>}
      {error && <p className="error" role="alert">{error}</p>}<Button type="submit" disabled={busy}>{busy ? "Saving…" : "Save connection"}</Button>
    </form>
  </DialogContent></Dialog>
}

createRoot(document.getElementById("root")!).render(<App />)
