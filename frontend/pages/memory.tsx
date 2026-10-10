import { useEffect, useMemo, useRef, useState } from "react"
import { queryOptions, useMutation, useQuery } from "@tanstack/react-query"
import { queryClient, unwrap } from "../api/client"
import { createMemory, deleteMemory, getMemoryLibrary, updateMemory, updateMemoryPreferences } from "../api/generated/sdk.gen"
import type { MemoryNote, MemoryReview, MemoryPreferences } from "../api/generated/types.gen"
import { zNote } from "../api/generated/zod.gen"
import { Button } from "../components/ui/button"
import { Input } from "../components/ui/input"
import { Label } from "../components/ui/label"
import { Textarea } from "../components/ui/textarea"
import { Dialog, DialogContent, DialogTitle } from "../components/ui/dialog"
import { FormSelect } from "../form-select"
import { relativeDate } from "../lib/time"

const kinds = { preference: "Preference", feedback: "Correction", project: "Project context", reference: "Reference" } as const
const searches = new Map<string | null, string>()
export const memoryQuery = () => queryOptions({
  queryKey: ["memory", window.MoyaiControllers.userId()],
  queryFn: async ({ signal }) => unwrap(await getMemoryLibrary({ signal })),
})

function options(choices: ReadonlyArray<readonly [string, string]>, selected: string) {
  const fragment = document.createDocumentFragment()
  for (const [value, label] of choices) fragment.append(new Option(label, value, value === selected, value === selected))
  return fragment
}

function reviewSummary(review: MemoryReview | null | undefined, preferences: MemoryPreferences) {
  if (!preferences.enabled || !preferences.auto_save) return ""
  if (!review?.enabled) return "Background review is disabled by the server."
  if (!review.configured) return "Background review needs a configured model gateway."
  const latest = review.latest
  const result = latest?.status === "failed" ? "The last review could not finish. Future completed turns will still be reviewed." : latest?.status === "completed" ? `Last review ${relativeDate(latest.updated_at)}: ${latest.saved_count ? `saved ${latest.saved_count} ${latest.saved_count === 1 ? "memory" : "memories"}.` : "no new lasting context to save."}` : "Finished sessions are reviewed after a short idle period. Recent sessions are checked when background review starts."
  return `${review.pending ? `${review.pending} finished ${review.pending === 1 ? "turn is" : "turns are"} waiting for review. ` : ""}${result}`
}

function MemoryEditor({ note, close }: { note: MemoryNote | null; close(): void }) {
  const [requestId] = useState(() => crypto.randomUUID())
  const formRef = useRef<HTMLFormElement>(null)
  const choices = useMemo(() => options(Object.entries(kinds), note?.kind ?? "preference"), [note?.kind])
  const save = useMutation({
    mutationFn: async (form: FormData) => {
      const body = zNote.parse({ key: note?.key ?? `note-${requestId}`, title: form.get("title"), content: form.get("content"), kind: form.get("kind"), repo_url: form.get("repo_url"), revision: note?.revision ?? 0, request_id: requestId })
      return unwrap(await (note ? updateMemory({ path: { note_id: note.id }, body }) : createMemory({ body })))
    },
    async onSuccess() {
      close()
      window.MoyaiControllers.invalidate()
      await queryClient.invalidateQueries({ queryKey: memoryQuery().queryKey })
    },
  })
  return <Dialog open onOpenChange={open => { if (!open) close() }}>
    <DialogContent className="memory-editor" data-dialog-id="memory-dialog">
      <form id="memory-form" ref={formRef} onSubmit={event => { event.preventDefault(); save.mutate(new FormData(event.currentTarget)) }}>
        <DialogTitle>{note ? "Edit memory" : "Add a memory"}</DialogTitle><p className="subtext">Keep one useful idea per note. It stays in your personal library.</p>
        <div className="field"><Label htmlFor="memory-title">Title</Label><Input id="memory-title" name="title" required minLength={3} maxLength={120} defaultValue={note?.title ?? ""} placeholder="How I like PR descriptions" autoFocus /></div>
        <div className="field"><Label htmlFor="memory-kind">Type</Label><FormSelect id="memory-kind" name="kind" initialOptions={choices} /></div>
        <div className="field"><Label htmlFor="memory-content">What should Moyai remember?</Label><Textarea id="memory-content" name="content" required minLength={3} maxLength={1200} rows={5} defaultValue={note?.content ?? ""} placeholder="Keep PR descriptions short and include the test results." /></div>
        <div className="field"><Label htmlFor="memory-repo">Repository (optional)</Label><Input type="url" id="memory-repo" name="repo_url" defaultValue={note?.repo_url ?? ""} placeholder="https://github.com/BerriAI/litellm" /><small>Leave blank to use across your sessions.</small></div>
        {note && note.source.type !== "manual" && <details className="memory-source"><summary>Why Moyai saved this</summary>{note.source.type === "observation" && <p>Observed during work in {note.source.scope}. Recheck if the environment changes.</p>}<blockquote>{note.source.type === "observation" ? note.source.evidence : note.source.quote}</blockquote><a href={`#run=${note.source.run_id}`} id="memory-source-link" onClick={close}>Open original session</a></details>}
        <p id="memory-error" role="alert">{save.error?.message}</p><div className="credential-actions"><Button type="submit" disabled={save.isPending}>Save memory</Button><Button type="button" variant="ghost" className="quiet" id="memory-cancel" onClick={close}>Cancel</Button></div>
      </form>
    </DialogContent>
  </Dialog>
}

