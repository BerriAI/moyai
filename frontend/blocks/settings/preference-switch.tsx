import { Label } from "../../components/ui/label"
import { Switch } from "../../components/ui/switch"

type Props = {
  id: string
  title: string
  description: string
  checked: boolean
  pending: boolean
  status: string
  onCheckedChange(checked: boolean): void
}

export function PreferenceSwitch({ id, title, description, checked, pending, status, onCheckedChange }: Props) {
  return <div className="settings-preference">
    <Label htmlFor={id}>
      <span className="settings-copy"><strong id={`${id}-title`}>{title}</strong><span id={`${id}-description`}>{description}</span></span>
      <Switch id={id} checked={checked} disabled={pending} onCheckedChange={onCheckedChange} aria-labelledby={`${id}-title`} aria-describedby={`${id}-description ${id}-status`} />
    </Label>
    <p id={`${id}-status`} className="subtext" role="status">{status}</p>
  </div>
}
