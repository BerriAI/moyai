import { createRoot } from "react-dom/client"
import { createRouter, RouterProvider } from "@tanstack/react-router"
import { routeTree } from "./routeTree.gen"
import { installRuntime } from "./legacy/runtime"
import { isView } from "./legacy/bridge"
import { queryClient } from "./api/client"

const legacyLocation = (hash: string) => {
  const session = hash.match(/^#run=([0-9a-f]{32})(.*)$/)
  if (session) return `/sessions/${session[1]}${location.search}${session[2] ? "#" + session[2].replace(/^&/, "") : ""}`
  const view = hash.slice(1)
  if (isView(view)) return `${view === "tasks" ? "/sessions/new" : view === "settings" ? "/settings" : `/settings/${view}`}${location.search}`
  return null
}
const initial = legacyLocation(location.hash)
if (initial) history.replaceState(null, "", initial)

const router = createRouter({ routeTree, context: { queryClient }, defaultPreload: false, defaultStaleTime: 0 })
declare module "@tanstack/react-router" { interface Register { router: typeof router } }

installRuntime({
  async view(view) {
    const previous = location.pathname
    if (view === "tasks") await router.navigate({ to: "/sessions/new" })
    else if (view === "settings") await router.navigate({ to: "/settings" })
    else await router.navigate({ to: "/settings/$page", params: { page: view } })
    // Re-selecting the current page is an explicit refresh in the old UI.
    if (previous === location.pathname) await router.invalidate()
  },
  async session(id, hash) {
    const previous = location.pathname
    const fragment = hash?.match(/^#run=[^&]+&(.*)$/)?.[1] ?? ""
    await router.navigate({ to: "/sessions/$sessionId", params: { sessionId: id }, hash: fragment })
    if (previous === location.pathname) await router.invalidate()
  },
  async reload() { await router.invalidate() },
})

// Compatibility bookmarks are translated once; TanStack owns browser history.
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

const host = document.createElement("div")
host.id = "application-routes"
document.body.append(host)
createRoot(host).render(<RouterProvider router={router} />)
