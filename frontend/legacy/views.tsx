import { useEffect, useLayoutEffect } from "react"
import { ContentHost } from "../blocks/app-shell"
import type { View } from "./bridge"
import { controllers } from "./controllers"
import { legacyHash, showError, takeSubmission } from "./runtime"

// A page still rendered by its controller: the route owns an empty host and
// the controller fills it. Leaving the route removes the host and its regions.
export function ControllerView({ view }: { view: View }) {
  useEffect(() => { controllers.page(view).catch(showError) }, [view])
  return <ContentHost />
}

export function ControllerSession({ id, hash }: { id: string; hash: string }) {
  useEffect(() => {
    controllers.session(id, legacyHash(), takeSubmission(id)).catch(showError)
  }, [id, hash])
  return <ContentHost />
}

// A React page. The controllers still own the rail, title and live streams.
export function ReactView({ view, children }: { view: View; children: React.ReactNode }) {
  useLayoutEffect(() => { controllers.enter(view) }, [view])
  return <ContentHost>{children}</ContentHost>
}
