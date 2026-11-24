import React, { useEffect, useRef, useState } from "react"
import { ArrowRight, Check, Github, Link2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { csrfToken } from "../api"
import {
  disconnectPersonalConnection, listPersonalConnections, startPersonalConnection,
  type PersonalOAuthView,
} from "../generated/api"

const descriptions: Record<PersonalOAuthView["provider"], string> = {
  linear: "Work with your issues, projects, and teams.",
  notion: "Find and update the pages you authorize.",
  github: "Work with your repositories, issues, and pull requests.",
}

export function PersonalConnections({ userId, individual, children }: { userId: string; individual: boolean; children?: React.ReactNode }) {
  const [revision, setRevision] = useState(0)
  const [state, setState] = useState<{ userId: string; rows?: PersonalOAuthView[]; error?: string }>({ userId })
  const [busy, setBusy] = useState<{ provider: PersonalOAuthView["provider"]; action: "connect" | "disconnect" } | null>(null)
  const [disconnect, setDisconnect] = useState<PersonalOAuthView | null>(null)
  const [notice, setNotice] = useState("")
  const generation = useRef(0)
  const pending = useRef(false)
  useEffect(() => {
    const controller = new AbortController()
    generation.current++
    setState({ userId }); setDisconnect(null); setBusy(null)
    if (individual) void listPersonalConnections({ signal: controller.signal }).then(
      ({ data }) => { if (!controller.signal.aborted) setState({ userId, rows: data }) },
      () => { if (!controller.signal.aborted) setState({ userId, error: "Your apps could not be loaded. Try again." }) },
    )
    return () => { generation.current++; controller.abort() }
  }, [userId, individual, revision])
  useEffect(() => { setNotice("") }, [userId, individual])
  useEffect(() => {
    const clear = () => {
      generation.current++; setState({ userId }); setDisconnect(null); setNotice("")
      setRevision(value => value + 1)
    }
    window.addEventListener("moyai:access-changed", clear)
    window.addEventListener("moyai:signed-out", clear)
    return () => {
      window.removeEventListener("moyai:access-changed", clear)
      window.removeEventListener("moyai:signed-out", clear)
    }
  }, [userId])
  const rows = state.userId === userId ? state.rows : undefined
  const run = async (row: PersonalOAuthView, action: "connect" | "disconnect") => {
    if (pending.current) return
    pending.current = true
    const started = generation.current, auth = csrfToken()
    const current = () => started === generation.current && auth === csrfToken()
    setBusy({ provider: row.provider, action }); setNotice("")
    try {
      if (action === "connect") {
        const { data } = await startPersonalConnection({ path: { provider: row.provider } })
        if (!current()) return
        const destination = new URL(data.url)
        if (destination.protocol !== "https:" || destination.username || destination.password) throw new Error()
        window.location.assign(destination.href)
      } else {
        await disconnectPersonalConnection({ path: { provider: row.provider } })
        if (!current()) return
        setDisconnect(null); setNotice(`${row.name} disconnected.`); setRevision(value => value + 1)
      }
    } catch {
      if (current()) setNotice(`Could not ${action} ${row.name}. Try again.`)
    } finally {
      pending.current = false
      if (current()) setBusy(null)
    }
  }
  const params = new URLSearchParams(location.search)
  const outcome = params.get("personal_connection")
  const connected = rows?.find(row => row.provider === params.get("provider") && row.connected)
  const callbackNotice = outcome === "success" && connected ? `${connected.name} is connected and ready for your private chats.`
    : outcome === "cancelled" ? "Authorization cancelled. Your existing connections are unchanged."
      : outcome === "expired" ? "This authorization request expired or was replaced. Click Connect to try again."
        : outcome === "failed" ? "The provider did not confirm authorization. Click Connect to try again." : ""
  return <section className="ns-personal-connections" aria-label="Your apps">
    <h2>Your apps</h2>
    <p className="ns-help">Connect an app, approve access, and you're ready. These accounts are personal and can be used only in your private chats.</p>
    {!individual ? <p className="ns-notice">Sign in with your individual work account to connect your apps.</p> : <>
      {(notice || callbackNotice) && <p className="ns-notice" role="status">{notice || callbackNotice}</p>}
      {state.error && <div className="ns-error" role="alert">{state.error} <Button variant="outline" onClick={() => setRevision(value => value + 1)}>Retry</Button></div>}
      {!rows && !state.error && <p role="status">Loading your apps…</p>}
      {rows && <div className="ns-records">{rows.map(row => {
        const Icon = row.provider === "github" ? Github : Link2
        return <article className="ns-record" key={row.provider}>
          <Icon className="ns-record-icon" aria-hidden="true" />
          <div className="ns-record-copy">
            <div className="ns-record-title"><h3>{row.name}</h3>{row.connected && <span className="ns-badge ns-oauth-connected"><Check size={13} aria-hidden="true" />Connected</span>}</div>
            <p>{descriptions[row.provider]}</p>
            <small>{row.connected ? "Only you · Changes require your approval" : row.configured ? "Approve access on the provider’s website." : "Not enabled on this Moyai deployment yet. Ask your administrator."}</small>
          </div>
          <div className="ns-row-actions">
            <Button size="sm" variant={row.connected ? "outline" : "default"} disabled={busy !== null || !row.configured}
              aria-label={`${row.connected ? "Reconnect" : "Connect"} personal ${row.name}`}
              onClick={() => void run(row, "connect")}>{busy?.provider === row.provider && busy.action === "connect" ? "Connecting…" : row.connected ? "Reconnect" : "Connect"}<ArrowRight aria-hidden="true" /></Button>
            {row.connected && <Button size="sm" variant="ghost" disabled={busy !== null} aria-label={`Disconnect personal ${row.name}`} onClick={() => setDisconnect(row)}>Disconnect</Button>}
          </div>
        </article>
      })}</div>}
      {children}
    </>}
    {disconnect && <Dialog open onOpenChange={open => { if (!open && !busy) setDisconnect(null) }}><DialogContent className="native-settings-dialog">
      <DialogHeader><DialogTitle>Disconnect {disconnect.name}?</DialogTitle><DialogDescription>Moyai will remove your saved tokens and stop using this account. You can also revoke Moyai in the provider’s settings.</DialogDescription></DialogHeader>
      <div className="ns-dialog-actions"><Button variant="outline" disabled={busy !== null} onClick={() => setDisconnect(null)}>Cancel</Button>
        <Button variant="destructive" disabled={busy !== null} onClick={() => void run(disconnect, "disconnect")}>{busy?.action === "disconnect" ? "Disconnecting…" : "Disconnect account"}</Button></div>
    </DialogContent></Dialog>}
  </section>
}
