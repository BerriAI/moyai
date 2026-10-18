import { useEffect, useRef, useState, type ComponentProps, type ReactNode } from "react"
import Markdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { Tabs as TabsPrimitive } from "radix-ui"
import { ArrowLeft, ChevronRight, ExternalLink, GitMerge, GitPullRequest, RefreshCw } from "lucide-react"
import { Button, buttonVariants } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { cn } from "@/lib/utils"
import { api, ApiError } from "./api"
import "./pull-request.css"

export type PRSummary = {
  url: string; title: string; repository: string; number: number
  state: "open" | "closed" | "merged" | "unknown"; draft: boolean; accessible: boolean
  error?: string; origin?: "attached" | "mentioned"
}
type Author = { login: string; bot: boolean }
type Detail = PRSummary & {
  description?: string; description_limited?: boolean; author?: Author; base?: string; head?: string
  head_sha?: string; base_sha?: string; additions?: number; deletions?: number
  changed_files?: number; commits?: number; updated_at?: string
}
type FileChange = {
  filename: string; previous_filename: string; status: string; additions: number; deletions: number
  patch: string; patch_status: "available" | "partial" | "unavailable"
}
type Discussion = {
  id: string; kind: string; author: Author; body: string; state: string; path: string
  line?: number; created_at: string; url: string | null; body_limited: boolean
}
type Commit = { sha: string; message: string; author: Author; created_at: string; url: string | null }
type Check = {
  id: string; name: string; status: string; url: string | null; title: string; summary: string
  text: string; annotations: number; output_limited: boolean
}
type Section<T> = { items: T[]; limited: boolean; error?: string | null; notice?: string; head_sha?: string }
type Sections = {
  files: Section<FileChange>; discussion: Section<Discussion>; commits: Section<Commit>; checks: Section<Check>
}
type Tab = "description" | keyof Sections
const tabs: { id: Tab; label: string }[] = [
  { id: "files", label: "Changes" }, { id: "description", label: "Description" },
  { id: "discussion", label: "Discussion" }, { id: "commits", label: "Commits" }, { id: "checks", label: "Checks" },
]
type Props = { sessionId: string; url: string; onBack: () => void; onUpdated?: (summary: PRSummary) => void }

// Shadcn's Radix Tabs composition, kept local while this slice owns only its
// feature files. Existing Button variants and .pr-view styles preserve geometry.
function Tabs(props: ComponentProps<typeof TabsPrimitive.Root>) {
  return <TabsPrimitive.Root data-slot="tabs" {...props} />
}
function TabsList(props: ComponentProps<typeof TabsPrimitive.List>) {
  return <TabsPrimitive.List data-slot="tabs-list" {...props} />
}
function TabsTrigger({ className, ...props }: ComponentProps<typeof TabsPrimitive.Trigger>) {
  return <TabsPrimitive.Trigger data-slot="tabs-trigger" className={cn(buttonVariants({ variant: "ghost", size: "sm" }), className)} {...props} />
}
function TabsContent(props: ComponentProps<typeof TabsPrimitive.Content>) {
  return <TabsPrimitive.Content data-slot="tabs-content" {...props} />
}
function Notice({ children, error = false, role }: { children: ReactNode; error?: boolean; role?: "status" }) {
  return <Alert className={`pr-notice${error ? " pr-error" : ""}`} variant={error ? "destructive" : "default"}
    role={error ? "alert" : role ?? "note"}>
    <AlertDescription>{children}</AlertDescription>
  </Alert>
}

export function PullRequestPanel(props: Props) {
  // Route changes destroy the previous request state before the next paint.
  return <Panel key={`${props.sessionId}:${props.url}`} {...props} />
}

