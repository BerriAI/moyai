import React, { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { Archive, ArchiveRestore, ChevronDown, Clock3, Folder, FolderPlus, GitMerge, GitPullRequest, GitPullRequestClosed, GitPullRequestDraft, PanelRightOpen, Pencil, Pin, PinOff, Trash2, Users } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { ActionItem, ActionMenu, SessionMenu, sessionHash, sessionIsReadOnly, sessionKey, useSessionActions } from "./session-actions"
import type { SessionFolder, SidebarSession } from "./session-actions"
import { currentDragSession, movableSession, SESSION_DRAG_TYPE, sessionCreatedByMe, sessionDrop, sessionPlacement } from "./sidebar-drag"
import type { SessionDrag, SessionDropTarget, SidebarDragContext } from "./sidebar-drag"
import { sessionTree } from "./session-tree"
import slackLogo from "./assets/slack-logo.svg"
import "./sidebar-layout.css"
export type { SessionFolder, SidebarSession } from "./session-actions"
export type SessionListView = "active" | "archived" | "deleted"
type PullRequestSummary = NonNullable<SidebarSession["pull_requests"]>[number]
type OpenPullRequest = (sessionId: string, url: string) => void

const pullRequestStates = {
  ready: { label: "PR is ready", plural: "Ready pull requests", Icon: GitPullRequest },
  draft: { label: "Draft", plural: "Draft pull requests", Icon: GitPullRequestDraft },
  merged: { label: "Merged", plural: "Merged pull requests", Icon: GitMerge },
  closed: { label: "Closed", plural: "Closed pull requests", Icon: GitPullRequestClosed },
}

function pullRequestState(pr: PullRequestSummary) {
  if (!pr.accessible || !pr.url || pr.state === "unknown") return null
  if (pr.state === "open") return pr.draft === false ? "ready" : pr.draft === true ? "draft" : null
  return pr.state === "merged" || pr.state === "closed" ? pr.state : null
}

function PullRequestIndicator({ session, state, requests, onOpen }: {
  session: SidebarSession; state: keyof typeof pullRequestStates; requests: PullRequestSummary[]; onOpen?: OpenPullRequest
}) {
  const [open, setOpen] = useState(false)
  const { label, plural, Icon } = pullRequestStates[state]
  const description = `${plural}: ${requests.length}. ${requests.map(pr => `${pr.repository}#${pr.number}: ${pr.title}`).join("; ")}`
  const content = <><span>{state === "ready" && requests.length > 1 ? "PRs are ready" : label}</span><Icon size={13} aria-hidden="true" /><span className="session-pr-count">{requests.length}</span></>
  const className = `session-pr-indicator session-pr-${state}`
  if (!onOpen) return <span className={className} title={description}><span className="visually-hidden">{description}</span><span className="session-pr-content" aria-hidden="true">{content}</span></span>
  if (requests.length === 1) return <Button type="button" variant="ghost" size="xs" className={className} title={description} aria-label={description} onClick={() => onOpen(session.id, requests[0].url)}>{content}</Button>
  return <Popover open={open} onOpenChange={setOpen}>
    <PopoverTrigger asChild><Button type="button" variant="ghost" size="xs" className={className} title={description} aria-label={description}>{content}</Button></PopoverTrigger>
    <PopoverContent className="native-session-menu sidebar-pr-menu" align="start" side="bottom" collisionPadding={12} aria-label={`${plural} for ${session.title}`}>
      <p>{plural}</p>
      {requests.map(pr => <Button key={pr.url} type="button" variant="ghost" onClick={() => { setOpen(false); onOpen(session.id, pr.url) }}>
        <Icon size={15} aria-hidden="true" /><span>{pr.title || `${pr.repository}#${pr.number}`}<small>{pr.repository}#{pr.number}</small></span>
      </Button>)}
    </PopoverContent>
  </Popover>
}

function PullRequestIndicators({ session, onOpen }: { session: SidebarSession; onOpen?: OpenPullRequest }) {
  const groups = new Map<keyof typeof pullRequestStates, PullRequestSummary[]>()
  const seen = new Set<string>()
  for (const pr of session.pull_requests ?? []) {
    const state = pullRequestState(pr)
    if (!state || seen.has(pr.url)) continue
    seen.add(pr.url)
    groups.set(state, [...(groups.get(state) ?? []), pr])
  }
  return [...groups].map(([state, requests]) => <PullRequestIndicator key={state} session={session} state={state} requests={requests} onOpen={onOpen} />)
}

function age(value?: string) {
  if (!value) return ""
  const minutes = Math.max(0, Math.floor((Date.now() - Date.parse(value)) / 60000))
  const days = Math.floor(minutes / 1440)
  return minutes < 1 ? "Just now" : minutes < 60 ? `${minutes} min ago` : minutes < 1440 ? `${Math.floor(minutes / 60)} hr ago` : `${days} ${days === 1 ? "day" : "days"} ago`
}

export function SessionSidebar({ sessions, folders, selected, userId, view, onViewChange, onOpenPullRequest, onOpenSideChat, hasMore, loading, notice, onLoadMore }: {
  sessions: SidebarSession[]; folders: SessionFolder[]; selected: string; userId: string
  view: SessionListView; onViewChange: (view: SessionListView) => void
  onOpenPullRequest?: OpenPullRequest
  onOpenSideChat?: (session: SidebarSession) => void
  hasMore?: boolean; loading?: boolean; notice?: string; onLoadMore?: () => void
}) {
  const { openEditor, edit, busy, userId: actionsUserId } = useSessionActions()
  const activeDrag = useRef<SessionDrag | null>(null)
  const current = useRef<SidebarDragContext>({ userId, actionsUserId, view, busy, sessions, folders })
  current.current = { userId, actionsUserId, view, busy, sessions, folders }
  const [dragging, setDragging] = useState<string | null>(null)
  const [dropTarget, setDropTarget] = useState<string | null>(null)
  const [dropNotice, setDropNotice] = useState("")
  const clearDrag = useCallback(() => {
    activeDrag.current = null; setDragging(null); setDropTarget(null)
  }, [])
  useEffect(() => {
    const cancel = () => clearDrag()
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") clearDrag() }
    const hidden = () => { if (document.hidden) clearDrag() }
    window.addEventListener("dragend", cancel)
    window.addEventListener("drop", cancel)
    window.addEventListener("blur", cancel)
    window.addEventListener("keydown", escape)
    window.addEventListener("moyai:access-changed", cancel)
    window.addEventListener("moyai:signed-out", cancel)
    document.addEventListener("visibilitychange", hidden)
    return () => {
      activeDrag.current = null
      window.removeEventListener("dragend", cancel)
      window.removeEventListener("drop", cancel)
      window.removeEventListener("blur", cancel)
      window.removeEventListener("keydown", escape)
      window.removeEventListener("moyai:access-changed", cancel)
      window.removeEventListener("moyai:signed-out", cancel)
      document.removeEventListener("visibilitychange", hidden)
    }
  }, [clearDrag])
  useEffect(clearDrag, [userId, actionsUserId, view, busy, clearDrag])
  useEffect(() => {
    if (activeDrag.current && !currentDragSession(activeDrag.current, current.current)) clearDrag()
  }, [sessions, clearDrag])
  useEffect(() => {
    if (dropTarget?.startsWith("folder:") && !folders.some(folder => `folder:${folder.id}` === dropTarget)) setDropTarget(null)
  }, [folders, dropTarget])
  const [closed, setClosed] = useState<string[]>(() => {
    try {
      const value: unknown = JSON.parse(localStorage.getItem(`moyai-native-folders:${userId}`) ?? "null")
      // Preserve existing folder choices while adopting the new Participated
      // default once. Subsequent explicit expansion remains the user's choice.
      if (Array.isArray(value)) return [...new Set([...value.filter(item => typeof item === "string"), "participated"])]
      if (value && typeof value === "object" && "version" in value && value.version === 2 && "closed" in value && Array.isArray(value.closed)) {
        return value.closed.filter(item => typeof item === "string")
      }
    } catch {}
    return ["participated"]
  })
  const toggle = (key: string) => setClosed(previous => {
    const next = previous.includes(key) ? previous.filter(item => item !== key) : [...previous, key]
    try { localStorage.setItem(`moyai-native-folders:${userId}`, JSON.stringify({ version: 2, closed: next })) } catch {}
    return next
  })
  const tree = useMemo(() => sessionTree(sessions), [sessions])
  const [collapsedAgents, setCollapsedAgents] = useState<string[]>(() => {
    try {
      const saved: unknown = JSON.parse(localStorage.getItem(`moyai-subagents:${userId}`) ?? "[]")
      if (Array.isArray(saved)) return saved.filter(value => typeof value === "string")
    } catch {}
    return []
  })
  const saveCollapsedAgents = (next: string[]) => {
    try { localStorage.setItem(`moyai-subagents:${userId}`, JSON.stringify(next)) } catch {}
    return next
  }
  const ancestry: string[] = []
  let selectedParent = tree.parents.get(`native:${selected}`)
  while (selectedParent) {
    ancestry.push(sessionKey(selectedParent)); selectedParent = tree.parents.get(sessionKey(selectedParent))
  }
  const ancestryKey = JSON.stringify(ancestry)
  useEffect(() => {
    const keys = JSON.parse(ancestryKey) as string[]
    if (keys.length) setCollapsedAgents(previous => saveCollapsedAgents(previous.filter(key => !keys.includes(key))))
  }, [selected, ancestryKey])
  const roots = view === "deleted" ? sessions : tree.roots
  const nativeRows = roots.filter(row => !sessionIsReadOnly(row))
  const placements = new Map(nativeRows.map(row => [row, sessionPlacement(row, folders, userId)]))
  const unfiled = roots.filter(row => sessionIsReadOnly(row)
    ? !row.pinned && !row.folder : ["recent", "participated"].includes(placements.get(row)!.kind))
  const pinned = roots.filter(row => sessionIsReadOnly(row) ? row.pinned : placements.get(row)!.kind === "pinned")
  const legacyFolders = [...new Map(roots.filter(row => sessionIsReadOnly(row) && row.folder).map(row =>
    [`${row.source ?? "native"}:${row.folder_id ?? row.folder}`, { key: `${row.source ?? "native"}:${row.folder_id ?? row.folder}`, name: row.folder! }],
  )).values()]
  function startDrag(event: React.DragEvent<HTMLDivElement>, row: SidebarSession) {
    if (busy || view === "deleted" || userId !== actionsUserId || !movableSession(row) ||
      (event.target instanceof Element && event.target.closest("button, input, textarea, select, [role='menuitem']"))) {
      event.preventDefault(); return
    }
    const drag: SessionDrag = { version: 1, userId, sessionId: row.id, nonce: crypto.randomUUID() }
    event.dataTransfer.clearData()
    event.dataTransfer.setData(SESSION_DRAG_TYPE, JSON.stringify(drag))
    event.dataTransfer.effectAllowed = "move"
    activeDrag.current = drag
    setDragging(row.id); setDropTarget(null); setDropNotice("")
  }
  function accepts(event: React.DragEvent, target: SessionDropTarget): boolean {
    return event.dataTransfer.types.includes(SESSION_DRAG_TYPE) &&
      Boolean(currentDragSession(activeDrag.current, current.current)) &&
      (target.kind !== "folder" || current.current.folders.some(folder => folder.id === target.folderId))
  }
  function dragOver(event: React.DragEvent, target: SessionDropTarget, key: string) {
    // Payload data is protected until drop; the in-memory source and MIME type
    // must both belong to this mounted sidebar before showing a drop affordance.
    if (!accepts(event, target)) return
    event.preventDefault(); event.stopPropagation()
    event.dataTransfer.dropEffect = "move"
    setDropTarget(key)
  }
  function leaveDrop(event: React.DragEvent<HTMLElement>, key: string) {
    if (event.relatedTarget instanceof Node && event.currentTarget.contains(event.relatedTarget)) return
    setDropTarget(previous => previous === key ? null : previous)
  }
  function drop(event: React.DragEvent, target: SessionDropTarget) {
    if (!accepts(event, target)) return
    event.preventDefault(); event.stopPropagation()
    const move = sessionDrop(event.dataTransfer.getData(SESSION_DRAG_TYPE), activeDrag.current, current.current, target)
    clearDrag()
    if (!move) { setDropNotice("The session or folder changed. Try moving it again."); return }
    setClosed(previous => {
      const next = previous.filter(key => key !== move.expand)
      try { localStorage.setItem(`moyai-native-folders:${userId}`, JSON.stringify({ version: 2, closed: next })) } catch {}
      return next
    })
    if (!move.changed) { setDropNotice(move.message); return }
    setDropNotice("")
    // SessionActions owns the save, refresh and truthful success/error toast.
    // Rows do not move optimistically, so a failed PATCH leaves their placement intact.
    void edit(move.row, move.body, move.message)
  }
  function onListBackground(event: React.DragEvent<HTMLElement>): boolean {
    return !(event.target instanceof Element && event.target.closest(".session-group, button"))
  }
  function rows(items: SidebarSession[], depth = 0): React.ReactNode {
    return items.map(row => {
      const readOnly = sessionIsReadOnly(row)
      const active = row.source === "legacy" ? selected === `run=${row.id}` || selected === sessionHash(row) : selected === row.id
      const key = sessionKey(row), descendants = view === "deleted" ? [] : tree.children.get(key) ?? []
      const expanded = !collapsedAgents.includes(key)
      const openAside = !readOnly && !row.deleted && row.parent_session_id && onOpenSideChat
        ? () => onOpenSideChat(row) : undefined
      return <div key={key} className="session-branch">
      <div data-session-key={key} draggable={!busy && view !== "deleted" && userId === actionsUserId && movableSession(row)}
        onDragStart={event => startDrag(event, row)} onDragEnd={clearDrag}
        className={`session-row ${depth ? "session-child" : ""} ${descendants.length ? "session-parent" : ""} ${active ? "selected" : ""} ${row.unread ? "unread" : ""} ${dragging === row.id ? "session-dragging" : ""}`}>
      {descendants.length > 0 && <Button type="button" variant="ghost" size="icon-sm" className="session-children-toggle"
        aria-label={`${expanded ? "Collapse" : "Expand"} subagents for ${row.title}`} aria-expanded={expanded}
        aria-controls={`subagents-${encodeURIComponent(key)}`}
        onClick={() => setCollapsedAgents(previous => saveCollapsedAgents(expanded ? [...previous, key] : previous.filter(value => value !== key)))}>
        <ChevronDown className={expanded ? "" : "closed"} size={12} />
      </Button>}
      <a draggable={false} href={row.deleted || row.deleting ? undefined : `#${sessionHash(row)}`} title={row.title} aria-current={active && !row.deleted && !row.deleting ? "page" : undefined}>
        <span className={`session-indicator ${!readOnly && ["running", "queued", "saving"].includes(row.status) ? "working" : !readOnly && row.status === "waiting" ? "waiting" : ""}`} aria-label={readOnly ? "Read-only history" : row.unread ? "Unread" : row.status} />
        <span className="session-title">{row.title}</span>
      </a>
      <div className="session-row-actions">
        {openAside && <Button type="button" variant="ghost" size="icon-sm" aria-label={`Open ${row.title} in side chat`} title="Open in side chat" onClick={openAside}><PanelRightOpen /></Button>}
        {!readOnly && !row.deleted && depth === 0 && <>
          <Button type="button" variant="ghost" size="icon-sm" disabled={busy} aria-label={`${row.pinned ? "Unpin" : "Pin"} ${row.title}`} aria-pressed={Boolean(row.pinned)} title={row.pinned ? "Unpin session" : "Pin session"}
            onClick={() => void edit(row, { pinned: !row.pinned }, row.pinned ? "Session unpinned." : "Session pinned.")}>{row.pinned ? <PinOff /> : <Pin />}</Button>
          <Button type="button" variant="ghost" size="icon-sm" disabled={busy || row.owner_id !== userId} aria-label={`${row.archived ? "Restore from archive" : "Archive"} ${row.title}`}
            title={row.owner_id !== userId ? "Only the session owner can change its archive status" : row.archived ? "Restore from archive" : "Archive session"} onClick={() => openEditor({ action: "archive", session: row })}>{row.archived ? <ArchiveRestore /> : <Archive />}</Button>
        </>}
        <SessionMenu session={row} onOpenSideChat={openAside} />
      </div>
      <div className="session-row-meta">
        {!readOnly && row.pinned && placements.get(row)?.kind === "folder" && <Pin size={12} aria-label="Pinned session" />}
        <span className="session-meta-status">{row.deleted ? "Deleted" : readOnly ? "Read-only history" : row.status === "waiting" ? "Needs a response" : ["running", "queued", "saving"].includes(row.status) ? row.status : age(row.updated_at)}</span>
        {!readOnly && <PullRequestIndicators session={row} onOpen={row.deleted ? undefined : onOpenPullRequest} />}
        {row.surface === "slack" && <img className="session-slack-logo" src={slackLogo} width={14} height={14} alt="Slack conversation" title="Slack conversation" />}
      </div>
    </div>
    {descendants.length > 0 && <div id={`subagents-${encodeURIComponent(key)}`} className="session-children" role="group" aria-label={`Subagents of ${row.title}`} hidden={!expanded}>{rows(descendants, depth + 1)}</div>}
    </div>})
  }
  function section(key: string, label: string, icon: React.ReactNode, items: SidebarSession[], folder?: SessionFolder) {
    const expanded = !closed.includes(key)
    const target: SessionDropTarget | null = folder ? { kind: "folder", folderId: folder.id }
      : ["recent", "participated"].includes(key) ? { kind: "unfiled" } : null
    const dropKey = folder ? `folder:${folder.id}` : key
    return <section key={key} className={`session-group ${dropTarget === dropKey ? "session-drop-target" : ""}`}
      onDragOver={event => { if (target) dragOver(event, target, dropKey) }}
      onDragLeave={event => leaveDrop(event, dropKey)}
      onDrop={event => { if (target) drop(event, target) }}>
      <div className="session-group-heading"><Button variant="ghost" className="session-group-toggle" aria-expanded={expanded} aria-controls={`sessions-${encodeURIComponent(key)}`} onClick={() => toggle(key)}>{icon}<span>{label}</span><small>{items.length}</small><ChevronDown className={expanded ? "" : "closed"} size={12} /></Button>
        {folder && <ActionMenu label={`Folder actions for ${folder.name}`}>{close => <>
          <ActionItem disabled={busy} onClick={() => { close(); openEditor({ action: "folder-rename", folder }) }}><Pencil />Rename folder</ActionItem>
          <ActionItem disabled={busy} onClick={() => { close(); openEditor({ action: "folder-remove", folder }) }}><Trash2 />Remove folder</ActionItem>
        </>}</ActionMenu>}
      </div>
      <div id={`sessions-${encodeURIComponent(key)}`} hidden={!expanded}>{rows(items)}{!items.length && <p className="folder-empty">{loading && !sessions.length ? "Loading sessions…" : folder ? "Drag sessions here, or use their ⋯ menu." : label === "Participated" ? "Shared sessions you’ve contributed to." : "No sessions here yet."}</p>}</div>
    </section>
  }
  return <>
    <div className="rail-caption"><span>{view === "archived" ? "Archived" : view === "deleted" ? "Deleted" : "Sessions"} <small>{sessions.length}{hasMore ? "+" : ""}</small></span><div>
      <Button size="icon-sm" variant="ghost" disabled={busy} aria-label="New folder" onClick={() => openEditor({ action: "folder-create" })}><FolderPlus /></Button>
      <Button size="icon-sm" variant="ghost" aria-pressed={view === "archived"} aria-label={view === "archived" ? "Show active conversations" : "Show archived conversations"} onClick={() => onViewChange(view === "archived" ? "active" : "archived")}><Archive /></Button>
      <Button size="icon-sm" variant="ghost" aria-pressed={view === "deleted"} aria-label={view === "deleted" ? "Show active conversations" : "Show deleted conversations"} onClick={() => onViewChange(view === "deleted" ? "active" : "deleted")}><Trash2 /></Button>
    </div></div>
    {dropNotice && <p className="sidebar-drop-notice" role="status">{dropNotice}</p>}
    <nav aria-label="Conversations" className={`session-list ${dropTarget === "unfiled" ? "session-drop-target" : ""}`}
      onDragOver={event => { if (onListBackground(event)) dragOver(event, { kind: "unfiled" }, "unfiled") }}
      onDragLeave={event => leaveDrop(event, "unfiled")}
      onDrop={event => { if (onListBackground(event)) drop(event, { kind: "unfiled" }) }}>
      {view === "deleted" ? <>{rows(sessions)}{!sessions.length && <p className="empty-rail muted">No deleted sessions.</p>}</> : <>
        {pinned.length > 0 && section("pinned", "Pinned", <Pin size={15} />, pinned)}
        {folders.map(folder => section(folder.id, folder.name, <Folder size={15} />, nativeRows.filter(row =>
          placements.get(row)?.key === folder.id).sort((a, b) => Number(Boolean(b.pinned)) - Number(Boolean(a.pinned))), folder))}
        {legacyFolders.map(folder => section(`history-folder:${folder.key}`, folder.name, <Folder size={15} />, sessions.filter(row =>
          sessionIsReadOnly(row) && !row.pinned && `${row.source ?? "native"}:${row.folder_id ?? row.folder}` === folder.key)))}
        {section("participated", "Participated", <Users size={15} />, unfiled.filter(row => row.participated && !sessionCreatedByMe(row, userId)))}
        {section("recent", "Recent", <Clock3 size={15} />, unfiled.filter(row => sessionCreatedByMe(row, userId)))}
      </>}
      {notice && <p className="empty-rail muted" role="status">{notice}</p>}
      {hasMore && onLoadMore && <Button className="sidebar-load-more" variant="ghost" disabled={loading} onClick={onLoadMore}>{loading ? "Loading…" : "Show more sessions"}</Button>}
    </nav>
  </>
}
