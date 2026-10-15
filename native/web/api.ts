let csrf = ""
export const setCsrf = (value: string) => { csrf = value }
export const csrfToken = () => csrf

export async function api<T>(path: string, method = "GET", body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method, signal, headers: { "Content-Type": "application/json", "X-Moyai-CSRF": csrf },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const value = await response.json()
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event("moyai:signed-out"))
    throw new Error(typeof value.detail === "string" ? value.detail : "The request could not be completed.")
  }
  return value
}
