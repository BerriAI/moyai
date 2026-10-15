import React, { useEffect, useRef, useState } from "react"
import { ArrowUp, ArrowUpRight, Code2, Search, Sparkles } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { Checkbox } from "@/components/ui/checkbox"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { mountSculpture } from "@/moyai-sculpture"
import "../../app/static/space-field.js"
import logo from "../../app/static/moyai-logo.png"
import { api } from "./api"
import { AttachButton, ImagePreviews, useImages } from "./attachments"

declare global {
  interface Window { MoyaiSpace?: { mount(canvas: HTMLCanvasElement, options?: { anchor?: HTMLElement }): () => void } }
}

export function Home({ models, defaultModel, onCreated }: { models: string[]; defaultModel: string; onCreated: () => Promise<void> }) {
  const [draft, setDraft] = useState("")
  const [model, setModel] = useState(defaultModel)
  const [privateSession, setPrivateSession] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const pictures = useImages(setError)
  const sculpture = useRef<HTMLDivElement>(null)
  const stars = useRef<HTMLCanvasElement>(null)
  const input = useRef<HTMLTextAreaElement>(null)
  const admission = useRef<{ signature: string; session?: string; request: string } | null>(null)
  useEffect(() => {
    document.title = "New session · Moyai"
    const cleanupSculpture = mountSculpture(sculpture.current)
    const cleanupStars = stars.current ? window.MoyaiSpace?.mount(stars.current, { anchor: sculpture.current ?? undefined }) : undefined
    return () => { cleanupSculpture(); cleanupStars?.() }
  }, [])

  async function send() {
    if (busy || pictures.uploading || (!draft.trim() && !pictures.images.length)) return
    setBusy(true); setError("")
    const signature = JSON.stringify([draft, model, privateSession, pictures.images.map(image => image.id)])
    if (admission.current?.signature !== signature) admission.current = { signature, request: crypto.randomUUID() }
    try {
      if (!admission.current.session) {
        const session = await api<{ id: string }>("/sessions", "POST", { model, visibility: privateSession ? "private" : "shared" })
        admission.current.session = session.id
      }
      await api(`/sessions/${admission.current.session}/messages`, "POST", {
        prompt: draft, model, attachments: pictures.images.map(image => image.id), request_key: admission.current.request,
      })
      location.hash = admission.current.session
      await onCreated()
    } catch (error) { setError(String(error)) } finally { setBusy(false) }
  }

  return <section className="home">
    <div className="home-crumb">✧ &nbsp; NEW SESSION</div>
    <canvas className="space-backdrop" ref={stars} />
    <div className="home-content">
      <p className="home-eyebrow">A space for your next big idea</p>
      <h1>What are we working on?</h1>
      <p className="home-description">From the first spark to the final commit. Make it happen with Moyai.</p>
      <div className="home-sculpture" ref={sculpture}><img src={logo} alt="Moyai" /></div>
      <div className="home-mode">Chat</div>
      <form className="home-form" onSubmit={event => { event.preventDefault(); void send() }}>
        <label className="check-label home-privacy"><Checkbox checked={privateSession} onCheckedChange={value => setPrivateSession(Boolean(value))} />Private session · Only you</label>
        <p className="home-privacy-hint">{privateSession ? "Your conversation, images and computer stay private." : "Workspace members can see messages, images, files and the computer, including browser sign-ins, and take turns controlling it."}</p>
        {error && <p className="error" role="alert">{error}</p>}
        <div className="composer home-composer">
          <ImagePreviews images={pictures.images} remove={pictures.remove} />
          <Textarea ref={input} value={draft} aria-label="Message Moyai" placeholder="Ask Moyai to build, investigate, or pick up a thread…" onChange={event => setDraft(event.target.value)} onPaste={pictures.paste} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send() } }} />
          <div className="composer-actions">
            <AttachButton add={pictures.add} disabled={pictures.uploading || busy} />
            <div className="send-actions"><Select value={model} onValueChange={setModel}><SelectTrigger aria-label="Model"><SelectValue /></SelectTrigger><SelectContent>{models.map(value => <SelectItem value={value} key={value}>{value.replace(/^openai\//, "")}</SelectItem>)}</SelectContent></Select>
              <Button type="submit" size="icon" aria-label="Send message" disabled={busy || pictures.uploading || (!draft.trim() && !pictures.images.length)}><ArrowUp /></Button>
            </div>
          </div>
        </div>
        <div className="home-composer-hint"><span>{pictures.uploading ? "Uploading images…" : "Your own cloud workspace"}</span><span>Paste images · Enter to send</span></div>
      </form>
      <div className="suggestions">{[
        { icon: Search, title: "Investigate a bug", detail: "Find the signal in the noise.", prompt: "Help me investigate a bug. " },
        { icon: Code2, title: "Explore a codebase", detail: "Get the lay of the land.", prompt: "Help me explore a codebase. " },
        { icon: Sparkles, title: "Build something", detail: "Turn an idea into working software.", prompt: "Help me build " },
      ].map(item => <button key={item.title} type="button" onClick={() => { setDraft(item.prompt); input.current?.focus() }}><item.icon size={19} /><span>{item.title}<small>{item.detail}</small></span><ArrowUpRight size={15} /></button>)}</div>
    </div>
  </section>
}
