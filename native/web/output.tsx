import { useEffect, useRef, useState, type ReactNode } from "react"
import Markdown from "react-markdown"
import { CodeBlock } from "./code-block"
import remarkGfm from "remark-gfm"
import { Download, ExternalLink, FileCode2, FileImage, FileText, Film, Globe, Music, Plus, RefreshCw, Search, X } from "lucide-react"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { api, ApiError } from "./api"
import { attachSessionLink, getWorkspace, removeSessionLink, type SessionWorkspace } from "./generated/api"
import type { OutputResource } from "./workspace-types"
import { fileReference, previewFileLink, type OpenFile } from "./file-links"
import "./output.css"

export type { OutputResource } from "./workspace-types"
type Preview = {
  id: string; kind: "artifact" | "upload"; name: string; size: number; media_type: string
  preview_kind: "text" | "markdown" | "code" | "image" | "video" | "audio" | "pdf" | "unsupported"
  text: string | null; language: string | null; truncated: boolean; notice: string | null
  text_byte_limit: number; text_line_limit: number; download_url: string; content_url: string | null
}
type Workspace = SessionWorkspace

function externalUrl(value: string): string | undefined {
  if (/[\u0000-\u0020\u007f]/.test(value)) return
  try {
    const parsed = new URL(value)
    const authority = value.match(/^https?:\/\/([^/?#]*)/i)?.[1]
    if (["https:", "http:"].includes(parsed.protocol) && authority && !authority.includes("@") && !parsed.username && !parsed.password) return parsed.href
  } catch { /* Invalid links remain inert. */ }
}
function sizeLabel(size?: number) {
  if (size === undefined || !Number.isFinite(size) || size < 0) return ""
  return size < 1024 ? `${size} B` : size < 1024 ** 2 ? `${(size / 1024).toFixed(1)} KB` : `${(size / 1024 ** 2).toFixed(1)} MB`
}
function Notice({ children, error = false }: { children: ReactNode; error?: boolean }) {
  return <Alert className={`output-notice${error ? " output-error" : ""}`} variant={error ? "destructive" : "default"} role={error ? "alert" : "note"}>
    <AlertDescription>{children}</AlertDescription>
  </Alert>
}
function ResourceIcon({ kind, media }: { kind: OutputResource["kind"]; media?: string }) {
  if (kind === "link") return <Globe size={18} />
  if (media?.startsWith("image/")) return <FileImage size={18} />
  if (media?.startsWith("video/")) return <Film size={18} />
  if (media?.startsWith("audio/")) return <Music size={18} />
  return <FileText size={18} />
}
function Prose({ text, sessionId, onOpenFile }: { text: string; sessionId: string; onOpenFile?: OpenFile }) {
  return <div className="output-markdown" onClickCapture={event => previewFileLink(event, sessionId, onOpenFile)}><Markdown skipHtml remarkPlugins={[remarkGfm]} components={{
    pre: CodeBlock,
    a: ({ href, children }) => {
      if (href && onOpenFile && fileReference(href, sessionId, location.origin)) return <a href={href}>{children}</a>
      const url = href ? externalUrl(href) : undefined
      return url ? <a href={url} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>
    },
    // Markdown may refer to arbitrary remote/private resources; never load them.
    img: ({ alt }) => <span className="output-muted">[Image: {alt || "not embedded"}]</span>,
    table: ({ children }) => <div className="output-table" role="region" aria-label="Document table" tabIndex={0}><table>{children}</table></div>,
  }}>{text}</Markdown></div>
}

type OutputPanelProps = { sessionId: string; resource: OutputResource; active?: boolean; onOpenFile?: OpenFile; onName?: (name: string) => void }
export function OutputPanel({ sessionId, resource, active = true, onOpenFile, onName }: OutputPanelProps) {
  return resource.kind === "link"
    ? <ExternalPreview key={`${sessionId}:${resource.url}`} resource={resource} active={active} />
    : <FilePreview key={`${sessionId}:${resource.kind}:${resource.id}`} sessionId={sessionId} resource={resource} active={active} onOpenFile={onOpenFile} onName={onName} />
}

function ExternalPreview({ resource, active }: { resource: OutputResource; active: boolean }) {
  const heading = useRef<HTMLHeadingElement>(null)
  const url = externalUrl(resource.url)
  useEffect(() => { if (active) heading.current?.focus() }, [])
  return <section className="output-view" aria-label="Output preview">
    <header className="output-header"><h1 ref={heading} tabIndex={-1}>{resource.name || "External link"}</h1><Badge variant="outline">External link</Badge></header>
    <div className="output-body">
      {!url ? <Notice error>This link is unavailable. Only HTTP(S) links without embedded credentials can be opened.</Notice> : <Card className="output-card"><CardContent>
        <Globe size={28} aria-hidden="true" /><h2>{new URL(url).hostname}</h2>
        <p className="output-muted">This website has no inline preview in Moyai. Open the link to view its contents.</p>
        <p className="output-url">{url}</p>
        <Button variant="outline" asChild><a href={url} target="_blank" rel="noopener noreferrer"><ExternalLink size={16} />Open in new tab</a></Button>
      </CardContent></Card>}
    </div>
  </section>
}

function FilePreview({ sessionId, resource, active, onOpenFile, onName }: OutputPanelProps & { active: boolean }) {
  const [preview, setPreview] = useState<Preview | null>(null)
  const [error, setError] = useState("")
  const [mediaError, setMediaError] = useState(false)
  const [loading, setLoading] = useState(true)
  const [source, setSource] = useState(false)
  const [revision, setRevision] = useState(0)
  const heading = useRef<HTMLHeadingElement>(null)
  const nameCallback = useRef(onName)
  nameCallback.current = onName
  const path = resource.id
    ? `/sessions/${encodeURIComponent(sessionId)}/file-preview/${resource.kind}/${encodeURIComponent(resource.id)}`
    : null
  // Derive all file URLs from the scoped ID. Never load resource.url as media.
  const contentUrl = path ? `/api${path}/content` : undefined
  const downloadUrl = resource.id ? resource.kind === "artifact" ? `/api/files/${encodeURIComponent(resource.id)}` : `/api${path}/download` : undefined
  useEffect(() => { if (active) heading.current?.focus() }, [])
  useEffect(() => {
    if (!active) { setPreview(null); setError(""); setLoading(false); return }
    if (!path) { setLoading(false); setError("This file has no identifier. Reopen it from Files."); return }
    const controller = new AbortController()
    setLoading(true); setError(""); setMediaError(false)
    void api<Preview>(path, "GET", undefined, controller.signal).then(value => {
      if (!controller.signal.aborted) { setPreview(value); setLoading(false); nameCallback.current?.(value.name) }
    }, reason => {
      if (!controller.signal.aborted) { setPreview(null); setError(String(reason)); setLoading(false) }
    })
    return () => controller.abort()
  }, [path, revision, active])
  const name = preview?.name || resource.name
  const text = preview?.text
  return <section className="output-view" aria-label="Output preview">
    <header className="output-header">
      <div className="output-title"><ResourceIcon kind={resource.kind} media={preview?.media_type} /><h1 ref={heading} tabIndex={-1}>{name}</h1></div>
      <div className="output-meta">
        <span>{sizeLabel(preview?.size ?? resource.size)}</span>
        {preview?.media_type && <span>{preview.media_type}</span>}
        {preview?.language && <Badge variant="outline"><FileCode2 size={12} />{preview.language}</Badge>}
      </div>
      <div className="output-actions">
        {downloadUrl && <Button size="sm" variant="outline" asChild><a href={downloadUrl} download><Download size={15} />Download original</a></Button>}
        <Button size="sm" variant="ghost" disabled={!active || loading || !path} onClick={() => setRevision(value => value + 1)}><RefreshCw size={15} />Refresh</Button>
      </div>
    </header>
    <div className="output-body">
      {active && loading && <p className="output-muted" role="status">Loading preview…</p>}
      {error && <Notice error>{error}<Button size="sm" variant="outline" disabled={!path} onClick={() => setRevision(value => value + 1)}>Retry</Button></Notice>}
      {active && preview && !loading && <>
        {preview.notice && <Notice>{preview.notice}</Notice>}
        {preview.truncated && <Notice>Partial preview: up to {Math.round(preview.text_byte_limit / 1024)} KB and {preview.text_line_limit.toLocaleString()} lines. Download the original for the complete file.</Notice>}
        {preview.preview_kind === "markdown" && <div className="output-switch" role="group" aria-label="Markdown display">
          <Button variant={source ? "ghost" : "secondary"} size="sm" aria-pressed={!source} onClick={() => setSource(false)}>Preview</Button>
          <Button variant={source ? "secondary" : "ghost"} size="sm" aria-pressed={source} onClick={() => setSource(true)}>Source</Button>
        </div>}
        {text !== null && text !== undefined && (text.length === 0 ? <p className="output-muted">This file is empty.</p>
          : preview.preview_kind === "markdown" && !source ? <Prose text={text} sessionId={sessionId} onOpenFile={onOpenFile} />
            : <pre className="output-source" tabIndex={0} aria-label="File source"><code>{text}</code></pre>)}
        {preview.preview_kind === "image" && <img className="output-image" src={contentUrl} alt={name} onError={() => setMediaError(true)} />}
        {preview.preview_kind === "video" && <MediaPreview kind="video" url={contentUrl} name={name} onError={() => setMediaError(true)} />}
        {preview.preview_kind === "audio" && <MediaPreview kind="audio" url={contentUrl} name={name} onError={() => setMediaError(true)} />}
        {mediaError && <Notice error>This browser could not display the media. Refresh to retry, or download the original.</Notice>}
        {["pdf", "unsupported"].includes(preview.preview_kind) && <div className="output-unavailable"><FileText size={32} aria-hidden="true" /><p className="output-muted">The original file is available using Download original.</p></div>}
      </>}
    </div>
  </section>
}

function MediaPreview({ kind, url, name, onError }: { kind: "video" | "audio"; url?: string; name: string; onError: () => void }) {
  const media = useRef<HTMLMediaElement | null>(null)
  useEffect(() => {
    const element = media.current
    return () => {
      // Hidden/unmounted tabs must not keep playing or buffering in the background.
      element?.pause()
      element?.removeAttribute("src")
      element?.load()
    }
  }, [url, kind])
  return kind === "video"
    ? <video ref={node => { media.current = node }} className="output-video" src={url} controls playsInline preload="metadata" aria-label={name} onError={onError} />
    : <audio ref={node => { media.current = node }} className="output-audio" src={url} controls preload="metadata" aria-label={name} onError={onError} />
}

type FilesProps = { sessionId: string; onOpen: (resource: OutputResource) => void; active?: boolean }
export function FilesPanel({ sessionId, onOpen, active = true }: FilesProps) {
  return <Files key={sessionId} sessionId={sessionId} onOpen={onOpen} active={active} />
}
function Files({ sessionId, onOpen, active = true }: FilesProps) {
  const [data, setData] = useState<Workspace | null>(null)
  const [error, setError] = useState("")
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState("")
  const [revision, setRevision] = useState(0)
  const [sourceOpen, setSourceOpen] = useState(false)
  const [actionError, setActionError] = useState("")
  const [notice, setNotice] = useState("")
  const [removing, setRemoving] = useState(false)
  const pendingRemoval = useRef(false)
  const mounted = useRef(true)
  const addButton = useRef<HTMLButtonElement>(null)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  useEffect(() => {
    if (!active) { setData(null); setLoading(false); setSourceOpen(false); return }
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    const load = async () => {
      setLoading(true)
      try {
        const { data: value } = await getWorkspace({ path: { session_id: sessionId }, signal: controller.signal })
        if (!controller.signal.aborted) { setData(value); setError("") }
      } catch (reason) {
        if (!controller.signal.aborted) {
          if (reason instanceof ApiError && [401, 403, 404].includes(reason.status)) setData(null)
          setError(String(reason))
        }
      } finally {
        if (!controller.signal.aborted) { setLoading(false); timer = setTimeout(load, 8000) }
      }
    }
    void load()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [sessionId, revision, active])
  const outputs: OutputResource[] = data ? [
    ...data.files.map(file => ({ ...file, kind: "artifact" as const })),
    ...data.links.filter(link => link.kind === "output").map(link => ({
      kind: "link" as const, id: link.id ?? undefined, name: link.title || link.url, url: link.url,
    })),
  ] : []
  const sources: OutputResource[] = data ? [
    ...data.sources.map(file => ({ ...file, kind: "upload" as const })),
    ...data.links.filter(link => link.kind === "source").map(link => ({
      kind: "link" as const, id: link.id ?? undefined, name: link.title || link.url, url: link.url,
    })),
  ] : []
  const matching = (items: OutputResource[]) => items.filter(item => `${item.name} ${item.kind === "link" ? item.url : ""}`.toLocaleLowerCase().includes(search.toLocaleLowerCase()))
  const filteredOutputs = matching(outputs), filteredSources = matching(sources)
  async function removeSource(resource: OutputResource) {
    if (!active || pendingRemoval.current || !resource.id) return
    pendingRemoval.current = true; setRemoving(true); setActionError(""); setNotice("")
    try {
      await removeSessionLink({ path: { session_id: sessionId, link_id: resource.id } })
      if (mounted.current) { setRevision(value => value + 1); setNotice("Source link removed.") }
    } catch (reason) {
      if (mounted.current) setActionError(String(reason))
    } finally {
      pendingRemoval.current = false
      if (mounted.current) setRemoving(false)
    }
  }
  function items(resources: OutputResource[], sourceGroup = false) {
    return <div className="files-list">{resources.map(resource => {
      const removable = sourceGroup && resource.kind === "link" && resource.id
        && data?.links.some(link => link.id === resource.id && link.kind === "source" && link.can_remove)
      return <div className="files-resource-row" key={`${resource.kind}:${resource.id ?? resource.url}`}>
        <Button type="button" variant="ghost" className="files-item" disabled={!active} onClick={() => onOpen(resource)}>
          <ResourceIcon kind={resource.kind} media={resource.media_type} />
          <span><strong>{resource.name}</strong><small>{resource.kind === "link" ? sourceGroup ? "Source link" : "External link" : resource.kind === "upload" ? "Attached file" : "Saved file"}{resource.size !== undefined ? ` · ${sizeLabel(resource.size)}` : ""}</small></span>
        </Button>
        {removable && <Button type="button" size="icon-sm" variant="ghost" disabled={!active || removing} aria-label={`Remove source ${resource.name}`} onClick={() => void removeSource(resource)}><X size={14} /></Button>}
      </div>
    })}</div>
  }
  return <section className="files-view" aria-label="Files and sources">
    <header className="files-header"><h2>Files and sources</h2><Button size="icon-sm" variant="ghost" aria-label="Refresh files" disabled={!active || loading} onClick={() => setRevision(value => value + 1)}><RefreshCw size={16} /></Button></header>
    <div className="files-search"><Search size={16} aria-hidden="true" /><Input type="search" aria-label="Find a file or link" placeholder="Find a file or link…" value={search} onChange={event => setSearch(event.target.value)} disabled={!active} /></div>
    {error && <Notice error>{error}<Button size="sm" variant="outline" onClick={() => setRevision(value => value + 1)}>Retry</Button></Notice>}
    {actionError && <Notice error>{actionError}<Button size="sm" variant="ghost" onClick={() => setActionError("")}>Dismiss</Button></Notice>}
    {notice && <p className="output-muted" role="status">{notice}</p>}
    {!data && loading && <p className="output-muted" role="status">Loading files…</p>}
    {data && search && !filteredOutputs.length && !filteredSources.length && <div className="files-empty"><p className="output-muted">No matching files or links.</p><Button size="sm" variant="ghost" onClick={() => setSearch("")}>Clear search</Button></div>}
    <section className="files-group" aria-label="Outputs">
      <div className="files-group-heading"><h3>Outputs</h3><span>{filteredOutputs.length}</span></div>
      {items(filteredOutputs)}
      {data && !outputs.length && <p className="output-muted">Saved files and attached output links will appear here.</p>}
    </section>
    <section className="files-group" aria-label="Sources">
      <div className="files-group-heading"><h3>Sources</h3><span>{filteredSources.length}</span>
        <Button ref={addButton} type="button" size="sm" variant="ghost" disabled={!active || !data || removing} onClick={() => { if (!pendingRemoval.current) setSourceOpen(true) }}><Plus size={15} />Add source link</Button>
      </div>
      {items(filteredSources, true)}
      {data && !sources.length && <p className="output-muted">Attach a source link or send a file in this session.</p>}
    </section>
    <SourceLinkDialog sessionId={sessionId} active={active} open={sourceOpen} onOpenChange={setSourceOpen}
      onFailure={setActionError} returnFocus={() => { if (active) addButton.current?.focus() }}
      onSaved={() => { setSearch(""); setActionError(""); setNotice("Source link added."); setRevision(value => value + 1) }} />
  </section>
}

function SourceLinkDialog({ sessionId, active, open, onOpenChange, onSaved, onFailure, returnFocus }: {
  sessionId: string; active: boolean; open: boolean; onOpenChange: (open: boolean) => void
  onSaved: () => void; onFailure: (message: string) => void; returnFocus: () => void
}) {
  const [url, setUrl] = useState("")
  const [title, setTitle] = useState("")
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  const pending = useRef(false)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  return <Dialog open={open && active} onOpenChange={value => { if (!pending.current) onOpenChange(value) }}>
    <DialogContent className="files-source-dialog" showCloseButton={!busy} onCloseAutoFocus={event => { event.preventDefault(); returnFocus() }}>
      <DialogHeader><DialogTitle>Add source link</DialogTitle><DialogDescription>The link will be visible to everyone who can access this session.</DialogDescription></DialogHeader>
      <form className="files-source-form" onSubmit={async event => {
        event.preventDefault()
        if (!active || pending.current) return
        const value = url.trim()
        if (!externalUrl(value) || new TextEncoder().encode(value).length > 2048) {
          setError("Use an HTTP(S) address without credentials, up to 2,048 bytes.")
          return
        }
        pending.current = true; setBusy(true); setError(""); onFailure("")
        try {
          // Once submitted, let this idempotent native write settle even if the
          // panel is hidden. Abort only reads; hiding must not imply an undo.
          await attachSessionLink({ path: { session_id: sessionId }, body: { kind: "source", url: value, title: title.trim() } })
          if (mounted.current) { setUrl(""); setTitle(""); onOpenChange(false); onSaved() }
        } catch (reason) {
          if (mounted.current) { setError(String(reason)); onFailure(String(reason)) }
        } finally {
          pending.current = false
          if (mounted.current) setBusy(false)
        }
      }}>
        <label>Source URL<Input autoFocus type="url" required maxLength={2048} placeholder="https://…" value={url} disabled={busy} onChange={event => setUrl(event.target.value)} /></label>
        <label>Label (optional)<Input value={title} maxLength={200} disabled={busy} onChange={event => setTitle(event.target.value)} /></label>
        {error && <Notice error>{error}</Notice>}
        <div className="files-source-actions"><Button type="button" variant="outline" disabled={busy} onClick={() => onOpenChange(false)}>Cancel</Button><Button type="submit" disabled={busy || !url.trim()}>{busy ? "Adding…" : "Add source"}</Button></div>
      </form>
    </DialogContent>
  </Dialog>
}
