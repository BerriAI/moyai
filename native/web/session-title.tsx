import React, { useLayoutEffect, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { sessionIsReadOnly, useSessionActions, type SidebarSession } from "./session-actions"

type Props = { session: SidebarSession }

export function SessionTitle(props: Props) {
  const actions = useSessionActions()
  const row = props.session
  return row.owner_id === actions.userId && !sessionIsReadOnly(row) && !row.deleted && !row.deleting
    ? <EditableTitle key={row.id} {...props} />
    : <>{row.title}</>
}

function EditableTitle({ session }: Props) {
  const actions = useSessionActions()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState("")
  const input = useRef<HTMLInputElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const editingNow = useRef(false)
  const restoreFocus = useRef(false)
  useLayoutEffect(() => {
    if (editing) { input.current?.focus(); input.current?.select() }
    else if (restoreFocus.current) { trigger.current?.focus(); restoreFocus.current = false }
  }, [editing])

  function finish(focus: boolean) {
    editingNow.current = false
    restoreFocus.current = focus
    setEditing(false)
  }
  function save(focus: boolean) {
    if (!editingNow.current || actions.busy) return
    const title = draft.trim()
    finish(focus)
    if (title && title !== session.title) void actions.edit(session, { title }, "Session renamed.")
  }

  return editing
    ? <Input ref={input} className="session-title-input" aria-label="Session name"
      title="Enter to save · Escape to cancel" value={draft} maxLength={300}
      readOnly={actions.busy}
      onChange={event => setDraft(event.target.value)} onBlur={() => void save(false)}
      onKeyDown={event => {
        if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return
        if (event.key === "Enter") { event.preventDefault(); void save(true) }
        if (event.key === "Escape") {
          event.preventDefault(); event.stopPropagation()
          finish(true)
        }
      }} />
    : <Button ref={trigger} type="button" variant="ghost" className="session-title-trigger"
      aria-label={`Rename session: ${session.title}`} title={`Rename session: ${session.title}`} disabled={actions.busy}
      onClick={() => { setDraft(session.title); editingNow.current = true; setEditing(true) }}>
      <span>{session.title}</span>
    </Button>
}
