import type { SourcedHistorySession } from "../generated/api"

export type SessionReference = Pick<SourcedHistorySession, "id">
  & Partial<Pick<SourcedHistorySession, "source" | "url" | "source_session_id">>
export function sessionReferenceHash(session: SessionReference): string {
  const original = session.url?.match(/^\/#run=([a-f0-9]{32})$/)?.[1]
  return original ? `run=${original}` : session.source === "legacy" ? `run=${encodeURIComponent(session.id)}` : encodeURIComponent(session.id)
}
export function continuationReference(session: SessionReference, original: string): SessionReference | null {
  return /^[a-f0-9]{32}$/.test(original) && session.source === "native"
    && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(session.id)
    && session.source_session_id === original && session.url === `/#run=${original}` ? session : null
}
