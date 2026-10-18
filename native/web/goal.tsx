import React, { useEffect, useState } from "react"
import { Target } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import "./goal.css"

export type Goal = {
  objective: string; status: "active" | "paused" | "complete" | "blocked" | "budget_limited"
  owner_id: string; generation: number; tokens_used: number; token_budget: number | null
  turns: number; blocked_count: number; reason: string; created_at: string
}
const labels = { active: "Working toward goal", paused: "Goal paused", complete: "Goal complete", blocked: "Goal needs attention", budget_limited: "Goal budget reached" }

export function GoalDialog({ open, onOpenChange, goal, onSubmit }: {
  open: boolean; onOpenChange: (open: boolean) => void; goal?: Goal | null
  onSubmit: (command: string) => Promise<void>
}) {
  const [objective, setObjective] = useState(""), [budget, setBudget] = useState("")
  const [busy, setBusy] = useState(false), [error, setError] = useState("")
  useEffect(() => {
    if (open) { setObjective(goal?.objective ?? ""); setBudget(goal?.token_budget?.toString() ?? ""); setError("") }
  }, [open, goal?.generation])
  return <Dialog open={open} onOpenChange={next => { if (!busy) onOpenChange(next) }}>
    <DialogContent className="goal-dialog">
      <DialogHeader><DialogTitle>{goal ? "Edit goal" : "Set a goal"}</DialogTitle>
        <DialogDescription>Moyai keeps working across turns until the goal is complete. You can pause or resume at any time.</DialogDescription></DialogHeader>
      <form onSubmit={event => {
        event.preventDefault(); setBusy(true); setError("")
        const budgetArg = budget ? `--budget ${budget} ` : goal?.token_budget ? "--budget none " : ""
        const command = `/goal ${goal ? "edit " : ""}${budgetArg}${objective.trim()}`
        void onSubmit(command).then(() => onOpenChange(false)).catch(reason => setError(String(reason))).finally(() => setBusy(false))
      }}>
        <label htmlFor="goal-objective">Objective</label>
        <Textarea id="goal-objective" value={objective} onChange={event => setObjective(event.target.value)} maxLength={4000} rows={5} required disabled={busy} placeholder="Describe the outcome and how to verify it…" />
        <label htmlFor="goal-budget">Token budget (optional)</label>
        <Input id="goal-budget" type="number" min={1} max={999999999} step={1} value={budget} onChange={event => setBudget(event.target.value)} disabled={busy} placeholder="No goal token limit" />
        {goal && <p className="muted">{goal.status === "active" ? "Active work continues with the revised objective." : "Resume after reviewing the updated goal."}</p>}
        {error && <p role="alert" className="error">{error}</p>}
        <DialogFooter><Button type="button" variant="ghost" disabled={busy} onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" disabled={busy || !objective.trim()}>{busy ? "Saving…" : goal ? "Save goal" : "Start goal"}</Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

export function GoalPanel({ goal, canControl, busy, onCommand, onEdit }: {
  goal: Goal; canControl: boolean; busy: boolean
  onCommand: (command: string) => void; onEdit: () => void
}) {
  const [clearing, setClearing] = useState(false)
  return <section className="goal-panel" aria-label="Conversation goal">
    <div className="goal-heading"><Target size={16} aria-hidden="true" /><strong role="status">{labels[goal.status]}</strong>
      <span className="goal-usage">{goal.tokens_used.toLocaleString()}{goal.token_budget ? ` / ${goal.token_budget.toLocaleString()}` : ""} tokens · {goal.turns} turns</span></div>
    <p className="goal-objective">{goal.objective}</p>
    {goal.reason && <p className="goal-reason">{goal.reason}</p>}
    {canControl && <div className="goal-actions">
      {goal.status === "active" ? <Button size="sm" variant="outline" disabled={busy} onClick={() => onCommand("/goal pause")}>Pause goal</Button>
        : goal.status !== "complete" && <Button size="sm" disabled={busy || goal.status === "budget_limited"} onClick={() => onCommand("/goal resume")}>Resume goal</Button>}
      <Button size="sm" variant="ghost" disabled={busy} onClick={onEdit}>Edit goal</Button>
      <Button size="sm" variant="ghost" disabled={busy} onClick={() => setClearing(true)}>Clear goal</Button>
    </div>}
    <Dialog open={clearing} onOpenChange={setClearing}><DialogContent>
      <DialogHeader><DialogTitle>Clear this goal?</DialogTitle><DialogDescription>This stops goal work and removes its objective. Your conversation and saved results remain available.</DialogDescription></DialogHeader>
      <DialogFooter><Button variant="ghost" onClick={() => setClearing(false)}>Cancel</Button>
        <Button onClick={() => { setClearing(false); onCommand("/goal clear") }}>Clear goal</Button></DialogFooter>
    </DialogContent></Dialog>
  </section>
}
