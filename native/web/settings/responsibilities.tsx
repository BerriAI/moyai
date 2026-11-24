import { useEffect, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { ApiError } from "../api"
import {
  controlResponsibility, createResponsibility, getMe, listResponsibilities,
  listResponsibilityInbox, listSessions, markResponsibilityFindingRead,
  listResponsibilityReviews, type ResponsibilityReview,
  type ResponsibilityInboxItem, type ResponsibilityView, type SessionListItem,
} from "../generated/api"
import "./responsibilities.css"

export function ResponsibilitiesPage({ userId }: { userId: string }) {
  const [items, setItems] = useState<ResponsibilityView[]>([])
  const [inbox, setInbox] = useState<ResponsibilityInboxItem[]>([])
  const [sources, setSources] = useState<SessionListItem[]>([])
  const [models, setModels] = useState<string[]>([])
  const [source, setSource] = useState("")
  const [model, setModel] = useState("")
  const [objective, setObjective] = useState("")
  const [budget, setBudget] = useState("10")
  const [open, setOpen] = useState(false)
  const [review, setReview] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [refresh, setRefresh] = useState(0)
  const active = useRef(true)
  const requestKey = useRef(crypto.randomUUID())
  useEffect(() => { active.current = true; return () => { active.current = false } }, [])
  useEffect(() => {
    const controller = new AbortController()
    let running = false
    const load = async () => {
      if (running) return
      running = true
      try {
        const [responsibilities, findings, chats, me] = await Promise.all([
          listResponsibilities({ signal: controller.signal }),
          listResponsibilityInbox({ signal: controller.signal }),
          listSessions({ signal: controller.signal }), getMe({ signal: controller.signal }),
        ])
        if (controller.signal.aborted) return
        setItems(responsibilities.data.items); setInbox(findings.data.items)
        setSources(chats.data.filter(chat => chat.owner_id === userId && chat.visibility === "private" && chat.surface === "web"))
        setModels(me.data.models); setModel(value => value || me.data.models[0] || "")
        setError("")
      } catch (reason) {
        if (controller.signal.aborted) return
        setError(reason instanceof Error ? reason.message : "Could not load your responsibilities.")
        // Never retain protected data after an access failure.
        if (reason instanceof ApiError && [401, 403, 404].includes(reason.status)) {
          setItems([]); setInbox([]); setSources([]); setOpen(false); setReview(null)
        }
      } finally {
        running = false
        if (!controller.signal.aborted) setLoading(false)
      }
    }
    void load()
    const timer = setInterval(() => { if (!document.hidden) void load() }, 10000)
    return () => { controller.abort(); clearInterval(timer) }
  }, [userId, refresh])
  async function act(operation: () => Promise<unknown>) {
    setBusy(true); setError("")
    try {
      await operation()
      if (active.current) setRefresh(value => value + 1)
      return true
    } catch (reason) {
      if (active.current) setError(reason instanceof Error ? reason.message : "Could not save the change.")
      return false
    } finally { if (active.current) setBusy(false) }
  }
  const changeDraft = () => { requestKey.current = crypto.randomUUID() }
  return <div className="dot-page">
    <div className="dot-toolbar"><p>Choose a private chat and what matters to you. Moyai checks new completed replies and brings useful changes here.</p>
      <Button onClick={() => setOpen(true)} disabled={loading}>Add responsibility</Button></div>
    {error && <div role="alert" className="ns-error">{error}<Button variant="outline" onClick={() => setRefresh(value => value + 1)}>Retry</Button></div>}
    {loading ? <p role="status">Loading your responsibilities…</p> : <>
      <section aria-labelledby="dot-inbox"><h2 id="dot-inbox">Your inbox</h2>
        {!inbox.length && <p className="ns-empty">Nothing needs your attention yet. Existing activity stays quiet; new findings will appear here.</p>}
        {inbox.map(item => <article className="dot-card" key={item.turn_id}>
          <div className="dot-row"><strong>{item.read_at ? "Reviewed" : "New finding"}</strong><time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString()}</time></div>
          <p className="dot-objective">{item.objective}</p><p className="dot-summary">{item.summary}</p>
          <details><summary>Source evidence</summary>{item.evidence.map((evidence, index) => <blockquote key={index}>{evidence.quote}</blockquote>)}</details>
          <div className="dot-actions"><a href={`#${item.source_session_id}`}>Open source chat</a>
            <Button variant="ghost" size="sm" onClick={() => setReview(item.responsibility_id)}>Review activity</Button>
            {!item.read_at && <Button variant="ghost" size="sm" disabled={busy} onClick={() => void act(() => markResponsibilityFindingRead({ path: { turn_id: item.turn_id } }))}>Mark reviewed</Button>}</div>
        </article>)}
      </section>
      <section aria-labelledby="dot-responsibilities"><h2 id="dot-responsibilities">Responsibilities</h2>
        {!items.length && <p className="ns-empty">Add your first responsibility to get started. You can pause it at any time.</p>}
        {items.map(item => <article className="dot-card" key={item.id}>
          <div className="dot-row"><strong className="dot-objective">{item.objective}</strong><span>{item.status.replaceAll("_", " ")}</span></div>
          <p>{item.evaluations} of {item.max_evaluations} reviews used · Minimum review interval: {item.interval_seconds} seconds</p>
          {item.reason && <p>{item.reason}</p>}
          <div className="dot-actions"><a href={`#${item.source_session_id}`}>Open source chat</a>
            <Button variant="ghost" size="sm" onClick={() => setReview(item.id)}>Review activity</Button>
            {item.status !== "budget_limited" && <Button variant="outline" size="sm" disabled={busy} onClick={() => void act(() => controlResponsibility({
              path: { responsibility_id: item.id },
              body: { generation: item.generation, action: ["paused", "blocked"].includes(item.status) ? "resume" : "pause" },
            }))}>{["paused", "blocked"].includes(item.status) ? "Resume" : "Pause"}</Button>}
          </div>
        </article>)}
      </section>
    </>}
    <Dialog open={open} onOpenChange={value => { if (!busy) setOpen(value) }}>
      <DialogContent className="dot-dialog"><DialogHeader><DialogTitle>Add responsibility</DialogTitle>
        <DialogDescription>Follow new replies in a private chat you own. Moyai can suggest next steps and place findings in your private inbox.</DialogDescription></DialogHeader>
        <form onSubmit={event => {
          event.preventDefault()
          void act(() => createResponsibility({ body: {
            request_key: requestKey.current, source_session_id: source, objective, model,
            max_evaluations: Number(budget), interval_seconds: 30,
          } })).then(ok => { if (ok && active.current) { setOpen(false); setObjective(""); changeDraft() } })
        }}>
          <label htmlFor="dot-source">Private source chat</label>
          <Select value={source} onValueChange={value => { setSource(value); changeDraft() }}><SelectTrigger id="dot-source"><SelectValue placeholder="Choose a chat" /></SelectTrigger><SelectContent>
            {sources.map(chat => <SelectItem key={chat.id} value={chat.id}>{chat.title}</SelectItem>)}
          </SelectContent></Select>
          {!sources.length && <p>Create a private chat in the workspace first.</p>}
          <label htmlFor="dot-objective">What should Moyai pay attention to?</label>
          <Textarea id="dot-objective" value={objective} required maxLength={2000} placeholder="Tell me when the launch plan changes or something needs my attention."
            onChange={event => { setObjective(event.target.value); changeDraft() }} />
          <label htmlFor="dot-model">Model</label>
          <Select value={model} onValueChange={value => { setModel(value); changeDraft() }}><SelectTrigger id="dot-model"><SelectValue /></SelectTrigger><SelectContent>
            {models.map(value => <SelectItem key={value} value={value}>{value}</SelectItem>)}
          </SelectContent></Select>
          <label htmlFor="dot-budget">Maximum reviews</label>
          <Input id="dot-budget" type="number" min={1} max={100} required value={budget} onChange={event => { setBudget(event.target.value); changeDraft() }} />
          <p>Reviews use your selected model. Unchanged chats do not use reviews. Monitoring pauses when this limit is reached.</p>
          {error && <p role="alert" className="error">{error}</p>}
          <div className="dot-actions"><Button type="button" variant="outline" disabled={busy} onClick={() => setOpen(false)}>Cancel</Button>
            <Button type="submit" disabled={busy || !source || !model || !objective.trim()}>{busy ? "Saving…" : "Start following"}</Button></div>
        </form>
      </DialogContent>
    </Dialog>
    <Dialog open={review !== null} onOpenChange={value => { if (!value) setReview(null) }}>
      <DialogContent className="dot-dialog"><DialogHeader><DialogTitle>Review activity</DialogTitle>
        <DialogDescription>Saved reviews, including failed attempts. Each attempt uses one review from your limit.</DialogDescription></DialogHeader>
        {review && <ReviewActivity key={`${userId}:${review}`} identity={review} />}
      </DialogContent>
    </Dialog>
  </div>
}

