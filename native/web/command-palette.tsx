import React, { useEffect, useId, useRef, useState } from "react"
import { ArrowLeft, Copy, FolderPlus, LayoutGrid, MessageSquare, PanelLeft, Pencil, Pin, Plus, Search, Settings } from "lucide-react"
import type { LucideIcon } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog"
import "./command-palette.css"

export const COMMAND_PALETTE_ID = "native-command-palette"
export type PaletteMode = "commands" | "sessions"
export type PaletteSession = {
  id: string; key?: string; title: string; status?: string; description?: string; parent_title?: string
  source?: string; source_format?: string; url?: string; read_only?: boolean
  deleted?: boolean; deleting?: boolean; parent_session_id?: string | null
  snippet?: string; search_snippet?: string; search_query?: string
  match?: { message_id?: string | number | null; snippet?: string } | null
  capabilities?: { read?: boolean; rename?: boolean; move?: boolean; pin?: boolean }
}
export type PaletteCurrentSession = PaletteSession & { pinned?: boolean }
export type PaletteSearchResult = { sessions: readonly PaletteSession[]; has_more?: boolean; notice?: string }
export type PaletteSearchAdapter = (query: string, options: { signal: AbortSignal; limit: number }) => Promise<PaletteSearchResult>
export type PaletteDestination = { page: string; label: string; description?: string; adminOnly?: boolean; available?: boolean }
type ActionResult = void | Promise<void>
export type CommandPaletteProps = {
  open: boolean; onOpenChange: (open: boolean) => void
  /** Change on identity, role, navigation scope, or route changes to invalidate results. */
  scopeKey: string; scopeLabel?: string; role?: string; enabled?: boolean
  search: PaletteSearchAdapter; recentSessions?: readonly PaletteSession[]
  currentSession?: PaletteCurrentSession | null; busy?: boolean
  destinations?: readonly PaletteDestination[]; initialMode?: PaletteMode; registerShortcut?: boolean
  onOpenSession: (session: PaletteSession) => ActionResult
  onNewSession: () => ActionResult; onNewFolder?: () => ActionResult
  onToggleSidebar: () => ActionResult; onOpenSettings: (page: string) => ActionResult
  onCopySessionLink?: (session: PaletteCurrentSession) => ActionResult
  onRenameSession?: (session: PaletteCurrentSession) => ActionResult
  onMoveSession?: (session: PaletteCurrentSession) => ActionResult
  onSetPinned?: (session: PaletteCurrentSession, pinned: boolean) => ActionResult
  onError: (message: string) => void
}
type PaletteRow = {
  key: string; label: string; description?: string; snippet?: string; shortcut?: string; keywords?: string
  Icon: LucideIcon; disabled?: boolean; keepOpen?: boolean
  run: () => ActionResult; allowed: () => boolean
}
type Latest = React.MutableRefObject<CommandPaletteProps>
const sessionKey = (session: PaletteSession) => session.key ?? `${session.source ?? session.source_format ?? "native"}:${session.id}`
const sessionAvailable = (session: PaletteSession) => !session.deleted && !session.deleting && session.status !== "deleting" && session.capabilities?.read !== false
const mutableSession = (session?: PaletteCurrentSession | null) => Boolean(session && sessionAvailable(session) && !session.read_only && !session.parent_session_id)
const errorMessage = (error: unknown) => error instanceof Error ? error.message : "The command could not be completed."

/** Mount once with controlled open state; routing, writes and search stay in the parent. */
export function CommandPalette(props: CommandPaletteProps) {
  const { open, enabled = true, registerShortcut = true, scopeKey } = props
  const latest = useRef(props)
  latest.current = props
  const mounted = useRef(false), opener = useRef<HTMLElement | null>(null), previouslyOpen = useRef(false)
  const previousScope = useRef(scopeKey)
  if (enabled && open && !previouslyOpen.current) opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
  previouslyOpen.current = enabled && open
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  useEffect(() => {
    const changed = previousScope.current !== scopeKey
    previousScope.current = scopeKey
    if (open && (changed || !enabled)) latest.current.onOpenChange(false)
  }, [scopeKey, enabled, open])
  useEffect(() => {
    if (!registerShortcut) return
    const shortcut = (event: KeyboardEvent) => {
      const current = latest.current
      if (event.defaultPrevented || event.isComposing || event.repeat || event.altKey || event.shiftKey || current.enabled === false) return
      if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== "k") return
      event.preventDefault()
      // Preserve drafts in other dialogs, matching the existing product palette.
      if (!current.open && document.querySelector('[data-slot="dialog-content"][data-state="open"]:not(.native-command-palette)')) return
      current.onOpenChange(!current.open)
    }
    window.addEventListener("keydown", shortcut, true)
    return () => window.removeEventListener("keydown", shortcut, true)
  }, [registerShortcut])
  return <Dialog open={enabled && open} onOpenChange={props.onOpenChange}>
    {enabled && open && <PaletteContents key={scopeKey} {...props} latest={latest} opener={opener} isCurrent={() => mounted.current && latest.current.enabled !== false && latest.current.scopeKey === scopeKey} />}
  </Dialog>
}

