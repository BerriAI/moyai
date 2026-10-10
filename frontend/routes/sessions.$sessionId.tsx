import { createFileRoute } from "@tanstack/react-router"
import { showSession } from "../legacy/runtime"

export const Route = createFileRoute("/sessions/$sessionId")({
  loader: ({ params, abortController }) => showSession(params.sessionId, abortController.signal),
  component: () => null,
})
