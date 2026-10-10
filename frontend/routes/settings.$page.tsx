import { createFileRoute, redirect } from "@tanstack/react-router"
import { isView, type View } from "../legacy/bridge"
import { nativeViews, viewPath } from "../legacy/runtime"
import { ControllerView } from "../legacy/views"

// Settings destinations still rendered by a controller. React-owned pages have
// their own routes; anything else returns to a new session, as old links did.
const controllerView = (page: string): View | null => isView(page) && page !== "tasks" ? page : null

export const Route = createFileRoute("/settings/$page")({
  beforeLoad: ({ params }) => {
    const view = controllerView(params.page)
    if (!view) throw redirect({ to: "/sessions/new", replace: true })
    if (nativeViews.has(view)) throw redirect({ href: viewPath(view), replace: true })
  },
  component: SettingsRoute,
})

function SettingsRoute() {
  const view = controllerView(Route.useParams().page)
  return view && <ControllerView view={view} />
}
