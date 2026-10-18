import React, { useEffect, useRef, useState } from "react"
import { ArrowUp, Bug, Code2, Globe, ListChecks, LockKeyhole } from "lucide-react"
import { Button } from "@/components/ui/button"
import { SkillComposer } from "./skill-composer"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { SpaceBackdrop } from "./space-backdrop"
import "./home.css"
import { api, csrfToken } from "./api"
import { AttachButton, AttachmentPreviews, useAttachments, useFileDrop, FileDropOverlay } from "./attachments"
import type { GeneralPreferences } from "./general-preferences"
import type { NewConversation, Session, Turn } from "./conversation-state"

type HomeConnection = { id: string; name: string; enabled: boolean; shared: boolean; read_only: boolean }
const starters = [
  { label: "Review code", prompt: "Review my changes for bugs and improvements.", Icon: Code2 },
  { label: "Fix a bug", prompt: "Help me investigate and fix this bug: ", Icon: Bug },
  { label: "Plan a feature", prompt: "Help me plan and implement this feature: ", Icon: ListChecks },
]
export function homeSubmission(prompt: string, model: string, privateSession: boolean, attachments: string[], selected: string[], available: HomeConnection[]) {
  const connections = [...new Set(selected)].sort()
  const allowed = new Set(available.filter(row => row.enabled && (privateSession || row.shared)).map(row => row.id))
  if (connections.some(id => !allowed.has(id))) throw new Error("Account access changed. Review the selected accounts before sending.")
  const body = { prompt, model, attachments, connections }, visibility = privateSession ? "private" : "shared"
  return { body, visibility, signature: JSON.stringify([visibility, body]) }
}

export function Home({ models, defaultModel, individual = true, productMode = "standalone", connectionVersion = "", preferences, onCreated, onStarted }: {
  models: string[]; defaultModel: string; individual?: boolean; productMode?: "standalone" | "existing"; connectionVersion?: string; preferences?: GeneralPreferences
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
  const needsAccounts = preferences?.connection_ids != null ? preferences.connection_ids.length > 0 : productMode === "existing"
  const selectedAccounts = skipAccounts || !needsAccounts ? [] : accounts.filter(row => preferences?.connection_ids == null || preferences.connection_ids.includes(row.id)).map(row => row.id)
  const accountsReady = !needsAccounts || skipAccounts || (catalog.scope === accountScope && !catalog.loading && !catalog.error)
  const composer = useRef<HTMLDivElement>(null)
  const admission = useRef<{ signature: string; session?: Session; request: string } | null>(null)
  const submitting = useRef(false)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    document.title = "New session · Moyai"
    return () => { mounted.current = false }
  }, [])
  useEffect(() => {
    if (!needsAccounts) return
    const controller = new AbortController(), scope = accountScope
    setCatalog({ scope, rows: [], loading: true, error: "" })
    void api<HomeConnection[]>(`/connections?shared=${!isPrivate}`, "GET", undefined, controller.signal).then(rows => {
      if (controller.signal.aborted) return
      const eligible = rows.filter(row => row.enabled && (isPrivate || row.shared))
      setCatalog({ scope, rows: eligible, loading: false, error: "" })
    }, reason => { if (!controller.signal.aborted) setCatalog({ scope, rows: [], loading: false, error: String(reason) }) })
    return () => controller.abort()
  }, [needsAccounts, accountScope, isPrivate])

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
    <div className="home-content">
      <header className="home-header">
        <p className="home-eyebrow"><span aria-hidden="true" />Your workspace</p>
        <h1>What are we working on?</h1>
        <p className="home-subtitle">Build something new, investigate an issue, or pick up where you left off.</p>
      </header>
      <form className="home-form" onSubmit={event => { event.preventDefault(); void send() }}>
        {error && <p className="error" role="alert">{error}</p>}
        {needsAccounts && catalog.scope === accountScope && catalog.error && !skipAccounts && <div className="home-connection-error" role="alert">
          <span className="error">{catalog.error}</span>
          <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => setAccountsVersion(value => value + 1)}>Retry connections</Button>
          <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => setSkippedScope(accountScope)}>Continue without connections</Button>
        </div>}
        <div className="composer home-composer">
          <AttachmentPreviews files={pictures.files} remove={busy ? undefined : pictures.remove} retry={busy ? undefined : pictures.retry} draft />
          <SkillComposer ref={composer} value={draft} onChange={setDraft} onSend={prompt => void send(prompt)} shared={!isPrivate} disabled={busy} placeholder="Describe the work, or type / for skills…" onPaste={pictures.paste} />
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
          {needsAccounts && !accountsReady && !catalog.error && <span role="status">Loading connections…</span>}
          <span>Drop files or paste images · Enter to send</span>
        </div>
      </form>
      <div className="home-starters" role="group" aria-label="Start with a prompt" aria-hidden={Boolean(draft.trim())}>
        {starters.map(({ label, prompt, Icon }) => <Button key={label} type="button" variant="outline" disabled={busy || Boolean(draft.trim())}
          onClick={() => {
            setDraft(prompt)
            requestAnimationFrame(() => {
              const input = composer.current
              if (!input) return
              input.focus()
              const selection = window.getSelection()
              selection?.selectAllChildren(input)
              selection?.collapseToEnd()
            })
          }}><Icon aria-hidden="true" />{label}</Button>)}
      </div>
    </div>
  </section>
}
