import React, { useCallback, useEffect, useId, useLayoutEffect, useReducer, useRef, useState } from "react"
import { createRoot } from "react-dom/client"
import Markdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { ArrowUp, FileText, Check, Clock, ExternalLink, Link2, LockKeyhole, LogOut, Menu, MessageCircleQuestion, MessageSquare, PanelRight, Plus, Search, Settings, Square, SquarePen, Users, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { SkillComposer } from "./skill-composer"
import { SkillMessage } from "./skill-content"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import "@/theme.css"
import "./style.css"
import "./product-shell.css"
import { api, ApiError, csrfToken, setCsrf } from "./api"
import { AttachButton, AttachmentPreviews, useAttachments, useFileDrop, FileDropOverlay } from "./attachments"
import { Home } from "./home"
import type { GeneralPreferences } from "./general-preferences"
import { MoyaiBrand, SidebarToggle } from "./brand"
import { SpaceBackdrop } from "./space-backdrop"
import { activeSession, pollVisible } from "./polling"
import { SessionSidebar, SessionFolder, SidebarSession, SessionListView } from "./sidebar"
import { SessionActionsProvider, SessionMenu, sessionIsReadOnly, sessionKey, useSessionActions } from "./session-actions"
import { SessionTitle } from "./session-title"
import { WorkspaceOverview, SubagentPanel, WorkspaceResourceProvider, WorkspaceMenuKey } from "./workspace"
import { SwarmPanel } from "./swarm"
import { PullRequestPanel } from "./pull-request"
import { FilesPanel, OutputPanel } from "./output"
import { PanelTabs, emptyPanelState, panelReducer, type AddTabKind, type PanelAction } from "./panel-tabs"
import type { ActionSelection, OutputResource, WorkspaceTab } from "./workspace-types"
import { CommandPalette, COMMAND_PALETTE_ID, type PaletteCurrentSession, type PaletteSearchAdapter, type PaletteSession } from "./command-palette"
import { applyLiveEvent, emptyLiveTranscript, liveFinalAnswer, mergeActivity, type LiveEvent } from "./live-transcript"
import { TurnProgress, toolLabel } from "./turn-progress"
import { ActionPanel } from "./action-panel"
import { useWorkProgress } from "./use-work-progress"
import { orderedWorkMessages, workSegments } from "./work-timeline"
import { PanelRatioProvider, PanelResizeHandle } from "./panel-resize"
import { WorkspaceOverlay, WorkspaceOverlayContent, WorkspaceOverlayTrigger } from "./workspace-overlay"
import { previewFileLink, type OpenFile } from "./file-links"
import { firstMessage, waitsBehindActive, type Session, type Turn, type Message, type NewConversation } from "./conversation-state"
import { ProductCredentialRequest } from "./product-credentials"
import { QuestionChoices, type Question } from "./question-choices"
import { BrowserLoginCard, type BrowserLoginMetadata, type LoginOutcome } from "./browser-login"

const ComputerPanel = React.lazy(() => import("./computer").then(module => ({ default: module.ComputerPanel })))
const SettingsPage = React.lazy(() => import("./settings").then(module => ({ default: module.SettingsPage })))
const ProductHistory = React.lazy(() => import("./product-history").then(module => ({ default: module.ProductHistory })))
const ProductHistoryPrefix = React.lazy(() => import("./product-history").then(module => ({ default: module.ProductHistoryPrefix })))

type PRSummary = NonNullable<SidebarSession["pull_requests"]>[number]
type WorkspaceRequest = { sessionId: string; sequence: number; tab: "computer" | "pull-request" | "side-chat"; url?: string; child?: { id: string; title: string } }

type Me = { user: { id: string; name: string; role: string; individual?: boolean }; csrf: string; models: string[]; default_model: string; product_mode?: "standalone" | "existing"; preferences?: GeneralPreferences }
type SessionInventory = { sessions: Session[]; ancestors?: Session[]; has_more: boolean; next_cursor?: string | null; notice?: string | null }
type Connection = { id: string; name: string; kind: string; owner_id: string; read_only: boolean; enabled: boolean; shared: boolean; revision?: number }
type Plan = { id: string; content: string; status: string }
type Call = { tool_call_id: string; tool_name: string; args: Record<string, unknown> | string }
type Pending = { calls: Call[]; approvals: Call[]; metadata?: Record<string, BrowserLoginMetadata & { request_id?: string }> }
type Effect = { id: string; turn_id: string; tool_name: string; arguments: unknown }
type View = { session: Session; turns: Turn[]; messages?: Message[]; pending?: Pending; can_answer?: boolean; can_reply_with_text?: boolean; question_deadline?: string; credential_requests?: { request_id: string }[]; activity?: Activity[]; effects?: Effect[]; has_unsettled_effects?: boolean }
type Activity = LiveEvent
const settingsDestinations = [
  { page: "general", label: "General" },
  { page: "skills", label: "Skills" }, { page: "automations", label: "Automations" }, { page: "memory", label: "Memory" },
  { page: "connections", label: "Connections" }, { page: "secrets", label: "Secrets" },
  { page: "personal-slack", label: "Personal Slack", productOnly: true },
  { page: "users", label: "Users", adminOnly: true }, { page: "performance", label: "Performance", adminOnly: true },
  { page: "usage", label: "Spend & usage", adminOnly: true },
]
function currentRoute() {
  const hash = location.hash.slice(1)
  if (hash === "tasks") return ""
  if (hash === "spend") return "settings/usage"
  if (settingsDestinations.some(item => item.page === hash)) return `settings/${hash}`
  return hash.startsWith("run=") || hash.startsWith("settings/") ? hash : decodeRoute(hash)
}
function decodeRoute(value: string) { try { return decodeURIComponent(value) } catch { return value } }
const searchSessions: PaletteSearchAdapter = (query, { signal, limit }) => api(
  query ? `/history/search?source=all&limit=${limit}&q=${encodeURIComponent(query)}` : `/history/sessions?source=all&limit=${limit}`,
  "GET", undefined, signal,
)
type SessionReference = Pick<PaletteSession, "id" | "source" | "url"> & { source_session_id?: string }
export function sessionReferenceHash(session: SessionReference): string {
  const original = session.url?.match(/^\/#run=([a-f0-9]{32})$/)?.[1]
  return original ? `run=${original}` : session.source === "legacy" ? `run=${encodeURIComponent(session.id)}` : encodeURIComponent(session.id)
}
export function continuationReference(session: SessionReference, original: string): SessionReference | null {
  return /^[a-f0-9]{32}$/.test(original) && session.source === "native"
    && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(session.id)
    && session.source_session_id === original && session.url === `/#run=${original}` ? session : null
}
function openSessionReference(session: SessionReference) {
  location.hash = sessionReferenceHash(session)
}

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
  const sessionsNow = useRef(sessions)
  sessionsNow.current = sessions
  const [sessionPageCount, setSessionPageCount] = useState(1)
  const [inventoryState, setInventoryState] = useState({ hasMore: false, loading: false, notice: "" })
  const [selected, setSelected] = useState(currentRoute)
  const [workspaceRequest, setWorkspaceRequest] = useState<WorkspaceRequest | null>(null)
  const [prStates, setPrStates] = useState<Record<string, PRSummary[]>>({})
  const [prRefresh, setPrRefresh] = useState(0)
  const [connections, setConnections] = useState<Connection[]>([])
  const [listView, setListView] = useState<SessionListView>("active")
  const listViewRef = useRef(listView)
  listViewRef.current = listView
  const [mobileNav, setMobileNav] = useState(false)
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [sidebarHidden, setSidebarHidden] = useState(false)
  const [currentSession, setCurrentSession] = useState<Session | null>(null)
  const [initialConversation, setInitialConversation] = useState<{ scope: string; value: NewConversation } | null>(null)
  const [routeRevision, setRouteRevision] = useState(0)
  const [continued, setContinued] = useState<{ scope: string; original: string; id: string } | null>(null)
  const continuedNow = useRef(continued)
  continuedNow.current = continued
  const [nativeRoute, setNativeRoute] = useState<{ key: string; ready?: boolean; error?: string; view?: View } | null>(null)
  const lastConversation = useRef("")
  const settingsOpen = selected === "settings" || selected.startsWith("settings/")
  const historical = selected.startsWith("run=")
  const scopeKey = `${me?.user.id}:${me?.user.role}:${me?.user.individual}:${me?.csrf}`
  const routeScope = `${scopeKey}:${routeRevision}`, routeKey = `${routeScope}:${selected}`
  const originalId = historical ? decodeRoute(selected.slice(4)) : ""
  const acceptedStart = (initialConversation?.scope === scopeKey && initialConversation.value.session.id === selected)
    || currentSession?.id === selected
  const nativeSelected = !selected || settingsOpen ? "" : historical
    ? continued?.scope === routeScope && continued.original === originalId ? continued.id : ""
    : me?.product_mode !== "existing" || acceptedStart || (nativeRoute?.key === routeKey && nativeRoute.ready) ? selected : ""
  const routeError = nativeRoute?.key === routeKey ? nativeRoute.error : undefined
  useEffect(() => {
    if (!me || me.product_mode !== "existing" || !selected || settingsOpen || historical) return
    const controller = new AbortController(), key = routeKey
    // A native UUID can itself be an alias. Only the authenticated read adapter
    // may supply that mapping; ordinary session reads still enforce native ACLs.
    void api<View & { reference: SessionReference }>(`/sessions/${encodeURIComponent(selected)}?resolve=true&include_activity=false&include_messages=true`, "GET", undefined, controller.signal).then(value => {
      const session = value.reference
      if (controller.signal.aborted || currentRoute() !== selected) return
      if (session.id !== selected || session.source !== "native") throw new Error("The session reference changed. Reopen the conversation.")
      if (session.source_session_id) {
        const reference = continuationReference(session, session.source_session_id)
        if (!reference) throw new Error("The saved continuation reference is invalid.")
        const original = session.source_session_id
        setContinued({ scope: routeScope, original, id: reference.id })
        history.replaceState(null, "", `#run=${original}`)
        setSelected(`run=${original}`)
      } else setNativeRoute({ key, ready: true, view: value })
    }).catch(reason => {
      if (!controller.signal.aborted && currentRoute() === selected) {
        setCurrentSession(null); setInitialConversation(null); setContinued(null)
        setNativeRoute({ key, error: reason instanceof Error ? reason.message : "The conversation could not be authorized." })
      }
    })
    return () => controller.abort()
  }, [me?.product_mode, selected, settingsOpen, historical, routeKey, routeScope])
  function openHistorySession(reference: SessionReference) {
    if (reference.source !== "native") { openSessionReference(reference); return }
    if (!historical || currentRoute() !== selected) return
    const authorized = continuationReference(reference, originalId)
    if (!authorized) { setError("The saved continuation reference is invalid. Refresh this history."); return }
    setContinued({ scope: routeScope, original: originalId, id: authorized.id })
    void refresh().catch(reason => setError(String(reason)))
  }
  useEffect(() => { if (selected && !settingsOpen) lastConversation.current = selected }, [selected, settingsOpen])
  const refresh = useCallback(async (signal?: AbortSignal) => {
    const version = ++refreshVersion.current
    setInventoryState(previous => ({ ...previous, loading: true }))
    const inventory = async () => {
      const rows = new Map<string, Session>(), notices = new Set<string>()
      let cursor: string | null = null, hasMore = false
      for (let page = 0; page < sessionPageCount; page++) {
        const next: SessionInventory = await api(`/history/sessions?source=all&scope=mine&limit=100&archived=${listView === "archived"}&deleted=${listView === "deleted"}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`, "GET", undefined, signal)
        if (signal?.aborted || version !== refreshVersion.current || listViewRef.current !== listView) return null
        for (const row of next.sessions) {
          let unread = row.unread
          if (row.source === "legacy" && row.read_marker_key && row.completion_marker) {
            try { unread = localStorage.getItem(row.read_marker_key) !== row.completion_marker } catch { unread = false }
          }
          rows.set(sessionKey(row), { ...row, unread })
        }
        for (const row of next.ancestors ?? []) {
          if (!rows.has(sessionKey(row))) rows.set(sessionKey(row), row)
        }
        if (next.notice) notices.add(next.notice)
        hasMore = Boolean(next.has_more && next.next_cursor)
        if (!hasMore || next.next_cursor === cursor) break
        cursor = next.next_cursor!
      }
      return { rows: [...rows.values()], hasMore, notice: [...notices].join(" ") }
    }
    try {
      const current = () => !signal?.aborted && version === refreshVersion.current && listViewRef.current === listView
      // Publish the list independently; a slow account catalog must not leave
      // the rail empty after its authenticated inventory has arrived.
      const results = await Promise.allSettled([
        inventory().then(result => {
          if (result && current()) {
            setSessions(result.rows)
            setInventoryState({ loading: false, hasMore: result.hasMore, notice: result.notice })
          }
        }).catch(reason => {
          if (current() && reason instanceof ApiError && [401, 403, 404].includes(reason.status)) setSessions([])
          throw reason
        }),
        api<Connection[]>("/connections", "GET", undefined, signal).then(accounts => { if (current()) setConnections(accounts) }),
        api<SessionFolder[]>("/session-folders", "GET", undefined, signal).then(groups => { if (current()) setFolders(groups) }),
      ])
      if (!current()) return
      const failure = results.find(result => result.status === "rejected")
      if (failure?.status === "rejected") throw failure.reason
    } catch (reason) {
      if (version !== refreshVersion.current) return
      if (reason instanceof ApiError && [401, 403, 404].includes(reason.status)) setSessions([])
      throw reason
    } finally {
      if (version === refreshVersion.current) setInventoryState(previous => ({ ...previous, loading: false }))
    }
  }, [listView, sessionPageCount])

  useEffect(() => {
    api<Me>("/me").then(value => { setCsrf(value.csrf); setMe(value) }).catch(() => {}).finally(() => setReady(true))
    const signedOut = () => { ++refreshVersion.current; lastConversation.current = ""; setCsrf(""); setMe(null); setSessions([]); setSessionPageCount(1); setInventoryState({ hasMore: false, loading: false, notice: "" }); setFolders([]); setConnections([]); setWorkspaceRequest(null); setPrStates({}); setCurrentSession(null); setInitialConversation(null); setPaletteOpen(false); setContinued(null); setNativeRoute(null); setRouteRevision(value => value + 1) }
    const accessChanged = () => {
      const requestCsrf = csrfToken()
      ++refreshVersion.current
      setSessions([]); setConnections([]); setFolders([]); setPrStates({}); setPrRefresh(value => value + 1); setCurrentSession(null); setInitialConversation(null); setPaletteOpen(false)
      setContinued(null); setNativeRoute(null); setRouteRevision(value => value + 1)
      void api<Me>("/me").then(value => { if (csrfToken() === requestCsrf) { setCsrf(value.csrf); setMe(value) } }, () => {})
    }
    const navigate = () => {
      const id = currentRoute()
      const alias = continuedNow.current
      if (alias && (id === alias.id || id === `run=${alias.original}`)) {
        // Revisiting the same logical conversation through its native alias
        // must not discard an in-progress composer or its panel tabs.
        history.replaceState(null, "", `#run=${alias.original}`)
        setSelected(`run=${alias.original}`); setMobileNav(false)
        return
      }
      if (id && id !== "settings" && !id.startsWith("settings/")) lastConversation.current = id
      setSelected(id); setMobileNav(false); setCurrentSession(null)
      setContinued(null); setNativeRoute(null)
      setWorkspaceRequest(request => request?.sessionId === id ? request : null)
    }
    const shortcut = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing || event.repeat || document.querySelector('[data-slot="dialog-content"][data-state="open"]')) return
      if (!(event.metaKey || event.ctrlKey)) return
      if (event.shiftKey && event.key.toLowerCase() === "o") { event.preventDefault(); location.hash = "" }
    }
    window.addEventListener("moyai:signed-out", signedOut)
    window.addEventListener("moyai:access-changed", accessChanged)
    window.addEventListener("hashchange", navigate)
    window.addEventListener("keydown", shortcut)
    return () => { window.removeEventListener("moyai:access-changed", accessChanged); window.removeEventListener("moyai:signed-out", signedOut); window.removeEventListener("hashchange", navigate); window.removeEventListener("keydown", shortcut) }
  }, [])
  useEffect(() => {
    if (!me) return
    const stop = pollVisible(refresh, () => sessionsNow.current.some(row => activeSession(row.status)) ? 4000 : 15000, reason => setError(String(reason)))
    return () => { stop(); ++refreshVersion.current }
  }, [me, refresh])

  const prSessionIds = [...new Set([...sessions.filter(row => !row.deleted && !sessionIsReadOnly(row)).map(row => row.id), ...(nativeSelected ? [nativeSelected] : [])])].sort().join(",")
  const connectionVersion = JSON.stringify(connections.map(item => [item.id, item.enabled, item.shared, item.revision]))
  useEffect(() => {
    setPrStates({})
    if (!me || !prSessionIds) return
    return pollVisible(async signal => {
        const ids = prSessionIds.split(",")
        const batches = await Promise.all(Array.from({ length: Math.ceil(ids.length / 100) }, (_, index) =>
          api<Record<string, PRSummary[]>>("/pull-requests/statuses", "POST", { session_ids: ids.slice(index * 100, (index + 1) * 100) }, signal),
        ))
        const states = Object.assign({}, ...batches) as Record<string, PRSummary[]>
        if (!signal.aborted) setPrStates(states)
    }, () => 20000, () => setPrStates({}))
  }, [me?.user.id, prSessionIds, connectionVersion, prRefresh])

  if (!ready) return <div className="center muted" role="status">Opening Moyai…</div>
  if (!me) return <Login onLogin={value => { setCsrf(value.csrf); setMe(value); setError("") }} />
  const newSession = () => { location.hash = ""; setMobileNav(false); setWorkspaceRequest(null) }
  const navigateSettings = (page: string) => { if (selected && !settingsOpen) lastConversation.current = selected; location.hash = `settings/${page}` }
  const toggleSidebar = () => { if (matchMedia("(max-width: 600px)").matches) setMobileNav(value => !value); else setSidebarHidden(value => !value) }
  return <PanelRatioProvider key={JSON.stringify([me.product_mode || "standalone", me.user.id])} scope={me.product_mode || "standalone"} userId={me.user.id}><SessionActionsProvider key={scopeKey} userId={me.user.id} folders={folders} refresh={refresh} onNewSession={newSession} onDeleted={id => { if (nativeSelected === id) newSession() }} onOpenComputer={sessionId => {
    setWorkspaceRequest(previous => ({ sessionId, sequence: (previous?.sequence ?? 0) + 1, tab: "computer" }))
    location.hash = sessionId; setMobileNav(false)
  }}><div className={`app ${settingsOpen ? "settings-open" : ""} ${sidebarHidden ? "sidebar-collapsed" : ""}`}>
    {!settingsOpen && <aside className={`sidebar ${mobileNav ? "open" : ""}`}>
      <div className="sidebar-brand-row"><MoyaiBrand /><SidebarToggle collapsed={sidebarHidden} onToggle={toggleSidebar} /></div>
      <Button variant="secondary" className="new-chat" onClick={newSession}><Plus />New session<span className="key-hint">⌘⇧O</span></Button>
      <Button variant="ghost" className="rail-palette-trigger" aria-haspopup="dialog" aria-controls={COMMAND_PALETTE_ID} onClick={() => setPaletteOpen(true)}><Search size={16} /><span>Search sessions & commands</span><span className="key-hint">⌘K</span></Button>
      <span className="rail-section-label">Workspace</span>
      <nav className="rail-destinations" aria-label="Workspace tools">
        <Button variant="ghost" className="rail-button" onClick={() => navigateSettings("skills")}><FileText />Skills</Button>
        <Button variant="ghost" className="rail-button" onClick={() => navigateSettings("automations")}><Clock />Automations</Button>
        <Button variant="ghost" className="rail-button" onClick={() => navigateSettings("connections")}><Link2 />Connections</Button>
      </nav>
      <SessionSidebar key={me.user.id} sessions={sessions.map(row => ({ ...row, pull_requests: sessionIsReadOnly(row) ? [] : prStates[row.id] ?? [] }))} folders={folders} selected={nativeSelected || selected} userId={me.user.id} view={listView}
        hasMore={inventoryState.hasMore} loading={inventoryState.loading} notice={inventoryState.notice} onLoadMore={() => setSessionPageCount(value => value + 1)}
        onViewChange={value => { ++refreshVersion.current; setSessions([]); setSessionPageCount(1); setInventoryState({ hasMore: false, loading: false, notice: "" }); setListView(value) }}
        onOpenSideChat={row => {
          const host = nativeSelected && nativeSelected !== row.id ? nativeSelected : row.parent_session_id
          if (!host) return
          setWorkspaceRequest(previous => ({ sessionId: host, tab: "side-chat", child: { id: row.id, title: row.title }, sequence: (previous?.sequence ?? 0) + 1 }))
          if (nativeSelected !== host) location.hash = host
          setMobileNav(false)
        }}
        onOpenPullRequest={(sessionId, url) => { setWorkspaceRequest(previous => ({ sessionId, url, tab: "pull-request", sequence: (previous?.sequence ?? 0) + 1 })); location.hash = sessionId; setMobileNav(false) }} />
      <div className="account"><span>{me.user.name}</span><Button variant="ghost" size="icon" aria-label="Settings" onClick={() => navigateSettings("overview")}><Settings /></Button><Button variant="ghost" size="icon" aria-label="Sign out" onClick={async () => {
        try { const result = await api<{ logout_url?: string }>("/logout", "POST"); window.dispatchEvent(new Event("moyai:signed-out")); if (result.logout_url) navigateAuth(result.logout_url, true) }
        catch (reason) { setError(String(reason)) }
      }}><LogOut /></Button></div>
    </aside>}
    <main className="main">
      {!settingsOpen && <Button className="mobile-menu" variant="outline" size="icon" aria-label="Toggle conversations" onClick={() => setMobileNav(!mobileNav)}>{mobileNav ? <X /> : <Menu />}</Button>}
      {error && <div className="error global-error" role="alert">{error}<Button variant="ghost" onClick={() => setError("")}>Dismiss</Button></div>}
      {settingsOpen
        ? <React.Suspense fallback={<div className="center muted">Opening settings…</div>}><SettingsPage user={me.user} productMode={me.product_mode} page={selected.split("/")[1] || "overview"} onNavigate={page => { location.hash = `settings/${page}` }} onBack={() => { location.hash = lastConversation.current }} sidebarCollapsed={sidebarHidden} onToggleSidebar={toggleSidebar} onPreferences={preferences => setMe(current => current?.user.id === me.user.id && current.csrf === me.csrf ? { ...current, preferences } : current)} /></React.Suspense>
        : historical && !nativeSelected
        ? <React.Suspense fallback={<div className="center muted">Opening saved history…</div>}><ProductHistory key={`${routeScope}:${selected}`} sessionId={originalId} scopeKey={routeScope} onOpenSession={openHistorySession} onStartNewNative={newSession} /></React.Suspense>
        : nativeSelected
        ? <Conversation key={`${scopeKey}:${nativeSelected}`} id={nativeSelected} me={me} connections={connections} connectionVersion={connectionVersion} refreshList={refresh} workspaceRequest={workspaceRequest?.sessionId === nativeSelected ? workspaceRequest : null} pullRequests={prStates[nativeSelected] ?? []} onPRUpdated={() => setPrRefresh(value => value + 1)}
          originalSessionId={originalId || undefined} authorityKey={routeScope}
          resolved={nativeRoute?.key === routeKey ? nativeRoute.view : undefined}
          initial={initialConversation?.scope === scopeKey && initialConversation.value.session.id === nativeSelected ? initialConversation.value : undefined}
          onSession={session => { setCurrentSession(session); setInitialConversation(previous => previous?.value.session.id === (session?.id ?? nativeSelected) ? null : previous) }} />
        : selected
        ? <div className="center" role={routeError ? "alert" : "status"}>{routeError || "Opening conversation…"}{routeError && <Button variant="outline" onClick={() => setRouteRevision(value => value + 1)}>Retry</Button>}</div>
        : <Home preferences={me.preferences} models={me.models} defaultModel={me.default_model} individual={me.user.individual !== false} productMode={me.product_mode} connectionVersion={connectionVersion} onCreated={refresh}
          onStarted={value => { setInitialConversation({ scope: scopeKey, value }); setSelected(value.session.id); location.hash = value.session.id }} />}
    </main>
    <AppCommandPalette open={paletteOpen} onOpenChange={setPaletteOpen} scopeKey={`${scopeKey}:${selected}`} role={me.user.role} productMode={me.product_mode}
      current={currentSession?.id === nativeSelected ? { ...currentSession, url: originalId ? `/#run=${originalId}` : currentSession.url } : sessions.find(row => !sessionIsReadOnly(row) && row.id === nativeSelected)}
      historicalId={historical && !nativeSelected ? originalId : undefined} onNewSession={newSession} onToggleSidebar={toggleSidebar} onOpenSettings={navigateSettings} onError={setError} />
  </div></SessionActionsProvider></PanelRatioProvider>
}

