import React, { useEffect, useRef, useState } from "react"
import { ArrowUpRight, Bug, Code2, FileCode2, Globe, ListChecks, LockKeyhole, Send } from "lucide-react"
import { Button } from "@/components/ui/button"
import { SkillComposer } from "./skill-composer"
import { GoalDialog, goalCommandAction } from "./goal"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { SpaceBackdrop } from "./space-backdrop"
import "./home.css"
import { api, csrfToken } from "./api"
import { AttachButton, AttachmentPreviews, useAttachments, useFileDrop, FileDropOverlay } from "./attachments"
import type { GeneralPreferences } from "./general-preferences"
import type { NewConversation, Session, Turn } from "./conversation-state"

type HomeConnection = { id: string; name: string; enabled: boolean; shared: boolean; read_only: boolean }
const starters = [
  { label: "Review code", detail: "Find the edge cases", prompt: "Review my changes for bugs and improvements.", Icon: Code2 },
  { label: "Fix a bug", detail: "Trace it to the source", prompt: "Help me investigate and fix this bug: ", Icon: Bug },
  { label: "Plan a feature", detail: "Make the next change clear", prompt: "Help me plan and implement this feature: ", Icon: ListChecks },
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
  const [goalDialog, setGoalDialog] = useState(false)
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
  const [composerFocus, setComposerFocus] = useState(0)
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
    if (prompt.trim() === "/goal") { setGoalDialog(true); return }
    const auth = csrfToken()
    try { await submit(prompt) }
    catch (reason) { if (mounted.current && csrfToken() === auth) setError(String(reason)) }
  }

  async function submit(prompt: string): Promise<void> {
    const action = goalCommandAction(prompt)
    if (action && action !== "set") throw new Error("Open an existing conversation to change its goal.")
    if (submitting.current) throw new Error("A message is already being sent.")
    if (pictures.uploading) throw new Error("Wait for the attached files to finish uploading, then retry.")
    if (pictures.hasErrors) throw new Error("Retry or remove the failed attachments before sending.")
    if (!accountsReady) throw new Error(catalog.error
      ? "Connections could not be loaded. Cancel to retry connections or continue without them."
      : "Connections are still loading. Retry when they are ready.")
    if (!prompt.trim() && !pictures.files.length) throw new Error("Enter a message or attach a file.")
    submitting.current = true
    setBusy(true); setError("")
    const auth = csrfToken(), attachments = pictures.readyFiles
    const requireCurrent = () => {
      if (!mounted.current || csrfToken() !== auth) throw new Error("Your sign-in changed. Retry from the current workspace.")
    }
    try {
      const submission = homeSubmission(prompt, model, isPrivate, attachments.map(file => file.id), selectedAccounts, accounts)
      const signature = JSON.stringify([auth, submission.signature])
      if (admission.current?.signature !== signature) admission.current = { signature, request: crypto.randomUUID() }
      const attempt = admission.current
      if (!attempt.session) {
        attempt.session = await api<Session>("/sessions", "POST", { model, visibility: submission.visibility })
      }
      requireCurrent()
      const turn = await api<Turn>(`/sessions/${attempt.session.id}/messages`, "POST", {
        ...submission.body, request_key: attempt.request,
      })
      requireCurrent()
      onStarted({ session: attempt.session, turn, prompt, attachments })
      // Admission succeeded. A list refresh failure must not invite a second start.
      try { await onCreated() }
      catch { if (mounted.current && csrfToken() === auth) setError("Your message was sent, but the session list could not be refreshed.") }
    }
    finally { submitting.current = false; if (mounted.current) setBusy(false) }
  }

  return <section className="home file-drop-zone" {...fileDrop.handlers}>
    <GoalDialog open={goalDialog} onOpenChange={setGoalDialog} onSubmit={async command => { setDraft(command); await submit(command) }} />
    {fileDrop.dragging && <FileDropOverlay />}
    <SpaceBackdrop />
    <div className="home-workbench-bar"><span><FileCode2 size={14} />Workspace<span className="home-path-separator">/</span><strong>New session</strong></span><span className="home-workbench-context"><Code2 size={13} />Moyai</span></div>
    <div className="home-content">
      <header className="home-header">
        <p className="home-eyebrow">Workspace</p>
        <h1>What are we <span>building?</span></h1>
        <p className="home-subtitle">A feature. A stubborn bug. A better way to do it.</p>
      </header>
      <form className="home-form" onSubmit={event => { event.preventDefault(); void send() }}>
        {error && <p className="error" role="alert">{error}</p>}
        {needsAccounts && catalog.scope === accountScope && catalog.error && !skipAccounts && <div className="home-connection-error" role="alert">
          <span className="error">{catalog.error}</span>
          <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => setAccountsVersion(value => value + 1)}>Retry connections</Button>
          <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => setSkippedScope(accountScope)}>Continue without connections</Button>
        </div>}
        <div className="composer home-composer">
          <div className="home-editor-heading"><span><FileCode2 size={14} />New session</span><span className="home-editor-mode">Draft</span></div>
          <AttachmentPreviews files={pictures.files} remove={busy ? undefined : pictures.remove} retry={busy ? undefined : pictures.retry} draft />
          <div className="home-editor-input"><SkillComposer focusRequest={composerFocus} value={draft} onChange={setDraft} onSend={prompt => void send(prompt)} shared={!isPrivate} disabled={busy} placeholder="Describe a change, paste an error, or ask a question…" onPaste={pictures.paste} /></div>
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
              <Button type="submit" size="icon" aria-label="Send message" disabled={busy || pictures.uploading || pictures.hasErrors || !accountsReady || (!draft.trim() && !pictures.files.length)}><Send /></Button>
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
        <p className="home-starters-heading">Start with a prompt</p>
        {starters.map(({ label, detail, prompt, Icon }) => <Button key={label} type="button" variant="ghost" aria-label={label} disabled={busy || Boolean(draft.trim())}
          onClick={() => {
            setDraft(prompt)
            setComposerFocus(value => value + 1)
          }}><Icon aria-hidden="true" /><span className="home-starter-label">{label}</span><span className="home-starter-detail" aria-hidden="true">{detail}</span><ArrowUpRight className="home-starter-arrow" aria-hidden="true" /></Button>)}
      </div>
    </div>
  </section>
}
