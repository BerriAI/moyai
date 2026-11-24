import { useEffect, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import { getPersonalDot, openPersonalDot, type DotView, type Me } from "../generated/api"

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
    <h2>What should I help you keep track of?</h2>
    <p className="muted">This is your private, ongoing conversation. Tell me your priorities, ask for help, or pick up where we left off.</p>
    {dot.attention_available
      ? <p className="muted">With a connected account and an enabled schedule, I can check for useful updates and bring them here. You can reply to keep the work moving.</p>
      : <p className="muted">You can chat here now. Proactive checks need the product automation service.</p>}
    <div className="dot-starters">
      {["Help me prepare for my upcoming meetings.", "Help me follow up on important unanswered messages."].map(text =>
        <Button key={text} variant="outline" disabled={hasDraft} onClick={() => onDraft(text)}>{text}</Button>)}
    </div>
    <p><a href="#settings/connections">Connect an account</a> · <a href="#settings/automations">Manage schedules</a> · <a href="#settings/responsibilities">Chat follow-ups</a></p>
  </div>
}