function safeLink(value: string | null | undefined): string | undefined {
  if (!value) return
  try {
    const parsed = new URL(value)
    // URL normalizes empty userinfo away; check the original authority as well.
    const authority = value.match(/^https:\/\/([^/?#]*)/i)?.[1]
    if (parsed.protocol === "https:" && authority && !authority.includes("@") && !parsed.username && !parsed.password) return parsed.href
  } catch { /* no link */ }
}
function External({ url, children }: { url?: string | null; children: ReactNode }) {
  const href = safeLink(url)
  return href ? <a href={href} target="_blank" rel="noreferrer noopener">{children}<ExternalLink size={13} aria-label="opens in a new tab" /></a> : <span>{children}</span>
}
function Prose({ children }: { children: string }) {
  return <div className="pr-prose"><Markdown remarkPlugins={[remarkGfm]} components={{
    a: ({ href, children }) => <External url={href}>{children}</External>,
    // No tracking images or remote embeds from PR Markdown.
    img: ({ alt }) => <span className="pr-muted">[Image: {alt || "view on GitHub"}]</span>,
  }}>{children}</Markdown></div>
}
function date(value?: string) {
  if (!value) return ""
  const parsed = new Date(value)
  return Number.isNaN(parsed.valueOf()) ? "" : parsed.toLocaleString()
}
function stateLabel(detail: PRSummary) {
  return !detail.accessible || detail.state === "unknown" ? "Unavailable" : detail.state === "merged" ? "Merged"
    : detail.state === "closed" ? "Closed" : detail.draft ? "Draft" : "Ready for review"
}
function Panel({ sessionId, url, onBack, onUpdated }: Props) {
  const [detail, setDetail] = useState<Detail | null>(null)
  const [tab, setTab] = useState<Tab>("files")
  const [sections, setSections] = useState<Partial<Sections>>({})
  const [error, setError] = useState("")
  const [sectionError, setSectionError] = useState("")
  const [loading, setLoading] = useState(true)
  const [sectionLoading, setSectionLoading] = useState(false)
  const [revision, setRevision] = useState(0)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const heading = useRef<HTMLHeadingElement>(null)
  const updated = useRef(onUpdated)
  updated.current = onUpdated
  const path = `/sessions/${encodeURIComponent(sessionId)}/pull-request?url=${encodeURIComponent(url)}`

  useEffect(() => { heading.current?.focus() }, [])
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true); setError(""); setSections({}); setSectionError(""); setSectionLoading(false)
    void api<Detail>(`${path}&refresh=${revision > 0}`, "GET", undefined, controller.signal).then(
      value => {
        if (controller.signal.aborted) return
        setDetail(value); setLoading(false); updated.current?.(value)
      },
      reason => {
        if (controller.signal.aborted) return
        setDetail(null); setError(String(reason)); setLoading(false)
      },
    )
    return () => controller.abort()
  }, [path, revision])

  useEffect(() => {
    setSectionError("")
    if (loading || !detail?.accessible || tab === "description" || sections[tab]) {
      setSectionLoading(false)
      return
    }
    const controller = new AbortController()
    const selected = tab
    setSectionLoading(true); setSectionError("")
    void api<Detail & { section?: Sections[typeof selected] }>(
      `${path}&tab=${selected}&refresh=${revision > 0}`, "GET", undefined, controller.signal,
    ).then(value => {
      if (controller.signal.aborted) return
      // A subsequent tab read may discover loss of provider access or a new head.
      setDetail(value)
      if (value.section) setSections(previous => ({
        ...(detail.head_sha === value.head_sha && detail.base_sha === value.base_sha ? previous : {}),
        [selected]: value.section,
      }))
      else setSections({})
      setSectionLoading(false)
    }, reason => {
      if (controller.signal.aborted) return
      setSectionError(String(reason)); setSectionLoading(false)
      if (reason instanceof ApiError && [401, 403, 404, 409].includes(reason.status)) {
        setDetail(null); setSections({}); setError(String(reason))
      }
    })
    return () => controller.abort()
    // Loaded sections stay mounted/cached only in this panel. Server scope checks
    // run on every request; Refresh discards all section snapshots.
  }, [path, revision, tab, loading, detail?.accessible])

  const refresh = () => { setRevision(value => value + 1) }
  const state = detail ? stateLabel(detail) : "Loading"
  const active = tab === "description" ? null : sections[tab]
  const expansion = { values: expanded, update: (key: string, open: boolean) => setExpanded(previous => ({ ...previous, [key]: open })) }
  return <section className="pr-view" aria-label="Pull request">
    <header className="pr-header">
      <div className="pr-actions">
        <Button variant="ghost" size="sm" onClick={onBack}><ArrowLeft size={16} />Back</Button>
        <div className="pr-actions">
          <Button variant="outline" size="sm" disabled={loading || sectionLoading} onClick={refresh}><RefreshCw size={15} />Refresh</Button>
          {safeLink(detail?.url || url) && <Button variant="outline" size="sm" asChild><a href={safeLink(detail?.url || url)} target="_blank" rel="noreferrer noopener"><ExternalLink size={15} />Open in GitHub</a></Button>}
        </div>
      </div>
      <div className="pr-identity">
        {detail?.state === "merged" ? <GitMerge size={16} /> : <GitPullRequest size={16} />}
        <h1 ref={heading} tabIndex={-1}>{detail?.title || "Pull request details"}</h1>
      </div>
      {detail && <Collapsible className="pr-metadata">
        <div className="pr-summary">
          <Badge variant="outline" className={`pr-state pr-state-${detail.accessible ? detail.draft && detail.state === "open" ? "draft" : detail.state : "unknown"}`}>{state}</Badge>
          {detail.accessible && <span className="pr-stats">
            <span>{detail.changed_files ?? 0} {(detail.changed_files ?? 0) === 1 ? "file" : "files"}</span>
            <span className="pr-added">+{detail.additions ?? 0}</span><span className="pr-removed">−{detail.deletions ?? 0}</span>
          </span>}
          <CollapsibleTrigger asChild><Button variant="ghost" size="sm" className="pr-details-trigger" aria-label="Review details"><ChevronRight className="pr-chevron" aria-hidden="true" />Details</Button></CollapsibleTrigger>
        </div>
        <CollapsibleContent className="pr-metadata-content">
          <p className="pr-repository"><code>{detail.repository} #{detail.number}</code></p>
          {detail.author && <p className="pr-muted">by {detail.author.login}</p>}
          {detail.head && <p className="pr-branches"><code>{detail.head}</code><span aria-label="into"> → </span><code>{detail.base || "unknown base"}</code></p>}
          {detail.accessible && <div className="pr-meta">
            {detail.head_sha && <code title={detail.head_sha}>{detail.head_sha.slice(0, 7)}</code>}
            {detail.updated_at && <span className="pr-muted">Updated {date(detail.updated_at)}</span>}
          </div>}
        </CollapsibleContent>
      </Collapsible>}
    </header>
    {loading && <Notice role="status">Loading pull request…</Notice>}
    {(error || detail?.error) && <Notice error><p>{error || detail?.error}</p><Button variant="outline" size="sm" disabled={loading} onClick={refresh}>Retry</Button></Notice>}
    {detail?.accessible && !loading && <Tabs value={tab} onValueChange={value => setTab(value as Tab)}>
      <TabsList className="pr-tabs" aria-label="Pull request sections">
        {tabs.map(item => <TabsTrigger key={item.id} value={item.id}>{item.label}</TabsTrigger>)}
      </TabsList>
      {tabs.map(item => <TabsContent className="pr-content" key={item.id} value={item.id}
        forceMount hidden={tab !== item.id}>
        {item.id === "description" ? <>
          {detail.description ? <Prose>{detail.description}</Prose> : <p className="pr-muted">No description provided.</p>}
          {detail.description_limited && <Notice>Description shortened. Open in GitHub for the full text.</Notice>}
        </> : <>
          {tab === item.id && sectionLoading && !active && <p role="status">Loading {item.label.toLowerCase()}…</p>}
          {tab === item.id && sectionError && <Notice error>{sectionError}<Button variant="outline" size="sm" onClick={refresh}>Retry</Button></Notice>}
          {sections[item.id]?.error && <Notice error>{sections[item.id]?.error}</Notice>}
          {sections[item.id]?.limited && <Notice>Showing a limited page from GitHub (up to 100 per source). Open in GitHub for the complete record.</Notice>}
          {item.id === "files" && sections.files && <Changes section={sections.files} expansion={expansion} />}
          {item.id === "discussion" && sections.discussion && <DiscussionList section={sections.discussion} />}
          {item.id === "commits" && sections.commits && <CommitList section={sections.commits} />}
          {item.id === "checks" && sections.checks && <Checks section={sections.checks} expansion={expansion} />}
        </>}
      </TabsContent>)}
    </Tabs>}
  </section>
}

