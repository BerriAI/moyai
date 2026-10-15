import React, { useState } from "react"
import { Archive, Check, ChevronDown, ChevronRight, Clock3, Copy, Folder, FolderPlus, Monitor, MoreHorizontal, Pencil, Pin, Users, Mail, Trash2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { api } from "./api"

export type SidebarSession = {
  id: string; title: string; owner_id: string; status: string; archived: boolean
  pinned?: boolean; folder?: string; participated?: boolean; unread?: boolean; updated_at?: string
}
export type SessionFolder = { id: string; name: string }
type Action = "rename" | "move" | "archive" | "folder-create" | "folder-rename" | "folder-remove"
type Editor = { action: Action; session?: SidebarSession; folder?: SessionFolder }

function Menu({ label, children }: { label: string; children: (close: () => void) => React.ReactNode }) {
  const [open, setOpen] = useState(false)
  return <Popover open={open} onOpenChange={setOpen}>
    <PopoverTrigger asChild><Button className="session-menu-trigger" variant="ghost" size="icon-sm" aria-label={label}><MoreHorizontal size={17} /></Button></PopoverTrigger>
    <PopoverContent className="native-session-menu" role="menu" aria-label={label} align="start" side={window.matchMedia("(max-width: 600px)").matches ? "bottom" : "right"} collisionPadding={12}
      onKeyDown={event => {
        if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return
        event.preventDefault()
        const items = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')]
        const index = items.indexOf(document.activeElement as HTMLButtonElement)
        items[event.key === "Home" ? 0 : event.key === "End" ? items.length - 1
          : (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length]?.focus()
      }}>{children(() => setOpen(false))}</PopoverContent>
  </Popover>
}
function Item({ children, onClick, disabled }: { children: React.ReactNode; onClick: () => void; disabled?: boolean }) {
  return <Button role="menuitem" variant="ghost" disabled={disabled} onClick={onClick}>{children}</Button>
}
function age(value?: string) {
  if (!value) return ""
  const minutes = Math.max(0, Math.floor((Date.now() - Date.parse(value)) / 60000))
  return minutes < 1 ? "Just now" : minutes < 60 ? `${minutes} min ago` : minutes < 1440 ? `${Math.floor(minutes / 60)} hr ago` : `${Math.floor(minutes / 1440)} days ago`
}

export function SessionSidebar({ sessions, folders, selected, userId, search, archived, refresh, onArchiveView, onOpenComputer }: {
  sessions: SidebarSession[]; folders: SessionFolder[]; selected: string; userId: string; search: string
  archived: boolean; refresh: () => Promise<void>; onArchiveView: () => void
  onOpenComputer: (sessionId: string) => void
}) {
  const [editor, setEditor] = useState<Editor | null>(null)
  const [name, setName] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [notice, setNotice] = useState("")
  const [closed, setClosed] = useState<string[]>(() => {
    try { const value: unknown = JSON.parse(localStorage.getItem(`moyai-native-folders:${userId}`) ?? "[]"); return Array.isArray(value) ? value.filter(item => typeof item === "string") : [] }
    catch { return [] }
  })
  const openEditor = (value: Editor) => { setError(""); setName(value.action === "folder-create" ? "" : value.folder?.name ?? value.session?.title ?? ""); setEditor(value) }
  async function mutate(action: () => Promise<unknown>, message: string) {
    if (busy) return false
    setBusy(true); setError(""); setNotice("")
    try { await action(); setEditor(null); await refresh(); setNotice(message); return true }
    catch (error) { setError(String(error)); return false }
    finally { setBusy(false) }
  }
  const edit = (row: SidebarSession, body: object, message: string) => mutate(() => api(`/sessions/${row.id}`, "PATCH", body), message)
  const toggle = (key: string) => setClosed(previous => {
    const next = previous.includes(key) ? previous.filter(item => item !== key) : [...previous, key]
    try { localStorage.setItem(`moyai-native-folders:${userId}`, JSON.stringify(next)) } catch {}
    return next
  })
  const matching = sessions.filter(row => `${row.title} ${row.folder ?? ""}`.toLowerCase().includes(search.toLowerCase()))
  const unfiled = matching.filter(row => !row.pinned && !row.folder)
  function rows(items: SidebarSession[]) {
    return items.map(row => <div key={row.id} className={`session-row ${selected === row.id ? "selected" : ""} ${row.unread ? "unread" : ""}`}>
      <a href={`#${row.id}`} aria-current={selected === row.id ? "page" : undefined}>
        <span className={`session-indicator ${["running", "queued", "saving"].includes(row.status) ? "working" : row.status === "waiting" ? "waiting" : ""}`} aria-label={row.unread ? "Unread" : row.status} />
        <span><span className="session-title">{row.title}</span><small>{row.status === "waiting" ? "Needs a response" : ["running", "queued", "saving"].includes(row.status) ? row.status : age(row.updated_at)}</small></span>
      </a>
      <Menu label={`Session actions for ${row.title}`}>{close => <>
        <Item onClick={() => { close(); openEditor({ action: "move", session: row }) }}><Folder />Folder<ChevronRight className="trailing" /></Item>
        <hr />
        <Item disabled={row.owner_id !== userId} onClick={() => { close(); openEditor({ action: "rename", session: row }) }}><Pencil />Rename</Item>
        <Item onClick={() => { close(); void navigator.clipboard.writeText(`${location.origin}/#${row.id}`).then(() => setNotice("Session link copied."), () => setError("Could not copy the link. Open the session and copy its address.")) }}><Copy />Copy session link</Item>
        <Item onClick={() => { close(); onOpenComputer(row.id) }}><Monitor />Open computer</Item>
        <Item onClick={() => { close(); void edit(row, { unread: !row.unread }, row.unread ? "Marked read." : "Marked unread.") }}><Mail />{row.unread ? "Mark read" : "Mark unread"}</Item>
        <Item onClick={() => { close(); void edit(row, { pinned: !row.pinned }, row.pinned ? "Session unpinned." : "Session pinned.") }}><Pin />{row.pinned ? "Unpin" : "Pin"}</Item>
        <hr />
        <Item disabled={row.owner_id !== userId} onClick={() => { close(); openEditor({ action: "archive", session: row }) }}><Archive />{row.archived ? "Restore session" : "Archive"}</Item>
      </>}</Menu>
    </div>)
  }
  function section(key: string, label: string, icon: React.ReactNode, items: SidebarSession[], folder?: SessionFolder) {
    const expanded = Boolean(search) || !closed.includes(key)
    return <section key={key} className="session-group">
      <div className="session-group-heading"><Button variant="ghost" className="session-group-toggle" aria-expanded={expanded} aria-controls={`sessions-${key}`} onClick={() => toggle(key)}>{icon}<span>{label}</span><small>{items.length}</small><ChevronDown className={expanded ? "" : "closed"} size={12} /></Button>
        {folder && <Menu label={`Folder actions for ${folder.name}`}>{close => <>
          <Item onClick={() => { close(); openEditor({ action: "folder-rename", folder }) }}><Pencil />Rename folder</Item>
          <Item onClick={() => { close(); openEditor({ action: "folder-remove", folder }) }}><Trash2 />Remove folder</Item>
        </>}</Menu>}
      </div>
      <div id={`sessions-${key}`} hidden={!expanded}>{rows(items)}{!items.length && <p className="folder-empty">{folder ? "Move sessions here using their ⋯ menu." : label === "Participated" ? "Shared sessions you’ve contributed to." : "No sessions here yet."}</p>}</div>
    </section>
  }
  const title = editor ? ({ rename: "Rename session", move: "Move session", archive: editor.session?.archived ? "Restore session?" : "Archive session?", "folder-create": "New folder", "folder-rename": "Rename folder", "folder-remove": "Remove folder?" })[editor.action] : ""
  return <>
    <div className="rail-caption"><span>{archived ? "Archived" : "Sessions"} <small>{matching.length}</small></span><div>
      <Button size="icon-sm" variant="ghost" aria-label="New folder" onClick={() => openEditor({ action: "folder-create" })}><FolderPlus /></Button>
      <Button size="icon-sm" variant="ghost" aria-label={archived ? "Show active conversations" : "Show archived conversations"} onClick={onArchiveView}><Archive /></Button>
    </div></div>
    <nav aria-label="Conversations" className="session-list">
      {matching.some(row => row.pinned) && section("pinned", "Pinned", <Pin size={15} />, matching.filter(row => row.pinned))}
      {folders.filter(folder => !search || folder.name.toLowerCase().includes(search.toLowerCase()) || matching.some(row => row.folder === folder.name))
        .map(folder => section(folder.id, folder.name, <Folder size={15} />, matching.filter(row => !row.pinned && row.folder === folder.name), folder))}
      {section("participated", "Participated", <Users size={15} />, unfiled.filter(row => row.participated))}
      {section("recent", "Recent", <Clock3 size={15} />, unfiled.filter(row => !row.participated))}
      {search && !matching.length && <p className="empty-rail muted">No matching sessions. <Button size="sm" variant="ghost" onClick={() => window.dispatchEvent(new Event("moyai:clear-search"))}>Clear search</Button></p>}
    </nav>
    {notice && <div className="sidebar-notice" role="status">{notice}<Button size="icon-sm" variant="ghost" aria-label="Dismiss notice" onClick={() => setNotice("")}><Check size={13} /></Button></div>}
    {error && !editor && <p className="error" role="alert">{error}<Button variant="ghost" size="sm" onClick={() => setError("")}>Dismiss</Button></p>}
    <Dialog open={Boolean(editor)} onOpenChange={open => { if (!open && !busy) { setEditor(null); setError("") } }}>
      <DialogContent className="sidebar-editor"><DialogHeader><DialogTitle>{title}</DialogTitle><DialogDescription>
        {editor?.action === "folder-remove" ? "The folder is removed from your sidebar. Its sessions stay saved and return to Recent or Participated."
          : editor?.action === "archive" ? "This changes the session for everyone. You can restore it from Archived sessions."
          : editor?.action === "rename" ? "Choose a title you can find later."
          : "Folders organize your sidebar. Session sharing stays the same."}
      </DialogDescription></DialogHeader>
      {error && <p className="error" role="alert">{error}</p>}
      {editor?.action === "move" ? <div className="folder-choices">
        {[{ id: "", name: "" }, ...folders].map(folder => <Button key={folder.id} disabled={busy} variant="ghost" onClick={() => void edit(editor.session!, { folder: folder.name }, folder.name ? `Moved to ${folder.name}.` : "Removed from folder.")}><Folder /><span>{folder.name || "No folder"}</span>{(editor.session!.folder ?? "") === folder.name && <Check className="trailing" />}</Button>)}
        <Button variant="outline" onClick={() => openEditor({ action: "folder-create", session: editor.session })}><FolderPlus />New folder</Button>
      </div> : <form onSubmit={event => {
        event.preventDefault()
        if (!editor) return
        if (editor.action === "rename") void edit(editor.session!, { title: name.trim() }, "Session renamed.")
        else if (editor.action === "archive") void edit(editor.session!, { archived: !editor.session!.archived }, editor.session!.archived ? "Session restored." : "Session archived.")
        else if (editor.action === "folder-remove") void mutate(() => api(`/session-folders/${editor.folder!.id}`, "DELETE"), "Folder removed. Sessions are still saved.")
        else if (editor.action === "folder-rename") void mutate(() => api(`/session-folders/${editor.folder!.id}`, "PATCH", { name }), "Folder renamed.")
        else void mutate(async () => {
          const created = await api<SessionFolder>("/session-folders", "POST", { name })
          // If the subsequent move fails, keep the created folder and retry the
          // move from its dialog instead of creating a duplicate.
          if (editor.session) {
            setEditor({ action: "move", session: editor.session }); await refresh()
            await api(`/sessions/${editor.session.id}`, "PATCH", { folder: created.name })
          }
        }, editor.session ? "Folder created and session moved." : "Folder created.")
      }}>
        {!["archive", "folder-remove"].includes(editor?.action ?? "") && <label>{editor?.action === "rename" ? "Session name" : "Folder name"}<Input autoFocus value={name} onChange={event => setName(event.target.value)} maxLength={editor?.action === "rename" ? 300 : 200} required disabled={busy} /></label>}
        <div className="dialog-actions"><Button type="button" variant="outline" disabled={busy} onClick={() => { setEditor(null); setError("") }}>Cancel</Button>
          <Button type="submit" disabled={busy || (!["archive", "folder-remove"].includes(editor?.action ?? "") && !name.trim())}>{busy ? "Saving…" : editor?.action === "archive" ? editor.session?.archived ? "Restore session" : "Archive session" : editor?.action === "folder-remove" ? "Remove folder" : "Save"}</Button></div>
      </form>}
    </DialogContent></Dialog>
  </>
}
