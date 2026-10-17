import React, { useEffect, useRef, useState } from "react"
import { Bot, Check, Circle, Crown, OctagonAlert } from "lucide-react"
import { Button } from "@/components/ui/button"
import logo from "./assets/moyai-logo.png"
import type { SwarmView } from "./swarm"
import type { SpaceGraph } from "./space-field"
import "./space-field.js"
import "./swarm-space.css"

const working = new Set(["running", "starting", "saving", "queued"])
const publicKinds = new Set(["finding", "result", "lead", "completed"])

function statusLabel(status: string, deleted: boolean) {
  if (deleted) return "Deleted"
  return ({ running: "Working", starting: "Starting", saving: "Saving", queued: "Queued", idle: "Ready",
    waiting: "Needs input", failed: "Failed", stopped: "Stopped" })[status] ?? status
}

export function SwarmSpace({ data, sessionId, visible, onConversation }: {
  data: SwarmView; sessionId: string; visible: boolean; onConversation: () => void
}) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const region = useRef<HTMLDivElement>(null)
  const stage = useRef<HTMLDivElement>(null)
  const redraw = useRef<(() => void) | undefined>(undefined)
  const [bounds, setBounds] = useState({ width: 800, height: 600 })
  const { width } = bounds
  const compact = width >= 440 && bounds.height < 700
  const workers = data.members.filter(member => !member.coordinator)
  const columns = Math.max(1, Math.min(workers.length, width < 440 ? 1 : width < 720 ? 2 : width < 1080 ? 3 : 4))
  const rows = Math.ceil(workers.length / columns)
  const height = compact ? 190 + rows * 200 : 240 + rows * 230
  const nodes = data.members.map(member => {
    const index = workers.indexOf(member), row = Math.floor(index / columns)
    const count = Math.min(columns, workers.length - row * columns)
    return { ...member, id: member.session_id, parentId: member.coordinator ? null : data.id,
      x: member.coordinator ? .5 : ((index % columns) + .5) / count,
      y: (member.coordinator ? 60 : compact ? 210 + row * 200 : 290 + row * 230) / height }
  })
  const graph = useRef<SpaceGraph>({ nodes, active: data.status === "active" })
  graph.current = { nodes, active: data.status === "active" }
  const updates = data.posts.filter(post => publicKinds.has(post.kind))
  const latest = new Map(updates.map(post => [post.author_session_id, post]))
  const feed = data.posts.filter(post => post.kind !== "goal").slice(-6).reverse()

  useEffect(() => {
    const element = stage.current
    if (!element || !region.current) return
    const measure = () => {
      const next = { width: element.clientWidth, height: region.current?.clientHeight ?? 600 }
      setBounds(previous => previous.width === next.width && previous.height === next.height ? previous : next)
    }
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    observer.observe(region.current)
    measure()
    return () => observer.disconnect()
  }, [])
  useEffect(() => {
    if (!visible || !canvas.current) return
    const cleanup = window.MoyaiSpace?.mount(canvas.current, { graph: () => graph.current })
    redraw.current = cleanup?.resize
    return () => { redraw.current = undefined; cleanup?.() }
  }, [visible])
  // Reduced-motion canvases have no animation loop; refresh their saved geometry too.
  useEffect(() => { redraw.current?.() }, [data, width, height])

  function open(event: React.MouseEvent<HTMLAnchorElement>, id: string) {
    if (id === sessionId && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey && event.button === 0) {
      event.preventDefault(); onConversation()
    }
  }

  return <div className="swarm-space" ref={region} role="region" aria-label="Live swarm space" data-compact={compact}>
    <div className="swarm-space-mission">
      <p className="swarm-space-kicker">One goal · {workers.length} independent workers</p>
      <details className="swarm-space-goal"><summary aria-label="Shared goal"><span>{data.goal}</span></summary><p>{data.goal}</p></details>
    </div>
    <div className="swarm-space-stage" ref={stage} style={{ height }}>
      <canvas ref={canvas} className="swarm-space-canvas" aria-hidden="true" />
      <div aria-label="Swarm members">{nodes.map(member => {
        const post = latest.get(member.session_id)
        const label = statusLabel(member.status, member.deleted)
        const active = data.status === "active" && working.has(member.status) && !member.deleted
        const attention = member.status === "waiting" || member.status === "failed"
        return <div key={member.session_id} className="swarm-space-node"
          style={{ left: `${member.x * 100}%`, top: `${member.y * 100}%`, width: Math.min(220, width / columns - 24) }}>
          <Button asChild variant="ghost" className="swarm-space-agent">
            <a href={`#${member.session_id}`} onClick={event => open(event, member.session_id)}
              aria-label={`${member.name} · ${label} · Open conversation`} aria-current={sessionId === member.session_id ? "page" : undefined}>
              <span className="swarm-space-mark" data-working={active} data-attention={attention}>
                {member.coordinator ? <img src={logo} alt="" width={36} height={36} /> : <Bot className="size-7" />}
                <span className="swarm-space-state" aria-hidden="true">
                  {attention ? <OctagonAlert size={12} /> : active ? <Circle size={10} /> : <Check size={12} />}
                </span>
              </span>
              <strong>{member.name}{member.session_id === data.leader_id && <span className="swarm-lead"><Crown size={11} />Lead</span>}</strong>
              <span className="swarm-space-model" title={member.model}>{member.model.replace(/^openai\//, "")}</span>
              <span className="swarm-space-status">{label}</span>
            </a>
          </Button>
          {!(compact && member.coordinator) && (post ? <a className="swarm-space-preview" href={`#${member.session_id}`} onClick={event => open(event, member.session_id)}
            aria-label={`${member.name} · Read latest ${post.kind === "result" ? "reply" : "update"}`}>
            <span>{post.content.slice(0, 2000)}</span><small>{post.kind === "result" ? "Reply" : "Board update"} · Open conversation</small>
          </a> : <p className="swarm-space-empty">No public update yet</p>)}
        </div>
      })}</div>
    </div>
    <section className="swarm-space-feed" aria-label="Shared live feed">
      <h3>Shared live feed <span>Latest team updates</span></h3>
      <div role="log" aria-live="off">
        {!feed.length && <p className="muted">Public findings and replies will appear here as the team works.</p>}
        {feed.map(post => <article key={post.id}>
          <div><strong>{post.author}</strong><span>{post.kind === "human" ? "Direction" : post.kind}</span>
            <time dateTime={post.created_at}>{new Date(post.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time></div>
          {post.author_session_id ? <a href={`#${post.author_session_id}`} onClick={event => open(event, post.author_session_id!)}>
            <span>{post.content.slice(0, 2000)}</span><small>Open conversation</small>
          </a> : <p>{post.content.slice(0, 2000)}</p>}
        </article>)}
      </div>
    </section>
  </div>
}
