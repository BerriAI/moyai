import React, { useEffect, useRef, useState } from "react"
import { ArrowUpRight, ChevronLeft, ChevronRight } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { SpaceBackdrop } from "./space-backdrop"
import "./swarm-space.css"

export type OrbitAgent = {
  id: string; name: string; label: string; status: string; role?: string; model?: string
  task?: string; update?: string; href?: string; restricted?: boolean; deleted?: boolean
  onOpen?: () => void; onStop?: () => Promise<void>
}
type Position = { x: number; y: number; depth: number; ring: number }
const working = new Set(["running", "starting", "saving", "steering"])

export function agentState(status: string, deleted = false) {
  if (deleted) return { label: "Deleted", tone: "quiet" }
  if (working.has(status)) return { label: "Working", tone: "working" }
  if (status === "failed" || status === "attention") return { label: "Failed", tone: "failed" }
  if (status === "waiting") return { label: "Needs input", tone: "waiting" }
  if (status === "idle") return { label: "Ready", tone: "done" }
  if (status === "completed") return { label: "Done", tone: "done" }
  return { label: ({ queued: "Queued", stopped: "Stopped", interrupted: "Interrupted" })[status] ?? status, tone: "quiet" }
}

function geometry(count: number, width: number, height: number) {
  const ringCount = count <= 6 ? 1 : count <= 14 ? 2 : 3
  const sizes = ringCount === 1 ? [count] : ringCount === 2 ? [4, count - 4] : [4, 6, count - 10]
  const scales = ringCount === 1 ? [width < 500 ? 1 : .87] : ringCount === 2 ? [.53, 1] : [.44, .71, 1]
  const cx = width / 2, cy = height / 2 - 18
  const rx = Math.max(70, (width - 92) / 2), ry = (height - 116) / 2
  const tilt = width < 600 ? 0 : -.12
  const positions: Position[] = []
  sizes.forEach((size, ring) => {
    for (let index = 0; index < size; index++) {
      const angle = count === 3 && width < 500 ? [-90, 12, 168][index] * Math.PI / 180 : ringCount > 1 && ring === 0
        ? [-35, 35, 145, 215][index] * Math.PI / 180
        : (index / size * Math.PI * 2) - Math.PI / 2 + (ring === 2 ? .35 : 0)
      const x = rx * scales[ring] * Math.cos(angle), y = ry * scales[ring] * Math.sin(angle)
      positions.push({
        x: cx + x * Math.cos(tilt) - y * Math.sin(tilt),
        y: cy + x * Math.sin(tilt) + y * Math.cos(tilt),
        depth: (Math.sin(angle) + 1) / 2, ring,
      })
    }
  })
  return { positions, scales, rx, ry, cx, cy, tilt }
}

function fits(layout: ReturnType<typeof geometry>, width: number, height: number) {
  const boxes = [
    { x: layout.cx - (width < 500 ? 48 : 74), y: layout.cy - 42, w: width < 500 ? 96 : 148, h: 122 },
    ...layout.positions.map(point => ({ x: point.x - 36, y: point.y - 14, w: 72, h: 58 })),
  ]
  return boxes.every((box, index) => box.x >= 0 && box.y >= 0 && box.x + box.w <= width && box.y + box.h <= height
    && boxes.slice(index + 1).every(other => box.x + box.w <= other.x || other.x + other.w <= box.x
      || box.y + box.h <= other.y || other.y + other.h <= box.y))
}

export function AgentViewSwitch({ space, onChange, disabled = false }: {
  space: boolean; onChange: (value: boolean) => void; disabled?: boolean
}) {
  return <div className="swarm-view-switch" role="group" aria-label="Agent view">
    <Button type="button" variant={space ? "secondary" : "ghost"} size="sm" aria-pressed={space}
      disabled={disabled} onClick={() => onChange(true)}>Space</Button>
    <Button type="button" variant={!space ? "secondary" : "ghost"} size="sm" aria-pressed={!space}
      onClick={() => onChange(false)}>Chat</Button>
  </div>
}

function AgentCard({ agent }: { agent: OrbitAgent }) {
  const state = agentState(agent.status, agent.deleted)
  const [busy, setBusy] = useState(false), [error, setError] = useState("")
  function open(event: React.MouseEvent<HTMLAnchorElement>) {
    if (agent.onOpen && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey && event.button === 0) {
      event.preventDefault(); agent.onOpen()
    }
  }
  return <PopoverContent className="orbit-agent-card" sideOffset={10} collisionPadding={12}
    aria-label={`${agent.name} details`}>
    <div className="orbit-card-heading"><strong>{agent.name}</strong><span data-tone={state.tone}>{state.label}</span></div>
    {(agent.role || agent.model) && <p className="orbit-card-meta">{[agent.role, agent.model].filter(Boolean).join(" · ")}</p>}
    {agent.restricted ? <p>Details are available only to the requester.</p> : <>
      {agent.task && <p className="orbit-card-task">{agent.task}</p>}
      {agent.update && <div className="orbit-card-update"><small>Latest update</small><p>{agent.update}</p></div>}
      {!agent.task && !agent.update && <p>Open the conversation to see this agent’s work.</p>}
      <div className="orbit-card-actions">
        {agent.href ? <Button asChild variant="ghost" size="sm"><a href={agent.href} onClick={open}>Open thread <ArrowUpRight size={13} /></a></Button>
          : agent.onOpen && <Button variant="ghost" size="sm" onClick={agent.onOpen}>Open details <ArrowUpRight size={13} /></Button>}
        {agent.onStop && <Button className="orbit-stop" variant="ghost" size="sm" disabled={busy} onClick={async () => {
          setBusy(true); setError("")
          try { await agent.onStop?.() } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not stop this agent.") }
          finally { setBusy(false) }
        }}>{busy ? "Stopping…" : "Stop agent"}</Button>}
      </div>
    </>}
    {error && <p role="alert" className="error">{error}</p>}
  </PopoverContent>
}

