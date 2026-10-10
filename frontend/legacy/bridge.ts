import type { WorkspaceSession } from "../api/generated/types.gen"
import type { SettingsGroup } from "../config/settings-navigation"

export const views = ["tasks", "settings", "automations", "connections", "runtime", "spend", "users", "environments", "secrets", "skills", "memory", "adoption"] as const
export type View = typeof views[number]
export type Role = "admin" | "member"

export function isView(value: string): value is View {
  return views.some(view => view === value)
}

// Implemented by app/static/app.js once the controller scripts have loaded.
// Read it only through ./controllers.
export type ControllerBridge = {
  authenticated(): boolean
  csrf(): string
  userId(): string | null
  role(): Role
  boot(): Promise<void>
  // Page chrome only (title, rail, streams) for React-owned pages.
  enter(view: View): void
  page(view: View): Promise<void>
  session(id: string, hash: string, submission?: unknown): Promise<void>
  applySession(session: WorkspaceSession): void
  invalidate(): void
}

// Implemented by frontend/legacy/runtime.tsx for the controllers.
export type AppBridge = {
  ready(): void
  navigate(view: string): Promise<void>
  openRun(id: string, hash?: string, submission?: unknown): Promise<void>
  legacyHash(): string
  replaceUrl(href: string): void
}

declare global {
  var MoyaiUI: typeof import("../render") & Pick<typeof import("../overlays"), "createDialog"> & {
    isOpen(host: HTMLElement): boolean
    toast(message: string): void
  }
  var MoyaiApp: AppBridge
  // Shared configuration the controllers read when they load.
  var MoyaiConfig: { settingsGroups: readonly SettingsGroup[] }
}

// Controllers depend on load order, not on finishing one download before the
// next starts. async=false downloads in parallel and executes in order.
export function loadScripts(sources: readonly string[]): Promise<void> {
  return Promise.all(sources.map(src => new Promise<void>((resolve, reject) => {
    const script = document.createElement("script")
    script.src = src
    script.async = false
    script.onload = () => resolve()
    script.onerror = () => reject(new Error(`Unable to load workspace controller: ${src}`))
    document.head.append(script)
  }))).then(() => undefined)
}
