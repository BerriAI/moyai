import React, { useEffect, useRef, useState } from "react"
import { ArrowUp, ArrowUpRight, Code2, Search, Sparkles } from "lucide-react"
import { Button } from "@/components/ui/button"
import { SkillComposer } from "./skill-composer"
import { Checkbox } from "@/components/ui/checkbox"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { mountSculpture } from "@/moyai-sculpture"
import "./space-field.js"
import logo from "./assets/moyai-logo.png"
import { api, csrfToken } from "./api"
import { AttachButton, AttachmentPreviews, useAttachments, useFileDrop, FileDropOverlay } from "./attachments"
import type { NewConversation, Session, Turn } from "./conversation-state"

declare global {
  interface Window { MoyaiSpace?: { mount(canvas: HTMLCanvasElement, options?: { anchor?: HTMLElement }): () => void } }
}

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
  const [privateSession, setPrivateSession] = useState(individual)
  const isPrivate = individual && privateSession
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const pictures = useAttachments(setError)
  const fileDrop = useFileDrop(pictures.add, setError, busy)
  const [selectedAccounts, setSelectedAccounts] = useState<string[]>([])
  const selectedNow = useRef(selectedAccounts)
  selectedNow.current = selectedAccounts
  const accountsInitialized = useRef(false)
  const [accountsVersion, setAccountsVersion] = useState(0), [accountNotice, setAccountNotice] = useState("")
  const [skipAccounts, setSkipAccounts] = useState(false)
  const [catalog, setCatalog] = useState<{ scope: string; rows: HomeConnection[]; loading: boolean; error: string }>({ scope: "", rows: [], loading: false, error: "" })
  const accountScope = `${isPrivate ? "private" : "shared"}:${connectionVersion}:${accountsVersion}`
  const accounts = catalog.scope === accountScope ? catalog.rows : []
  const accountsReady = productMode !== "existing" || skipAccounts || (catalog.scope === accountScope && !catalog.loading && !catalog.error
    && !accountNotice && selectedAccounts.every(id => accounts.some(row => row.id === id && row.enabled && (isPrivate || row.shared))))
  const sculpture = useRef<HTMLDivElement>(null)
  const stars = useRef<HTMLCanvasElement>(null)
  const input = useRef<HTMLDivElement>(null)
  const admission = useRef<{ signature: string; session?: Session; request: string } | null>(null)
  const submitting = useRef(false)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    document.title = "New session · Moyai"
    const cleanupSculpture = mountSculpture(sculpture.current)
    const cleanupStars = stars.current ? window.MoyaiSpace?.mount(stars.current, { anchor: sculpture.current ?? undefined }) : undefined
    return () => { mounted.current = false; cleanupSculpture(); cleanupStars?.() }
  }, [])
  useEffect(() => {
    if (productMode !== "existing") return
    const controller = new AbortController(), scope = accountScope
    setCatalog({ scope, rows: [], loading: true, error: "" })
    void api<HomeConnection[]>(`/connections?shared=${!isPrivate}`, "GET", undefined, controller.signal).then(rows => {
      if (controller.signal.aborted) return
      const eligible = rows.filter(row => row.enabled && (isPrivate || row.shared))
      const next = accountsInitialized.current ? selectedNow.current.filter(id => eligible.some(row => row.id === id)) : eligible.map(row => row.id)
      if (accountsInitialized.current && next.length !== selectedNow.current.length) setAccountNotice("Account access changed. Review your selections before starting this session.")
      accountsInitialized.current = true; selectedNow.current = next; setSelectedAccounts(next)
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
      if (admission.current?.signature !== submission.signature) admission.current = { signature: submission.signature, request: crypto.randomUUID() }
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
    <div className="home-crumb">✧ &nbsp; NEW SESSION</div>
    <canvas className="space-backdrop" ref={stars} />
    <div className="home-content">
      <p className="home-eyebrow">A space for your next big idea</p>
      <h1>What are we working on?</h1>
      <p className="home-description">From the first spark to the final commit. Make it happen with Moyai.</p>
      <div className="home-sculpture" ref={sculpture}><img src={logo} alt="Moyai" /></div>
      <div className="home-mode">Chat</div>
      <form className="home-form" onSubmit={event => { event.preventDefault(); void send() }}>
        <label className="check-label home-privacy"><Checkbox checked={isPrivate} disabled={!individual || busy} onCheckedChange={value => { setSkipAccounts(false); setPrivateSession(Boolean(value)) }} />Private session · Only you</label>
        {!individual && <p className="home-privacy-hint">This shared sign-in creates shared sessions. Use an individual sign-in for private work.</p>}
        <p className="home-privacy-hint">{isPrivate ? "Your conversation, files and computer stay private." : "Workspace members can see messages, images, files and the computer, including browser sign-ins, and take turns controlling it."}</p>
        {error && <p className="error" role="alert">{error}</p>}
        <div className="composer home-composer">
          <AttachmentPreviews files={pictures.files} remove={busy ? undefined : pictures.remove} retry={busy ? undefined : pictures.retry} draft />
          <SkillComposer ref={input} value={draft} onChange={setDraft} onSend={prompt => void send(prompt)} shared={!isPrivate} disabled={busy} placeholder="Ask Moyai to build, investigate, or type / for skills…" onPaste={pictures.paste} />
          <div className="composer-actions">
            <AttachButton add={pictures.add} disabled={busy} />
            <div className="send-actions"><Select value={model} onValueChange={setModel}><SelectTrigger aria-label="Model"><SelectValue /></SelectTrigger><SelectContent>{models.map(value => <SelectItem value={value} key={value}>{value.replace(/^openai\//, "")}</SelectItem>)}</SelectContent></Select>
              <Button type="submit" size="icon" aria-label="Send message" disabled={busy || pictures.uploading || pictures.hasErrors || !accountsReady || (!draft.trim() && !pictures.files.length)}><ArrowUp /></Button>
            </div>
          </div>
        </div>
        {productMode === "existing" && <details className="account-selection"><summary>Context & connections · {selectedAccounts.length} selected</summary>
          {(catalog.scope !== accountScope || catalog.loading) && <p className="muted" role="status">Loading accounts for this session…</p>}
          {catalog.error && <p className="error" role="alert">{catalog.error}<Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => { setSkipAccounts(false); setAccountsVersion(value => value + 1) }}>Retry accounts</Button><Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => { accountsInitialized.current = true; selectedNow.current = []; setSelectedAccounts([]); setAccountNotice(""); setSkipAccounts(true) }}>Use no accounts for this session</Button></p>}
          {accountNotice && <p className="notice" role="status">{accountNotice}<Button type="button" variant="ghost" size="sm" onClick={() => setAccountNotice("")}>Selections reviewed</Button></p>}
          <div className="connection-options">{accounts.map(account => <label key={account.id}><Checkbox disabled={busy} checked={selectedAccounts.includes(account.id)} onCheckedChange={checked => {
            const next = checked ? [...new Set([...selectedNow.current, account.id])] : selectedNow.current.filter(id => id !== account.id)
            accountsInitialized.current = true; selectedNow.current = next; setSelectedAccounts(next); setAccountNotice(""); setSkipAccounts(false)
          }} />{account.name}<span className="muted">{account.read_only ? "Read only" : "Can make changes"}</span></label>)}</div>
          {!catalog.loading && !catalog.error && !accounts.length && <p className="muted">No accounts are available for this session. <a href="#settings/connections">Manage Connections</a>.</p>}
          {skipAccounts && <p className="muted" role="status">This session will start without connected accounts.</p>}
        </details>}
        <div className="home-composer-hint"><span>{pictures.uploading ? "Uploading files…" : "Your own cloud workspace"}</span><span>Drop files or paste images · Enter to send</span></div>
      </form>
      <div className="suggestions">{[
        { icon: Search, title: "Investigate a bug", detail: "Find the signal in the noise.", prompt: "Help me investigate a bug. " },
        { icon: Code2, title: "Explore a codebase", detail: "Get the lay of the land.", prompt: "Help me explore a codebase. " },
        { icon: Sparkles, title: "Build something", detail: "Turn an idea into working software.", prompt: "Help me build " },
      ].map(item => <button key={item.title} type="button" onClick={() => { setDraft(item.prompt); input.current?.focus() }}><item.icon size={19} /><span>{item.title}<small>{item.detail}</small></span><ArrowUpRight size={15} /></button>)}</div>
    </div>
  </section>
}
