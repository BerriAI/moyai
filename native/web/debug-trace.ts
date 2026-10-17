import { api } from "./api"

export async function downloadDebugTrace(sessionId: string): Promise<void> {
  const trajectory = await api<object>(`/sessions/${encodeURIComponent(sessionId)}/debug-trace`)
  const blob = new Blob([JSON.stringify(trajectory, null, 2)], { type: "application/json" })
  const objectUrl = URL.createObjectURL(blob)
  const link = document.createElement("a")
  link.href = objectUrl
  link.download = `moyai-trace-${sessionId}.json`
  document.body.append(link)
  try {
    link.click()
  } finally {
    link.remove()
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0)
  }
}
