import React, { useEffect, useRef } from "react"
import { AgentSpace, AgentViewSwitch, type OrbitAgent } from "./agent-space"
import { api, ApiError, csrfToken } from "./api"
import { useWorkspaceResource, type Subagent } from "./workspace"

const stoppable = new Set(["running", "starting", "queued", "waiting", "saving"])

export function SubagentSpacePanel({ session, space, blocked, onSpaceChange, onAvailable, onOpen }: {
  session: { id: string; title: string; status: string; model: string }
  space: boolean; blocked: boolean; onSpaceChange: (value: boolean) => void
  onAvailable: (value: boolean) => void; onOpen: (agent: Subagent) => void
}) {
  const resource = useWorkspaceResource()
  const agents = resource?.data?.subagents ?? []
  const available = agents.length > 0
  const mounted = useRef(true)
  const writes = useRef(new Set<AbortController>())
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; writes.current.forEach(controller => controller.abort()); writes.current.clear() }
  }, [session.id])
  useEffect(() => { onAvailable(available) }, [available, onAvailable])
  if (!available) return null
  const nodes: OrbitAgent[] = agents.map((agent, index) => ({
    id: agent.id, name: agent.name, label: String(index + 1).padStart(2, "0"), status: agent.status,
    model: agent.can_inspect ? agent.model ?? undefined : undefined,
    task: agent.can_inspect ? agent.task ?? undefined : undefined,
    role: agent.depth ? `Subagent · level ${agent.depth + 1}` : "Subagent",
    restricted: !agent.can_inspect,
    href: agent.can_inspect && agent.session_id ? `#${agent.session_id}` : undefined,
    onOpen: agent.can_inspect && !agent.session_id ? () => onOpen(agent) : undefined,
    onStop: agent.can_inspect && agent.session_id && stoppable.has(agent.status) ? async () => {
      const controller = new AbortController(), token = csrfToken()
      writes.current.add(controller)
      try {
        await api(`/sessions/${encodeURIComponent(agent.session_id!)}/stop`, "POST", undefined, controller.signal)
        if (mounted.current && csrfToken() === token) await resource?.refresh()
      } catch (reason) {
        if (mounted.current && csrfToken() === token && reason instanceof ApiError && [401, 403, 404].includes(reason.status)) resource?.fail(reason)
        throw reason
      } finally { writes.current.delete(controller) }
    } : undefined,
  }))
  const working = agents.filter(agent => ["running", "starting", "saving"].includes(agent.status)).length
  return <section className="swarm-panel subagent-space-panel" aria-label="Subagent team">
    <div className="swarm-heading">
      <span className="swarm-heading-title">Agents <span>{agents.length} subagents</span></span>
      <span className="swarm-meta">{working ? `${working} working` : "Delegated work"}</span>
      <div className="swarm-toolbar"><AgentViewSwitch space={space} onChange={onSpaceChange} disabled={blocked} /></div>
    </div>
    {resource?.error && <p className="notice" role="status">{resource.error}</p>}
    {space && <div className="swarm-details swarm-space-details">
      <AgentSpace center={{ id: session.id, name: "Moyai", label: "Moyai", role: "Lead", model: session.model,
        status: session.status, href: `#${session.id}`, onOpen: () => onSpaceChange(false) }}
        agents={nodes} title={session.title} kicker={`One conversation · ${agents.length} subagents`}
        label="Live subagent space">
        {resource?.data?.subagents_limited && <p className="muted orbit-limited">Showing recent delegated work.</p>}
      </AgentSpace>
    </div>}
  </section>
}
