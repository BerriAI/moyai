import { useEffect, useId, useRef, useState } from "react"
import { Switch } from "@/components/ui/switch"
import { ApiError, csrfToken } from "../api"
import { updateConnectionAttention, type ConnectionView } from "../generated/api"

type Attention = NonNullable<ConnectionView["attention"]>

const reasons: Record<NonNullable<Attention["reason"]>, string> = {
  automation_unavailable: "Connected for chat. Background attention is unavailable on this installation.",
  shared_account: "Shared workspace accounts are not included in personal Dot checks.",
  disconnected: "Enable this connection to use it proactively.",
  unsupported_provider: "This connection does not support automatic attention.",
  read_tools_required: "Automatic checks need at least one read-only tool from this connection.",
  provider_disabled: "This provider is disabled by your workspace administrator.",
}

function description(attention: Attention) {
  if (attention.reason) return reasons[attention.reason]
  switch (attention.status) {
    case "active": return "Included in your Dot’s scheduled attention checks."
    case "pending": return "Saved. Dot is setting up its next check."
    case "paused": return "Saved. Your Dot attention is paused."
    case "unavailable": return "Connected for chat. Background attention is unavailable on this installation."
    case "disabled": return "Available in chat. Automatic checks are off."
  }
}

export function ConnectionAttention({ connection, onUpdated }: {
  connection: Pick<ConnectionView, "id" | "revision" | "shared" | "attention">
  onUpdated: () => void
}) {
  const id = useId()
  const [busy, setBusy] = useState(false), [error, setError] = useState("")
  const pending = useRef(false)
  const generation = useRef(0)
  useEffect(() => {
    generation.current++
    setBusy(false); setError(""); pending.current = false
    return () => { generation.current++ }
  }, [connection.id, connection.revision, connection.attention?.consent_revision])
  const attention = connection.attention
  if (!attention) return null
  const disabled = busy || (!attention.use_proactively && !attention.eligible && attention.reason !== "read_tools_required")
  const change = async (enabled: boolean) => {
    if (pending.current) return
    const auth = csrfToken(), started = generation.current
    const current = () => started === generation.current && auth === csrfToken()
    pending.current = true
    setBusy(true); setError("")
    try {
      await updateConnectionAttention({
        path: { connection_id: connection.id },
        body: {
          revision: connection.revision, use_proactively: enabled,
          ...(attention.consent_revision != null ? { consent_revision: attention.consent_revision } : {}),
        },
      })
      if (current()) onUpdated()
    } catch (error) {
      if (current()) {
        setError("Could not change proactive use. Refresh the connection and try again.")
        if (error instanceof ApiError && error.status === 403) window.dispatchEvent(new Event("moyai:access-changed"))
      }
    } finally {
      if (current()) { pending.current = false; setBusy(false) }
    }
  }
  return <div className="ns-google-attention">
    {!connection.shared && <label htmlFor={id}><Switch id={id}
      checked={attention.use_proactively} disabled={disabled}
      onCheckedChange={enabled => void change(enabled)} />Use proactively</label>}
    <p className="ns-help">{description(attention)}</p>
    {error && <p className="ns-error" role="alert">{error}</p>}
  </div>
}
