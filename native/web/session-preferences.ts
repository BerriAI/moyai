import type { SidebarSession } from "./session-actions"

export type SessionPreferences = Partial<Pick<SidebarSession, "pinned" | "folder">>
type Pending = { patch: SessionPreferences }
type Entry = { saved: SessionPreferences; pending: Pending[]; settledAt: number; tail: Promise<void> }

export function preferencePatch(body: object): SessionPreferences | null {
  const keys = Object.keys(body)
  if (!keys.length || keys.some(key => key !== "pinned" && key !== "folder")) return null
  if ("pinned" in body && typeof body.pinned !== "boolean") return null
  if ("folder" in body && typeof body.folder !== "string") return null
  return body as SessionPreferences
}

/** Overlay personal preferences until a read started after their save confirms them. */
export class SessionPreferenceUpdates {
  private entries = new Map<string, Entry>()
  private generation = 0
  private controller = new AbortController()
  private version = 0

  constructor(
    private save: (id: string, patch: SessionPreferences, signal: AbortSignal) => Promise<unknown>,
    private changed: () => void,
  ) {}

  checkpoint() { return this.version }

  apply<T extends SidebarSession>(row: T): T {
    if (row.source === "legacy" || row.read_only) return row
    const entry = this.entries.get(row.id)
    return entry ? Object.assign({}, row, entry.saved, ...entry.pending.map(item => item.patch)) : row
  }

  acceptRead(checkpoint: number, rows: readonly SidebarSession[]) {
    const confirmed = new Set(rows.filter(row => row.source !== "legacy" && !row.read_only).map(row => row.id))
    for (const [id, entry] of this.entries) {
      if (confirmed.has(id) && !entry.pending.length && entry.settledAt <= checkpoint) this.entries.delete(id)
    }
  }

  clear() {
    this.generation++
    this.version++
    this.controller.abort()
    this.controller = new AbortController()
    this.entries.clear()
    this.changed()
  }

  update(id: string, patch: SessionPreferences): Promise<void> {
    const generation = this.generation, signal = this.controller.signal
    let entry = this.entries.get(id)
    if (!entry) {
      entry = { saved: {}, pending: [], settledAt: 0, tail: Promise.resolve() }
      this.entries.set(id, entry)
    }
    const current = entry, pending = { patch: { ...patch } }
    current.pending.push(pending)
    this.version++
    this.changed()
    // Keep rapid changes in gesture order for this session, without blocking others.
    const result = current.tail.then(async () => {
      if (generation !== this.generation) throw new DOMException("Cancelled", "AbortError")
      try {
        await this.save(id, pending.patch, signal)
        if (generation !== this.generation) throw new DOMException("Cancelled", "AbortError")
        Object.assign(current.saved, pending.patch)
      } finally {
        if (generation === this.generation) {
          current.pending = current.pending.filter(item => item !== pending)
          current.settledAt = ++this.version
          this.changed()
        }
      }
    })
    current.tail = result.catch(() => {})
    return result
  }
}
