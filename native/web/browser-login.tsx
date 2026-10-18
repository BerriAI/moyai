import { Monitor } from "lucide-react"
import { Button } from "@/components/ui/button"

export type LoginOutcome = "signed_in" | "cancelled"
export type BrowserLoginMetadata = { kind?: string; site?: string; reason?: string; workspace_id?: string }

export function BrowserLoginCard({ metadata, outcome, disabled, onOpen, onAnswer }: {
  metadata?: BrowserLoginMetadata; outcome?: LoginOutcome; disabled: boolean
  onOpen: () => void; onAnswer: (outcome: LoginOutcome) => void
}) {
  return <section className="browser-login-request" aria-label="Browser sign-in needed">
    <h3><Monitor size={16} aria-hidden="true" />Browser sign-in needed</h3>
    {metadata?.site && <p className="browser-login-site">{metadata.site}</p>}
    <p>{metadata?.reason || "Moyai needs you to sign in before it can continue."}</p>
    <p className="muted">In Computer, take control and sign in. Release control when you’re done, then confirm below. Keep passwords and verification codes in the browser.</p>
    <div className="browser-login-actions">
      <Button type="button" variant="outline" onClick={onOpen}>Open Computer</Button>
      <Button type="button" disabled={disabled} aria-pressed={outcome === "signed_in"} onClick={() => onAnswer("signed_in")}>I’ve signed in</Button>
      <Button type="button" variant="ghost" disabled={disabled} aria-pressed={outcome === "cancelled"} onClick={() => onAnswer("cancelled")}>Cancel sign-in</Button>
    </div>
  </section>
}
