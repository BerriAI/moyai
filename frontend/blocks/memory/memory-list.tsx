import type { MemoryNote } from "../../api/generated/types.gen"
import { Button } from "../../components/ui/button"
import { relativeDate } from "../../lib/time"
import { memoryKinds, memorySources } from "./labels"

function MemoryCard({ note, onEdit, onDelete }: { note: MemoryNote; onEdit(): void; onDelete(): void }) {
  const expired = !!note.expires_at && new Date(note.expires_at) <= new Date()
  const scope = note.repo_url ? note.repo_url.replace("https://github.com/", "") : note.source.type === "observation" ? note.source.scope : "Across your sessions"
  return <article className="memory-card">
    <div className="memory-card-top"><span className="memory-kind">{memoryKinds[note.kind]}</span><span className="subtext">{expired ? "Expired" : `Updated ${relativeDate(note.updated_at)}`}</span></div>
    <h2>{note.title}</h2><p className="memory-content">{note.content}</p>
    <div className="memory-meta">{scope}{note.expires_at && ` · ${expired ? "Expired" : "Expires"} ${new Date(note.expires_at).toLocaleDateString()}`}</div>
    <div className="memory-card-bottom"><span className="subtext">{memorySources[note.source.type]}</span><div>
      <Button variant="ghost" className="quiet" data-memory-edit={note.id} onClick={onEdit}>Review &amp; edit</Button>
      <Button variant="ghost" className="quiet danger" data-memory-delete={note.id} onClick={onDelete}>Delete</Button>
    </div></div>
  </article>
}

type Props = { notes: MemoryNote[]; filtered: boolean; onEdit(note: MemoryNote): void; onDelete(note: MemoryNote): void }

export function MemoryList({ notes, filtered, onEdit, onDelete }: Props) {
  return <div id="memory-list">{notes.length ? notes.map(note => <MemoryCard key={note.id} note={note} onEdit={() => onEdit(note)} onDelete={() => onDelete(note)} />) : (
    <div className="memory-empty"><span aria-hidden="true">✧</span>
      <h2>{filtered ? "No matching memories" : "Start with what matters to you"}</h2>
      <p className="subtext">{filtered ? "Try another search or clear the search field." : "“Keep PR descriptions short.” “Use staging for benchmark runs.” Moyai can remember useful preferences as you work, or you can add one here."}</p>
    </div>
  )}</div>
}
