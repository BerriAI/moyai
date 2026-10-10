import { createRoot } from "react-dom/client"
import { flushSync } from "react-dom"
import { QueryClientProvider } from "@tanstack/react-query"
import { createRouter, RouterProvider } from "@tanstack/react-router"
import { routeTree } from "./routeTree.gen"
import { queryClient } from "./api/client"
import { initializeShell } from "./overlays"
import { observeRemovals } from "./render"
import { isView, loadScripts } from "./legacy/bridge"
import { installRuntime, viewPath } from "./legacy/runtime"
import { controllerScripts } from "./legacy/scripts"
import "./styles.css"

const router = createRouter({ routeTree, defaultPreload: false })
declare module "@tanstack/react-router" { interface Register { router: typeof router } }

// Old #view and #run= links are translated once; TanStack owns history.
function legacyLocation(hash: string) {
  const session = hash.match(/^#run=([0-9a-f]{32})(.*)$/)
  if (session) return `/sessions/${session[1]}${location.search}${session[2] ? "#" + session[2].replace(/^&/, "") : ""}`
  const view = hash.slice(1)
  return isView(view) ? `${viewPath(view)}${location.search}` : null
}

document.addEventListener("click", event => {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
  const link = event.target instanceof Element ? event.target.closest<HTMLAnchorElement>("a[href^='#']") : null
  if (!link || link.target || link.hasAttribute("download")) return
  const destination = legacyLocation(link.getAttribute("href") ?? "")
  if (!destination) return
  event.preventDefault()
  void router.navigate({ href: destination })
})
window.addEventListener("hashchange", () => {
  const destination = legacyLocation(location.hash)
  if (destination) void router.navigate({ href: destination, replace: true })
})

async function start() {
  const initial = legacyLocation(location.hash)
  if (initial) history.replaceState(null, "", initial)
  installRuntime(router)
  await router.load()
  const host = document.querySelector<HTMLElement>("#app")
  if (!host) throw new Error("Missing application root")
  // Controllers bind to the shell when they load, so it must be committed first.
  flushSync(() => createRoot(host).render(<QueryClientProvider client={queryClient}><RouterProvider router={router} /></QueryClientProvider>))
  if (!document.querySelector("#sidebar")) throw new Error("The workspace shell did not render")
  initializeShell({ renderShell: false })
  observeRemovals()
  await loadScripts(controllerScripts)
}

void start()
