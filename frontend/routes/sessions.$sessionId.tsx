import { createFileRoute, redirect, useLocation } from "@tanstack/react-router"
import { ControllerSession } from "../legacy/views"

export const Route = createFileRoute("/sessions/$sessionId")({
  beforeLoad: ({ params }) => {
    if (!/^[0-9a-f]{32}$/.test(params.sessionId)) throw redirect({ to: "/sessions/new", replace: true })
  },
  component: SessionRoute,
})

function SessionRoute() {
  const { sessionId } = Route.useParams()
  const hash = useLocation({ select: location => location.hash })
  return <ControllerSession id={sessionId} hash={hash} />
}