type Expansion = { values: Record<string, boolean>; update: (key: string, open: boolean) => void }

function Changes({ section, expansion }: { section: Section<FileChange>; expansion: Expansion }) {
  return <div className="pr-files">
    {!section.items.length && !section.error && <p className="pr-muted">No changed files returned by GitHub.</p>}
    {section.items.map(file => <Collapsible className="pr-file" key={file.filename}
      open={expansion.values[`file:${file.filename}`] ?? section.items.length <= 3}
      onOpenChange={open => expansion.update(`file:${file.filename}`, open)}>
      <CollapsibleTrigger className="pr-file-trigger"><span className="pr-filename"><ChevronRight className="pr-chevron" aria-hidden="true" />{file.filename}{file.previous_filename && <small>Renamed from {file.previous_filename}</small>}</span>
        <span className="pr-file-stats"><span>{file.status}</span><span className="pr-added">+{file.additions}</span><span className="pr-removed">−{file.deletions}</span></span>
      </CollapsibleTrigger>
      <CollapsibleContent className="pr-file-content">
      {file.patch_status !== "available" && <Notice>{file.patch_status === "partial" ? "Partial patch. Some changes are omitted." : "Patch unavailable. This may be a binary, empty, or large file."} Open in GitHub to inspect the full change.</Notice>}
      {file.patch && <pre className="pr-diff" tabIndex={0} aria-label={`Diff for ${file.filename}`}><code>{file.patch.split("\n").map((line, index) =>
        <span key={index} className={line.startsWith("@@") ? "pr-hunk" : line.startsWith("+") ? "pr-diff-added" : line.startsWith("-") ? "pr-diff-removed" : ""}>{line}{"\n"}</span>,
      )}</code></pre>}
      </CollapsibleContent>
    </Collapsible>)}
    {section.notice && <p className="pr-muted pr-section-note">{section.notice}</p>}
  </div>
}
function DiscussionList({ section }: { section: Section<Discussion> }) {
  return <div className="pr-list">
    {!section.items.length && !section.error && <p className="pr-muted">No discussion returned by GitHub.</p>}
    {section.items.map(item => <article key={item.id} className="pr-entry">
      <div className="pr-entry-heading"><strong>{item.author.login}</strong>{item.author.bot && <Badge variant="secondary">Bot</Badge>}
        <span className="pr-muted">{item.kind}{item.state ? ` · ${item.state.replaceAll("_", " ").toLowerCase()}` : ""}</span>
        <External url={item.url}>{date(item.created_at)}</External>
      </div>
      {item.path && <p className="pr-muted"><code>{item.path}{item.line ? `:${item.line}` : ""}</code></p>}
      {item.body ? <Prose>{item.body}</Prose> : <p className="pr-muted">No written comment.</p>}
      {item.body_limited && <Notice>Comment shortened. Open in GitHub for the full text.</Notice>}
    </article>)}
  </div>
}
function CommitList({ section }: { section: Section<Commit> }) {
  return <div className="pr-list">
    {!section.items.length && !section.error && <p className="pr-muted">No commits returned by GitHub.</p>}
    {section.items.map(item => <article key={item.sha} className="pr-entry">
      <div className="pr-entry-heading"><External url={item.url}><code>{item.sha.slice(0, 7)}</code></External><span>{item.author.login}</span><span className="pr-muted">{date(item.created_at)}</span></div>
      <pre>{item.message}</pre>
    </article>)}
  </div>
}
function Checks({ section, expansion }: { section: Section<Check>; expansion: Expansion }) {
  return <div className="pr-list">
    {section.head_sha && <p className="pr-muted">Checks for <code>{section.head_sha.slice(0, 7)}</code></p>}
    {!section.items.length && !section.error && <p className="pr-muted">No checks reported for this revision. This does not establish that CI passed.</p>}
    {section.items.map(item => <article className="pr-entry" key={item.id}>
      <div className="pr-entry-heading"><External url={item.url}><strong>{item.name}</strong></External>
        <Badge variant="outline" className={`pr-check-${["success", "failure", "error"].includes(item.status) ? item.status : "pending"}`}>{item.status.replaceAll("_", " ")}</Badge>
      </div>
      {item.title && <p>{item.title}</p>}
      {(item.summary || item.text) && <Collapsible open={expansion.values[`check:${item.id}`] ?? false}
        onOpenChange={open => expansion.update(`check:${item.id}`, open)}>
        <CollapsibleTrigger className="pr-disclosure-trigger"><ChevronRight className="pr-chevron" aria-hidden="true" />Check output and findings</CollapsibleTrigger>
        <CollapsibleContent><Prose>{[item.summary, item.text].filter(Boolean).join("\n\n")}</Prose></CollapsibleContent>
      </Collapsible>}
      {item.annotations > 0 && <p className="pr-muted">{item.annotations} file annotations available in GitHub.</p>}
      {item.output_limited && <Notice>Check output shortened. Open in GitHub for the full output.</Notice>}
    </article>)}
  </div>
}
