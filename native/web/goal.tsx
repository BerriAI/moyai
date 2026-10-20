import React, { useEffect, useId, useState } from "react"
import { Maximize2, Pause, Play, Target, Trash2, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import "./goal.css"

import type { GoalView } from "./generated/api"
export type Goal = GoalView

const labels = { active: "Pursuing goal", paused: "Paused goal", complete: "Goal complete", blocked: "Goal needs attention", budget_limited: "Goal budget reached" }

export function goalCommandAction(prompt: string): "view" | "set" | "edit" | "pause" | "resume" | "clear" | null {
  if (/^( {4}|\t)/.test(prompt) || !/^\/goal(?:\s|$)/.test(prompt.trimStart())) return null
  const value = prompt.trim().slice(5).trim()
  if (!value) return "view"
  if (value === "pause" || value === "resume" || value === "clear") return value
  return /^edit(?:\s|$)/.test(value) ? "edit" : "set"
}

export function GoalChip({ onRemove, disabled = false, replacing = false }: { onRemove: () => void; disabled?: boolean; replacing?: boolean }) {
  return <Button type="button" variant="ghost" size="sm" className="goal-chip" disabled={disabled} onClick={onRemove}
    aria-label="Remove goal mode" title={replacing ? "Send to replace the current goal. Click to remove goal mode." : "Send as a goal. Click to remove goal mode."}>
    <Target size={14} aria-hidden="true" /><span>Goal</span><X size={12} className="goal-chip-remove" aria-hidden="true" />
  </Button>
}

export function GoalMessageLabel() {
  return <div className="goal-message-label"><Target size={13} aria-hidden="true" />Sent as goal</div>
}

export function GoalEditor({ goal, canControl, busy, onSubmit }: {
  goal: Goal; canControl: boolean; busy: boolean; onSubmit: (command: string) => Promise<void>
}) {
  const id = useId()
  const [saved, setSaved] = useState(goal)
  const [objective, setObjective] = useState(goal.objective), [budget, setBudget] = useState(goal.token_budget?.toString() ?? "")
  const [saving, setSaving] = useState(false), [error, setError] = useState("")
  const changed = objective !== saved.objective || budget !== (saved.token_budget?.toString() ?? "")
  const stale = saved.generation !== goal.generation
  function reset() { setSaved(goal); setObjective(goal.objective); setBudget(goal.token_budget?.toString() ?? ""); setError("") }
  useEffect(() => { if (!changed && stale) reset() }, [goal.generation, changed, stale])
  return <form className="goal-editor" aria-label="Edit goal" onSubmit={event => {
    event.preventDefault()
    if (!canControl || busy || saving || stale) return
    setSaving(true); setError("")
    const command = `/goal edit --budget ${budget || "none"} ${objective.trim()}`
    void onSubmit(command).then(() => { setObjective(objective.trim()); setSaved({ ...goal, objective: objective.trim(), token_budget: budget ? Number(budget) : null }) })
      .catch(reason => setError(String(reason))).finally(() => setSaving(false))
  }}>
    <div className="goal-editor-toolbar"><span>{saving ? "Saving…" : changed ? "Unsaved changes" : "Saved"}</span>
      {canControl && <div><Button type="button" variant="ghost" size="sm" disabled={busy || saving || (!changed && !stale)} onClick={reset}>Reset</Button>
        <Button type="submit" size="sm" disabled={busy || saving || stale || !changed || !objective.trim()}>Save goal</Button></div>}
    </div>
    {stale && <p className="notice" role="alert">The goal changed. <Button type="button" variant="ghost" size="sm" onClick={reset}>Load current goal</Button></p>}
    {error && <p role="alert" className="error">{error}</p>}
    <label className="visually-hidden" htmlFor={`${id}-objective`}>Objective</label>
    <Textarea id={`${id}-objective`} className="goal-editor-objective" value={objective} onChange={event => setObjective(event.target.value)} maxLength={4000} required readOnly={!canControl} disabled={saving} placeholder="Describe the outcome and how to verify it…" />
    <details className="goal-editor-options"><summary>Goal details</summary>
      <p>{labels[goal.status]} · {goal.tokens_used.toLocaleString()} tokens · {goal.turns} turns</p>
      {goal.reason && <p>{goal.reason}</p>}
      <label htmlFor={`${id}-budget`}>Token budget (optional)</label>
      <Input id={`${id}-budget`} type="number" min={1} max={999999999} step={1} value={budget} onChange={event => setBudget(event.target.value)} readOnly={!canControl} disabled={saving} placeholder="No goal token limit" />
      <p className="muted">{goal.status === "active" ? "Saving updates the active goal." : "Saving keeps the goal paused. Resume when ready."}</p>
    </details>
  </form>
}

function duration(seconds: number) {
  const value = Math.max(0, Math.floor(seconds))
  return value < 60 ? `${value}s` : value < 3600 ? `${Math.floor(value / 60)}m ${value % 60}s` : `${Math.floor(value / 3600)}h ${Math.floor(value % 3600 / 60)}m`
}

export function GoalPanel({ goal, canControl, busy, onCommand, onEdit }: {
  goal: Goal; canControl: boolean; busy: boolean
  onCommand: (command: string) => void; onEdit: () => void
}) {
  const usage = `${goal.tokens_used.toLocaleString()}${goal.token_budget ? ` / ${goal.token_budget.toLocaleString()}` : ""} tokens · ${goal.turns} turns`
  return <section className="goal-panel" aria-label="Conversation goal">
    <div className="goal-bar">
      <Target size={14} aria-hidden="true" />
      <button type="button" className="goal-summary" onClick={onEdit} title={`${goal.objective}\n${usage}`} aria-label="View goal details">
        <span role="status">{labels[goal.status]}</span><span className="goal-objective">{goal.objective}</span>
      </button>
      <span className="goal-elapsed" title="Time spent in goal turns">{duration(goal.elapsed_seconds)}</span>
      <span className="goal-usage" title={usage}>{usage}</span>
      <div className="goal-actions">
        {canControl && <>
          <Button type="button" size="icon-sm" variant="ghost" disabled={busy} aria-label="Clear goal" title="Clear goal and stop work" onClick={() => onCommand("/goal clear")}><Trash2 size={14} /></Button>
          {goal.status !== "complete" && <Button type="button" size="icon-sm" variant="ghost" disabled={busy} aria-label={goal.status === "active" ? "Pause goal" : goal.status === "budget_limited" ? "Edit goal budget" : "Resume goal"}
            title={goal.status === "active" ? "Pause goal" : goal.status === "budget_limited" ? "Increase the budget to resume" : "Resume goal"}
            onClick={() => goal.status === "budget_limited" ? onEdit() : onCommand(goal.status === "active" ? "/goal pause" : "/goal resume")}>
            {goal.status === "active" ? <Pause size={14} /> : <Play size={14} />}
          </Button>}
        </>}
        <Button type="button" size="icon-sm" variant="ghost" aria-label="Expand goal" title={canControl ? "Edit goal" : "View goal"} onClick={onEdit}><Maximize2 size={14} /></Button>
      </div>
    </div>
    {goal.reason && ["blocked", "budget_limited"].includes(goal.status) && <p className="goal-reason" role="status">{goal.reason}</p>}
  </section>
}
