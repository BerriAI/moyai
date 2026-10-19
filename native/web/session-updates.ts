import type { SidebarSession } from "./session-actions"
import type { SessionEdit } from "./generated/api"

export type SessionUpdatePatch = { [K in "title" | "pinned" | "folder"]?: NonNullable<SessionEdit[K]> }
type Pending = { patch: SessionUpdatePatch }
type Entry = {
  saved: SessionUpdatePatch; pending: Pending[]; settledAt: number; tail: Promise<void>
  ownerId: string; controller: AbortController
}

export function sessionUpdatePatch(body: SessionEdit): SessionUpdatePatch | null {
  const keys = Object.keys(body)
  if (!keys.length || keys.some(key => !["title", "pinned", "folder"].includes(key))) return null
  if ("pinned" in body && typeof body.pinned !== "boolean") return null
  if ("folder" in body && typeof body.folder !== "string") return null
  if ("title" in body && typeof body.title !== "string") return null
  return {
    ...(typeof body.title === "string" ? { title: body.title } : {}),
    ...(typeof body.pinned === "boolean" ? { pinned: body.pinned } : {}),
    ...(typeof body.folder === "string" ? { folder: body.folder } : {}),
  }
}

/** Share pending edits and confirmed titles across independently refreshed views. */
export class SessionUpdates {
  private entries = new Map<string, Entry>()
  private version = 0

  constructor(
    private save: (id: string, patch: SessionUpdatePatch, signal: AbortSignal) => Promise<unknown>,
    private changed: () => void,
  ) {}

  checkpoint() { return this.version }

  apply<T extends SidebarSession>(row: T): T {
    if (row.source === "legacy" || row.read_only) return row
    const entry = this.entries.get(row.id)
    if (entry?.saved.title !== undefined && (row.owner_id !== entry.ownerId || row.deleted || row.deleting)) return row
    return entry ? Object.assign({}, row, entry.saved, ...entry.pending.map(item => item.patch)) : row
  }

  acceptRead(checkpoint: number, rows: readonly SidebarSession[], titlesOnly = false) {
    let changed = false
    for (const row of rows) {
      if (row.source === "legacy") continue
      const entry = this.entries.get(row.id)
      if (!entry) continue
      if (entry.saved.title !== undefined && (row.owner_id !== entry.ownerId || row.read_only || row.deleted || row.deleting)) {
        this.invalidate(row.id)
        continue
      }
      if (row.read_only || entry.pending.length || entry.settledAt > checkpoint) continue
      if (entry.saved.title !== undefined) {
        // Keep the last authoritative title: another mounted view may still
        // hold an older row, even after this read has confirmed the save.
        if (entry.saved.title !== row.title) {
          entry.saved.title = row.title
          entry.settledAt = ++this.version
          changed = true
        }
        if (!titlesOnly) entry.saved = { title: entry.saved.title }
      } else if (!titlesOnly) this.entries.delete(row.id)
    }
    if (changed) this.changed()
  }

  invalidate(id: string) {
    this.entries.get(id)?.controller.abort()
    this.entries.delete(id)
    this.version++
    this.changed()
  }

  clear() {
    this.version++
    for (const entry of this.entries.values()) entry.controller.abort()
    this.entries.clear()
    this.changed()
  }

  update(row: SidebarSession, patch: SessionUpdatePatch): Promise<void> {
    const id = row.id
    let entry = this.entries.get(id)
    if (!entry) {
      entry = { saved: {}, pending: [], settledAt: 0, tail: Promise.resolve(), ownerId: row.owner_id, controller: new AbortController() }
      this.entries.set(id, entry)
    }
    if (patch.title !== undefined && entry.saved.title === undefined) entry.saved.title = row.title
    const current = entry, pending = { patch: { ...patch } }
    current.pending.push(pending)
    this.version++
    this.changed()
    // Keep rapid changes in gesture order for this session, without blocking others.
    const result = current.tail.then(async () => {
      const cancelled = () => this.entries.get(id) !== current
      if (cancelled()) throw new DOMException("Cancelled", "AbortError")
      try {
        await this.save(id, pending.patch, current.controller.signal)
        if (cancelled()) throw new DOMException("Cancelled", "AbortError")
        Object.assign(current.saved, pending.patch)
      } catch (error) {
        if (cancelled()) throw new DOMException("Cancelled", "AbortError")
        throw error
      } finally {
        if (!cancelled()) {
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