function PaletteContents(props: CommandPaletteProps & {
  latest: Latest; opener: React.MutableRefObject<HTMLElement | null>; isCurrent: () => boolean
}) {
  const { latest, opener, isCurrent, scopeKey } = props
  const [mode, setMode] = useState<PaletteMode>(props.initialMode ?? "commands")
  const [input, setInput] = useState(""), [query, setQuery] = useState("")
  const [activeKey, setActiveKey] = useState<string | null>(null), [scrollTick, setScrollTick] = useState(0)
  const [retry, setRetry] = useState(0)
  const [result, setResult] = useState<{ query: string; sessions: readonly PaletteSession[]; loading: boolean; failed: boolean; hasMore: boolean; notice?: string }>({
    query: "", sessions: props.recentSessions ?? [], loading: true, failed: false, hasMore: false,
  })
  const inputRef = useRef<HTMLInputElement>(null), composing = useRef(false)
  const pending = useRef<PaletteRow | null>(null), scrollRequested = useRef(false)
  const generation = useRef(0)
  const prefix = useId(), listId = `${prefix}-results`, scopeId = `${prefix}-scope`
  const modifier = /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘" : "Ctrl"
  useEffect(() => {
    const version = ++generation.current, controller = new AbortController(), term = query.trim()
    setResult({ query: term, sessions: term ? [] : latest.current.recentSessions ?? [], loading: true, failed: false, hasMore: false })
    const timer = setTimeout(async () => {
      try {
        const found = await latest.current.search(term, { signal: controller.signal, limit: term ? 20 : 6 })
        if (!controller.signal.aborted && version === generation.current && isCurrent()) {
          if (!Array.isArray(found.sessions)) throw new Error("Invalid session search response.")
          setResult({ query: term, sessions: found.sessions, loading: false, failed: false, hasMore: Boolean(found.has_more), notice: found.notice })
        }
      } catch {
        if (!controller.signal.aborted && version === generation.current && isCurrent()) setResult({ query: term, sessions: [], loading: false, failed: true, hasMore: false })
      }
    }, term ? 200 : 0)
    return () => { controller.abort(); clearTimeout(timer); ++generation.current }
    // Callback/inventory identity changes from sidebar polling must not reset the palette.
  }, [query, scopeKey, retry])

  function setQueryValue(value: string) { setInput(value); setQuery(value.trim()); setActiveKey(null) }
  function switchMode(next: PaletteMode) { setMode(next); setActiveKey(null); inputRef.current?.focus() }
  const enabled = () => isCurrent()
  const currentIs = (session: PaletteCurrentSession) => {
    const now = latest.current.currentSession
    return Boolean(isCurrent() && now && sessionKey(now) === sessionKey(session) && sessionAvailable(now))
  }
  const canMutate = (session: PaletteCurrentSession, capability: "rename" | "move" | "pin") => {
    const now = latest.current.currentSession
    return Boolean(currentIs(session) && mutableSession(now) && !latest.current.busy && now?.capabilities?.[capability] === true)
  }
  // IDs, labels, ordering and the sessions-only mode mirror app/static/command-palette.js.
  const commands: PaletteRow[] = [
    { key: "command:new", label: "Start session with a prompt…", keywords: "new session new chat", Icon: Plus, shortcut: `${modifier} ⇧ O`, run: () => latest.current.onNewSession(), allowed: enabled },
    { key: "command:search", label: "Search sessions…", Icon: Search, keepOpen: true, run: () => switchMode("sessions"), allowed: enabled },
  ]
  const current = props.currentSession
  if (current && sessionAvailable(current)) {
    if (props.onCopySessionLink) commands.push({ key: "command:copy", label: "Copy session link", Icon: Copy, run: () => latest.current.onCopySessionLink?.(current), allowed: () => currentIs(current) && Boolean(latest.current.onCopySessionLink) })
    if (mutableSession(current)) {
      if (current.capabilities?.rename && props.onRenameSession) commands.push({ key: "command:rename", label: "Rename session…", Icon: Pencil, disabled: props.busy, run: () => latest.current.onRenameSession?.(current), allowed: () => canMutate(current, "rename") && Boolean(latest.current.onRenameSession) })
      if (current.capabilities?.move && props.onMoveSession) commands.push({ key: "command:move", label: "Move to folder…", Icon: FolderPlus, disabled: props.busy, run: () => latest.current.onMoveSession?.(current), allowed: () => canMutate(current, "move") && Boolean(latest.current.onMoveSession) })
      if (current.capabilities?.pin && props.onSetPinned) commands.push({ key: "command:pin", label: current.pinned ? "Unpin session" : "Pin session", Icon: Pin, disabled: props.busy, run: () => latest.current.onSetPinned?.(current, !current.pinned), allowed: () => canMutate(current, "pin") && Boolean(latest.current.onSetPinned) })
    }
  }
  if (props.onNewFolder) commands.push({ key: "command:folder", label: "New folder…", Icon: FolderPlus, disabled: props.busy, run: () => latest.current.onNewFolder?.(), allowed: () => isCurrent() && !latest.current.busy && Boolean(latest.current.onNewFolder) })
  commands.push(
    { key: "command:sidebar", label: "Toggle sidebar", Icon: PanelLeft, run: () => latest.current.onToggleSidebar(), allowed: enabled },
    { key: "command:settings", label: "Settings", Icon: Settings, run: () => latest.current.onOpenSettings("overview"), allowed: enabled },
  )
  if (query) for (const destination of props.destinations ?? []) {
    if (destination.available === false || (destination.adminOnly && props.role !== "admin")) continue
    commands.push({
      key: `settings:${destination.page}`, label: destination.label, description: destination.description, Icon: LayoutGrid,
      run: () => latest.current.onOpenSettings(destination.page),
      allowed: () => isCurrent() && Boolean(latest.current.destinations?.some(item => item.page === destination.page && item.available !== false && (!item.adminOnly || latest.current.role === "admin"))),
    })
  }
  const needle = query.toLocaleLowerCase()
  const actions = mode === "commands" ? commands.filter(row => !query || `${row.label} ${row.description ?? ""} ${row.keywords ?? ""}`.toLocaleLowerCase().includes(needle)) : []
  const seen = new Set<string>()
  const sessions = (result.query === query ? result.sessions : []).filter(session => {
    const key = sessionKey(session)
    if (!sessionAvailable(session) || seen.has(key)) return false
    seen.add(key); return true
  }).slice(0, query ? 50 : 6)
  const sessionRows: PaletteRow[] = sessions.map(session => {
    const snippet = session.match?.snippet ?? session.snippet ?? (
      !session.search_query || session.search_query.trim().toLocaleLowerCase() === needle ? session.search_snippet : ""
    )
    return {
      key: `session:${sessionKey(session)}`, label: session.title || "Untitled session", Icon: MessageSquare,
      description: session.description ?? [session.parent_title, session.status, session.read_only ? "Read-only history" : ""].filter(Boolean).join(" · "),
      snippet: query ? snippet : undefined,
      run: () => latest.current.onOpenSession(session), allowed: () => isCurrent() && sessionAvailable(session),
    }
  })
  const rows = [...actions, ...sessionRows]
  const previousIndex = rows.findIndex(row => row.key === activeKey && !row.disabled)
  const activeIndex = previousIndex >= 0 ? previousIndex : rows.findIndex(row => !row.disabled)
  const activeId = activeIndex < 0 ? undefined : `${prefix}-option-${activeIndex}`
  useEffect(() => {
    if (scrollRequested.current && activeId) document.getElementById(activeId)?.scrollIntoView({ block: "nearest" })
    scrollRequested.current = false
  }, [scrollTick, activeId])
  function selectNext(delta: number) {
    if (!rows.length) return
    for (let step = 1; step <= rows.length; step++) {
      const index = (Math.max(0, activeIndex) + delta * step + rows.length * 2) % rows.length
      if (!rows[index].disabled) { setActiveKey(rows[index].key); scrollRequested.current = true; setScrollTick(value => value + 1); return }
    }
  }
  function activate(row?: PaletteRow) {
    if (!row || row.disabled || pending.current || !row.allowed()) return
    if (row.keepOpen) { void row.run(); return }
    pending.current = row
    props.onOpenChange(false)
  }
  function group(label: string, items: PaletteRow[], start: number) {
    return items.length > 0 && <div role="group" aria-label={label}><p className="palette-group-title" aria-hidden="true">{label}</p>{items.map((row, index) => {
      const position = start + index
      return <div key={row.key} id={`${prefix}-option-${position}`} role="option" aria-selected={position === activeIndex} aria-disabled={row.disabled || undefined} data-palette-index={position} className="palette-option"
        onPointerMove={() => { if (!row.disabled) setActiveKey(row.key) }} onPointerDown={event => { if (event.pointerType === "mouse") event.preventDefault() }} onClick={() => activate(row)}>
        <span className="palette-icon" aria-hidden="true"><row.Icon size={20} /></span><span className="palette-copy"><span className="palette-label">{row.label}</span>
          {row.description && <span className="palette-description">{row.description}</span>}{row.snippet && <span className="palette-snippet" title={row.snippet}>{row.snippet}</span>}
          {row.disabled && <span className="palette-description">Wait for the current change to finish.</span>}</span>{row.shortcut && <kbd>{row.shortcut}</kbd>}
      </div>
    })}</div>
  }
  const loading = result.query !== query || result.loading
  const failed = result.query === query && result.failed
  const more = result.hasMore || (query ? result.sessions.length > 50 : result.sessions.length > 6)
  return <DialogContent id={COMMAND_PALETTE_ID} className="native-command-palette" showCloseButton={false}
    onOpenAutoFocus={event => { event.preventDefault(); inputRef.current?.focus() }}
    onCloseAutoFocus={event => {
      event.preventDefault()
      const action = pending.current
      pending.current = null
      if (!isCurrent()) return
      if (!document.querySelector('[data-slot="dialog-content"][data-state="open"]:not(.native-command-palette)') && opener.current?.isConnected) opener.current.focus({ preventScroll: true })
      if (action) queueMicrotask(() => {
        if (!isCurrent() || !action.allowed()) return
        void Promise.resolve().then(action.run).catch(error => { if (isCurrent()) latest.current.onError(errorMessage(error)) })
      })
    }}
    onEscapeKeyDown={event => { event.stopPropagation(); if (event.isComposing || composing.current) event.preventDefault() }}
    onKeyDown={event => {
      if (event.nativeEvent.isComposing || composing.current) return
      if ((event.metaKey || event.ctrlKey) && !event.altKey && event.shiftKey && event.key.toLowerCase() === "o" && !event.repeat) {
        event.preventDefault(); activate(commands[0]); return
      }
      if (event.target !== inputRef.current) return
      if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); selectNext(event.key === "ArrowDown" ? 1 : -1) }
      if (event.key === "Enter") { event.preventDefault(); activate(rows[activeIndex]) }
    }}>
    <DialogTitle className="visually-hidden">Commands and session search</DialogTitle>
    <DialogDescription className="visually-hidden">Search commands, session titles, and saved messages. Use arrow keys to choose a result.</DialogDescription>
    <div className="palette-search-bar">
      {mode === "sessions" && <Button type="button" size="icon-sm" variant="ghost" aria-label="Back to commands" onClick={() => switchMode("commands")}><ArrowLeft size={18} /></Button>}
      <Search className="palette-search-icon" size={20} aria-hidden="true" />
      <Input ref={inputRef} role="combobox" aria-label="Search commands and sessions" aria-autocomplete="list" aria-expanded="true" aria-controls={listId} aria-activedescendant={activeId} aria-describedby={scopeId}
        autoComplete="off" autoCapitalize="none" spellCheck={false} maxLength={200} value={input} placeholder={mode === "sessions" ? "Search titles and messages…" : "Type a command or search…"}
        onCompositionStart={() => { composing.current = true }}
        onCompositionEnd={event => { composing.current = false; setQueryValue(event.currentTarget.value) }}
        onChange={event => { if (composing.current) setInput(event.target.value); else setQueryValue(event.target.value) }} />
      <Button className="palette-close" type="button" variant="ghost" aria-label="Close command palette" onClick={() => props.onOpenChange(false)}><kbd>esc</kbd></Button>
    </div>
    <div className="palette-body"><p id={scopeId} className="palette-scope">{props.scopeLabel ?? "Available sessions"} · Search titles and messages</p>
      <div id={listId} role="listbox" aria-label="Commands and sessions" aria-busy={loading}>{group("Actions", actions, 0)}{group(query ? "Matching sessions" : "Recent sessions", sessionRows, actions.length)}</div>
      <div className="palette-status" role="status" aria-live="polite">
        {loading ? query ? "Searching titles and messages…" : "Loading recent sessions…" : failed ? <>Sessions could not be loaded. <Button type="button" variant="outline" size="sm" onClick={() => setRetry(value => value + 1)}>Retry search</Button></>
          : !rows.length ? <>{query ? "No matching sessions or commands." : "No sessions yet."}{query && <Button type="button" variant="outline" size="sm" onClick={() => { setQueryValue(""); inputRef.current?.focus() }}>Clear search</Button>}</>
            : <>{result.notice && <p>{result.notice}</p>}{query && more && <p>Showing the first results. Refine your search to narrow the list.</p>}</>}
      </div>
    </div>
    <div className="palette-footer"><span><kbd>↑</kbd><kbd>↓</kbd>Navigate</span><span><kbd>↵</kbd>Select</span><span><kbd>esc</kbd>Close</span></div>
  </DialogContent>
}
