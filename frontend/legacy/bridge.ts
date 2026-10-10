import type { WorkspaceSession, ChatPreferences } from "../api/generated/types.gen"

export const views = ["tasks", "settings", "automations", "connections", "runtime", "spend", "users", "environments", "secrets", "skills", "memory", "adoption"] as const
export type View = typeof views[number]
export type Session = WorkspaceSession
export type Preferences = ChatPreferences

export type ControllerBridge = {
  authenticated(): boolean
  csrf(): string
  userId(): string | null
  role(): "admin" | "member"
  page(view: View): Promise<void>
  session(id: string, hash?: string, submission?: unknown): Promise<void>
  applySession(session: Session): void
  updatePreferences(preferences: Partial<Preferences>): void
  invalidate(): void
}

export type AppBridge = {
  ready(): void
  navigate(view: string): Promise<void>
  openRun(id: string, hash?: string, submission?: unknown): Promise<void>
  renderPage(view: string): boolean
  legacyHash(): string
}

declare global {
  var MoyaiUI: typeof import("../render") & Pick<typeof import("../overlays"), "createDialog"> & { isOpen(host: HTMLElement): boolean }
  var MoyaiControllers: ControllerBridge
  var MoyaiApp: AppBridge
}

export function isView(value: string): value is View {
  return views.some(view => view === value)
}

export function loadScript(src: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const script = document.createElement("script")
    script.src = src
    script.async = false
    script.onload = () => resolve()
    script.onerror = () => reject(new Error(`Unable to load workspace controller: ${src}`))
    document.head.append(script)
  })
}
