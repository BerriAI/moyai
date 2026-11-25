import { useEffect, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { getDotNotifications, updateDotNotifications, type DotNotificationSettings } from "../generated/api"

export function DotNotifications() {
  const [open, setOpen] = useState(false)
  const [settings, setSettings] = useState<DotNotificationSettings | null>(null)
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const generation = useRef(0)
  useEffect(() => {
    const current = ++generation.current
    setSettings(null)
    setError("")
    setBusy(false)
    if (!open) return
    const controller = new AbortController()
    void getDotNotifications({ signal: controller.signal }).then(({ data }) => {
      if (current === generation.current && !controller.signal.aborted) setSettings(data)
    }, reason => {
      if (current === generation.current && !controller.signal.aborted)
        setError(reason instanceof Error ? reason.message : "Could not load notification settings.")
    })
    return () => { controller.abort(); generation.current++ }
  }, [open, refresh])
  async function change(enabled: boolean) {
    if (!settings || busy) return
    const current = generation.current
    setBusy(true)
    setError("")
    try {
      const { data } = await updateDotNotifications({ body: { enabled, revision: settings.revision } })
      if (current === generation.current) setSettings(data)
    } catch (reason) {
      if (current === generation.current) {
        setSettings(null)
        setError(reason instanceof Error ? reason.message : "Could not update notifications.")
      }
    } finally {
      if (current === generation.current) setBusy(false)
    }
  }
  return <Dialog open={open} onOpenChange={setOpen}>
    <DialogTrigger asChild><Button variant="ghost" size="sm">Slack notifications</Button></DialogTrigger>
    <DialogContent className="dot-following-dialog">
      <DialogHeader>
        <DialogTitle>Your Dot in Slack</DialogTitle>
        <DialogDescription>Receive useful updates in your own Slack DM. Reply to an update in its thread, or send a new DM to start another conversation.</DialogDescription>
      </DialogHeader>
      {error && <p role="alert">{error} <Button variant="outline" onClick={() => setRefresh(value => value + 1)}>Retry</Button></p>}
      {!settings && !error && <p role="status">Loading notification settings…</p>}
      {settings && <>
        <p role="status">{settings.description}</p>
        {settings.status !== "on" && settings.status !== "unavailable" &&
          <p>Your published updates and replies will appear in your private DM with the workspace’s Moyai bot. Background research stays private.</p>}
        {settings.last_delivery && <p className="muted" role="status">{{
          pending: "Latest update: waiting to send.",
          sending: "Latest update: confirming delivery. Moyai will not resend an uncertain message.",
          sent: "Latest update: delivered to Slack.",
          rejected: "Slack rejected the latest update. Check the workspace bot’s permissions.",
          suppressed: "Latest update: not sent because notifications or access changed.",
        }[settings.last_delivery]}</p>}
        {settings.status === "unavailable" && <a href="#settings/connections">Open connection settings</a>}
        <div className="dot-following-actions">
          {settings.status !== "on" && settings.status !== "unavailable" &&
            <Button disabled={busy} onClick={() => void change(true)}>{busy ? "Connecting…" : settings.enabled ? "Reconnect Slack" : "Enable Slack notifications"}</Button>}
          {settings.enabled && <Button variant="outline" disabled={busy} onClick={() => void change(false)}>Pause notifications</Button>}
        </div>
        <p className="muted">Pausing stops new proactive messages. Existing conversations remain available, and you can still message your Dot.</p>
      </>}
    </DialogContent>
  </Dialog>
}
