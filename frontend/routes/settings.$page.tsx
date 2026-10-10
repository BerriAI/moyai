import { createFileRoute } from "@tanstack/react-router"
import { isView } from "../legacy/bridge"
import { showView } from "../legacy/runtime"

export const Route = createFileRoute("/settings/$page")({
  loader: ({ params, abortController }) => {
    if (!isView(params.page) || params.page === "tasks") throw new Error("Unknown settings page")
    return showView(params.page, abortController.signal)
  },
  component: () => null,
})
