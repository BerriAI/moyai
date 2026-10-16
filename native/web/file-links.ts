import type { MouseEvent } from "react"
import type { OutputResource } from "./workspace-types"

export type OpenFile = (resource: OutputResource, sessionId: string) => void
export type FileReference = { kind: "artifact" | "upload"; id: string; sessionId: string; url: string }

/** Recognize Moyai file routes, never an external URL merely ending in .mp4/.md. */
export function fileReference(href: string, sessionId: string, origin: string): FileReference | null {
  if (!href || /[\u0000-\u0020\u007f\\]/.test(href)) return null
  let url: URL
  try { url = new URL(href, origin + "/") } catch { return null }
  if (!["http:", "https:"].includes(url.protocol) || url.origin !== origin || url.username || url.password) return null
  const direct = url.pathname.match(/^\/api\/(files|uploads)\/([A-Za-z0-9_-]{1,200})\/?$/)
  if (direct && sessionId) return {
    kind: direct[1] === "files" ? "artifact" : "upload", id: direct[2], sessionId,
    url: `/api/${direct[1]}/${direct[2]}`,
  }
  const scoped = url.pathname.match(
    /^\/api\/sessions\/([A-Za-z0-9_-]{1,200})\/file-preview\/(artifact|upload)\/([A-Za-z0-9_-]{1,200})(?:\/(?:content|download))?\/?$/,
  )
  return scoped ? {
    kind: scoped[2] as "artifact" | "upload", id: scoped[3], sessionId: scoped[1],
    url: url.pathname,
  } : null
}

/** Ordinary link activation previews; browser modifiers and explicit downloads retain their intent. */
export function previewFileLink(event: MouseEvent<HTMLElement>, sessionId: string, open?: OpenFile): void {
  if (!open || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
  const anchor = event.target instanceof Element ? event.target.closest("a[href]") : null
  if (!(anchor instanceof HTMLAnchorElement) || !event.currentTarget.contains(anchor) || anchor.hasAttribute("download")) return
  const reference = fileReference(anchor.getAttribute("href") || "", sessionId, location.origin)
  if (!reference) return
  event.preventDefault()
  // The preview API authorizes the referenced file and returns its real name.
  open({
    kind: reference.kind, id: reference.id, url: reference.url,
    name: (anchor.textContent?.trim() || "Saved file").slice(0, 300),
  }, reference.sessionId)
}
