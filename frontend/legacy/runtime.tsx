import { QueryClientProvider } from "@tanstack/react-query"
import { createRoot } from "react-dom/client"
import { flushSync } from "react-dom"
import { AppShell } from "../app-shell"
import { queryClient } from "../api/client"
import * as ui from "../render"
import { createDialog, initializeShell } from "../overlays"
import { SettingsPage } from "../pages/settings"
import { MemoryPage } from "../pages/memory"
import { controllerScripts } from "./scripts"
import { isView, loadScript, type View } from "./bridge"
import "../theme.css"

let resolveReady: () => void
let rejectReady: (error: Error) => void
const ready = new Promise<void>((resolve, reject) => { resolveReady = resolve; rejectReady = reject })
let booted = false

export type Navigation = {
  view(view: View): Promise<void>
  session(id: string, hash?: string): Promise<void>
  reload(): Promise<void>
}
const submissions = new Map<string, unknown>()

export function installRuntime(navigation: Navigation) {
  window.MoyaiUI = { ...ui, createDialog, isOpen: host => host.dataset.open === "true" }
  window.MoyaiApp = {
    ready() {
      queryClient.clear()
      if (booted) void navigation.reload()
      else { booted = true; resolveReady() }
    },
    navigate(view) {
      if (!isView(view)) return Promise.reject(new Error(`Unknown workspace page: ${view}`))
      return navigation.view(view)
    },
    openRun(id, hash, submission) {
      if (submission) submissions.set(id, submission)
      return navigation.session(id, hash)
    },
    renderPage(view) {
      if (view !== "settings" && view !== "memory") return false
      const content = document.querySelector<HTMLElement>("#content")
      if (!content) throw new Error("Missing workspace content host")
      ui.renderComponent(content, <QueryClientProvider client={queryClient}>{view === "settings" ? <SettingsPage /> : <MemoryPage />}</QueryClientProvider>)
      return true
    },
    legacyHash() {
      const session = location.pathname.match(/^\/sessions\/([^/]+)$/)
      if (session && session[1] !== "new") return `#run=${session[1]}${location.hash.replace(/^#/, "&")}`
      const view = location.pathname.startsWith("/settings/") ? location.pathname.slice(10) : location.pathname === "/settings" ? "settings" : "tasks"
      return `#${view}`
    },
  }
  const shell = document.querySelector<HTMLElement>("#app")
  if (!shell) throw new Error("Missing application root")
  shell.style.display = "contents"
  flushSync(() => createRoot(shell).render(<AppShell />))
  initializeShell({ renderShell: false })
  ui.observeRemovals()
  void (async () => {
    try { for (const source of controllerScripts) await loadScript(source) }
    catch (error) { rejectReady(error instanceof Error ? error : new Error(String(error))) }
  })()
}

export async function showView(view: View, signal: AbortSignal) {
  await ready
  if (!signal.aborted && window.MoyaiControllers.authenticated()) await window.MoyaiControllers.page(view)
}

export async function showSession(id: string, signal: AbortSignal) {
  await ready
  if (signal.aborted || !window.MoyaiControllers.authenticated()) return
  const submission = submissions.get(id)
  submissions.delete(id)
  await window.MoyaiControllers.session(id, window.MoyaiApp.legacyHash(), submission)
}
