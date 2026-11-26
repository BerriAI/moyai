import { useEffect, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { editDotItem, getPersonalDot, listDotItems, openPersonalDot, type DotItemEdit, type DotItemsView, type DotItemView, type DotView, type Me } from "../generated/api"
import "./personal-dot.css"
import { DotNotifications } from "./dot-notifications"

export function usePersonalDot(me: Me | null, scope: string, route: string, onError: (message: string) => void) {
  const [state, setState] = useState<{ scope: string; value: DotView } | null>(null)
  const [opening, setOpening] = useState(false)
  const current = useRef({ scope, route })
  current.current = { scope, route }
  useEffect(() => {
    setOpening(false)
    if (!me || me.user.individual === false) return
    const controller = new AbortController()
    void getPersonalDot({ signal: controller.signal }).then(({ data }) => {
      if (!controller.signal.aborted) setState({ scope, value: data })
    }, () => {})
    return () => controller.abort()
  }, [scope, me?.user.individual])
  return {
    dot: state?.scope === scope ? state.value : undefined,
    opening,
    async open() {
      if (opening) return
      const requested = current.current
      setOpening(true)
      try {
        const { data } = await openPersonalDot()
        if (current.current.scope !== requested.scope) return
        setState({ scope, value: data })
        if (current.current.route === requested.route && data.session_id) location.hash = data.session_id
      } catch (reason) {
        if (current.current.scope === requested.scope && current.current.route === requested.route)
          onError(reason instanceof Error ? reason.message : "Your Dot could not be opened. Try again.")
      } finally {
        if (current.current.scope === requested.scope) setOpening(false)
      }
    },
  }
}

export function DotWelcome({ dot, onDraft, hasDraft }: { dot: DotView; onDraft: (text: string) => void; hasDraft: boolean }) {
  return <div className="empty-chat">
    <h2>What should I know about your day?</h2>
    <p className="muted">This is your private, ongoing conversation. Tell me your priorities, ask for help, or pick up where we left off.</p>
    {dot.attention_available
      ? <p className="muted">Connect Gmail or Google Calendar with “Use proactively” enabled, and I’ll check them automatically for useful developments and unfinished work. No reminders or schedules to set up.</p>
      : <p className="muted">You can chat here now. Proactive checks need the product automation service.</p>}
    <div className="dot-starters">
      {["Help me decide what to focus on today.", "Help me prepare for my upcoming meetings."].map(text =>
        <Button key={text} variant="outline" disabled={hasDraft} onClick={() => onDraft(text)}>{text}</Button>)}
    </div>
    <p><a href="#settings/connections">Connect an account</a> · <a href="#settings/automations">Manage schedules</a> · <a href="#settings/responsibilities">Chat follow-ups</a></p>
  </div>
}

export function DotFollowing() {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState<DotItemsView | null>(null)
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  const [query, setQuery] = useState("")
  const [refresh, setRefresh] = useState(0)
  const generation = useRef(0)
  useEffect(() => {
    const current = ++generation.current
    if (!open) { setData(null); return }
    const controller = new AbortController()
    setError("")
    void listDotItems({ signal: controller.signal }).then(({ data: value }) => {
      if (generation.current === current && !controller.signal.aborted) setData(value)
    }, reason => {
      if (generation.current === current && !controller.signal.aborted) {
        setData(null)
        setError(reason instanceof Error ? reason.message : "Could not load follow-ups.")
      }
    })
    return () => { controller.abort(); generation.current++ }
  }, [open, refresh])
  async function change(item: DotItemView, state: DotItemEdit["state"]) {
    const current = generation.current
    setBusy(true)
    setError("")
    try {
      const { data: updated } = await editDotItem({
        path: { identity: item.id },
        body: {
          revision: item.revision, state,
          snoozed_until: state === "snoozed" ? new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString() : null,
        },
      })
      if (generation.current === current) setData(previous => previous && ({
        ...previous, items: previous.items.map(value => value.id === updated.id ? updated : value),
      }))
    } catch (reason) {
      if (generation.current === current) {
        setData(null)
        setError(reason instanceof Error ? reason.message : "Could not update this follow-up.")
      }
    } finally {
      setBusy(false)
    }
  }
  const items = data?.items.filter(item => `${item.title} ${item.summary} ${item.state}`.toLowerCase().includes(query.toLowerCase()))
  return <div className="dot-following-bar"><Dialog open={open} onOpenChange={setOpen}>
    <DialogTrigger asChild><Button variant="ghost" size="sm">Following</Button></DialogTrigger>
    <DialogContent className="dot-following-dialog">
      <DialogHeader>
        <DialogTitle>What your Dot is following</DialogTitle>
        <DialogDescription>Discoveries from the accounts you approved. Your choices carry into future checks.</DialogDescription>
      </DialogHeader>
      <Input aria-label="Filter follow-ups" placeholder="Filter follow-ups" value={query} onChange={event => setQuery(event.target.value)} />
      {error && <p role="alert">{error} <Button variant="outline" size="sm" onClick={() => setRefresh(value => value + 1)}>Retry</Button></p>}
      {!data && !error && <p role="status">Loading follow-ups…</p>}
      {data && <div className="dot-following-items">
        {!items?.length && <p>{query ? "No matching follow-ups." : "Nothing to follow yet. With “Use proactively” enabled for Gmail or Google Calendar, useful discoveries will appear here automatically."}</p>}
        {items?.map(item => <article key={item.id} className="dot-following-item">
          <h3>{item.title}</h3>
          <p>{item.summary}</p>
          <p className="muted">{item.state === "snoozed" && item.snoozed_until
            ? `Snoozed until ${new Date(item.snoozed_until).toLocaleString()}` : item.state}</p>
          {/^\bhttps?:\/\//i.test(item.source)
            ? <a href={item.source} target="_blank" rel="noreferrer">View source</a>
            : <p className="muted">{item.source}</p>}
          <div className="dot-following-actions">
            {item.state !== "open" && <Button variant="outline" size="sm" disabled={busy} onClick={() => void change(item, "open")}>Reopen</Button>}
            {item.state !== "resolved" && <Button variant="outline" size="sm" disabled={busy} onClick={() => void change(item, "resolved")}>Resolved</Button>}
            {item.state !== "dismissed" && <Button variant="ghost" size="sm" disabled={busy} onClick={() => void change(item, "dismissed")}>Dismiss</Button>}
            {item.state === "open" && <Button variant="ghost" size="sm" disabled={busy} onClick={() => void change(item, "snoozed")}>Snooze 1 day</Button>}
          </div>
        </article>)}
        {data.limited && <p className="muted">Showing the 100 most recently checked items.</p>}
      </div>}
      <a href="#settings/automations">Manage attention schedule</a>
    </DialogContent>
  </Dialog><DotNotifications /></div>
}