function AppCommandPalette({ current, historicalId, productMode, ...props }: {
  current?: Session; historicalId?: string; productMode?: Me["product_mode"]; open: boolean; onOpenChange: (open: boolean) => void; scopeKey: string; role: string
  onNewSession: () => void; onToggleSidebar: () => void; onOpenSettings: (page: string) => void; onError: (error: string) => void
}) {
  const actions = useSessionActions()
  const session: PaletteCurrentSession | undefined = current ? {
    ...current, source: "native", capabilities: { read: true, rename: current.owner_id === actions.userId, pin: true, move: true },
  } : historicalId ? { id: historicalId, title: "Saved session", source: "legacy", read_only: true } : undefined
  return <CommandPalette {...props} scopeLabel="Your accessible sessions" currentSession={session} busy={actions.busy} search={searchSessions}
    destinations={settingsDestinations.filter(item => !item.productOnly || productMode === "existing")} onOpenSession={openSessionReference}
    onNewFolder={() => actions.openEditor({ action: "folder-create" })}
    onCopySessionLink={async row => { await navigator.clipboard.writeText(`${location.origin}/#${sessionReferenceHash(row)}`) }}
    onRenameSession={() => { if (current) actions.openEditor({ action: "rename", session: current }) }}
    onMoveSession={() => { if (current) actions.openEditor({ action: "move", session: current }) }}
    onSetPinned={async (_row, pinned) => { if (current) await actions.edit(current, { pinned }, pinned ? "Session pinned." : "Session unpinned.") }} />
}

