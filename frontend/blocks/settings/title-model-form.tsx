import { Button } from "../../components/ui/button"
import { Input } from "../../components/ui/input"
import { Label } from "../../components/ui/label"

type Props = {
  model: string
  editable: boolean
  pending: boolean
  status: string
  onModelChange(model: string): void
  onSubmit(): void
}

export function TitleModelForm({ model, editable, pending, status, onModelChange, onSubmit }: Props) {
  return <section className="card settings-form-section" id="title-model-settings">
    <div><h2>Session titles</h2><p className="subtext">Choose the model that names new chats. Existing titles and the chat model stay the same.</p></div>
    <form id="title-model-form" onSubmit={event => { event.preventDefault(); onSubmit() }}>
      <Label htmlFor="title-model">Gateway model ID</Label>
      <Input id="title-model" maxLength={200} required placeholder="openai/gpt-4.1-nano" aria-describedby="title-model-status" disabled={!editable} value={model} onChange={event => onModelChange(event.target.value)} />
      <Button type="submit" disabled={!editable || pending}>Save model</Button>
      <p id="title-model-status" role="status">{status}</p>
    </form>
  </section>
}
