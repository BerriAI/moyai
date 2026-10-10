import { createFileRoute } from "@tanstack/react-router"
import { showView } from "../legacy/runtime"

export const Route = createFileRoute("/settings/")({
  loader: ({ abortController }) => showView("settings", abortController.signal),
  component: () => null,
})