function navigateAuth(value: string, sameOrigin = false) {
  const url = new URL(value, location.origin)
  if (url.username || url.password || (sameOrigin ? url.origin !== location.origin : url.protocol !== "https:" && url.origin !== location.origin)) throw new Error("The sign-in destination is invalid.")
  location.assign(url.href)
}
type AuthConfig = { mode: "standalone" | "existing"; password_enabled: boolean; google_enabled: boolean; access_enabled: boolean }
function Login({ onLogin }: { onLogin: (me: Me) => void }) {
  const [config, setConfig] = useState<AuthConfig | null>(null)
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    setError("")
    void api<AuthConfig>("/auth/config", "GET", undefined, controller.signal).then(value => { if (!controller.signal.aborted) setConfig(value) }, reason => { if (!controller.signal.aborted) setError(String(reason)) })
    return () => controller.abort()
  }, [retry])
  return <main className="center"><section className="login" aria-labelledby="login-title">
    <h1 id="login-title">Welcome to Moyai</h1><p className="muted">Sign in to your workspace.</p>
    {!config && !error && <p role="status">Loading sign-in options…</p>}
    {config?.google_enabled && <Button variant="outline" disabled={busy} onClick={async () => {
      setBusy(true); setError("")
      try { const result = await api<{ url: string }>("/auth/google/start", "POST", { return_to: "/" }); navigateAuth(result.url) }
      catch (reason) { setError(String(reason)); setBusy(false) }
    }}>Continue with Google</Button>}
    {config?.password_enabled && <form className="login-password" onSubmit={async event => {
      event.preventDefault(); setBusy(true); setError("")
      const form = new FormData(event.currentTarget)
      try {
        const result = await api<{ csrf?: string }>("/login", "POST", Object.fromEntries(form))
        if (result.csrf) setCsrf(result.csrf)
        onLogin(await api<Me>("/me"))
      } catch (reason) { setError(String(reason)) } finally { setBusy(false) }
    }}>
      {config.mode === "standalone" && <label>Username<Input name="username" autoComplete="username" required autoFocus disabled={busy} /></label>}
      <label>{config.mode === "existing" ? "Workspace password" : "Password"}<Input name="password" type="password" autoComplete="current-password" required autoFocus={config.mode === "existing" && !config.google_enabled} disabled={busy} /></label>
      <Button type="submit" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</Button>
    </form>}
    {config?.access_enabled && <><p className="muted">Your organization uses Cloudflare Access. Continue through your organization’s sign-in to open Moyai.</p><Button onClick={() => location.reload()}>Continue to workspace</Button></>}
    {config && !config.password_enabled && !config.google_enabled && !config.access_enabled && <p className="muted">No sign-in method is enabled. Contact your workspace administrator.</p>}
    {error && <p className="error" role="alert">{error}</p>}
    {!config && error && <Button variant="outline" onClick={() => setRetry(value => value + 1)}>Retry sign-in options</Button>}
  </section></main>
}