function ReviewActivity({ identity }: { identity: string }) {
  const [items, setItems] = useState<ResponsibilityReview[]>([])
  const [error, setError] = useState("")
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    const controller = new AbortController()
    let running = false
    const load = async () => {
      if (running) return
      running = true
      try {
        const { data } = await listResponsibilityReviews({ path: { responsibility_id: identity }, signal: controller.signal })
        if (!controller.signal.aborted) { setItems(data.items); setError("") }
      } catch (reason) {
        if (!controller.signal.aborted) {
          setItems([])
          setError(reason instanceof Error ? reason.message : "Could not load review activity.")
        }
      } finally {
        running = false
        if (!controller.signal.aborted) setLoading(false)
      }
    }
    void load()
    const timer = setInterval(() => { if (!document.hidden) void load() }, 10000)
    return () => { controller.abort(); clearInterval(timer) }
  }, [identity])
  return <div>
    {error && <p role="alert">{error}</p>}
    {loading ? <p role="status">Loading reviews…</p> : !error && !items.length && <p>No reviews yet.</p>}
    {items.map(item => <article className="dot-card" key={item.turn_id}>
      <div className="dot-row"><strong>{item.validation === "invalid" ? "Validation failed" : item.validation === "pending" ? "Awaiting validation" : item.status}</strong><time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString()}</time></div>
      <p>Generation {item.generation} · Source through completion {item.through_sequence}</p>
      {item.error && <p>{item.error}</p>}
      {item.validation === "valid" && item.decision && <><p>{item.decision.summary}</p>{item.decision.evidence.map((evidence, index) => <blockquote key={index}>{evidence.quote}</blockquote>)}</>}
    </article>)}
  </div>
}
