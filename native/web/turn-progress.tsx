import React, { useEffect, useRef, useState } from "react"
import { SkillMarkdown } from "./skill-markdown"
import { Check, ChevronDown, ChevronRight, Circle, ExternalLink, Terminal, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import type { Turn } from "./conversation-state"
import type { LiveTranscript } from "./live-transcript"
import { AgentAvatar } from "./workspace"
import { workItems, type AgentProgress, type ToolProgress, type WorkEntry, type WorkSegment } from "./work-timeline"
import type { ActionSelection } from "./workspace-types"
import "./turn-progress.css"

const activeStates = ["starting", "running", "saving", "stopping"]
const statusLabels: Record<string, string> = {
  running: "Working", starting: "Starting", completed: "Completed", failed: "Failed",
  interrupted: "Interrupted", stopped: "Stopped", stopping: "Stopping", waiting: "Needs input",
}
const toolLabels: Record<string, string> = {
  shell: "Run command", read_file: "Read file", list_files: "List files", grep: "Search files",
  write_file: "Write file", edit_file: "Edit file", apply_patch: "Apply changes",
  delegate_task: "Delegate work", write_plan: "Update plan", read_plan: "Read plan",
  browser_open: "Open browser", browser_read: "Inspect browser", browser_action: "Use browser",
  search_conversation_history: "Search conversation history",
}
export const toolLabel = (name: string) => toolLabels[name] ?? name.replaceAll("_", " ")

function duration(start?: string | number | null, end?: string | number | null): string {
  const from = typeof start === "number" ? start : Date.parse(start ?? "")
  const to = typeof end === "number" ? end : Date.parse(end ?? "")
  if (!Number.isFinite(from) || !Number.isFinite(to)) return ""
  const seconds = Math.max(0, Math.floor((to - from) / 1000))
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`
}

type ActionControls = {
  onAction: (tool: ToolProgress, agent?: AgentProgress) => void
  selectedAction?: ActionSelection
  turnId: string
  agent?: AgentProgress
}

function ToolList({ tools, onAction, selectedAction, turnId, agent }: { tools: ToolProgress[] } & ActionControls) {
  return <ol className="work-tool-list">{tools.map(tool => {
    const Status = tool.status === "completed" ? Check : tool.status === "failed" ? X : Circle
    const selected = selectedAction?.turnId === turnId && selectedAction.toolId === tool.id && selectedAction.agentId === agent?.id
    return <li key={tool.id}><Button type="button" variant="ghost" className="work-tool-action"
      aria-label={`Open ${toolLabel(tool.name)} action`} aria-pressed={selected} onClick={() => onAction(tool, agent)}>
      <Status size={13} aria-hidden="true" />
      <span>{toolLabel(tool.name)}</span>
      <small className={tool.status === "failed" ? "error" : ""}>
        {tool.status === "running" ? "Running" : statusLabels[tool.status] ?? tool.status}
        {tool.finished_at && duration(tool.created_at, tool.finished_at) ? ` · ${duration(tool.created_at, tool.finished_at)}` : ""}
      </small>
      <ChevronRight size={13} aria-hidden="true" />
    </Button></li>
  })}</ol>
}

function ToolGroup({ tools, ...controls }: { tools: ToolProgress[] } & ActionControls) {
  const [open, setOpen] = useState(false)
  const running = tools.some(tool => tool.status === "running")
  return <Collapsible open={open} onOpenChange={setOpen} className="work-tool-group">
    <CollapsibleTrigger asChild><Button type="button" variant="ghost" className="work-detail-toggle">
      <Terminal size={14} aria-hidden="true" />
      <span>{tools.length} {tools.length === 1 ? "action" : "actions"}</span>
      {running && <span className="running-dot" aria-hidden="true" />}
      {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
    </Button></CollapsibleTrigger>
    <CollapsibleContent><ToolList tools={tools} {...controls} /></CollapsibleContent>
  </Collapsible>
}

function AgentWork({ agent, now, onOpen, ...controls }: {
  agent: AgentProgress; now: number; onOpen?: (agent: AgentProgress) => void
} & ActionControls) {
  const [open, setOpen] = useState(false)
  const latest = agent.messages.at(-1)
  const working = ["running", "starting", "stopping"].includes(agent.status)
  const elapsed = duration(agent.created_at, working ? now : agent.updated_at)
  const currentTool = agent.tools.filter(tool => tool.status === "running").at(-1)
  return <Collapsible open={open} onOpenChange={setOpen} className="work-agent" data-agent-id={agent.id}>
    <div className="work-agent-heading">
      <CollapsibleTrigger asChild><Button type="button" variant="ghost" className="work-detail-toggle">
        <AgentAvatar avatar={agent.avatar} />
        <strong>{agent.name}</strong>
        <span className="work-agent-state">{statusLabels[agent.status] ?? agent.status}{elapsed ? ` · ${elapsed}` : ""}</span>
        {working && <span className="running-dot" aria-hidden="true" />}
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
      </Button></CollapsibleTrigger>
      {onOpen && <Button type="button" variant="ghost" size="icon-sm"
        aria-label={`Open ${agent.name} details`} onClick={() => onOpen(agent)}><ExternalLink size={13} /></Button>}
    </div>
    {agent.parent_name && <p className="work-agent-parent">Delegated by {agent.parent_name}</p>}
    {!open && latest && <div className="work-agent-preview message-content">
      <SkillMarkdown>{latest.text}</SkillMarkdown>
    </div>}
    {!latest && <p className="muted work-agent-preview">{working ? "Waiting for the first update…" : "No public update was recorded."}</p>}
    {currentTool && <p className="work-agent-action" role="status">{toolLabel(currentTool.name)}…</p>}
    <CollapsibleContent className="work-agent-history">
      {agent.messages.map(message => <div key={message.id} className="turn-progress-entry message-content">
        {message.phase === "final_answer" && <small className="work-result-label">{agent.name} result</small>}
        <SkillMarkdown>{message.text}</SkillMarkdown>
      </div>)}
      {agent.tools.length > 0 && <ToolGroup tools={agent.tools} agent={agent} {...controls} />}
      {(agent.messages_limited || agent.tools_limited) && <p className="muted">Showing recent subagent activity.</p>}
    </CollapsibleContent>
  </Collapsible>
}

/** A real final answer collapses its segment; steering keeps earlier activity. */
export function TurnProgress({ segment, entry, live, visible, request, onSubagent, onContentChange, onAction, selectedAction }: {
  segment: WorkSegment; entry?: WorkEntry; live?: LiveTranscript; visible: boolean
  request: (turn: Turn) => Promise<void>
  onSubagent?: (agent: AgentProgress) => void
  onContentChange?: () => void
  onAction: ActionControls["onAction"]
  selectedAction?: ActionSelection
}) {
  const { turn, earlier, hasFinalAnswer } = segment
  const running = !earlier && activeStates.includes(turn.status)
  const [open, setOpen] = useState(!hasFinalAnswer)
  const [now, setNow] = useState(Date.now)
  const requested = useRef("")
  useEffect(() => { if (hasFinalAnswer) setOpen(false) }, [hasFinalAnswer])
  useEffect(() => {
    if (!running || !visible) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [running, visible])
  useEffect(() => {
    if (!visible || (!open && !running && !requested.current) || requested.current === turn.status) return
    requested.current = turn.status
    if (entry?.status !== turn.status || !entry.data) void request(turn)
  }, [visible, open, running, turn, entry?.status, entry?.data, request])
  const items = workItems(segment, entry?.data, live)
  const signature = items.map(item => item.kind === "message" ? `${item.id}:${item.message.text}`
    : item.kind === "tools" ? item.tools.map(tool => `${tool.id}:${tool.status}`).join(",")
      : `${item.id}:${item.agent.status}:${item.agent.updated_at}`).join("|")
  useEffect(() => { if (visible) onContentChange?.() }, [signature, visible, onContentChange])
  const elapsed = duration(segment.after ?? turn.created_at, segment.before ?? turn.finished_at ?? now)
  const label = earlier ? "Activity" : hasFinalAnswer ? "Worked"
    : running ? "Working" : turn.status === "waiting" ? "Waiting for your response"
      : turn.status === "failed" ? "Work stopped with an error" : turn.status === "stopped" ? "Work stopped" : "Activity"
  const latest = items.filter(item => item.kind === "message").at(-1)
  if (turn.status === "queued" || (earlier && !items.length && !entry?.loading)) return null
  return <section className={`turn-progress ${running ? "is-working" : "is-finished"}`}
    aria-label="Work progress" data-turn-id={turn.id} data-segment-id={segment.id}>
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger asChild><Button type="button" variant="ghost" className="turn-progress-toggle">
        {open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
        <span>{label}{!earlier && elapsed ? ` for ${elapsed}` : ""}</span>
        {running && <span className="running-dot" aria-hidden="true" />}
      </Button></CollapsibleTrigger>
      {!open && running && latest?.kind === "message" && <div className="turn-progress-latest message-content">
        <SkillMarkdown>{latest.message.text}</SkillMarkdown>
      </div>}
      <CollapsibleContent className="turn-progress-history">
        {items.map(item => item.kind === "message"
          ? <div className="turn-progress-entry message-content" key={item.id}>
            <SkillMarkdown>{item.message.text}</SkillMarkdown>
          </div>
          : item.kind === "tools" ? <ToolGroup key={item.id} tools={item.tools} turnId={turn.id} onAction={onAction} selectedAction={selectedAction} />
            : <AgentWork key={item.id} agent={item.agent} now={running ? now : Date.now()} onOpen={onSubagent} turnId={turn.id} onAction={onAction} selectedAction={selectedAction} />)}
        {!items.length && !entry?.error && <p className="muted">{running ? "Waiting for the first update…" : entry?.loading ? "Loading saved activity…" : "No progress updates were recorded."}</p>}
        {entry?.error && <p className="error" role="alert">Could not refresh work details.
          <Button variant="ghost" size="sm" onClick={() => void request(turn)}>Retry</Button></p>}
        {(entry?.data?.tools_limited || entry?.data?.subagents_limited) && <p className="muted">Showing recent work details.</p>}
      </CollapsibleContent>
    </Collapsible>
  </section>
}