function Conversation({ id, me, connections, connectionVersion, refreshList, workspaceRequest, pullRequests, onPRUpdated, sideMode = false, visible = true, onTitle, onSession, onOpenFile, onOpenAction, initial, resolved, originalSessionId, authorityKey }: { id: string; me: Me; connections: Connection[]; connectionVersion: string; refreshList: () => Promise<void>; workspaceRequest: WorkspaceRequest | null; pullRequests: PRSummary[]; onPRUpdated: () => void; sideMode?: boolean; visible?: boolean; onTitle?: (title: string) => void; onSession?: (session: Session | null) => void; onOpenFile?: OpenFile; onOpenAction?: (action: ActionSelection) => void; initial?: NewConversation; resolved?: View; originalSessionId?: string; authorityKey?: string }) {
  const [view, setView] = useState<View | null>(() => initial ? { session: initial.session, turns: [initial.turn] } : resolved?.messages !== undefined ? resolved : null)
  const [messages, setMessages] = useState<Message[]>(() => initial ? [firstMessage(initial)] : resolved?.messages ?? [])
  const [plan, setPlan] = useState<Plan[]>([])
  const [panelLoading, setPanelLoading] = useState(false)
  const [panelError, setPanelError] = useState("")
  const resolvedOnce = useRef(resolved)
  const viewNow = useRef(view)
  viewNow.current = view
  const sensitiveVersion = useRef(0)
  const [draft, setDraft] = useState("")
  const [answeredRequest, setAnsweredRequest] = useState<string | null>(null)
  const [swarmSpace, setSwarmSpace] = useState<boolean | null>(null)
  const [model, setModel] = useState(view?.session.model ?? me.default_model)
  const [selectedAccounts, setSelectedAccounts] = useState<string[]>(() => {
    const latest = view?.turns[0]
    return latest?.actor_id === me.user.id ? latest.connections ?? [] : []
  })
  const [accountRevision, setAccountRevision] = useState(0)
  const [accountNotice, setAccountNotice] = useState("")
  const [accountCatalog, setAccountCatalog] = useState<{ scope: string; rows: Connection[]; loading: boolean; error: string }>({ scope: "", rows: [], loading: false, error: "" })
  const mode = me.preferences?.delivery_mode ?? "when_idle"
  const [skippedAccountScope, setSkippedAccountScope] = useState("")
  const [error, setError] = useState("")
  const pictures = useAttachments(setError)
  const [busy, setBusy] = useState(false)
  const [activity, setActivity] = useState<Activity[]>([])
  const [liveText, setLiveText] = useState(emptyLiveTranscript)
  const liveNow = useRef(liveText)
  const [reconnecting, setReconnecting] = useState(false)
  const endedStreams = useRef(new Set<string>())
  const [panelState, dispatchPanel] = useReducer((state: typeof emptyPanelState, action: PanelAction | { type: "reset" }) => action.type === "reset" ? emptyPanelState : panelReducer(state, action), emptyPanelState)
  const [workspaceOpen, setWorkspaceOpen] = useState(false)
  const openedBrowserLogin = useRef("")
  const panelReturnFocus = useRef<HTMLElement | null>(null)
  const sessionActions = useSessionActions()
  const titleCallback = useRef(onTitle)
  titleCallback.current = onTitle
  const sessionCallback = useRef(onSession)
  sessionCallback.current = onSession
  const [editing, setEditing] = useState<Message | null>(null)
  const [sendingNow, setSendingNow] = useState<Set<string>>(() => new Set())
  const fileDrop = useFileDrop(pictures.add, setError, busy || Boolean(editing) || !visible)
  const [editText, setEditText] = useState("")
  const composerInput = useRef<HTMLDivElement>(null)
  const heading = useRef<HTMLHeadingElement>(null)
  const transcript = useRef<HTMLDivElement>(null)
  const followingWork = useRef(true)
  const followWork = useCallback(() => {
    if (followingWork.current && transcript.current) transcript.current.scrollTop = transcript.current.scrollHeight
  }, [])
  const workspaceElement = useRef<HTMLDivElement>(null), chatId = useId()
  const lastRequest = useRef<{ payload: string; key: string } | null>(null)
  const composerInitialized = useRef(Boolean(view))
  const readThrough = useRef<number | null>(null)
  const loadVersion = useRef(0)
  const visibleNow = useRef(visible)
  visibleNow.current = visible
  function clearSensitive() {
    ++loadVersion.current
    ++sensitiveVersion.current
    resolvedOnce.current = undefined
    composerInitialized.current = false
    readThrough.current = null
    liveNow.current = emptyLiveTranscript()
    setLiveText(liveNow.current); setView(null); setMessages([]); setPlan([]); setActivity([]); setPanelError("")
    setDraft(""); setAnsweredRequest(null); setEditing(null); setEditText(""); pictures.clear(); setSendingNow(new Set()); setModel(me.default_model); setSelectedAccounts([]); setWorkspaceOpen(false)
    setAccountCatalog({ scope: "", rows: [], loading: false, error: "" }); setAccountNotice(""); setSkippedAccountScope("")
    dispatchPanel({ type: "reset" })
    openedBrowserLogin.current = ""
    sessionCallback.current?.(null)
  }
  const clearSensitiveNow = useRef(clearSensitive)
  clearSensitiveNow.current = clearSensitive
  useEffect(() => () => { ++loadVersion.current }, [])
  const load = useCallback(async (signal?: AbortSignal) => {
    const version = ++loadVersion.current
    try {
      const cached = resolvedOnce.current
      resolvedOnce.current = undefined
      const next = cached ?? await api<View>(`/sessions/${id}?include_activity=false&include_messages=true`, "GET", undefined, signal)
      // Older servers can omit the transcript during a rolling deployment.
      const history = next.messages ?? await api<Message[]>(`/sessions/${id}/messages`, "GET", undefined, signal)
      if (signal?.aborted || version !== loadVersion.current) return
      // Publish a complete composer with the transcript, before read bookkeeping.
      // Seed only once so polling and late receipts cannot overwrite user edits.
      if (!composerInitialized.current) {
        setModel(next.session.model)
        const latest = next.turns[0]
        setSelectedAccounts(latest?.actor_id === me.user.id ? latest.connections ?? [] : [])
        composerInitialized.current = true
      }
      viewNow.current = next
      setView(next); setMessages(history)
      if (next.activity) setActivity(next.activity)
      setSendingNow(previous => {
        const pending = [...previous].filter(id => next.turns.some(turn => turn.id === id && turn.status === "queued" && turn.mode !== "asap"))
        return pending.length === previous.size ? previous : new Set(pending)
      })
      sessionCallback.current?.(next.session)
      const latestMessage = history.at(-1)?.id ?? 0
      if (visibleNow.current && readThrough.current !== latestMessage) {
        await api(`/sessions/${id}/read`, "POST", { message_id: latestMessage }, signal)
        if (signal?.aborted || version !== loadVersion.current) return
        readThrough.current = latestMessage
      }
    } catch (reason) {
      if (signal?.aborted || version !== loadVersion.current) return
      if (reason instanceof ApiError && [401, 403, 404].includes(reason.status)) {
        clearSensitiveNow.current()
      }
      throw reason
    }
  }, [id, me.user.id, sideMode])
  useEffect(() => {
    if (sideMode && !visible) return
    return pollVisible(load, () => {
      const current = viewNow.current
      return !current || activeSession(current.session.status) || current.turns.some(turn => activeSession(turn.status)) ? 1500 : 10000
    }, reason => setError(String(reason)))
  }, [load, sideMode, visible])
  const auxiliaryPanel = panelState.visible && visible
    ? panelState.tabs.find(tab => tab.id === panelState.activeId)?.kind : undefined
  useEffect(() => {
    if (!view || (auxiliaryPanel !== "plan" && auxiliaryPanel !== "activity")) return
    const version = sensitiveVersion.current
    setPanelLoading(true); setPanelError("")
    return pollVisible(async signal => {
      const rows = await api<Plan[] | Activity[]>(`/sessions/${id}/${auxiliaryPanel}`, "GET", undefined, signal)
      if (signal.aborted || version !== sensitiveVersion.current) return
      if (auxiliaryPanel === "plan") setPlan(rows as Plan[])
      else setActivity(rows as Activity[])
      setPanelLoading(false); setPanelError("")
    }, () => activeSession(viewNow.current?.session.status) ? 1500 : 10000, reason => {
      if (version !== sensitiveVersion.current) return
      if (reason instanceof ApiError && [401, 403, 404].includes(reason.status)) { clearSensitiveNow.current(); setError(String(reason)) }
      else { setPanelLoading(false); setPanelError(String(reason)) }
    })
  }, [id, auxiliaryPanel, Boolean(view)])
  const accountScope = view ? `${id}:${view.session.visibility}:${view.session.surface}:${connectionVersion}:${accountRevision}` : ""
  const effectiveConnections = accountCatalog.scope === accountScope ? accountCatalog.rows.filter(row => row.enabled && (view?.session.visibility === "private" || row.shared)) : []
  const preferredAccounts = me.preferences?.connection_ids ?? selectedAccounts
  const skipAccounts = skippedAccountScope === accountScope
  const sendAccounts = skipAccounts ? [] : preferredAccounts.filter(id => effectiveConnections.some(row => row.id === id))
  const accountBlocked = !skipAccounts && (Boolean(accountNotice) || preferredAccounts.length > 0 && (accountCatalog.scope !== accountScope || accountCatalog.loading || Boolean(accountCatalog.error)))
  useEffect(() => {
    if (!view || !visible) return
    const controller = new AbortController(), scope = accountScope
    setAccountCatalog({ scope, rows: [], loading: true, error: "" })
    const query = new URLSearchParams({ session_id: id })
    void api<Connection[]>(`/connections?${query}`, "GET", undefined, controller.signal).then(rows => {
      if (!controller.signal.aborted) setAccountCatalog({ scope, rows, loading: false, error: "" })
    }, reason => { if (!controller.signal.aborted) setAccountCatalog({ scope, rows: [], loading: false, error: String(reason) }) })
    return () => controller.abort()
  }, [accountScope, visible])
  useEffect(() => {
    if (me.preferences?.connection_ids != null || accountCatalog.scope !== accountScope || accountCatalog.loading || accountCatalog.error) return
    const available = new Set(accountCatalog.rows.filter(row => row.enabled).map(row => row.id))
    const retained = selectedAccounts.filter(account => available.has(account))
    if (retained.length !== selectedAccounts.length) {
      setSelectedAccounts(retained)
      setAccountNotice("Some connections are no longer available for this chat.")
    }
  }, [accountScope, accountCatalog, selectedAccounts, me.preferences?.connection_ids])
  useEffect(() => {
    if (!workspaceRequest || sideMode) return
    setWorkspaceOpen(false)
    if (workspaceRequest.tab === "computer") dispatchPanel({ type: "open", tab: { id: "computer", kind: "computer", title: "Computer" } })
    else if (workspaceRequest.tab === "side-chat" && workspaceRequest.child) dispatchPanel({ type: "open", tab: {
      id: `side-chat:${workspaceRequest.child.id}`, kind: "side-chat",
      sessionId: workspaceRequest.child.id, title: workspaceRequest.child.title,
    } })
    else if (workspaceRequest.url) dispatchPanel({ type: "open", tab: { id: `pr:${workspaceRequest.url.toLowerCase()}`, kind: "pull-request", title: "Pull request", url: workspaceRequest.url } })
  }, [workspaceRequest, sideMode])
  useEffect(() => { if (visible) heading.current?.focus() }, [Boolean(view), visible])
  useEffect(() => {
    if (!sideMode) document.title = `${view?.session.title ?? "Conversation"} · Moyai`
    else if (view?.session.title) titleCallback.current?.(view.session.title)
  }, [view?.session.title, sideMode])
  useEffect(() => {
    const element = transcript.current
    if (element) { followingWork.current = true; element.scrollTop = element.scrollHeight }
  }, [messages.length, sendingNow])
  const active = view?.turns.find(turn => turn.id === view.session.active_turn)
  const work = useWorkProgress(
    id, `${authorityKey}:${connectionVersion}:${id}:${me.user.id}`,
    active?.actor_id === me.user.id ? active : undefined, visible && !swarmSpace,
    () => clearSensitiveNow.current(),
  )
  const requestAnswered = Boolean(active && answeredRequest === active.id)
  const browserLoginIds = view?.pending?.calls.filter(call => call.tool_name === "request_browser_login")
    .map(call => call.tool_call_id).join(",") ?? ""
  const browserLogin = view?.pending?.calls.find(call => call.tool_name === "request_browser_login")
  const loginMetadata = browserLogin ? view?.pending?.metadata?.[browserLogin.tool_call_id] : undefined
  const canResolveInput = Boolean(active && (view?.can_answer ?? active.actor_id === me.user.id))
  useEffect(() => {
    if (sideMode || !visible || !browserLoginIds || !canResolveInput || requestAnswered) return
    const identity = `${id}:${active?.id}:${browserLoginIds}`
    if (openedBrowserLogin.current === identity) return
    openedBrowserLogin.current = identity
    setWorkspaceOpen(false)
    dispatchPanel({ type: "open", tab: { id: "computer", kind: "computer", title: "Computer" } })
  }, [id, active?.id, browserLoginIds, canResolveInput, visible, requestAnswered, sideMode])
  const answeringQuestion = Boolean(view?.session.status === "waiting" && view.can_reply_with_text && active && !requestAnswered && pictures.files.length === 0)
  const activeId = visible && active && ["running", "saving"].includes(active.status) && active.actor_id === me.user.id ? active.id : null
  useEffect(() => {
    if (!activeId || endedStreams.current.has(activeId)) return
    if (liveNow.current.turnId !== activeId) {
      liveNow.current = emptyLiveTranscript(activeId)
      setLiveText(liveNow.current)
    }
    const auth = csrfToken()
    let closed = false
    const stream = new EventSource(`/api/sessions/${encodeURIComponent(id)}/events?turn_id=${encodeURIComponent(activeId)}&offset=${liveNow.current.offset + 1}`)
    const current = () => !closed && auth === csrfToken() && visibleNow.current
    const refreshSaved = () => { if (current()) void load().catch(reason => { if (current()) setError(String(reason)) }) }
    stream.onmessage = event => {
      if (!current() || !/^\d+$/.test(event.lastEventId)) return
      try {
        const value: LiveEvent = JSON.parse(event.data)
        if (!value || typeof value.type !== "string") return
        const next = applyLiveEvent(liveNow.current, value, Number(event.lastEventId))
        if (next !== liveNow.current) { liveNow.current = next; setLiveText(next) }
      } catch { /* A malformed presentation event must not interrupt saved-state refresh. */ }
    }
    stream.addEventListener("connected", () => { if (current()) setReconnecting(false) })
    stream.addEventListener("refresh", refreshSaved)
    stream.addEventListener("finished", () => {
      if (!current()) return
      endedStreams.current.add(activeId)
      liveNow.current = { ...liveNow.current, finished: true }
      setLiveText(liveNow.current); setReconnecting(false)
      refreshSaved(); closed = true; stream.close()
    })
    stream.addEventListener("access_revoked", () => {
      if (!current()) return
      closed = true; stream.close(); setReconnecting(false); endedStreams.current.add(activeId)
      clearSensitiveNow.current()
      window.dispatchEvent(new Event("moyai:access-changed"))
      void load().catch(reason => { if (auth === csrfToken()) setError(String(reason)) })
    })
    stream.onerror = () => { if (current()) { setReconnecting(true); refreshSaved() } }
    return () => { closed = true; stream.close() }
  }, [activeId, id, load])
  useEffect(() => {
    followWork()
  }, [liveText, followWork])

  async function send(promptOverride?: string) {
    const prompt = promptOverride ?? (editing ? editText : draft)
    if (editing) {
      if (busy || !prompt.trim()) return
      setBusy(true); setError("")
      try { await api(`/turns/${editing.turn_id}`, "PATCH", { prompt }); setEditing(null); await load() }
      catch (error) { setError(String(error)) } finally { setBusy(false) }
      return
    }
    if ((!prompt.trim() && !pictures.files.length) || busy || pictures.uploading || pictures.hasErrors || accountBlocked) return
    setBusy(true); setError("")
    const body = {
      prompt, model: answeringQuestion ? active?.model ?? model : model, mode,
      connections: answeringQuestion ? active?.connections ?? sendAccounts : sendAccounts,
      attachments: pictures.readyFiles.map(file => file.id),
      ...(answeringQuestion ? { reply_to: active?.id } : {}),
    }
    const serialized = JSON.stringify(body)
    if (lastRequest.current?.payload !== serialized) lastRequest.current = { payload: serialized, key: crypto.randomUUID() }
    let accepted = false
    try {
      await api(`/sessions/${id}/messages`, "POST", { ...body, request_key: lastRequest.current.key })
      accepted = true
      if (body.reply_to) setAnsweredRequest(body.reply_to)
      setDraft(""); pictures.clear(); lastRequest.current = null; await load(); await refreshList()
    } catch (e) { setError(accepted && body.reply_to ? "Your response was sent, but the conversation could not be refreshed." : String(e)) } finally { setBusy(false) }
  }
  async function sendNow(turnId: string) {
    if (busy) return
    setBusy(true); setError("")
    setSendingNow(previous => new Set(previous).add(turnId))
    try {
      const turn = await api<Turn>(`/turns/${turnId}/steer`, "POST")
      setView(previous => previous ? { ...previous, turns: previous.turns.map(item => item.id === turn.id ? turn : item) } : previous)
      await load()
    } catch (reason) {
      setSendingNow(previous => { const next = new Set(previous); next.delete(turnId); return next })
      setError(String(reason))
    } finally { setBusy(false) }
  }
  if (!view) return <div className="center" role="status">{error || "Opening conversation…"}</div>
  const session = originalSessionId ? { ...view.session, url: `/#run=${originalSessionId}` } : view.session
  const savedAssistant = messages.some(message => message.role === "assistant" && message.turn_id === liveText.turnId)
  const finalText = liveFinalAnswer(liveText)
  const liveMessage = !savedAssistant && finalText ? <article className="message assistant live-message" data-turn-id={liveText.turnId}>
    <div className="message-author">Moyai</div><div className="message-content"><Markdown remarkPlugins={[remarkGfm]}>{finalText}</Markdown></div>
  </article> : null
  const displayedActivity = mergeActivity(activity, liveText.activity)
  const queued = messages.filter(message => message.author_id === me.user.id && view.turns.some(turn =>
    turn.id === message.turn_id && waitsBehindActive(turn, active, sendingNow.has(turn.id))))
  const visibleMessages = orderedWorkMessages(messages.filter(message =>
    (message.role !== "system" || message.source === "delegation") && !queued.some(item => item.id === message.id)), view.turns)
  const segments = workSegments(visibleMessages, view.turns)
  const starting = !active && !view.has_unsettled_effects && view.turns.some(turn => turn.status === "queued")
  const displayStatus = starting || active?.status === "starting" ? "starting" : session.status
  function openTab(tab: WorkspaceTab) { setWorkspaceOpen(false); dispatchPanel({ type: "open", tab }) }
  function openAction(action: ActionSelection) {
    if (sideMode && onOpenAction) { onOpenAction(action); return }
    openTab({ id: "action", kind: "action", title: "Action", action })
  }
  const selectedTab = panelState.visible ? panelState.tabs.find(tab => tab.id === panelState.activeId) : undefined
  function openOutput(resource: OutputResource, sourceSessionId = id) {
    openTab({
      id: `output:${sourceSessionId}:${resource.kind}:${resource.id ?? resource.url}`,
      kind: "output", title: resource.name, resource, sessionId: sourceSessionId,
    })
  }
  function addTab(kind: AddTabKind) {
    const titles = { "side-chat": "Side chat", computer: "Computer", files: "Files", activity: "Activity", plan: "Plan" }
    openTab({ id: kind === "side-chat" ? `side-chat:new:${crypto.randomUUID()}` : kind, kind, title: titles[kind] })
  }
  // Questions and effect reviews remain in the conversation where they can be answered.
  const space = Boolean(session.swarm_id && !sideMode && (swarmSpace ?? session.swarm_id === id)
    && !view.pending && !view.credential_requests?.length && !view.has_unsettled_effects)
  const chat = <section className={`chat file-drop-zone${space ? " swarm-space-mode" : ""}`} id={chatId} {...fileDrop.handlers}>
      <div className="chat-layout"><div className="chat-content">
        {!space && <SpaceBackdrop variant="chat" />}
        {fileDrop.dragging && <FileDropOverlay />}
        {session.swarm_id && <SwarmPanel key={id} sessionId={id} visible={visible} onChanged={load}
          space={space} onSpaceChange={sideMode ? undefined : setSwarmSpace} />}
        {originalSessionId && !space && <React.Suspense fallback={<div className="muted" role="status">Opening earlier history…</div>}>
          <ProductHistoryPrefix sessionId={originalSessionId} scopeKey={`${authorityKey}:${connectionVersion}`} active={visible} onOpenSession={openSessionReference} />
        </React.Suspense>}
        <div className="transcript" ref={transcript} hidden={space}
          onScroll={event => {
            const element = event.currentTarget
            followingWork.current = element.scrollHeight - element.scrollTop - element.clientHeight < 180
          }}
          onClickCapture={event => previewFileLink(event, id, sideMode ? onOpenFile : openOutput)}>
          {!messages.length && <div className="empty-chat"><h2>Start with a task or a question.</h2><p className="muted">Your work and saved outputs stay with this conversation.</p></div>}
          {visibleMessages.map(message => {
            const turn = view.turns.find(item => item.id === message.turn_id)
            const segment = segments.get(message.id)
            return <React.Fragment key={message.id}><article className={`message ${message.role}`}>
              <div className="message-author">{message.source === "delegation" ? "Parent agent" : message.role === "assistant" ? "Moyai" : message.author_id === me.user.id ? "You" : "Teammate"}
                {message.role === "user" && turn && waitsBehindActive(turn, active, sendingNow.has(turn.id)) && <span className="status">Queued</span>}
                {message.role === "user" && turn?.status === "stopped" && <span className="muted">Stopped</span>}
              </div>
              <div className={message.role === "user" ? "user-bubble" : undefined}>
                {message.content && <div className="message-content">{message.role === "assistant" ? <Markdown remarkPlugins={[remarkGfm]}>{message.content}</Markdown> : <SkillMessage content={message.content} />}</div>}
                <AttachmentPreviews files={message.attachments ?? []} />
              </div>

            </article>{segment?.turn.actor_id === me.user.id && <TurnProgress
              key={`${authorityKey}:${connectionVersion}:${segment.id}`} segment={segment} visible={visible && !space}
              entry={work.entries[segment.turn.id]} request={work.load}
              live={liveText.turnId === segment.turn.id ? liveText : undefined}
              onContentChange={followWork}
              selectedAction={selectedTab?.kind === "action" ? selectedTab.action : undefined}
              onAction={(tool, agent) => openAction({ sessionId: id, turnId: segment.turn.id, toolId: tool.id, agentId: agent?.id, name: toolLabel(tool.name) })}
              onSubagent={agent => openTab(agent.session_id ? {
                id: `side-chat:${agent.session_id}`, kind: "side-chat", title: agent.name, sessionId: agent.session_id,
              } : {
                id: `subagent:${agent.id}`, kind: "subagent", title: agent.name, runId: agent.id,
                name: agent.name, status: agent.status, avatar: agent.avatar,
              })}
            />}</React.Fragment>
          })}
          {!savedAssistant && liveMessage}
          {starting && <div className="working live-action" role="status"><span className="running-dot" /><span>Starting…</span></div>}
          {active && ["starting", "running", "saving"].includes(active.status) && <div className="working live-action" role="status"><span className="running-dot" /><span>{active.status === "saving" ? "Saving the response…" : active.status === "starting" ? "Starting…" : reconnecting ? "Reconnecting to live progress…" : active.id === liveText.turnId ? liveText.action || "Working…" : "Working…"}</span>{!sideMode && <Button type="button" variant="ghost" size="sm" onClick={() => addTab("activity")}>Activity</Button>}</div>}
          {view.pending && active && (view.can_answer ?? active.actor_id === me.user.id) && <>
            {view.question_deadline && <p className="notice">Optional question. If unanswered, Moyai will continue using its judgment after {new Date(view.question_deadline).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })}.</p>}
            <PendingRequests key={active.id} pending={view.pending} turnId={active.id} sessionId={id} submitted={requestAnswered} onAccepted={() => setAnsweredRequest(active.id)} onDone={load} onOpenComputer={() => sideMode ? sessionActions.openComputer(id) : addTab("computer")} />
          </>}
          {session.status === "waiting" && !view.can_answer && !view.pending && <p className="notice">Waiting for the original requester’s response. New messages will be queued.</p>}
          {view.credential_requests?.map(request => <div className="pending" key={request.request_id}><p>This task needs organization access.</p><ProductCredentialRequest requestId={request.request_id} onResolved={load} /></div>)}
          {view.turns[0]?.error && <p className="error" role="alert">{view.turns[0].error}</p>}
          {!session.active_turn && view.effects?.map(effect => <EffectReview key={effect.id} effect={effect} onDone={load} />)}
          {!session.active_turn && view.has_unsettled_effects && !view.effects?.length && <p className="notice">An earlier action needs review by its requester before more changes can be made.</p>}
          {session.save_warning && <p className="notice" role="status">{session.save_warning}</p>}
        </div>
        <form className="composer-wrap" onSubmit={e => { e.preventDefault(); void send() }}>
          {error && <div className="error" role="alert">{error}<Button size="sm" variant="ghost" onClick={() => setError("")}>Dismiss</Button></div>}
          {answeringQuestion && <p className="question-reply-hint">Your reply will answer the pending question.</p>}
          {requestAnswered && <p className="question-reply-hint">Your answer was sent. You can send a follow-up.</p>}
          {session.status === "waiting" && view.can_answer && !answeringQuestion && !requestAnswered && <p className="notice">Resolve the requests above to continue. New messages will be queued.</p>}
          {queued.length > 0 && <div className="queued-messages" aria-label="Queued messages">{queued.map(message => <div className="queued-message" key={message.id}>
            <span><small>Queued after the current task</small><span>{message.content ? <SkillMessage content={message.content} /> : "File message"}</span></span>
            <Button type="button" size="sm" variant="ghost" disabled={busy || Boolean(editing)} onClick={() => void sendNow(message.turn_id)}>Send now</Button>
            <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => { setEditing(message); setEditText(message.content); requestAnimationFrame(() => composerInput.current?.focus()) }}>Edit</Button>
            <Button type="button" size="icon-sm" variant="ghost" aria-label="Cancel queued message" disabled={busy} onClick={async () => { try { await api(`/turns/${message.turn_id}`, "DELETE"); if (editing?.id === message.id) setEditing(null); await load() } catch (error) { setError(String(error)) } }}><X size={14} /></Button>
          </div>)}</div>}
          {editing && <div className="editing-message" role="status"><span>Editing queued message · attachments are kept</span><Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => setEditing(null)}>Cancel edit</Button></div>}
          <div className="composer">
            <AttachmentPreviews files={editing ? editing.attachments ?? [] : pictures.files} remove={editing || busy ? undefined : pictures.remove} retry={editing || busy ? undefined : pictures.retry} draft />
            <SkillComposer ref={composerInput} value={editing ? editText : draft} onChange={editing ? setEditText : setDraft} onSend={prompt => void send(prompt)} shared={session.visibility !== "private"} sessionId={id} disabled={busy} placeholder={answeringQuestion ? "Type your answer to the pending question…" : "Describe the work, or type / for skills…"} onPaste={editing ? undefined : pictures.paste} />
            <div className="composer-actions">
              <AttachButton add={pictures.add} disabled={Boolean(editing) || busy} />
              <Choice value={model} onChange={setModel} label="Model" options={me.models.map(item => ({ value: item, label: item }))} />
              <div className="send-actions">
                {session.active_turn && <Button type="button" variant="outline" aria-label="Stop task" onClick={async () => { try { await api(`/sessions/${id}/stop`, "POST"); await load() } catch (e) { setError(String(e)) } }}><Square size={13} /><span className="composer-optional-label">Stop</span></Button>}
                <Button type="submit" size={editing ? "sm" : "icon"} aria-label={editing ? "Save queued message" : answeringQuestion ? "Answer question" : "Send message"} disabled={(editing ? !editText.trim() : !draft.trim() && !pictures.files.length) || busy || (!editing && (pictures.uploading || pictures.hasErrors || accountBlocked))}>{editing ? <><Check className="composer-save-icon" /><span className="composer-optional-label">Save message</span></> : <ArrowUp />}</Button>
              </div>
            </div>
          </div>
          {accountNotice && !skipAccounts && <p className="notice" role="status">{accountNotice}<Button type="button" variant="ghost" size="sm" onClick={() => setAccountNotice("")}>Continue</Button><a href="#settings/general">Preferences</a></p>}
          {accountBlocked && !accountNotice && !accountCatalog.error && <p className="composer-hint" role="status">Loading connections…</p>}
          {accountBlocked && accountCatalog.error && <p className="error" role="alert">{accountCatalog.error}<Button type="button" variant="outline" size="sm" onClick={() => setAccountRevision(value => value + 1)}>Retry connections</Button><Button type="button" variant="ghost" size="sm" onClick={() => { setSkippedAccountScope(accountScope); setAccountNotice("") }}>Continue without connections</Button></p>}
          {pictures.uploading && <p className="composer-hint" role="status">Uploading files…</p>}
        </form>
      </div>
      {!sideMode && <WorkspaceOverlayContent>
        <WorkspaceOverview id={id} pullRequests={pullRequests} onUpdated={onPRUpdated}
          onPullRequest={(url, title) => openTab({ id: `pr:${url.toLowerCase()}`, kind: "pull-request", url, title })}
          onSubagent={agent => {
            if (agent.session_id) { window.location.hash = agent.session_id; return }
            openTab({ id: `subagent:${agent.id}`, kind: "subagent", title: agent.name, runId: agent.id, name: agent.name, status: agent.status, avatar: agent.avatar })
          }}
          onSubagentSideChat={agent => {
            if (agent.session_id) openTab({ id: `side-chat:${agent.session_id}`, kind: "side-chat", sessionId: agent.session_id, title: agent.name })
          }}
          onOutput={openOutput} />
      </WorkspaceOverlayContent>}
      </div>
      </section>
  if (sideMode) return <div className="conversation side-conversation">
    <header className="side-chat-heading"><h2 ref={heading} tabIndex={-1} title={session.title}>{session.title}</h2><a href={`#${id}`} title="Open as main conversation" aria-label="Open side chat as main conversation"><ExternalLink size={15} /></a></header>
    {chat}
  </div>
  return <div className="conversation">
    <WorkspaceResourceProvider id={id} visible={visible} expanded={workspaceOpen}>
    <WorkspaceOverlay open={workspaceOpen} onOpenChange={setWorkspaceOpen} panelVisible={panelState.visible} panelExpanded={panelState.visible && panelState.expanded}>
    <div ref={workspaceElement} className={`workspace ${panelState.visible ? "with-panel" : "panel-closed"} ${panelState.visible && panelState.expanded ? "panel-expanded" : ""}`}>
      <header className="topbar">
        <div className="session-heading"><h1 ref={heading} tabIndex={-1} title={session.title} aria-label={session.title}>
          <SessionTitle session={session} onRenamed={title => {
            // Discard reads started before the rename so they cannot restore the old title.
            ++loadVersion.current
            const current = viewNow.current
            if (!current) return
            const next = { ...current, session: { ...current.session, title } }
            viewNow.current = next; setView(next); sessionCallback.current?.(next.session)
          }} />
        </h1>
          <span className={`status ${displayStatus}`}>{displayStatus === "waiting" ? "Needs input" : displayStatus}</span>
          <span className="privacy-icon" title={session.surface === "slack" ? "Replies are delivered to Slack" : session.visibility === "private" ? "Private conversation" : "Shared conversation"} aria-label={session.surface === "slack" ? "Slack conversation" : `${session.visibility} conversation`}>{session.visibility === "private" ? <LockKeyhole size={13} /> : <Users size={13} />}</span>
        </div>
        <div className="header-controls">
          {session.parent_session_id && <Button asChild variant="ghost" size="sm"><a href={`#${session.parent_session_id}`}>Parent chat</a></Button>}
          <Button variant={panelState.visible ? "secondary" : "ghost"} size="icon-sm" aria-label={panelState.visible ? "Hide side panel" : "Open side panel"} title={panelState.visible ? "Hide panel" : "Open panel"} onClick={() => dispatchPanel({ type: panelState.visible ? "hide" : "show" })}><PanelRight size={16} /></Button>
          <WorkspaceOverlayTrigger asChild><WorkspaceMenuKey open={workspaceOpen} sessionFailed={session.status === "failed"}
            aria-label={workspaceOpen ? "Close workspace" : "Open workspace"} /></WorkspaceOverlayTrigger>
          <Button className="header-new-session" type="button" variant="ghost" size="icon-sm" aria-label="New session" title="New session" onClick={sessionActions.newSession}><SquarePen size={16} /></Button>
          <SessionMenu session={session} header />
        </div>
      </header>
      {chat}

      <PanelResizeHandle workspace={workspaceElement} enabled={panelState.visible && !panelState.expanded} chatId={chatId} />
      <PanelTabs state={panelState} dispatch={dispatchPanel} onAdd={addTab} returnFocus={panelReturnFocus}>{(tab, activeTab) => {
        if (tab.kind === "action") return <ActionPanel
          key={JSON.stringify([authorityKey, connectionVersion, me.user.id, me.csrf, tab.action])}
          action={tab.action} active={activeTab} onDenied={() => clearSensitiveNow.current()} />
        if (tab.kind === "pull-request") return <PullRequestPanel key={`${connectionVersion}:${session.visibility}:${session.surface}`} sessionId={id} url={tab.url} onBack={() => { dispatchPanel({ type: "hide" }); setWorkspaceOpen(true) }} onUpdated={summary => { dispatchPanel({ type: "rename", id: tab.id, title: summary.title }); onPRUpdated() }} />
        if (tab.kind === "subagent") return <SubagentPanel sessionId={id} agent={{ id: tab.runId, name: tab.name, status: tab.status, avatar: tab.avatar }} active={activeTab} onOpenFile={openOutput} />
        if (tab.kind === "output") return <OutputPanel sessionId={tab.sessionId ?? id} resource={tab.resource} active={activeTab}
          onOpenFile={openOutput} onName={title => dispatchPanel({ type: "rename", id: tab.id, title })} />
        if (tab.kind === "files") return <FilesPanel sessionId={id} onOpen={openOutput} active={activeTab} />
        if (tab.kind === "computer") return <div className="browser-login-computer">
          {browserLoginIds && canResolveInput && !requestAnswered && <div className="browser-login-panel-hint" role="region" aria-label="Browser sign-in instructions">
            <strong>Sign in using Computer</strong>
            {loginMetadata?.site && <p>{loginMetadata.site}</p>}
            {loginMetadata?.reason && <p>{loginMetadata.reason}</p>}
            <p>Take control, complete sign-in, then release control. Return to the request to confirm or cancel.</p>
            <Button type="button" variant="outline" size="sm" onClick={() => {
              panelReturnFocus.current = transcript.current?.querySelector<HTMLButtonElement>(".browser-login-request button[aria-pressed]") ?? null
              dispatchPanel({ type: "hide" })
            }}>Back to sign-in request</Button>
          </div>}
          <React.Suspense fallback={<div className="panel-empty" role="status">Opening computer…</div>}><ComputerPanel id={id} hasWorkspace={Boolean(session.workspace)} active={activeTab} /></React.Suspense>
        </div>
        if (tab.kind === "activity") return <>{panelError && <p className="error" role="alert">{panelError}</p>}{!displayedActivity.length && <div className="panel-empty"><MessageSquare /><p>{panelLoading ? "Loading activity…" : session.status === "running" ? "Connecting to live activity…" : "Live steps appear here while Moyai works."}</p></div>}{displayedActivity.map((item, index) => <ActivityRow key={index} item={item} />)}</>
        if (tab.kind === "plan") return <>{panelError && <p className="error" role="alert">{panelError}</p>}{!plan.length && <div className="panel-empty"><Check /><p>{panelLoading ? "Loading plan…" : "Moyai’s task plan will appear here."}</p></div>}{plan.map(task => <div className="plan-row" key={task.id}><span className={`plan-mark ${task.status}`}>{task.status === "completed" ? "✓" : "○"}</span><div>{task.content}<small>{task.status.replaceAll("_", " ")}</small></div></div>)}</>
        if (tab.kind === "side-chat") return tab.sessionId
          ? <Conversation key={`${me.user.id}:${tab.sessionId}`} id={tab.sessionId} me={me} connections={connections} connectionVersion={connectionVersion} refreshList={refreshList} workspaceRequest={null} pullRequests={[]} onPRUpdated={onPRUpdated} sideMode visible={activeTab} onOpenFile={openOutput} onOpenAction={openAction} onTitle={title => dispatchPanel({ type: "rename", id: tab.id, title })} />
          : <SideChatPicker parentId={id} defaultModel={me.default_model} individual={me.user.individual !== false} onCreated={refreshList} onSelect={selected => dispatchPanel({ type: "replace", id: tab.id, tab: { id: `side-chat:${selected.id}`, kind: "side-chat", sessionId: selected.id, title: selected.title } })} />
        return null
      }}</PanelTabs>
    </div>
    </WorkspaceOverlay>
    </WorkspaceResourceProvider>
  </div>
}

