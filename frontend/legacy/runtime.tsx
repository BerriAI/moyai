import { useSyncExternalStore } from "react"
import { flushSync } from "react-dom"
import { toast } from "sonner"
import type { AnyRouter } from "@tanstack/react-router"
import { queryClient } from "../api/client"
import { queries } from "../api/queries"
import type { WorkspaceSession } from "../api/generated/types.gen"
import * as ui from "../render"
import { createDialog } from "../overlays"
import { settingsGroups } from "../config/settings-navigation"
import { isView, type Role, type View } from "./bridge"
import { controllers } from "./controllers"

// Controllers own authentication. Routes render only after the controllers
// report a signed-in workspace; until then #content belongs to boot().
type Runtime = { ready: boolean; authenticated: boolean; userId: string | null; role: Role; generation: number }
let runtime: Runtime = { ready: false, authenticated: false, userId: null, role: "member", generation: 0 }
const listeners = new Set<() => void>()

function publish(next: Runtime) {
  runtime = next
  // Commit before the caller writes into the host this state selects.
  flushSync(() => { for (const listener of listeners) listener() })
}

export function useRuntime() {
  return useSyncExternalStore(listener => { listeners.add(listener); return () => listeners.delete(listener) }, () => runtime)
}

export function useIdentity() {
  const { userId, role } = useRuntime()
  return { userId, role }
}

// For callbacks that finish after the identity may have changed.
export function currentUserId() {
  return runtime.userId
}

// Remounts the current route, e.g. re-selecting the page you are on.
export function remountRoute() {
  publish({ ...runtime, generation: runtime.generation + 1 })
}

export const nativeViews = new Set<View>(["settings", "memory"])

export function viewPath(view: View) {
  return view === "tasks" ? "/sessions/new" : view === "settings" ? "/settings" : `/settings/${view}`
}

export function legacyHash() {
  const session = location.pathname.match(/^\/sessions\/([0-9a-f]{32})$/)
  if (session) return `#run=${session[1]}${location.hash.replace(/^#/, "&")}`
  const view = location.pathname === "/settings" ? "settings" : location.pathname.startsWith("/settings/") ? location.pathname.slice(10) : "tasks"
  return `#${view}`
}

// A submission is handed from the new-session composer to the session route.
const submissions = new Map<string, unknown>()
export function takeSubmission(id: string) {
  const submission = submissions.get(id)
  submissions.delete(id)
  return submission
}

export function showError(error: unknown) {
  toast(error instanceof Error ? error.message : String(error))
}

// The session query is the source of truth; the controllers mirror it.
function syncSession() {
  return queryClient.getQueryCache().subscribe(event => {
    // Fetches and setQueryData both settle as "success".
    if (event.type !== "updated" || event.action.type !== "success" || event.query.queryKey[0] !== queries.session._def[0]) return
    const session = event.query.state.data as WorkspaceSession | undefined
    if (!session) return
    controllers.applySession(session)
    const userId = controllers.userId(), role = controllers.role()
    if (userId !== runtime.userId || role !== runtime.role) publish({ ...runtime, userId, role })
    // A signed-out session belongs to the controllers' sign-in flow.
    if (!session.authenticated) void controllers.boot()
  })
}

export function installRuntime(router: AnyRouter) {
  syncSession()
  window.MoyaiUI = { ...ui, createDialog, isOpen: host => host.dataset.open === "true", toast: message => { toast(message) } }
  window.MoyaiConfig = { settingsGroups }
  window.MoyaiApp = {
    ready() {
      queryClient.clear()
      publish({ ready: true, authenticated: controllers.authenticated(), userId: controllers.userId(), role: controllers.role(), generation: runtime.generation + 1 })
    },
    async navigate(view) {
      if (!isView(view)) throw new Error(`Unknown workspace page: ${view}`)
      const path = viewPath(view)
      if (location.pathname !== path && !(view === "tasks" && location.pathname === "/")) return void await router.navigate({ href: path })
      // Re-selecting the current page refreshes it, as the sidebar always has.
      if (nativeViews.has(view)) remountRoute()
      else await controllers.page(view)
    },
    async openRun(id, hash = `#run=${id}`, submission) {
      const fragment = hash.match(/^#run=[^&]+&(.*)$/)?.[1] ?? ""
      if (location.pathname === `/sessions/${id}` && location.hash.slice(1) === fragment) {
        return controllers.session(id, hash, submission)
      }
      if (submission) submissions.set(id, submission)
      await router.navigate({ href: `/sessions/${id}${location.search}${fragment ? `#${fragment}` : ""}` })
    },
    legacyHash,
    replaceUrl(href) { router.history.replace(href) },
  }
}