export function AgentSpace({ center, agents, title, goal, kicker, label, active = true, children }: {
  center: OrbitAgent; agents: OrbitAgent[]; title: string; goal?: string; kicker: string
  label: string; active?: boolean; children?: React.ReactNode
}) {
  const region = useRef<HTMLDivElement>(null)
  const [bounds, setBounds] = useState({ width: 800, height: 560 })
  const [page, setPage] = useState(0), [selected, setSelected] = useState<string | null>(null)
  useEffect(() => {
    const element = region.current
    if (!element) return
    const observer = new ResizeObserver(() => {
      const next = { width: element.clientWidth, height: element.clientHeight }
      setBounds(previous => previous.width === next.width && previous.height === next.height ? previous : next)
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [])
  const width = Math.max(220, bounds.width), height = Math.max(350, Math.min(600, bounds.height - 100))
  let capacity = width >= 900 ? 24 : width >= 600 ? 12 : width >= 420 ? 6 : 4
  while (capacity > 2 && !fits(geometry(Math.min(capacity, agents.length), width, height), width, height)) capacity--
  const pages = Math.max(1, Math.ceil(agents.length / capacity)), current = Math.min(page, pages - 1)
  const shown = agents.slice(current * capacity, (current + 1) * capacity)
  const layout = geometry(shown.length, width, height)
  const nodes = [{ agent: center, x: layout.cx, y: layout.cy, depth: 1, sun: true },
    ...shown.map((agent, index) => ({ agent, ...layout.positions[index], sun: false }))]
  return <div className="swarm-space" ref={region} role="region" aria-label={label} data-compact={width < 500}>
    <SpaceBackdrop variant="chat" />
    <div className="swarm-space-mission">
      <p className="swarm-space-kicker">{kicker}</p>
      {goal ? <details className="swarm-space-goal"><summary aria-label="Shared goal"><span>{title}</span></summary><p>{goal}</p></details>
        : <p className="swarm-space-goal">{title}</p>}
    </div>
    <div className="orbit-stage" style={{ height }}>
      <svg className="orbit-lines" viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
        <g transform={`rotate(${layout.tilt * 180 / Math.PI} ${layout.cx} ${layout.cy})`}>
          {layout.scales.map(scale => <ellipse key={scale} cx={layout.cx} cy={layout.cy}
            rx={layout.rx * scale} ry={layout.ry * scale} />)}
        </g>
        {active && nodes.filter(node => !node.sun && working.has(node.agent.status)).map(node =>
          <line key={node.agent.id} className="orbit-beam" x1={layout.cx} y1={layout.cy} x2={node.x} y2={node.y} />)}
      </svg>
      <div aria-label="Agents">{nodes.map(({ agent, x, y, depth, sun }) => {
        const state = agentState(agent.status, agent.deleted)
        return <Popover key={agent.id} open={selected === agent.id} onOpenChange={open => setSelected(open ? agent.id : null)}>
          <PopoverTrigger asChild>
            <Button variant="ghost" className={`orbit-agent ${sun ? "orbit-sun" : ""}`} data-agent-id={agent.id}
              data-tone={state.tone} data-working={active && state.tone === "working"}
              style={{ left: x, top: y, "--planet-size": `${17 + depth * 7}px`, "--planet-depth": .55 + depth * .45 } as React.CSSProperties}
              aria-label={`${agent.name} · ${state.label} · View details`}>
              <span className="orbit-planet" aria-hidden="true">{sun && <span>moyai</span>}<i /></span>
              <span className="orbit-name">{sun ? agent.role ?? agent.name : agent.label}</span>
              {sun ? <span className="orbit-sun-caption">{agent.role ? agent.name : state.label}{agent.model && ` · ${agent.model.replace(/^openai\//, "")}`}</span>
                : <span className="orbit-status">{state.label}</span>}
            </Button>
          </PopoverTrigger>
          <AgentCard agent={agent} />
        </Popover>
      })}</div>
    </div>
    {pages > 1 && <nav className="orbit-pagination" aria-label="Agent pages">
      <Button variant="ghost" size="icon-sm" disabled={current === 0} aria-label="Previous agents"
        onClick={() => { setSelected(null); setPage(current - 1) }}><ChevronLeft size={16} /></Button>
      <span>{current * capacity + 1}–{Math.min((current + 1) * capacity, agents.length)} of {agents.length} agents</span>
      <Button variant="ghost" size="icon-sm" disabled={current === pages - 1} aria-label="Next agents"
        onClick={() => { setSelected(null); setPage(current + 1) }}><ChevronRight size={16} /></Button>
    </nav>}
    {children}
  </div>
}
