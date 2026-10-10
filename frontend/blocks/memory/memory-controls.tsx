import { useMemo } from "react"
import type { MemoryPreferences, MemoryReview } from "../../api/generated/types.gen"
import { Button } from "../../components/ui/button"
import { Label } from "../../components/ui/label"
import { FormSelect } from "../../form-select"
import { optionFragment } from "../../lib/options"
import { relativeDate } from "../../lib/time"

function reviewSummary(review: MemoryReview | null | undefined) {
  if (!review?.enabled) return "Automatic saving during sessions is available. Background review is disabled."
  if (!review.configured) return "Background review needs a configured model connection."
  const latest = review.latest
  const result = latest?.status === "failed" ? "The last review could not finish. Future completed turns will still be reviewed."
    : latest?.status === "completed" ? `Last review ${relativeDate(latest.updated_at)}: ${latest.saved_count ? `saved ${latest.saved_count} ${latest.saved_count === 1 ? "memory" : "memories"}.` : "no new lasting context to save."}`
    : "Finished sessions are reviewed after a short idle period. Recent sessions are checked when background review starts."
  return `${review.pending ? `${review.pending} finished ${review.pending === 1 ? "turn is" : "turns are"} waiting for review. ` : ""}${result}`
}

type Props = {
  preferences: MemoryPreferences
  review: MemoryReview | null | undefined
  pending: boolean
  onChange(changes: Partial<MemoryPreferences>): void
  onRefresh(): void
}

export function MemoryControls({ preferences, review, pending, onChange, onRefresh }: Props) {
  const learning = useMemo(() => optionFragment([["auto", "Save useful context automatically"], ["manual", "Only save manually"]], preferences.auto_save ? "auto" : "manual"), [preferences.auto_save])
  return <>
    <div className="memory-controls">
      <div><strong>{preferences.enabled ? "Moyai remembers useful context" : "Memory is paused"}</strong>
        <p className="subtext">{preferences.enabled ? "Preferences, corrections, and lessons learned during work can carry into future sessions." : "Your notes are kept here. Moyai won’t retrieve or save them while paused."}</p></div>
      <Button variant="ghost" className="quiet" id="memory-toggle" disabled={pending} onClick={() => onChange({ enabled: !preferences.enabled })}>{preferences.enabled ? "Pause memory" : "Resume memory"}</Button>
    </div>
    <div className="memory-learning"><Label htmlFor="memory-learning">How new memories are saved</Label>
      <FormSelect id="memory-learning" disabled={!preferences.enabled || pending} initialOptions={learning} onChange={event => onChange({ auto_save: event.currentTarget.value === "auto" })} /></div>
    {preferences.enabled && preferences.auto_save && <div className="memory-review">
      <p className="subtext" role="status">{reviewSummary(review)}</p>
      <Button variant="ghost" className="quiet" id="memory-refresh" onClick={onRefresh}>Refresh</Button>
    </div>}
  </>
}
