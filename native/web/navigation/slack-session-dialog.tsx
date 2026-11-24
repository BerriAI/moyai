import { useEffect, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { connectSessionSlack, getSessionSlack, type SlackWebStatus } from "../generated/api"

export function SlackSessionDialog({ sessionId, onClose }: { sessionId: string; onClose: () => void }) {
  const [status, setStatus] = useState<SlackWebStatus | null>(null)
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  const active = useRef(true)
  useEffect(() => {
    const controller = new AbortController()
    active.current = true
    setStatus(null); setError("")
    void getSessionSlack({ path: { session_id: sessionId }, signal: controller.signal }).then(
      ({ data }) => { if (!controller.signal.aborted) setStatus(data) },
      error => { if (!controller.signal.aborted) setError(String(error)) },
    )
    return () => { active.current = false; controller.abort() }
  }, [sessionId])
  async function connect() {
    if (busy) return
    setBusy(true); setError("")
    try {
      const { data } = await connectSessionSlack({ path: { session_id: sessionId } })
      if (active.current) setStatus(data)
    } catch (error) { if (active.current) setError(String(error)) }
    finally { if (active.current) setBusy(false) }
  }
  return <Dialog open onOpenChange={open => { if (!open) onClose() }}>
    <DialogContent className="sidebar-editor">
      <DialogHeader>
        <DialogTitle>Continue in Slack</DialogTitle>
        <DialogDescription>Moyai will DM your verified Slack account. Replies in that thread continue this session. New messages and answers sync between Slack and the web.</DialogDescription>
      </DialogHeader>
      {status && <div><strong>{status.title}</strong><p>{status.context}</p></div>}
      {!status && !error && <p role="status">Loading session…</p>}
      {error && <p className="error" role="alert">{error}</p>}
      {status?.status === "linked" && <p role="status">Connected. Your session’s sharing settings stay the same.</p>}
      <div className="dialog-actions">
        <Button variant="outline" onClick={onClose}>{status?.status === "linked" ? "Done" : "Cancel"}</Button>
        {status?.status === "linked" && status.url
          ? <Button asChild><a href={status.url} target="_blank" rel="noreferrer">Open Slack thread</a></Button>
          : <Button disabled={!status || busy} onClick={() => void connect()}>
            {busy ? "Connecting…" : status?.status === "pending" ? "Check existing DM" : "Send me a DM"}
          </Button>}
      </div>
    </DialogContent>
  </Dialog>
}
