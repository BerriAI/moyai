import { createRootRoute, Outlet } from "@tanstack/react-router"
import { AppShell, ContentHost } from "../blocks/app-shell"
import { useRuntime } from "../legacy/runtime"

export const Route = createRootRoute({ component: RootLayout })

// Until the controllers report a signed-in workspace, #content belongs to
// their boot and sign-in flow. A new identity remounts the current route.
function RootLayout() {
  const { ready, authenticated, generation } = useRuntime()
  return <AppShell>{ready && authenticated ? <Outlet key={generation} /> : <ContentHost />}</AppShell>
}
