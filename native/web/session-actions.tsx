import React, { createContext, useContext, useEffect, useRef, useState } from "react"
import { Archive, Check, ChevronRight, Copy, Download, Folder, FolderPlus, History, Mail, Monitor, MoreHorizontal, PanelRightOpen, Pencil, Pin, RotateCcw, Trash2, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { api } from "./api"
import { preferencePatch, type SessionPreferenceUpdates } from "./session-preferences"
import { downloadDebugTrace } from "./debug-trace"

export type SidebarSession = {
  id: string; title: string; owner_id: string; status: string; archived: boolean; deleted?: boolean
  pinned?: boolean; folder?: string; participated?: boolean; created_by_me?: boolean; unread?: boolean; updated_at?: string
  surface?: string
  parent_session_id?: string | null
  source?: "native" | "legacy"; key?: string; url?: string; read_only?: boolean; deleting?: boolean; folder_id?: string | null
  pull_requests?: { url: string; title: string; repository: string; number: number; state: "open" | "closed" | "merged" | "unknown"; draft: boolean; accessible: boolean; error?: string }[]
}
export const sessionIsReadOnly = (row: SidebarSession) => row.source === "legacy" || row.read_only === true
export const sessionKey = (row: SidebarSession) => row.key ?? `${row.source ?? "native"}:${row.id}`
export const sessionHash = (row: SidebarSession) => `${row.source === "legacy" ? "run=" : ""}${encodeURIComponent(row.id)}`
export type SessionFolder = { id: string; name: string }
type Action = "rename" | "move" | "archive" | "delete" | "restore" | "folder-create" | "folder-rename" | "folder-remove"
type Editor = { action: Action; session?: SidebarSession; folder?: SessionFolder }
type Actions = {
  userId: string; busy: boolean
  preferenceSession: (row: SidebarSession) => SidebarSession
  openEditor: (editor: Editor) => void
  edit: (row: SidebarSession, body: object, message: string) => Promise<boolean>
  copy: (row: SidebarSession) => void
  openComputer: (id: string) => void
  newSession: () => void
}
const ActionsContext = createContext<Actions | null>(null)
export function useSessionActions() {
  const value = useContext(ActionsContext)
  if (!value) throw new Error("Session actions require their provider.")
  return value
}

export function ActionMenu({ label, children, header = false }: { label: string; children: (close: () => void) => React.ReactNode; header?: boolean }) {
  const [open, setOpen] = useState(false)
  return <Popover open={open} onOpenChange={setOpen}>
    <PopoverTrigger asChild><Button type="button" className="session-menu-trigger" variant="ghost" size="icon-sm" aria-label={label} aria-haspopup="menu" title={label}><MoreHorizontal size={17} /></Button></PopoverTrigger>
    <PopoverContent className="native-session-menu" role="menu" aria-label={label} align={header ? "end" : "start"} side={header || window.matchMedia("(max-width: 600px)").matches ? "bottom" : "right"} collisionPadding={12}
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
export function ActionItem({ children, onClick, disabled }: { children: React.ReactNode; onClick: () => void; disabled?: boolean }) {
  return <Button type="button" role="menuitem" variant="ghost" disabled={disabled} onClick={onClick}>{children}</Button>
}

export function SessionMenu({ session, header = false, onOpenSideChat }: {
  session: SidebarSession; header?: boolean; onOpenSideChat?: () => void
}) {
  const actions = useSessionActions()
  const row = actions.preferenceSession(session)
  const [traceBusy, setTraceBusy] = useState(false)
  const [traceError, setTraceError] = useState("")
  const owner = row.owner_id === actions.userId
  const exportTrace = async (close: () => void) => {
    setTraceBusy(true)
    setTraceError("")
    try {
      await downloadDebugTrace(row.id)
      close()
    } catch (error) {
      setTraceError(String(error))
    } finally {
      setTraceBusy(false)
    }
  }
  return <ActionMenu header={header} label={header ? "Session actions" : `Session actions for ${row.title}`}>{close => {
    if (sessionIsReadOnly(row)) return <>
      <ActionItem disabled={row.deleted || row.deleting} onClick={() => { close(); location.hash = sessionHash(row) }}><History />Open history</ActionItem>
      <ActionItem onClick={() => { close(); actions.copy(row) }}><Copy />Copy session link</ActionItem>
    </>
    const editor = (action: Action) => { close(); actions.openEditor({ action, session: row }) }
    return <>
      <ActionItem disabled={traceBusy} onClick={() => void exportTrace(close)}><Download />{traceBusy ? "Preparing trace…" : "Get debug trace"}</ActionItem>
      {traceError && <p className="error" role="alert">{traceError}</p>}
      {row.deleted ? <ActionItem disabled={!owner || actions.busy} onClick={() => editor("restore")}><RotateCcw />Restore session</ActionItem> : <>
        {onOpenSideChat && <ActionItem onClick={() => { close(); onOpenSideChat() }}><PanelRightOpen />Open in side chat</ActionItem>}
        <ActionItem disabled={actions.busy} onClick={() => editor("move")}><Folder />Folder<ChevronRight className="trailing" /></ActionItem>
        <ActionItem disabled={!owner || actions.busy} onClick={() => editor("rename")}><Pencil />Rename</ActionItem>
        <ActionItem onClick={() => { close(); actions.copy(row) }}><Copy />Copy session link</ActionItem>
        <ActionItem onClick={() => { close(); actions.openComputer(row.id) }}><Monitor />Open computer</ActionItem>
        <ActionItem disabled={actions.busy} onClick={() => { close(); void actions.edit(row, { unread: !row.unread }, row.unread ? "Marked read." : "Marked unread.") }}><Mail />{row.unread ? "Mark read" : "Mark unread"}</ActionItem>
        <ActionItem disabled={actions.busy} onClick={() => { close(); void actions.edit(row, { pinned: !row.pinned }, row.pinned ? "Session unpinned." : "Session pinned.") }}><Pin />{row.pinned ? "Unpin" : "Pin"}</ActionItem>
        <hr />
        <ActionItem disabled={!owner || actions.busy} onClick={() => editor("archive")}><Archive />{row.archived ? "Restore from archive" : "Archive"}</ActionItem>
        <ActionItem disabled={!owner || actions.busy} onClick={() => editor("delete")}><Trash2 />Delete session…</ActionItem>
      </>}
    </>
  }}</ActionMenu>
}

export function SessionActionsProvider({ userId, folders, refresh, preferences, preferenceSession, onOpenComputer, onNewSession, onDeleted, children }: {
  userId: string; folders: SessionFolder[]; refresh: () => Promise<void>
  preferences: SessionPreferenceUpdates; preferenceSession: (row: SidebarSession) => SidebarSession
  onOpenComputer: (id: string) => void; onNewSession: () => void; onDeleted: (id: string) => void
  children: React.ReactNode
}) {
  const [editor, setEditor] = useState<Editor | null>(null)
  const [name, setName] = useState("")
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const [error, setError] = useState("")
  const [notice, setNotice] = useState("")
  const active = useRef(true), preferenceSequence = useRef(0)
  useEffect(() => {
    active.current = true
    return () => { active.current = false }
  }, [])
  const openEditor = (value: Editor) => { if (busyRef.current || (value.session && sessionIsReadOnly(value.session))) return; setError(""); setName(value.action === "folder-create" ? "" : value.folder?.name ?? value.session?.title ?? ""); setEditor(value.session ? { ...value, session: preferenceSession(value.session) } : value) }
  async function mutate(action: () => Promise<unknown>, message: string) {
    if (busyRef.current) return false
    busyRef.current = true
    setBusy(true); setError(""); setNotice("")
    try { await action(); setEditor(null); await refresh(); setNotice(message); return true }
    catch (error) { setError(String(error)); return false }
    finally { busyRef.current = false; setBusy(false) }
  }
  async function edit(row: SidebarSession, body: object, message: string) {
    if (sessionIsReadOnly(row) || busyRef.current) return false
    const patch = preferencePatch(body)
    if (!patch) return mutate(() => api(`/sessions/${row.id}`, "PATCH", body), message)
    const sequence = ++preferenceSequence.current
    setEditor(null); setError(""); setNotice("")
    try {
      await preferences.update(row.id, patch)
      if (active.current && sequence === preferenceSequence.current) setNotice(message)
      return true
    } catch (error) {
      if (active.current && !(error instanceof DOMException && error.name === "AbortError")) setError(String(error))
      return false
    }
  }
  const title = editor ? ({
    rename: "Rename session", move: "Move session", archive: editor.session?.archived ? "Restore session?" : "Archive session?",
    delete: "Delete session?", restore: "Restore deleted session?", "folder-create": "New folder",
    "folder-rename": "Rename folder", "folder-remove": "Remove folder?",
  })[editor.action] : ""
  const named = editor && ["rename", "folder-create", "folder-rename"].includes(editor.action)
  return <ActionsContext.Provider value={{
    userId, busy, preferenceSession, openEditor, edit, openComputer: onOpenComputer, newSession: onNewSession,
    copy: row => { void navigator.clipboard.writeText(`${location.origin}/#${sessionHash(row)}`).then(
      () => setNotice("Session link copied."),
      () => setError("Could not copy the link. Open the session and copy its address."),
    ) },
  }}>
    {children}
    {(notice || (error && !editor)) && <div className={`action-toast ${error && !editor ? "error" : ""}`} role={error && !editor ? "alert" : "status"}>{error && !editor ? error : notice}<Button type="button" size="icon-sm" variant="ghost" aria-label="Dismiss notification" onClick={() => { setNotice(""); setError("") }}><X size={14} /></Button></div>}
    <Dialog open={Boolean(editor)} onOpenChange={open => { if (!open && !busy) { setEditor(null); setError("") } }}>
      <DialogContent className="sidebar-editor"><DialogHeader><DialogTitle>{title}</DialogTitle><DialogDescription>
        {editor?.action === "delete" ? "Stops active and queued work and removes this session for everyone. Saved history and files are kept. You can restore it from Deleted sessions."
          : editor?.action === "restore" ? "Return this session to the active list. Stopped tasks will stay stopped."
          : editor?.action === "folder-remove" ? "Its sessions stay saved and remain available through search or their links."
          : editor?.action === "archive" ? "This changes the session for everyone. You can restore it from Archived sessions."
          : editor?.action === "rename" ? "Choose a title you can find later."
          : "Folders organize your sidebar. Session sharing stays the same."}
      </DialogDescription></DialogHeader>
      {error && <p className="error" role="alert">{error}</p>}
      {editor?.action === "move" ? <div className="folder-choices">
        {[{ id: "", name: "" }, ...folders].map(folder => <Button key={folder.id} disabled={busy} variant="ghost" onClick={() => void edit(editor.session!, { folder: folder.name }, folder.name ? `Moved to ${folder.name}.` : "Removed from folder.")}><Folder /><span>{folder.name || "No folder"}</span>{(editor.session!.folder ?? "") === folder.name && <Check className="trailing" />}</Button>)}
        <Button variant="outline" disabled={busy} onClick={() => openEditor({ action: "folder-create", session: editor.session })}><FolderPlus />New folder</Button>
      </div> : <form onSubmit={event => {
        event.preventDefault()
        if (!editor) return
        if (editor.action === "rename") void edit(editor.session!, { title: name.trim() }, "Session renamed.")
        else if (editor.action === "archive") void edit(editor.session!, { archived: !editor.session!.archived }, editor.session!.archived ? "Session restored." : "Session archived.")
        else if (editor.action === "delete") void mutate(async () => { await api(`/sessions/${editor.session!.id}`, "DELETE"); onDeleted(editor.session!.id) }, "Moved to Deleted sessions.")
        else if (editor.action === "restore") void mutate(() => api(`/sessions/${editor.session!.id}/restore`, "POST"), "Restored to active sessions.")
        else if (editor.action === "folder-remove") void mutate(() => api(`/session-folders/${editor.folder!.id}`, "DELETE"), "Folder removed. Sessions are still saved.")
        else if (editor.action === "folder-rename") void mutate(() => api(`/session-folders/${editor.folder!.id}`, "PATCH", { name }), "Folder renamed.")
        else void mutate(async () => {
          const created = await api<SessionFolder>("/session-folders", "POST", { name })
          if (editor.session) {
            setEditor({ action: "move", session: editor.session }); await refresh()
            await api(`/sessions/${editor.session.id}`, "PATCH", { folder: created.name })
          }
        }, editor.session ? "Folder created and session moved." : "Folder created.")
      }}>
        {named && <label>{editor?.action === "rename" ? "Session name" : "Folder name"}<Input autoFocus value={name} onChange={event => setName(event.target.value)} maxLength={editor?.action === "rename" ? 300 : 200} required disabled={busy} /></label>}
        <div className="dialog-actions"><Button autoFocus={!named} type="button" variant="outline" disabled={busy} onClick={() => { setEditor(null); setError("") }}>Cancel</Button>
          <Button type="submit" variant={editor?.action === "delete" ? "destructive" : "default"} disabled={busy || Boolean(named && !name.trim())}>{busy ? "Saving…" : editor?.action === "delete" ? "Delete session" : editor?.action === "restore" ? "Restore session" : editor?.action === "archive" ? editor.session?.archived ? "Restore session" : "Archive session" : editor?.action === "folder-remove" ? "Remove folder" : "Save"}</Button></div>
      </form>}
    </DialogContent></Dialog>
  </ActionsContext.Provider>
}
