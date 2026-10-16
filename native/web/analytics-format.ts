/** Scoped dependencies for the unchanged legacy analytics renderer. */
export function esc(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]!)
}

export function finiteValue(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
}

export function dollars(value: unknown): string {
  if (finiteValue(value) && value > 0 && value < .000001) return "Less than $0.000001"
  return finiteValue(value) ? new Intl.NumberFormat("en-US", {
    style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 6,
  }).format(value) : "Unavailable"
}

export function spendCount(value: unknown): string {
  return finiteValue(value) ? value.toLocaleString() : "Unavailable"
}

export function elapsed(value: unknown): string {
  if (!finiteValue(value)) return "Unavailable"
  return value < 1000 ? `${Math.round(value)} ms` : value < 60000 ? `${(value / 1000).toFixed(1)} s` : `${(value / 60000).toFixed(1)} min`
}
