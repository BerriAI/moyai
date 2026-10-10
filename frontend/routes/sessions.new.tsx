import { createFileRoute } from "@tanstack/react-router"
import { showView } from "../legacy/runtime"

export const Route = createFileRoute("/sessions/new")({
  loader: ({ abortController }) => showView("tasks", abortController.signal),
  component: () => null,
})
