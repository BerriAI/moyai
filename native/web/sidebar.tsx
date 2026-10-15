import React, { useState } from "react"
import { Archive, ArchiveRestore, ChevronDown, Clock3, Folder, FolderPlus, GitMerge, GitPullRequest, GitPullRequestClosed, GitPullRequestDraft, Pencil, Pin, PinOff, Trash2, Users } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { ActionItem, ActionMenu, SessionMenu, useSessionActions } from "./session-actions"
import type { SessionFolder, SidebarSession } from "./session-actions"
import slackLogo from "../../app/static/slack-logo.svg"
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

export function SessionSidebar({ sessions, folders, selected, userId, search, view, onViewChange, onOpenPullRequest }: {
  sessions: SidebarSession[]; folders: SessionFolder[]; selected: string; userId: string; search: string
  view: SessionListView; onViewChange: (view: SessionListView) => void
  onOpenPullRequest?: OpenPullRequest
}) {
  const { openEditor, edit, busy } = useSessionActions()
  const [closed, setClosed] = useState<string[]>(() => {
    try { const value: unknown = JSON.parse(localStorage.getItem(`moyai-native-folders:${userId}`) ?? "[]"); return Array.isArray(value) ? value.filter(item => typeof item === "string") : [] }
    catch { return [] }
  })
  const toggle = (key: string) => setClosed(previous => {
    const next = previous.includes(key) ? previous.filter(item => item !== key) : [...previous, key]
    try { localStorage.setItem(`moyai-native-folders:${userId}`, JSON.stringify(next)) } catch {}
    return next
  })
  const matching = sessions.filter(row => `${row.title} ${row.folder ?? ""}`.toLowerCase().includes(search.toLowerCase()))
  const unfiled = matching.filter(row => !row.pinned && !row.folder)
  function rows(items: SidebarSession[]) {
    return items.map(row => <div key={row.id} className={`session-row ${selected === row.id ? "selected" : ""} ${row.unread ? "unread" : ""}`}>
      <a href={row.deleted ? undefined : `#${row.id}`} title={row.title} aria-current={selected === row.id && !row.deleted ? "page" : undefined}>
        <span className={`session-indicator ${["running", "queued", "saving"].includes(row.status) ? "working" : row.status === "waiting" ? "waiting" : ""}`} aria-label={row.unread ? "Unread" : row.status} />
        <span className="session-title">{row.title}</span>
      </a>
      <div className="session-row-actions">
        {!row.deleted && <>
          <Button type="button" variant="ghost" size="icon-sm" disabled={busy} aria-label={`${row.pinned ? "Unpin" : "Pin"} ${row.title}`} aria-pressed={Boolean(row.pinned)} title={row.pinned ? "Unpin session" : "Pin session"}
            onClick={() => void edit(row, { pinned: !row.pinned }, row.pinned ? "Session unpinned." : "Session pinned.")}>{row.pinned ? <PinOff /> : <Pin />}</Button>
          <Button type="button" variant="ghost" size="icon-sm" disabled={busy || row.owner_id !== userId} aria-label={`${row.archived ? "Restore from archive" : "Archive"} ${row.title}`}
            title={row.owner_id !== userId ? "Only the session owner can change its archive status" : row.archived ? "Restore from archive" : "Archive session"} onClick={() => openEditor({ action: "archive", session: row })}>{row.archived ? <ArchiveRestore /> : <Archive />}</Button>
        </>}
        <SessionMenu session={row} />
      </div>
      <div className="session-row-meta">
        <span className="session-meta-status">{row.deleted ? "Deleted" : row.status === "waiting" ? "Needs a response" : ["running", "queued", "saving"].includes(row.status) ? row.status : age(row.updated_at)}</span>
        <PullRequestIndicators session={row} onOpen={row.deleted ? undefined : onOpenPullRequest} />
        {row.surface === "slack" && <img className="session-slack-logo" src={slackLogo} width={14} height={14} alt="Slack conversation" title="Slack conversation" />}
      </div>
    </div>)
  }
  function section(key: string, label: string, icon: React.ReactNode, items: SidebarSession[], folder?: SessionFolder) {
    const expanded = Boolean(search) || !closed.includes(key)
    return <section key={key} className="session-group">
      <div className="session-group-heading"><Button variant="ghost" className="session-group-toggle" aria-expanded={expanded} aria-controls={`sessions-${key}`} onClick={() => toggle(key)}>{icon}<span>{label}</span><small>{items.length}</small><ChevronDown className={expanded ? "" : "closed"} size={12} /></Button>
        {folder && <ActionMenu label={`Folder actions for ${folder.name}`}>{close => <>
          <ActionItem disabled={busy} onClick={() => { close(); openEditor({ action: "folder-rename", folder }) }}><Pencil />Rename folder</ActionItem>
          <ActionItem disabled={busy} onClick={() => { close(); openEditor({ action: "folder-remove", folder }) }}><Trash2 />Remove folder</ActionItem>
        </>}</ActionMenu>}
      </div>
      <div id={`sessions-${key}`} hidden={!expanded}>{rows(items)}{!items.length && <p className="folder-empty">{folder ? "Move sessions here using their ⋯ menu." : label === "Participated" ? "Shared sessions you’ve contributed to." : "No sessions here yet."}</p>}</div>
    </section>
  }
  return <>
    <div className="rail-caption"><span>{view === "archived" ? "Archived" : view === "deleted" ? "Deleted" : "Sessions"} <small>{matching.length}</small></span><div>
      <Button size="icon-sm" variant="ghost" disabled={busy} aria-label="New folder" onClick={() => openEditor({ action: "folder-create" })}><FolderPlus /></Button>
      <Button size="icon-sm" variant="ghost" aria-pressed={view === "archived"} aria-label={view === "archived" ? "Show active conversations" : "Show archived conversations"} onClick={() => onViewChange(view === "archived" ? "active" : "archived")}><Archive /></Button>
      <Button size="icon-sm" variant="ghost" aria-pressed={view === "deleted"} aria-label={view === "deleted" ? "Show active conversations" : "Show deleted conversations"} onClick={() => onViewChange(view === "deleted" ? "active" : "deleted")}><Trash2 /></Button>
    </div></div>
    <nav aria-label="Conversations" className="session-list">
      {view === "deleted" ? <>{rows(matching)}{!matching.length && <p className="empty-rail muted">No deleted sessions.</p>}</> : <>
        {matching.some(row => row.pinned) && section("pinned", "Pinned", <Pin size={15} />, matching.filter(row => row.pinned))}
        {folders.filter(folder => !search || folder.name.toLowerCase().includes(search.toLowerCase()) || matching.some(row => row.folder === folder.name))
          .map(folder => section(folder.id, folder.name, <Folder size={15} />, matching.filter(row => !row.pinned && row.folder === folder.name), folder))}
        {section("participated", "Participated", <Users size={15} />, unfiled.filter(row => row.participated))}
        {section("recent", "Recent", <Clock3 size={15} />, unfiled.filter(row => !row.participated))}
      </>}
      {search && !matching.length && <p className="empty-rail muted">No matching sessions. <Button size="sm" variant="ghost" onClick={() => window.dispatchEvent(new Event("moyai:clear-search"))}>Clear search</Button></p>}
    </nav>
  </>
}
