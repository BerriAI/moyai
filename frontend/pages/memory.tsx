import { useEffect, useRef, useState } from "react"
import { useMutation, useQuery } from "@tanstack/react-query"
import { toast } from "sonner"
import { queryClient, unwrap } from "../api/client"
import { queries } from "../api/queries"
import { useIdentity } from "../legacy/runtime"
import { createMemory, deleteMemory, updateMemory, updateMemoryPreferences } from "../api/generated/sdk.gen"
import type { MemoryNote, MemoryPreferences } from "../api/generated/types.gen"
import { Button } from "../components/ui/button"
import { Input } from "../components/ui/input"
import { DeleteMemoryDialog, MemoryEditorDialog, type MemoryDraft } from "../blocks/memory/memory-dialogs"
import { MemoryControls } from "../blocks/memory/memory-controls"
import { MemoryList } from "../blocks/memory/memory-list"

// The search survives leaving and returning to the page, per user.
const searches = new Map<string | null, string>()

async function changed(message?: string) {
  if (message) toast(message)
  await queryClient.invalidateQueries({ queryKey: queries.memory._def })
}

function EditMemory({ note, close }: { note: MemoryNote | null; close(): void }) {
  const [requestId] = useState(() => crypto.randomUUID())
  const save = useMutation({
    mutationFn: async (draft: MemoryDraft) => {
      const body = { ...draft, key: note?.key ?? `note-${requestId}`, revision: note?.revision ?? 0, request_id: requestId }
      return unwrap(await (note ? updateMemory({ path: { note_id: note.id }, body }) : createMemory({ body })))
    },
    async onSuccess() { close(); await changed("Memory saved.") },
  })
  return <MemoryEditorDialog note={note} pending={save.isPending} error={save.error?.message} onSubmit={draft => save.mutate(draft)} onClose={close} />
}

function ForgetMemory({ note, close }: { note: MemoryNote; close(): void }) {
  const remove = useMutation({
    mutationFn: async () => unwrap(await deleteMemory({ path: { note_id: note.id }, body: { revision: note.revision } })),
    async onSuccess() { close(); await changed("Memory deleted.") },
  })
  return <DeleteMemoryDialog note={note} pending={remove.isPending} error={remove.error?.message} onConfirm={() => remove.mutate()} onClose={close} />
}

export function MemoryPage() {
  const { userId } = useIdentity()
  const library = useQuery(queries.memory.library(userId))
  const [search, setSearch] = useState(() => searches.get(userId) ?? "")
  const [editing, setEditing] = useState<{ note: MemoryNote | null } | null>(null)
  const [deleting, setDeleting] = useState<MemoryNote | null>(null)
  const heading = useRef<HTMLHeadingElement>(null)
  useEffect(() => { searches.set(userId, search) }, [userId, search])
  useEffect(() => { heading.current?.focus({ preventScroll: true }) }, [library.isSuccess])
  const preferences = useMutation({
    mutationFn: async ({ current, changes }: { current: MemoryPreferences; changes: Partial<MemoryPreferences> }) =>
      unwrap(await updateMemoryPreferences({ body: { ...current, ...changes } })),
    onSettled: () => changed(),
    onError: error => { toast(error.message) },
  })
  if (library.isPending) return <p className="subtext" role="status">Loading your memories…</p>
  if (library.isError) return <section className="memory-page"><h1>Memory</h1><div role="alert"><p className="subtext">{library.error.message}</p><p>Check your connection and try again.</p></div><Button id="memory-retry" onClick={() => void library.refetch()}>Try again</Button></section>
  const { memories, limit, review } = library.data
  const query = search.toLocaleLowerCase().trim()
  const rows = memories.filter(note => `${note.title} ${note.content} ${note.repo_url}`.toLocaleLowerCase().includes(query))
  return <section className="memory-page">
    <div className="section-header"><div><h1 ref={heading} tabIndex={-1}>Memory</h1><p className="subtext">Preferences and context for future sessions.</p></div><Button className="primary" id="memory-add" onClick={() => setEditing({ note: null })}>Add memory</Button></div>
    <MemoryControls preferences={library.data.preferences} review={review} pending={preferences.isPending}
      onChange={changes => preferences.mutate({ current: library.data.preferences, changes })} onRefresh={() => void library.refetch()} />
    <p className="memory-privacy">These notes are available only for your requests. Answers in shared sessions may reflect them. Keep keys in <a href="#secrets">Secrets</a> and reusable team workflows in <a href="#skills">Skills</a>.</p>
    <div className="memory-filter"><Input type="search" id="memory-search" aria-label="Search your memories" placeholder="Search your memories" value={search} onChange={event => setSearch(event.target.value)} /><span className="subtext">{memories.length} / {limit} notes</span></div>
    <MemoryList notes={rows} filtered={!!query} onEdit={note => setEditing({ note })} onDelete={setDeleting} />
    <p className="subtext memory-footnote">Moyai recalls a few relevant notes at a time. Project context and references expire after 90 days; edit to refresh them. Deleting a note stops future recall, but does not remove existing conversations or backups.</p>
    {editing && <EditMemory note={editing.note} close={() => setEditing(null)} />}
    {deleting && <ForgetMemory note={deleting} close={() => setDeleting(null)} />}
  </section>
}