function SideChatPicker({ parentId, defaultModel, individual, onCreated, onSelect }: { parentId: string; defaultModel: string; individual: boolean; onCreated: () => Promise<void>; onSelect: (session: Session) => void }) {
  const [sessions, setSessions] = useState<Session[]>([])
  const [query, setQuery] = useState("")
  const [error, setError] = useState("")
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [revision, setRevision] = useState(0)
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true); setError("")
    void api<Session[]>("/sessions", "GET", undefined, controller.signal).then(rows => {
      if (!controller.signal.aborted) { setSessions(rows.filter(row => row.id !== parentId && !row.deleted)); setLoading(false) }
    }, reason => { if (!controller.signal.aborted) { setLoading(false); setError(String(reason)) } })
    return () => controller.abort()
  }, [parentId, revision])
  return <div className="side-chat-picker"><h2>Side chat</h2><p>Start a separate conversation, or open an existing one beside this session.</p>
    {!individual && <p className="muted">This shared sign-in creates shared sessions. Use an individual sign-in for private work.</p>}
    <Button className="side-chat-new" disabled={busy} onClick={async () => {
      setBusy(true); setError("")
      try {
        const next = await api<Session>("/sessions", "POST", { visibility: "shared", model: defaultModel })
        if (alive.current) onSelect(next)
        // The session already exists; a sidebar refresh failure must not turn
        // a confirmed creation into an invitation to create it again.
        void onCreated().catch(() => {})
      } catch (reason) { if (alive.current) setError(String(reason)) }
      finally { if (alive.current) setBusy(false) }
    }}><Plus size={16} />{busy ? "Creating…" : "New side chat"}</Button>
    <label className="side-chat-search"><Search size={16} /><Input aria-label="Find a side chat" placeholder="Search conversations…" value={query} onChange={event => setQuery(event.target.value)} /></label>
    {error && <p className="error" role="alert">{error}<Button variant="ghost" size="sm" onClick={() => setRevision(value => value + 1)}>Retry</Button></p>}
    {loading && <p className="muted" role="status">Loading conversations…</p>}
    {sessions.filter(row => row.title.toLowerCase().includes(query.toLowerCase())).map(row => <Button key={row.id} className="side-chat-choice" variant="ghost" disabled={busy} onClick={() => onSelect(row)}><MessageSquare size={16} /><span>{row.title}<small>{row.visibility === "shared" ? "Shared conversation" : "Private conversation"}</small></span></Button>)}
    {!loading && !error && !sessions.some(row => row.title.toLowerCase().includes(query.toLowerCase())) && <p className="muted">No matching conversations.</p>}
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

