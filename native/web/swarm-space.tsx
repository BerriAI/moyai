import React from "react"
import { AgentSpace, type OrbitAgent } from "./agent-space"
import type { SwarmView } from "./swarm"

const publicKinds = new Set(["finding", "result", "lead", "completed"])

export function SwarmSpace({ data, sessionId, onConversation }: {
  data: SwarmView; sessionId: string; visible: boolean; onConversation: () => void
}) {
  const center = data.members.find(member => member.session_id === data.leader_id)
    ?? data.members.find(member => member.coordinator)
  if (!center) return null
  const latest = new Map(data.posts.filter(post => publicKinds.has(post.kind)).map(post => [post.author_session_id, post]))
  const workers = data.members.filter(member => !member.coordinator)
  const nodes: OrbitAgent[] = data.members.map(member => ({
    id: member.session_id, name: member.name, model: member.model, status: member.status, deleted: member.deleted,
    label: member.coordinator ? "Moyai" : String(workers.indexOf(member) + 1).padStart(2, "0"),
    role: member.session_id === data.leader_id ? "Lead" : member.coordinator ? "Coordinator" : undefined,
    task: member.assignment, update: latest.get(member.session_id)?.content,
    href: `#${member.session_id}`, onOpen: member.session_id === sessionId ? onConversation : undefined,
  }))
  const feed = data.posts.filter(post => post.kind !== "goal").slice(-6).reverse()
  return <AgentSpace center={nodes.find(node => node.id === center.session_id)!}
    agents={nodes.filter(node => node.id !== center.session_id)} title={data.title ?? data.goal.split("\n")[0]}
    goal={data.goal} kicker={`One goal · ${workers.length} independent workers`}
    label="Live swarm space" active={data.status === "active"}>
    <details className="swarm-space-feed">
      <summary>Team updates <span>{feed.length ? "Latest public findings and replies" : "No public updates yet"}</span></summary>
      <div role="log" aria-live="off">
        {feed.map(post => <article key={post.id}>
          <div><strong>{post.author}</strong><span>{post.kind === "human" ? "Direction" : post.kind}</span>
            <time dateTime={post.created_at}>{new Date(post.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time></div>
          {post.author_session_id ? <a href={`#${post.author_session_id}`} onClick={event => {
            if (post.author_session_id === sessionId && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey && event.button === 0) {
              event.preventDefault(); onConversation()
            }
          }}><span>{post.content.slice(0, 2000)}</span><small>Open thread</small></a>
            : <p>{post.content.slice(0, 2000)}</p>}
        </article>)}
      </div>
    </details>
  </AgentSpace>
}
