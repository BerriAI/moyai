import React, { useEffect, useRef, useState } from "react"
import { ArrowUp, Globe, LockKeyhole } from "lucide-react"
import { Button } from "@/components/ui/button"
import { SkillComposer } from "./skill-composer"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { DecodeHeadline } from "./decode-headline"
import { SpaceBackdrop } from "./space-backdrop"
import "./home.css"
import meteorPoster from "./assets/moyai-meteor-poster.webp"
import { api, csrfToken } from "./api"
import { AttachButton, AttachmentPreviews, useAttachments, useFileDrop, FileDropOverlay } from "./attachments"
import type { NewConversation, Session, Turn } from "./conversation-state"

type HomeConnection = { id: string; name: string; enabled: boolean; shared: boolean; read_only: boolean }
export function homeSubmission(prompt: string, model: string, privateSession: boolean, attachments: string[], selected: string[], available: HomeConnection[]) {
  const connections = [...new Set(selected)].sort()
  const allowed = new Set(available.filter(row => row.enabled && (privateSession || row.shared)).map(row => row.id))
  if (connections.some(id => !allowed.has(id))) throw new Error("Account access changed. Review the selected accounts before sending.")
  const body = { prompt, model, attachments, connections }, visibility = privateSession ? "private" : "shared"
  return { body, visibility, signature: JSON.stringify([visibility, body]) }
}