function PendingRequests({ pending, turnId, sessionId, submitted, onAccepted, onDone, onOpenComputer }: { pending: Pending; turnId: string; sessionId: string; submitted: boolean; onAccepted: () => void; onDone: () => Promise<void>; onOpenComputer: () => void }) {
  const [values, setValues] = useState<Record<string, string[]>>({})
  const [custom, setCustom] = useState<Record<string, string>>({})
  const [approvals, setApprovals] = useState<Record<string, boolean>>({})
  const [browserLogins, setBrowserLogins] = useState<Record<string, LoginOutcome>>({})
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  const form = useRef<HTMLFormElement>(null)
  const restoreFocus = useRef<HTMLElement | null>(null)
  const requestKey = useRef(crypto.randomUUID())
  const submitting = useRef(false)
  const disabled = busy || submitted
  useLayoutEffect(() => {
    if (busy || submitted || !restoreFocus.current) return
    const target = restoreFocus.current
    restoreFocus.current = null
    if (target.isConnected && document.activeElement === document.body) target.focus()
  }, [busy, submitted])
  const questions = pending.calls.filter(call => call.tool_name === "ask_user_question").map(call => ({ call, questions: (typeof call.args === "string" ? JSON.parse(call.args) : call.args).questions as Question[] }))
  const credentials = pending.calls.filter(call => call.tool_name === "credentials_request")
  const logins = pending.calls.filter(call => call.tool_name === "request_browser_login")
  const unknown = pending.calls.some(call => !["ask_user_question", "credentials_request", "request_browser_login"].includes(call.tool_name))
  const isComplete = (nextValues: typeof values, nextCustom: typeof custom, nextLogins = browserLogins) => questions.every(({ call, questions }) => questions.every(question => {
    const key = `${call.tool_call_id}:${question.header}`
    return Boolean(nextCustom[key]?.trim() || nextValues[key]?.length)
  })) && pending.approvals.every(call => typeof approvals[call.tool_call_id] === "boolean")
    && logins.every(call => Boolean(nextLogins[call.tool_call_id]))
  const requiresContinue = pending.approvals.length > 0 || credentials.length > 0 || logins.length > 1 || (logins.length > 0 && questions.length > 0)
  async function refreshSubmitted() {
    setBusy(true); setError("")
    try { await onDone() }
    catch { setError("Your response was sent, but the conversation could not be refreshed.") }
    finally { setBusy(false) }
  }
  async function submit(nextValues = values, nextCustom = custom, nextLogins = browserLogins) {
    if (submitting.current || submitted || unknown) return
    if (!isComplete(nextValues, nextCustom, nextLogins)) {
      setError(requiresContinue ? "Complete the remaining questions and approval requests before continuing." : "Answer the remaining questions before continuing.")
      return
    }
    submitting.current = true
    restoreFocus.current = document.activeElement instanceof HTMLElement && form.current?.contains(document.activeElement)
      ? document.activeElement : null
    setBusy(true); setError("")
    const answers = Object.fromEntries(questions.map(({ call, questions }) => [call.tool_call_id, { answers: questions.map(question => {
      const key = `${call.tool_call_id}:${question.header}`
      return { header: question.header, selected: nextCustom[key]?.trim() ? [] : nextValues[key] ?? [], custom_answer: nextCustom[key]?.trim() || null }
    }) }]))
    try { await api(`/sessions/${sessionId}/answers`, "POST", { turn_id: turnId, request_key: requestKey.current, approvals, answers, ...(logins.length ? { browser_logins: nextLogins } : {}) }) }
    catch (e) {
      submitting.current = false
      setError(String(e)); setBusy(false)
      return
    }
    // Once accepted, only refresh the conversation; never reopen this answer.
    restoreFocus.current = null
    onAccepted()
    await refreshSubmitted()
  }
  return <>
    {credentials.map(call => {
      const requestId = pending.metadata?.[call.tool_call_id]?.request_id
      return <div className="pending" key={call.tool_call_id}><h3>Secure access requested</h3><p>Provide access through the secure form to continue this task.</p>{requestId ? <ProductCredentialRequest requestId={requestId} onResolved={onDone} /> : <p role="alert">This request has no secure form reference. Refresh the session to check its current state.</p>}</div>
    })}
    {unknown && <p className="notice" role="status">This task is waiting for an input that this client cannot resolve.</p>}
    {(questions.length > 0 || pending.approvals.length > 0 || logins.length > 0) && <form ref={form} aria-busy={busy} className={questions.length ? "question-form" : "pending"} onSubmit={e => { e.preventDefault(); void submit() }}>
    {questions.length
      ? <div className="question-form-header"><MessageCircleQuestion size={14} aria-hidden="true" /><h3>{questions.reduce((count, item) => count + item.questions.length, 0) > 1 ? "Questions" : "Question"}</h3>{disabled && <span className="question-send-status" role="status">{submitted ? "Answer sent" : "Sending…"}</span>}</div>
      : logins.length === 0 && <h3>Your input is needed</h3>}
    {logins.map(call => <BrowserLoginCard key={call.tool_call_id} metadata={pending.metadata?.[call.tool_call_id]}
      outcome={browserLogins[call.tool_call_id]} disabled={disabled} onOpen={onOpenComputer}
      onAnswer={outcome => {
        if (submitting.current) return
        const next = { ...browserLogins, [call.tool_call_id]: outcome }
        setBrowserLogins(next)
        if (!requiresContinue && isComplete(values, custom, next)) void submit(values, custom, next)
      }} />)}
    {questions.map(({ call, questions }) => questions.map(question => {
      const key = `${call.tool_call_id}:${question.header}`
      return <QuestionChoices key={key} question={question} selected={values[key] ?? []} customAnswer={custom[key] ?? ""} disabled={disabled}
        onSelect={selected => {
          if (submitting.current) return
          const nextValues = { ...values, [key]: selected }, nextCustom = { ...custom, [key]: "" }
          setValues(nextValues); setCustom(nextCustom)
          if (!question.multi_select && !requiresContinue && isComplete(nextValues, nextCustom)) void submit(nextValues, nextCustom)
        }}
        onCustomAnswer={value => {
          if (submitting.current) return
          setCustom(previous => ({ ...previous, [key]: value }))
          if (value.trim()) setValues(previous => ({ ...previous, [key]: [] }))
        }} />
    }))}
    {pending.approvals.map(call => <fieldset key={call.tool_call_id} className={questions.length ? "question-approval" : undefined}><legend>Allow {call.tool_name.replaceAll("_", " ")}?</legend><pre>{JSON.stringify(typeof call.args === "string" ? JSON.parse(call.args) : call.args, null, 2)}</pre><div className="question-options">{[true, false].map(value => <Button key={String(value)} type="button" disabled={disabled} variant={approvals[call.tool_call_id] === value ? "secondary" : "outline"} onClick={() => setApprovals(previous => ({ ...previous, [call.tool_call_id]: value }))}>{value ? "Allow this action" : "Decline"}</Button>)}</div></fieldset>)}
    {credentials.length > 0 && <p className="muted">Resolve secure access above before continuing with these answers.</p>}
    {error && <p className="error" role="alert">{error}</p>}
    {submitted && <div className={questions.length ? "question-form-actions" : undefined}><Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void refreshSubmitted()}>Refresh conversation</Button></div>}
    {requiresContinue && <div className={questions.length ? "question-form-actions" : undefined}><Button type="submit" disabled={disabled || unknown || !isComplete(values, custom)}>{submitted ? "Submitted" : busy ? "Submitting…" : "Continue"}</Button></div>}
  </form>}
  </>
}

createRoot(document.getElementById("root")!).render(<App />)
