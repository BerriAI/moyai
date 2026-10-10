import { useMemo } from "react"
import type { MemoryNote } from "../../api/generated/types.gen"
import { Button } from "../../components/ui/button"
import { Dialog, DialogContent, DialogTitle } from "../../components/ui/dialog"
import { Input } from "../../components/ui/input"
import { Label } from "../../components/ui/label"
import { Textarea } from "../../components/ui/textarea"
import { FormSelect } from "../../form-select"
import { optionFragment } from "../../lib/options"
import { isMemoryKind, memoryKinds } from "./labels"

export type MemoryDraft = Pick<MemoryNote, "title" | "content" | "kind" | "repo_url">

type EditorProps = { note: MemoryNote | null; pending: boolean; error?: string; onSubmit(draft: MemoryDraft): void; onClose(): void }

export function MemoryEditorDialog({ note, pending, error, onSubmit, onClose }: EditorProps) {
  const kinds = useMemo(() => optionFragment(Object.entries(memoryKinds), note?.kind ?? "preference"), [note?.kind])
  const source = note?.source.type === "manual" ? null : note?.source
  return <Dialog open onOpenChange={open => { if (!open) onClose() }}>
    <DialogContent className="memory-editor" data-dialog-id="memory-dialog">
      <form id="memory-form" onSubmit={event => {
        event.preventDefault()
        const form = new FormData(event.currentTarget)
        const text = (name: string) => String(form.get(name) ?? "")
        const kind = text("kind")
        onSubmit({ title: text("title"), content: text("content"), kind: isMemoryKind(kind) ? kind : "preference", repo_url: text("repo_url") })
      }}>
        <DialogTitle>{note ? "Edit memory" : "Add a memory"}</DialogTitle><p className="subtext">Keep one useful idea per note. It stays in your personal library.</p>
        <div className="field"><Label htmlFor="memory-title">Title</Label><Input id="memory-title" name="title" required minLength={3} maxLength={120} defaultValue={note?.title ?? ""} placeholder="How I like PR descriptions" autoFocus /></div>
        <div className="field"><Label htmlFor="memory-kind">Type</Label><FormSelect id="memory-kind" name="kind" initialOptions={kinds} /></div>
        <div className="field"><Label htmlFor="memory-content">What should Moyai remember?</Label><Textarea id="memory-content" name="content" required minLength={3} maxLength={1200} rows={5} defaultValue={note?.content ?? ""} placeholder="Keep PR descriptions short and include the test results." /></div>
        <div className="field"><Label htmlFor="memory-repo">Repository (optional)</Label><Input type="url" id="memory-repo" name="repo_url" defaultValue={note?.repo_url ?? ""} placeholder="https://github.com/BerriAI/litellm" /><small>Leave blank to use across your sessions.</small></div>
        {source && <details className="memory-source"><summary>Why Moyai saved this</summary>
          {source.type === "observation" && <p>Observed during work in {source.scope}. Recheck if the environment changes.</p>}
          <blockquote>{source.type === "observation" ? source.evidence : source.quote}</blockquote>
          <a href={`#run=${source.run_id}`} id="memory-source-link" onClick={onClose}>Open original session</a>
        </details>}
        <p id="memory-error" role="alert">{error}</p>
        <div className="credential-actions"><Button type="submit" disabled={pending}>Save memory</Button><Button type="button" variant="ghost" className="quiet" id="memory-cancel" onClick={onClose}>Cancel</Button></div>
      </form>
    </DialogContent>
  </Dialog>
}

type DeleteProps = { note: MemoryNote; pending: boolean; error?: string; onConfirm(): void; onClose(): void }

// The least destructive choice takes focus.
export function DeleteMemoryDialog({ note, pending, error, onConfirm, onClose }: DeleteProps) {
  return <Dialog open onOpenChange={open => { if (!open) onClose() }}>
    <DialogContent className="memory-editor" data-dialog-id="memory-dialog" onOpenAutoFocus={event => { event.preventDefault(); document.querySelector<HTMLButtonElement>("#memory-cancel")?.focus() }}>
      <DialogTitle>Delete this memory?</DialogTitle><p>{note.title}</p>
      <p className="subtext">Moyai will stop recalling this note. Existing conversations and backups are not changed.</p>
      <p id="memory-error" role="alert">{error}</p>
      <div className="credential-actions">
        <Button variant="destructive" className="danger" id="memory-confirm-delete" disabled={pending} onClick={onConfirm}>Delete memory</Button>
        <Button variant="ghost" className="quiet" id="memory-cancel" onClick={onClose}>Cancel</Button>
      </div>
    </DialogContent>
  </Dialog>
}