function DeleteMemory({ note, close }: { note: MemoryNote; close(): void }) {
  const remove = useMutation({
    mutationFn: async () => unwrap(await deleteMemory({ path: { note_id: note.id }, body: { revision: note.revision } })),
    async onSuccess() { close(); window.MoyaiControllers.invalidate(); await queryClient.invalidateQueries({ queryKey: memoryQuery().queryKey }) },
  })
  return <Dialog open onOpenChange={open => { if (!open) close() }}><DialogContent className="memory-editor" data-dialog-id="memory-dialog" onOpenAutoFocus={event => { event.preventDefault(); document.querySelector<HTMLButtonElement>("#memory-cancel")?.focus() }}>
    <DialogTitle>Delete this memory?</DialogTitle><p>{note.title}</p><p className="subtext">Moyai will stop recalling this note. Existing conversations and backups are not changed.</p><p id="memory-error" role="alert">{remove.error?.message}</p><div className="credential-actions"><Button variant="destructive" className="danger" id="memory-confirm-delete" disabled={remove.isPending} onClick={() => remove.mutate()}>Delete memory</Button><Button variant="ghost" className="quiet" id="memory-cancel" onClick={close}>Cancel</Button></div>
  </DialogContent></Dialog>
}

export function MemoryPage() {
  const userId = window.MoyaiControllers.userId()
  const library = useQuery(memoryQuery())
  const [search, setSearch] = useState(() => searches.get(userId) ?? "")
  const [editor, setEditor] = useState<{ note: MemoryNote | null } | null>(null)
  const [deleting, setDeleting] = useState<MemoryNote | null>(null)
  const heading = useRef<HTMLHeadingElement>(null)
  const preferences = library.data?.preferences
  const learning = useMemo(() => options([["auto", "Save useful context automatically"], ["manual", "Only save manually"]], preferences?.auto_save ? "auto" : "manual"), [preferences?.auto_save])
  useEffect(() => { searches.set(userId, search) }, [userId, search])
  useEffect(() => { heading.current?.focus({ preventScroll: true }) }, [library.isSuccess])
  const updatePreferences = useMutation({
    mutationFn: async (changes: Partial<MemoryPreferences>) => {
      if (!preferences) throw new Error("Memory settings are still loading.")
      return unwrap(await updateMemoryPreferences({ body: { ...preferences, ...changes } }))
    },
    async onSuccess(saved) {
      window.MoyaiControllers.invalidate()
      queryClient.setQueryData(memoryQuery().queryKey, current => current && ({ ...current, preferences: saved }))
      await queryClient.invalidateQueries({ queryKey: memoryQuery().queryKey })
    },
  })
  if (library.isPending) return <p className="subtext" role="status">Loading your memories…</p>
  if (library.isError) return <section className="memory-page"><h1>Memory</h1><div role="alert"><p className="subtext">{library.error.message}</p><p>Check your connection and try again.</p></div><Button id="memory-retry" onClick={() => void library.refetch()}>Try again</Button></section>
  const data = library.data, prefs = data.preferences
  const query = search.toLocaleLowerCase().trim()
  const rows = data.memories.filter(note => `${note.title} ${note.content} ${note.repo_url}`.toLocaleLowerCase().includes(query))
  return <section className="memory-page">
    <div className="section-header"><div><h1 ref={heading} tabIndex={-1}>Memory</h1><p className="subtext">Preferences and context for future sessions.</p></div><Button className="primary" id="memory-add" onClick={() => setEditor({ note: null })}>Add memory</Button></div>
    <div className="memory-controls"><div><strong>{prefs.enabled ? "Moyai remembers useful context" : "Memory is paused"}</strong><p className="subtext">{prefs.enabled ? "Preferences, corrections, and lessons learned during work can carry into future sessions." : "Your notes are kept here. Moyai won’t retrieve or save them while paused."}</p></div><Button variant="ghost" className="quiet" id="memory-toggle" disabled={updatePreferences.isPending} onClick={() => updatePreferences.mutate({ enabled: !prefs.enabled })}>{prefs.enabled ? "Pause memory" : "Resume memory"}</Button></div>
    <div className="memory-learning"><Label htmlFor="memory-learning">How new memories are saved</Label><FormSelect id="memory-learning" disabled={!prefs.enabled || updatePreferences.isPending} initialOptions={learning} onChange={event => updatePreferences.mutate({ auto_save: event.currentTarget.value === "auto" })} /></div>
    {updatePreferences.error && <p role="alert">{updatePreferences.error.message} Refresh before saving again.</p>}
    {prefs.enabled && prefs.auto_save && <div className="memory-review"><p className="subtext" role="status">{reviewSummary(data.review, prefs)}</p><Button variant="ghost" className="quiet" id="memory-refresh" onClick={() => void library.refetch()}>Refresh</Button></div>}
    <p className="memory-privacy">These notes are available only for your requests. Answers in shared sessions may reflect them. Keep keys in <a href="#secrets">Secrets</a> and reusable team workflows in <a href="#skills">Skills</a>.</p>
    <div className="memory-filter"><Input type="search" id="memory-search" aria-label="Search your memories" placeholder="Search your memories" value={search} onChange={event => setSearch(event.target.value)} /><span className="subtext">{data.memories.length} / {data.limit} notes</span></div>
    <div id="memory-list">{rows.length ? rows.map(note => {
      const expired = !!note.expires_at && new Date(note.expires_at) <= new Date()
      return <article key={note.id} className="memory-card">
        <div className="memory-card-top"><span className="memory-kind">{kinds[note.kind]}</span><span className="subtext">{expired ? "Expired" : `Updated ${relativeDate(note.updated_at)}`}</span></div>
        <h2>{note.title}</h2><p className="memory-content">{note.content}</p>
        <div className="memory-meta">{note.repo_url ? note.repo_url.replace("https://github.com/", "") : note.source.type === "observation" ? note.source.scope : "Across your sessions"}{note.expires_at && ` · ${expired ? "Expired" : "Expires"} ${new Date(note.expires_at).toLocaleDateString()}`}</div>
        <div className="memory-card-bottom"><span className="subtext">{note.source.type === "chat" ? "Learned from your message" : note.source.type === "observation" ? "Observed during work" : "Added by you"}</span><div><Button variant="ghost" className="quiet" data-memory-edit={note.id} onClick={() => setEditor({ note })}>Review &amp; edit</Button><Button variant="ghost" className="quiet danger" data-memory-delete={note.id} onClick={() => setDeleting(note)}>Delete</Button></div></div>
      </article>
    }) : <div className="memory-empty"><span aria-hidden="true">✧</span><h2>{query ? "No matching memories" : "Start with what matters to you"}</h2><p className="subtext">{query ? "Try another search or clear the search field." : "“Keep PR descriptions short.” “Use staging for benchmark runs.” Moyai can remember useful preferences as you work, or you can add one here."}</p></div>}</div>
    <p className="subtext memory-footnote">Moyai recalls a few relevant notes at a time. Project context and references expire after 90 days; edit to refresh them. Deleting a note stops future recall, but does not remove existing conversations or backups.</p>
    {editor && <MemoryEditor note={editor.note} close={() => setEditor(null)} />}
    {deleting && <DeleteMemory note={deleting} close={() => setDeleting(null)} />}
  </section>
}