export function Home({ models, defaultModel, individual = true, productMode = "standalone", connectionVersion = "", onCreated, onStarted }: {
  models: string[]; defaultModel: string; individual?: boolean; productMode?: "standalone" | "existing"; connectionVersion?: string
  onCreated: () => Promise<void>; onStarted: (conversation: NewConversation) => void
}) {
  const [draft, setDraft] = useState("")
  const [model, setModel] = useState(defaultModel)
  const [privateSession, setPrivateSession] = useState(false)
  const isPrivate = individual && privateSession
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const pictures = useAttachments(setError)
  const fileDrop = useFileDrop(pictures.add, setError, busy)
  const [accountsVersion, setAccountsVersion] = useState(0)
  const [skippedScope, setSkippedScope] = useState("")
  const [catalog, setCatalog] = useState<{ scope: string; rows: HomeConnection[]; loading: boolean; error: string }>({ scope: "", rows: [], loading: false, error: "" })
  const accountScope = `${isPrivate ? "private" : "shared"}:${connectionVersion}:${accountsVersion}`
  const accounts = catalog.scope === accountScope ? catalog.rows : []
  const skipAccounts = skippedScope === accountScope
  const selectedAccounts = skipAccounts ? [] : accounts.map(row => row.id)
  const accountsReady = productMode !== "existing" || skipAccounts || (catalog.scope === accountScope && !catalog.loading && !catalog.error)
  const sculpture = useRef<HTMLDivElement>(null)
  const admission = useRef<{ signature: string; session?: Session; request: string } | null>(null)
  const submitting = useRef(false)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    document.title = "New session · Moyai"
    let disposed = false
    let cleanupSculpture = () => {}
    // The composer is useful before the decorative WebGL module is downloaded.
    void import("@/moyai-sculpture").then(({ mountSculpture }) => {
      if (!disposed) cleanupSculpture = mountSculpture(sculpture.current)
    }).catch(() => {}) // The static logo remains a complete fallback.
    return () => { disposed = true; mounted.current = false; cleanupSculpture() }
  }, [])
  useEffect(() => {
    if (productMode !== "existing") return
    const controller = new AbortController(), scope = accountScope
    setCatalog({ scope, rows: [], loading: true, error: "" })
    void api<HomeConnection[]>(`/connections?shared=${!isPrivate}`, "GET", undefined, controller.signal).then(rows => {
      if (controller.signal.aborted) return
      const eligible = rows.filter(row => row.enabled && (isPrivate || row.shared))
      setCatalog({ scope, rows: eligible, loading: false, error: "" })
    }, reason => { if (!controller.signal.aborted) setCatalog({ scope, rows: [], loading: false, error: String(reason) }) })
    return () => controller.abort()
  }, [productMode, accountScope, isPrivate])

  async function send(prompt = draft) {
    if (submitting.current || pictures.uploading || pictures.hasErrors || !accountsReady || (!prompt.trim() && !pictures.files.length)) return
    submitting.current = true
    setBusy(true); setError("")
    const auth = csrfToken(), attachments = pictures.readyFiles
    try {
      const submission = homeSubmission(prompt, model, isPrivate, attachments.map(file => file.id), selectedAccounts, accounts)
      const signature = submission.signature
      if (admission.current?.signature !== signature) admission.current = { signature, request: crypto.randomUUID() }
      if (!admission.current.session) {
        admission.current.session = await api<Session>("/sessions", "POST", { model, visibility: submission.visibility })
      }
      const turn = await api<Turn>(`/sessions/${admission.current.session.id}/messages`, "POST", {
        ...submission.body, request_key: admission.current.request,
      })
      if (!mounted.current || csrfToken() !== auth) return
      onStarted({ session: admission.current.session, turn, prompt, attachments })
      await onCreated()
    } catch (error) { if (mounted.current && csrfToken() === auth) setError(String(error)) }
    finally { submitting.current = false; if (mounted.current) setBusy(false) }
  }

  return <section className="home file-drop-zone" {...fileDrop.handlers}>
    {fileDrop.dragging && <FileDropOverlay />}
    <SpaceBackdrop />
    <div className="home-sculpture" ref={sculpture}><img src={meteorPoster} alt="Moyai meteor" /></div>
    <div className="home-content">
      <p className="home-eyebrow">A space for your next big idea</p>
      <DecodeHeadline paused={busy || Boolean(draft)} />
      <form className="home-form" onSubmit={event => { event.preventDefault(); void send() }}>
        {error && <p className="error" role="alert">{error}</p>}
        {productMode === "existing" && catalog.scope === accountScope && catalog.error && !skipAccounts && <div className="home-connection-error" role="alert">
          <span className="error">{catalog.error}</span>
          <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => setAccountsVersion(value => value + 1)}>Retry connections</Button>
          <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => setSkippedScope(accountScope)}>Continue without connections</Button>
        </div>}
        <div className="composer home-composer">
          <AttachmentPreviews files={pictures.files} remove={busy ? undefined : pictures.remove} retry={busy ? undefined : pictures.retry} draft />
          <SkillComposer value={draft} onChange={setDraft} onSend={prompt => void send(prompt)} shared={!isPrivate} disabled={busy} placeholder="Ask Moyai to build, investigate, or type / for skills…" onPaste={pictures.paste} />
          <div className="composer-actions">
            <div className="home-composer-tools">
              <AttachButton add={pictures.add} disabled={busy} compact />
              <Button type="button" variant="ghost" size="sm" className="home-privacy-toggle" aria-label="Private session" aria-pressed={isPrivate} disabled={!individual || busy}
                title={!individual ? "Private sessions require an individual sign-in." : isPrivate ? "Private · Only you. Click to share with your workspace." : "Public · Shared with your workspace. Click to make private."}
                onClick={() => { setSkippedScope(""); setPrivateSession(value => !value) }}>
                {isPrivate ? <LockKeyhole /> : <Globe />}{isPrivate ? "Private" : "Public"}
              </Button>
            </div>
            <div className="send-actions"><Select value={model} onValueChange={setModel}><SelectTrigger aria-label="Model"><SelectValue /></SelectTrigger><SelectContent>{models.map(value => <SelectItem value={value} key={value}>{value.replace(/^openai\//, "")}</SelectItem>)}</SelectContent></Select>
              <Button type="submit" size="icon" aria-label="Send message" disabled={busy || pictures.uploading || pictures.hasErrors || !accountsReady || (!draft.trim() && !pictures.files.length)}><ArrowUp /></Button>
            </div>
          </div>
        </div>
        <div className="home-composer-hint">
          {pictures.uploading && <span role="status">Uploading files…</span>}
          {productMode === "existing" && !accountsReady && !catalog.error && <span role="status">Loading connections…</span>}
          <span>Drop files or paste images · Enter to send</span>
        </div>
      </form>
    </div>
  </section>
}
